"""Unauthenticated media HTTP with exact source policy and pinned public DNS answers."""

import asyncio
import ipaddress
import socket
import ssl
from datetime import UTC, datetime, timedelta
from email.utils import parsedate_to_datetime
from urllib.parse import urljoin, urlsplit

import httpcore
import httpx

from vagent.video.contracts import SOURCE_POLICY_VERSION
from vagent.video.media import MediaError, file_io

SOURCE_HOSTS = {
    "wan-result-hosts-v1": frozenset(
        {
            "dashscope-result-sh.oss-accelerate.aliyuncs.com",
            "dashscope-a717.oss-accelerate.aliyuncs.com",
        }
    ),
}


def check_source(url: str, version: str) -> str:
    try:
        parsed = urlsplit(url)
        host = parsed.hostname
        unsafe = (
            len(url) > 8192
            or any(ord(char) < 33 or ord(char) == 127 for char in url)
            or "\\" in url
            or parsed.scheme != "https"
            or not host
            or parsed.username is not None
            or parsed.password is not None
            or parsed.port not in {None, 443}
            or bool(parsed.fragment)
        )
        if host:
            try:
                ipaddress.ip_address(host)
                unsafe = True
            except ValueError:
                pass
    except (ValueError, TypeError):
        unsafe, host = True, None
    if unsafe:
        raise MediaError("MEDIA_SOURCE_UNSAFE")
    if host not in SOURCE_HOSTS.get(version, ()):
        raise MediaError("MEDIA_SOURCE_UNAPPROVED")
    return host


def public_address(value):
    try:
        address = ipaddress.ip_address(value)
    except ValueError:
        return False
    if getattr(address, "ipv4_mapped", None):
        return public_address(str(address.ipv4_mapped))
    return address.is_global and not address.is_reserved and not address.is_multicast


class PinnedPublicBackend(httpcore.AsyncNetworkBackend):
    def __init__(self, *, backend=None, resolver=None):
        self.backend = backend or httpcore.AnyIOBackend()
        self.resolver = resolver

    async def connect_tcp(self, host, port, timeout=None, local_address=None, socket_options=None):
        if port != 443 or host not in set().union(*SOURCE_HOSTS.values()):
            raise MediaError("MEDIA_SOURCE_UNAPPROVED")
        async with asyncio.timeout(timeout):
            resolve = self.resolver or asyncio.get_running_loop().getaddrinfo
            entries = await resolve(host, port, type=socket.SOCK_STREAM)
            addresses = list(dict.fromkeys(entry[4][0] for entry in entries))
            if not addresses or not all(public_address(address) for address in addresses):
                raise MediaError("MEDIA_SOURCE_UNSAFE")
            # The backend receives a numeric address, never the original DNS name.
            # httpcore still uses the original origin for Host, TLS SNI and certificate validation.
            return await self.backend.connect_tcp(
                addresses[0],
                port,
                timeout=timeout,
                local_address=local_address,
                socket_options=socket_options,
            )

    async def sleep(self, seconds):
        await asyncio.sleep(seconds)


class CoreStream(httpx.AsyncByteStream):
    def __init__(self, stream):
        self.stream = stream

    async def __aiter__(self):
        async for chunk in self.stream:
            yield chunk

    async def aclose(self):
        await self.stream.aclose()


class MediaTransport(httpx.AsyncBaseTransport):
    def __init__(self, *, network_backend=None):
        self.pool = httpcore.AsyncConnectionPool(
            ssl_context=ssl.create_default_context(),
            max_connections=1,
            max_keepalive_connections=0,
            retries=0,
            http2=False,
            network_backend=network_backend or PinnedPublicBackend(),
        )

    async def handle_async_request(self, request):
        response = await self.pool.handle_async_request(
            httpcore.Request(
                method=request.method,
                url=httpcore.URL(
                    scheme=request.url.raw_scheme,
                    host=request.url.raw_host,
                    port=request.url.port,
                    target=request.url.raw_path,
                ),
                headers=request.headers.raw,
                content=request.stream,
                extensions=request.extensions,
            )
        )
        return httpx.Response(
            response.status,
            headers=response.headers,
            stream=CoreStream(response.stream),
            extensions=response.extensions,
        )

    async def aclose(self):
        await self.pool.aclose()


