import asyncio
import io
import logging
import socket
import ssl
from datetime import datetime

import httpcore
import httpx
import pytest
from media_support import SAMPLE, download_service
from wan_support import VIDEO_URL

from vagent.video.contracts import SOURCE_POLICY_VERSION, DownloadPolicy
from vagent.video.media import MediaError
from vagent.video.media_http import (
    MediaHttpClient,
    MediaTransport,
    PinnedPublicBackend,
    check_source,
    public_address,
)
from vagent.video.media_worker import MediaWorker


@pytest.mark.parametrize(
    "url,code",
    [
        ("http://dashscope-result-sh.oss-accelerate.aliyuncs.com/a", "MEDIA_SOURCE_UNSAFE"),
        ("https://user:password@dashscope-result-sh.oss-accelerate.aliyuncs.com/a", "MEDIA_SOURCE_UNSAFE"),
        ("https://dashscope-result-sh.oss-accelerate.aliyuncs.com:8443/a", "MEDIA_SOURCE_UNSAFE"),
        ("https://127.0.0.1/a", "MEDIA_SOURCE_UNSAFE"),
        ("https://[::1]/a", "MEDIA_SOURCE_UNSAFE"),
        ("https://dashscope-result-sh.oss-accelerate.aliyuncs.com./a", "MEDIA_SOURCE_UNAPPROVED"),
        ("https://other.oss-accelerate.aliyuncs.com/a", "MEDIA_SOURCE_UNAPPROVED"),
        ("https://dashscope-result-sh.oss-accelerate.aliyuncs.com.evil.test/a", "MEDIA_SOURCE_UNAPPROVED"),
        ("https://dashscope-result-sh.oss-accelerate.aliyuncs.com/\na", "MEDIA_SOURCE_UNSAFE"),
        ("https://dashscope-result-sh.oss-accelerate.aliyuncs.com/a#fragment", "MEDIA_SOURCE_UNSAFE"),
    ],
)
def test_source_policy_uses_exact_https_hosts(url, code):
    with pytest.raises(MediaError) as caught:
        check_source(url, SOURCE_POLICY_VERSION)
    assert caught.value.code == code


@pytest.mark.parametrize(
    "address",
    [
        "127.0.0.1",
        "10.1.2.3",
        "192.168.1.2",
        "169.254.169.254",
        "100.64.0.1",
        "192.0.2.1",
        "224.0.0.1",
        "::1",
        "fe80::1",
        "fc00::1",
        "::ffff:127.0.0.1",
        "2001:db8::1",
        "ff02::1",
    ],
)
def test_nonpublic_dns_answers_are_never_connectable(address):
    assert not public_address(address)


async def test_connection_uses_validated_ip_and_original_tls_origin(store, video_clock, caplog):
    class Stream(httpcore.AsyncMockStream):
        async def start_tls(self, ssl_context, server_hostname=None, timeout=None):
            assert ssl_context.check_hostname and ssl_context.verify_mode == ssl.CERT_REQUIRED
            observed["sni"] = server_hostname
            return self

        async def write(self, buffer, timeout=None):
            observed.setdefault("writes", []).append(buffer)
            await super().write(buffer, timeout)

    class Backend(httpcore.AsyncNetworkBackend):
        async def connect_tcp(self, host, port, **kwargs):
            observed["connected"] = (host, port)
            return Stream([b"HTTP/1.1 200 OK\r\nContent-Type: video/mp4\r\nContent-Length: 3\r\n\r\nabc"])

    observed = {}

    async def resolver(host, port, **_):
        observed["resolved"] = (host, port)
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("8.8.8.8", port))]

    async with download_service(store, video_clock) as (_, job, _):
        transport = MediaTransport(network_backend=PinnedPublicBackend(backend=Backend(), resolver=resolver))
        with caplog.at_level(logging.DEBUG):
            async with MediaHttpClient(transport=transport) as client:
                output = io.BytesIO()
                await client.download(job, output, timestamp=job.updated_at)
                assert output.getvalue() == b"abc"
        assert observed["connected"] == ("8.8.8.8", 443)
        assert observed["sni"] == observed["resolved"][0] == check_source(VIDEO_URL, SOURCE_POLICY_VERSION)
        assert b"Host: dashscope-result-sh.oss-accelerate.aliyuncs.com" in b"".join(observed["writes"])
        assert "private-signature" not in caplog.text
        assert client.is_closed


async def test_mixed_dns_answer_rejects_before_any_connection():
    class Backend(httpcore.AsyncNetworkBackend):
        async def connect_tcp(self, *_args, **_kwargs):
            pytest.fail("Must reject the entire DNS answer before opening a socket")

    async def resolver(_host, port, **_):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, port)) for ip in ["8.8.8.8", "127.0.0.1"]]

    backend = PinnedPublicBackend(backend=Backend(), resolver=resolver)
    with pytest.raises(MediaError) as caught:
        await backend.connect_tcp(check_source(VIDEO_URL, SOURCE_POLICY_VERSION), 443, timeout=1)
    assert caught.value.code == "MEDIA_SOURCE_UNSAFE"


