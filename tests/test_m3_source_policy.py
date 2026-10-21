"""M3 E2 result-source policy: exact origin binding beside the frozen Wan allowlist."""

import hashlib

import pytest

from vagent.video.contracts import (
    FAL_SOURCE_POLICY_VERSION,
    KLING_SOURCE_POLICY_VERSION,
    bound_result_source,
)
from vagent.video.media import MediaError
from vagent.video.media_http import PinnedPublicBackend, check_source

KLING_URL = "https://cdn.example-provider.test/out/task-1.mp4?Signature=a%2Fb&Expires=1"


def binding(url=KLING_URL, task_id="task-1", version=KLING_SOURCE_POLICY_VERSION):
    return bound_result_source(task_id, url, version)


def test_an_authenticated_binding_approves_only_its_exact_url_and_origin():
    source = binding()
    assert check_source(KLING_URL, version=KLING_SOURCE_POLICY_VERSION, binding=source) == (
        "cdn.example-provider.test"
    )
    assert source.origin == "https://cdn.example-provider.test:443"
    assert source.url_fingerprint == hashlib.sha256(KLING_URL.encode()).hexdigest()


@pytest.mark.parametrize(
    "url",
    [
        KLING_URL.replace("Signature=a%2Fb", "Signature=a/b"),  # percent decoding is not normalization
        KLING_URL.replace("Expires=1", "Expires=2"),  # signed query changed
        KLING_URL.replace("task-1", "task-2"),  # same origin, different path
        KLING_URL.replace("https://", "http://"),  # not HTTPS
        KLING_URL.replace("cdn.example-provider.test", "cdn.example-provider.test:8443"),
        KLING_URL.replace("cdn.example-provider.test", "user@cdn.example-provider.test"),
        KLING_URL + "#fragment",
    ],
)
def test_the_binding_refuses_any_url_but_the_recorded_one(url):
    with pytest.raises(MediaError) as caught:
        check_source(url, version=KLING_SOURCE_POLICY_VERSION, binding=binding())
    assert caught.value.code in {"MEDIA_SOURCE_UNSAFE", "MEDIA_SOURCE_UNAPPROVED"}


def test_a_binding_from_another_provider_or_policy_is_not_accepted():
    with pytest.raises(MediaError):
        check_source(KLING_URL, version=FAL_SOURCE_POLICY_VERSION, binding=binding())
    with pytest.raises(MediaError):
        check_source(KLING_URL, version=KLING_SOURCE_POLICY_VERSION, binding=None)


def test_an_ip_literal_or_private_result_origin_cannot_be_bound():
    for url in (
        "https://127.0.0.1/out.mp4",
        "https://[::1]/out.mp4",
        "https://cdn.example-provider.test:80/out.mp4",
        "https:///out.mp4",
    ):
        with pytest.raises(ValueError):
            bound_result_source("task-1", url, KLING_SOURCE_POLICY_VERSION)


def test_the_wan_allowlist_is_unchanged_and_still_static():
    allowed = "https://dashscope-result-bj.oss-cn-beijing.aliyuncs.com/a.mp4"
    assert check_source(allowed, "wan-result-hosts-v2") == ("dashscope-result-bj.oss-cn-beijing.aliyuncs.com")
    with pytest.raises(MediaError) as caught:
        check_source("https://cdn.example-provider.test/a.mp4", "wan-result-hosts-v2")
    assert caught.value.code == "MEDIA_SOURCE_UNAPPROVED"


class RecordingBackend:
    def __init__(self):
        self.connected = []

    async def connect_tcp(self, host, port, timeout=None, local_address=None, socket_options=None):
        self.connected.append(host)
        return object()


def resolver_for(*addresses):
    async def resolve(host, port, type=None):
        return [(2, 1, 6, "", (address, 0)) for address in addresses]

    return resolve


async def test_the_pinned_backend_reaches_only_the_bound_public_origin():
    backend = RecordingBackend()
    pinned = PinnedPublicBackend(backend=backend, resolver=resolver_for("93.184.216.34"))
    with pytest.raises(MediaError) as caught:
        await pinned.connect_tcp("cdn.example-provider.test", 443, timeout=1)
    assert caught.value.code == "MEDIA_SOURCE_UNAPPROVED"  # Unbound hosts are refused.
    pinned.bind_origin("cdn.example-provider.test")
    await pinned.connect_tcp("cdn.example-provider.test", 443, timeout=1)
    assert backend.connected == ["93.184.216.34"]  # The numeric address is pinned.
    with pytest.raises(MediaError):
        await pinned.connect_tcp("cdn.example-provider.test", 80, timeout=1)
    with pytest.raises(MediaError):
        await pinned.connect_tcp("other.example-provider.test", 443, timeout=1)


@pytest.mark.parametrize(
    "addresses",
    [("10.0.0.1",), ("127.0.0.1",), ("93.184.216.34", "10.0.0.1")],
)
async def test_the_pinned_backend_refuses_private_or_mixed_dns_answers(addresses):
    pinned = PinnedPublicBackend(backend=RecordingBackend(), resolver=resolver_for(*addresses))
    pinned.bind_origin("cdn.example-provider.test")
    with pytest.raises(MediaError) as caught:
        await pinned.connect_tcp("cdn.example-provider.test", 443, timeout=1)
    assert caught.value.code == "MEDIA_SOURCE_UNSAFE"


async def test_every_connection_re_resolves_instead_of_reusing_a_verified_answer():
    answers = [["93.184.216.34"], ["10.0.0.1"]]

    async def resolve(host, port, type=None):
        return [(2, 1, 6, "", (answers.pop(0)[0], 0))]

    pinned = PinnedPublicBackend(backend=RecordingBackend(), resolver=resolve)
    pinned.bind_origin("cdn.example-provider.test")
    await pinned.connect_tcp("cdn.example-provider.test", 443, timeout=1)
    with pytest.raises(MediaError) as caught:
        await pinned.connect_tcp("cdn.example-provider.test", 443, timeout=1)
    assert caught.value.code == "MEDIA_SOURCE_UNSAFE"  # A rebound answer is not trusted.