def retry_after(value, timestamp):
    if value is None or len(value) > 128:
        return None
    try:
        current = datetime.fromisoformat(timestamp)
        if value.isascii() and value.isdigit():
            return (current + timedelta(seconds=int(value))).isoformat()
        date = parsedate_to_datetime(value)
        if date.utcoffset() is None:
            return None
        return max(current, date.astimezone(UTC)).isoformat()
    except (ValueError, OverflowError, TypeError):
        return None


class MediaHttpClient:
    def __init__(self, *, transport=None):
        # Use the transport directly: no cookie jar, ambient auth, environment proxy,
        # implicit redirect handling, or httpx INFO logging of private signed URLs.
        self.transport = transport or MediaTransport()
        self.is_closed = False

    async def __aenter__(self):
        await self.transport.__aenter__()
        return self

    async def __aexit__(self, *_):
        await self.transport.aclose()
        self.is_closed = True

    async def download(self, job, file, *, timestamp):
        output, policy = job.provider_output, job.download_policy
        if output.url_expires_at and datetime.fromisoformat(output.url_expires_at) <= datetime.fromisoformat(
            timestamp
        ):
            raise MediaError("MEDIA_URL_EXPIRED")
        url = output.video_url
        try:
            for hop in range(policy.max_redirects + 1):
                check_source(url, job.download.source_policy_version)
                # Construct a fresh request, bypassing client cookie/default header merging.
                request = httpx.Request(
                    "GET",
                    url,
                    headers={
                        "Accept-Encoding": "identity",
                        "Accept": "video/mp4, application/octet-stream",
                        "User-Agent": "vagent-media/1",
                    },
                    extensions={
                        "timeout": {
                            "connect": policy.connect_timeout_seconds,
                            "read": policy.read_timeout_seconds,
                            "write": policy.read_timeout_seconds,
                            "pool": policy.connect_timeout_seconds,
                        }
                    },
                )
                response = await self.transport.handle_async_request(request)
                try:
                    if response.status_code in {301, 302, 303, 307, 308}:
                        if hop == policy.max_redirects or "location" not in response.headers:
                            raise MediaError("MEDIA_SOURCE_UNAPPROVED")
                        url = urljoin(url, response.headers["location"])
                        check_source(url, job.download.source_policy_version)
                        continue
                    if response.status_code != 200:
                        code = (
                            "MEDIA_RESULT_UNAVAILABLE"
                            if response.status_code in {404, 410}
                            else "MEDIA_HTTP_ERROR"
                        )
                        raise MediaError(
                            code,
                            retryable=response.status_code == 429 or 500 <= response.status_code <= 599,
                            retry_after_at=retry_after(response.headers.get("retry-after"), timestamp),
                        )
                    encoding = response.headers.get("content-encoding", "identity").lower().strip()
                    media_type = response.headers.get("content-type", "").split(";", 1)[0].strip().lower()
                    if encoding not in {"", "identity"} or media_type not in {
                        "",
                        "video/mp4",
                        "application/octet-stream",
                    }:
                        raise MediaError("MEDIA_INVALID_TYPE")
                    length = response.headers.get("content-length")
                    if length is not None and (not length.isascii() or not length.isdigit()):
                        raise MediaError("DOWNLOAD_INCOMPLETE", retryable=True)
                    expected = int(length) if length is not None else None
                    if expected is not None and expected > policy.max_bytes:
                        raise MediaError("MEDIA_TOO_LARGE")
                    written = 0
                    async for chunk in response.aiter_bytes(chunk_size=65536):
                        written += len(chunk)
                        if written > policy.max_bytes:
                            raise MediaError("MEDIA_TOO_LARGE")
                        count = await file_io(file.write, chunk)
                        if count != len(chunk):
                            raise MediaError("MEDIA_IO_ERROR")
                    if expected is not None and expected != written:
                        raise MediaError("DOWNLOAD_INCOMPLETE", retryable=True)
                    return
                finally:
                    await response.aclose()
        except (TimeoutError, httpx.TimeoutException, httpcore.TimeoutException):
            raise MediaError("DOWNLOAD_TIMEOUT", retryable=True) from None
        except (httpx.RemoteProtocolError, httpcore.RemoteProtocolError):
            raise MediaError("DOWNLOAD_INCOMPLETE", retryable=True) from None
        except (httpx.TransportError, httpcore.NetworkError, httpcore.ProtocolError, socket.gaierror):
            raise MediaError("DOWNLOAD_NETWORK", retryable=True) from None


assert SOURCE_POLICY_VERSION in SOURCE_HOSTS