@pytest.mark.parametrize(
    "location,code",
    [("https://evil.test/a", "MEDIA_SOURCE_UNAPPROVED"), ("https://127.0.0.1/a", "MEDIA_SOURCE_UNSAFE")],
)
async def test_redirect_is_rechecked_before_following(store, video_clock, location, code):
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(302, headers={"Location": location})

    async with download_service(store, video_clock) as (jobs, _, _):
        async with MediaHttpClient(transport=httpx.MockTransport(handler)) as client:
            job = await MediaWorker(jobs.media, client).run_once()
            assert job.status == "download_failed" and job.error.code == code
    assert len(calls) == 1


async def test_allowed_redirect_does_not_forward_cookies_and_has_bounded_hops(store, video_clock):
    calls = []

    def handler(request):
        calls.append(request)
        if len(calls) < 4:
            return httpx.Response(302, headers={"Location": "/next", "Set-Cookie": "credential=private"})
        return httpx.Response(200, content=SAMPLE.read_bytes())

    async with download_service(store, video_clock) as (jobs, _, _):
        async with MediaHttpClient(transport=httpx.MockTransport(handler)) as client:
            completed = await MediaWorker(jobs.media, client).run_once()
            assert completed.status == "succeeded" and len(calls) == 4
    for request in calls:
        assert "authorization" not in request.headers and "cookie" not in request.headers
        assert "range" not in request.headers and request.headers["accept-encoding"] == "identity"


class Chunks(httpx.AsyncByteStream):
    def __init__(self, chunks):
        self.chunks, self.closed = chunks, False

    async def __aiter__(self):
        for chunk in self.chunks:
            if isinstance(chunk, Exception):
                raise chunk
            yield chunk

    async def aclose(self):
        self.closed = True


@pytest.mark.parametrize(
    "status,headers,chunks,code,retries",
    [
        (200, {}, [b"x" * 64, b"x" * 65], "MEDIA_TOO_LARGE", False),
        (200, {"Content-Length": "200"}, [b"x"], "MEDIA_TOO_LARGE", False),
        (200, {"Content-Length": "4"}, [b"ab"], "DOWNLOAD_INCOMPLETE", True),
        (200, {"Content-Length": "1"}, [b"ab"], "DOWNLOAD_INCOMPLETE", True),
        (200, {}, [b"ab", httpx.ReadError("private-signature")], "DOWNLOAD_NETWORK", True),
        (200, {"Content-Type": "text/html"}, [b"<html>"], "MEDIA_INVALID_TYPE", False),
        (200, {"Content-Type": "application/json"}, [b"{}"], "MEDIA_INVALID_TYPE", False),
        (200, {"Content-Encoding": "gzip"}, [b"compressed"], "MEDIA_INVALID_TYPE", False),
        (200, {}, [b"empty movie"], "MEDIA_INVALID_MP4", False),
        (206, {}, [b"partial"], "MEDIA_HTTP_ERROR", False),
        (403, {}, [], "MEDIA_HTTP_ERROR", False),
        (404, {}, [], "MEDIA_RESULT_UNAVAILABLE", False),
        (410, {}, [], "MEDIA_RESULT_UNAVAILABLE", False),
        (429, {"Retry-After": "60"}, [], "MEDIA_HTTP_ERROR", True),
        (503, {"Retry-After": "60"}, [], "MEDIA_HTTP_ERROR", True),
    ],
)
async def test_stream_limits_errors_and_retry_policy(
    store, video_clock, status, headers, chunks, code, retries
):
    stream = Chunks(chunks)
    async with download_service(store, video_clock, policy=DownloadPolicy(max_bytes=128)) as (
        jobs,
        _,
        submits,
    ):
        async with MediaHttpClient(
            transport=httpx.MockTransport(lambda _: httpx.Response(status, headers=headers, stream=stream))
        ) as client:
            job = await MediaWorker(jobs.media, client).run_once()
            assert job.download.error.code == code and job.download.attempts == 1
            assert job.download.phase == ("pending" if retries else "failed")
            assert job.result is None and not jobs.store.snapshot()["media"] and len(submits) == 1
            assert stream.closed and "private-signature" not in str(job.download.error)
            if status in {429, 503}:
                assert (
                    datetime.fromisoformat(job.download.next_attempt_at) - video_clock()
                ).total_seconds() == 60


async def test_known_signature_expiry_skips_http(store, video_clock):
    def handler(_):
        pytest.fail("Expired URL must not be fetched")

    async with download_service(store, video_clock, url=VIDEO_URL + "&Expires=1") as (jobs, _, submits):
        async with MediaHttpClient(transport=httpx.MockTransport(handler)) as client:
            job = await MediaWorker(jobs.media, client).run_once()
            assert job.error.code == "MEDIA_URL_EXPIRED" and len(submits) == 1


async def test_total_timeout_closes_response_and_consumes_attempt(store, video_clock):
    class Hanging(Chunks):
        async def __aiter__(self):
            await asyncio.Event().wait()
            yield b""

    stream = Hanging([])
    async with download_service(store, video_clock, policy=DownloadPolicy(total_timeout_seconds=0.04)) as (
        jobs,
        _,
        _,
    ):
        async with MediaHttpClient(
            transport=httpx.MockTransport(lambda _: httpx.Response(200, stream=stream))
        ) as client:
            job = await MediaWorker(jobs.media, client).run_once()
            assert job.download.error.code == "DOWNLOAD_TIMEOUT" and job.download.attempts == 1
            assert stream.closed
