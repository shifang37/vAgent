"""M3 E2: multi-provider protocol, budget buckets and authenticated result origins.

Every provider call here is a controlled httpx response: no credential, no network
and no video submit reaches an upstream. The application's own adapters, Jobs,
Worker, Store validators and media pipeline run unchanged.
"""

import hashlib
import json

import httpx
import pytest

from vagent.errors import AppError
from vagent.video.contracts import (
    FAL_ADAPTER_VERSION,
    FAL_ENDPOINT_PROFILE,
    FAL_MODEL,
    FAL_PRICE_VERSION,
    FAL_REGION,
    FAL_SOURCE_POLICY_VERSION,
    KLING_ADAPTER_VERSION,
    KLING_ENDPOINT_PROFILE,
    KLING_MODEL,
    KLING_PRICE_VERSION,
    KLING_REGION,
    KLING_SOURCE_POLICY_VERSION,
    MULTI_PROVIDER_CAPABILITIES_VERSION,
    FalParameters,
    KlingParameters,
    ProviderCallError,
    ProviderPrice,
    ShotSpecV3,
    VideoRequestV6,
    fal_capabilities,
    kling_capabilities,
    provider_price,
    resource_estimate,
)
from vagent.video.providers.fal import FalAdapter
from vagent.video.providers.kling import KlingAdapter

KLING_VIDEO_URL = "https://cdn.example-provider.test/results/task-1.mp4?Signature=secret&Expires=1"
FAL_VIDEO_URL = "https://v3.fal.media/files/example/result.mp4?token=secret"


def kling_request(prompt="雨夜咖啡店，镜头缓慢推近。"):
    return VideoRequestV6(
        provider="kling",
        model=KLING_MODEL,
        region=KLING_REGION,
        endpoint_profile=KLING_ENDPOINT_PROFILE,
        adapter_version=KLING_ADAPTER_VERSION,
        capabilities_version=MULTI_PROVIDER_CAPABILITIES_VERSION,
        price_version=KLING_PRICE_VERSION,
        source_policy_version=KLING_SOURCE_POLICY_VERSION,
        prompt=prompt,
        spec=ShotSpecV3(mode="t2v", duration_seconds=5, aspect_ratio="16:9"),
        provider_prompt=prompt,
        parameters=KlingParameters(),
    )


def fal_request(prompt="雨夜咖啡店，镜头缓慢推近。"):
    return VideoRequestV6(
        provider="fal",
        model=FAL_MODEL,
        region=FAL_REGION,
        endpoint_profile=FAL_ENDPOINT_PROFILE,
        adapter_version=FAL_ADAPTER_VERSION,
        capabilities_version=MULTI_PROVIDER_CAPABILITIES_VERSION,
        price_version=FAL_PRICE_VERSION,
        source_policy_version=FAL_SOURCE_POLICY_VERSION,
        prompt=prompt,
        spec=ShotSpecV3(mode="t2v", duration_seconds=5, aspect_ratio="16:9"),
        provider_prompt=prompt,
        parameters=FalParameters(),
    )


class Upstream:
    """A scripted provider: records every request and answers by method and path."""

    def __init__(self, *responses, status=200):
        self.requests = []
        self.responses = list(responses)
        self.status = status

    def __call__(self, request):
        self.requests.append(request)
        response = self.responses.pop(0) if self.responses else {"ok": True}
        if callable(response):
            return response(request)
        if isinstance(response, httpx.Response):
            return response
        return httpx.Response(self.status, json=response)


def kling_created(task_id="kling-task-1"):
    return {"code": 0, "message": "SUCCESS", "data": {"id": task_id}}


def kling_status(status, task_id="kling-task-1", *, url=None, usage=None):
    entry = {"id": task_id, "status": status}
    if url is not None:
        entry["outputs"] = [{"type": "video", "url": url}]
    if usage is not None:
        entry["usage"] = usage
    return {"code": 0, "message": "SUCCESS", "data": [entry]}


async def test_the_capability_table_publishes_one_frozen_entry_per_provider():
    kling, fal = kling_capabilities(), fal_capabilities()
    for capabilities in (kling, fal):
        assert capabilities.capabilities_version == "multi-provider-v3"
        assert capabilities.mode == "live"
        assert capabilities.shot_modes == ("t2v",)
        assert capabilities.input_types == ("text",)
        assert capabilities.specs == (ShotSpecV3(mode="t2v", duration_seconds=5, aspect_ratio="16:9"),)
        assert not capabilities.supports_cancel
        assert not capabilities.supports_idempotent_submit
        assert not capabilities.supports_result_url_refresh
        assert capabilities.rate.currency == "USD"
    assert (kling.audio_mode, kling.resource is not None) == ("native", True)
    assert (fal.audio_mode, fal.resource) == ("silent", None)
    assert (kling.model, fal.model) == (KLING_MODEL, FAL_MODEL)


async def test_the_frozen_quotes_keep_each_provider_currency_and_units():
    kling, fal = kling_capabilities(), fal_capabilities()
    assert provider_price(kling.rate, 5) == ProviderPrice(
        currency="USD",
        unit_price="0.112",
        quantity="5",
        amount="0.56",
        price_version=KLING_PRICE_VERSION,
        checked_at="2026-10-21",
        source_url="https://kling.ai/document-api/pricing/base/video.md",
    )
    assert resource_estimate(kling.resource, 5).units == 4
    assert provider_price(fal.rate, 5).amount == "0.42"


async def test_kling_submit_and_query_bind_the_result_origin():
    upstream = Upstream(
        kling_created(),
        kling_status("submitted"),
        kling_status("processing"),
        kling_status("succeeded", url=KLING_VIDEO_URL, usage={"duration": 5}),
    )
    async with KlingAdapter(api_key="kling-secret", transport=httpx.MockTransport(upstream)) as adapter:
        handle = await adapter.submit(kling_request(), "run:1:batch")
        assert handle.task_id == "kling-task-1"
        assert (await adapter.query(handle.task_id)).status == "queued"
        assert (await adapter.query(handle.task_id)).status == "running"
        snapshot = await adapter.query(handle.task_id)
    submitted = upstream.requests[0]
    assert str(submitted.url) == "https://api-singapore.klingai.com/text-to-video/kling-3.0-turbo"
    assert submitted.headers["Authorization"] == "Bearer kling-secret"
    body = json.loads(submitted.content)
    assert body == {
        "prompt": "雨夜咖啡店，镜头缓慢推近。",
        "settings": {"duration": 5, "resolution": "720p", "aspect_ratio": "16:9"},
        "options": {"external_task_id": KlingAdapter.external_task_id("run:1:batch")},
    }
    assert snapshot.status == "succeeded"
    output = snapshot.output
    assert output.video_url == KLING_VIDEO_URL
    assert output.result_source.origin == "https://cdn.example-provider.test:443"
    assert output.result_source.url_fingerprint == hashlib.sha256(KLING_VIDEO_URL.encode()).hexdigest()
    assert output.result_source.source_policy_version == KLING_SOURCE_POLICY_VERSION
    assert output.result_source.task_id == "kling-task-1"
    assert output.usage.duration == 5


async def test_kling_rejections_never_look_like_a_confirmed_submission():
    cases = [
        (401, {"code": 1001, "message": "invalid key"}, "PROVIDER_AUTH_FAILED"),
        (403, {"code": 1004, "message": "denied"}, "PROVIDER_ACCESS_DENIED"),
        (400, {"code": 1201, "message": "sensitive"}, "PROVIDER_CONTENT_REJECTED"),
        (429, {"code": 1102, "message": "resource pack"}, "PROVIDER_BILLING_BLOCKED"),
        (429, {"code": 1302, "message": "rate limit"}, "PROVIDER_RATE_LIMITED"),
        (422, {"code": "InvalidParameter", "message": "bad"}, "PROVIDER_INVALID_REQUEST"),
    ]
    for status, payload, code in cases:
        upstream = Upstream(payload, status=status)
        async with KlingAdapter(api_key="k", transport=httpx.MockTransport(upstream)) as adapter:
            with pytest.raises(ProviderCallError) as caught:
                await adapter.submit(kling_request(), "run:1:batch")
        assert caught.value.error.code == code
        assert caught.value.submission_outcome == "not_accepted"


async def test_kling_ambiguous_submit_is_unknown_and_never_retried():
    for response in (
        httpx.Response(500, json={"code": 500, "message": "boom"}),
        httpx.Response(200, json={"code": 0, "data": {}}),
        httpx.Response(200, content=b"not json"),
    ):
        upstream = Upstream(response)
        async with KlingAdapter(api_key="k", transport=httpx.MockTransport(upstream)) as adapter:
            with pytest.raises(ProviderCallError) as caught:
                await adapter.submit(kling_request(), "run:1:batch")
        assert caught.value.error.code == "SUBMISSION_UNKNOWN"
        assert caught.value.submission_outcome == "unknown"
        assert len(upstream.requests) == 1


async def test_kling_query_errors_pause_or_back_off():
    async with KlingAdapter(
        api_key="k", transport=httpx.MockTransport(Upstream({"code": 1001}, status=401))
    ) as adapter:
        with pytest.raises(ProviderCallError) as caught:
            await adapter.query("kling-task-1")
        assert caught.value.pause_reason == "configuration"
    async with KlingAdapter(
        api_key="k", transport=httpx.MockTransport(Upstream({"code": 1005}, status=404))
    ) as adapter:
        with pytest.raises(ProviderCallError) as caught:
            await adapter.query("kling-task-1")
        assert caught.value.pause_reason == "task_unavailable"
    async with KlingAdapter(
        api_key="k",
        transport=httpx.MockTransport(
            Upstream(httpx.Response(429, json={"code": 1302}, headers={"Retry-After": "60"}))
        ),
    ) as adapter:
        with pytest.raises(ProviderCallError) as caught:
            await adapter.query("kling-task-1")
        assert caught.value.error.code == "PROVIDER_RATE_LIMITED"
        assert caught.value.retry_after_at is not None
    async with KlingAdapter(
        api_key="k", transport=httpx.MockTransport(Upstream({"code": 0, "data": []}))
    ) as adapter:
        with pytest.raises(ProviderCallError) as caught:
            await adapter.query("kling-task-1")
        assert caught.value.pause_reason == "task_unavailable"


async def test_kling_success_requires_exactly_one_video_output():
    for outputs in (
        [],
        [{"type": "video", "url": KLING_VIDEO_URL}, {"type": "video", "url": KLING_VIDEO_URL}],
    ):
        payload = kling_status("succeeded")
        payload["data"][0]["outputs"] = outputs
        async with KlingAdapter(api_key="k", transport=httpx.MockTransport(Upstream(payload))) as adapter:
            with pytest.raises(ProviderCallError) as caught:
                await adapter.query("kling-task-1")
            assert caught.value.error.code == "QUERY_PROTOCOL_ERROR"


async def test_fal_submit_status_and_result_use_the_app_root_paths():
    upstream = Upstream(
        {"request_id": "fal-request-1", "status": "IN_QUEUE"},
        {"request_id": "fal-request-1", "status": "IN_QUEUE"},
        {"request_id": "fal-request-1", "status": "IN_PROGRESS"},
        {"request_id": "fal-request-1", "status": "COMPLETED"},
        {"video": {"url": FAL_VIDEO_URL, "content_type": "video/mp4"}},
    )
    async with FalAdapter(api_key="fal-secret", transport=httpx.MockTransport(upstream)) as adapter:
        handle = await adapter.submit(fal_request(), "run:1:batch")
        assert handle.task_id == "fal-request-1"
        assert (await adapter.query(handle.task_id)).status == "queued"
        assert (await adapter.query(handle.task_id)).status == "running"
        snapshot = await adapter.query(handle.task_id)
    assert snapshot.status == "succeeded"
    assert snapshot.output.result_source.origin == "https://v3.fal.media:443"
    submitted = upstream.requests[0]
    assert str(submitted.url) == "https://queue.fal.run/fal-ai/kling-video/v3/standard/text-to-video"
    assert submitted.headers["Authorization"] == "Key fal-secret"
    assert json.loads(submitted.content) == {
        "prompt": "雨夜咖啡店，镜头缓慢推近。",
        "duration": "5",
        "aspect_ratio": "16:9",
        "generate_audio": False,
        "shot_type": "customize",
        "enable_safety_checker": True,
        "cfg_scale": 0.5,
        "negative_prompt": "blur, distort, and low quality",
    }
    paths = [request.url.path for request in upstream.requests]
    assert paths == [
        "/fal-ai/kling-video/v3/standard/text-to-video",
        "/fal-ai/kling-video/requests/fal-request-1/status",
        "/fal-ai/kling-video/requests/fal-request-1/status",
        "/fal-ai/kling-video/requests/fal-request-1/status",
        "/fal-ai/kling-video/requests/fal-request-1",
    ]


async def test_fal_rejections_and_completed_errors_are_distinct():
    async with FalAdapter(
        api_key="k", transport=httpx.MockTransport(Upstream({"detail": "bad"}, status=422))
    ) as adapter:
        with pytest.raises(ProviderCallError) as caught:
            await adapter.submit(fal_request(), "run:1:batch")
        assert caught.value.error.code == "PROVIDER_INVALID_REQUEST"
        assert caught.value.submission_outcome == "not_accepted"
    async with FalAdapter(
        api_key="k",
        transport=httpx.MockTransport(
            Upstream(
                {"request_id": "fal-1", "status": "COMPLETED"},
                {"error": "generation failed"},
            )
        ),
    ) as adapter:
        snapshot = await adapter.query("fal-1")
    assert snapshot.status == "failed"
    assert snapshot.error.code == "PROVIDER_GENERATION_FAILED"
    async with FalAdapter(
        api_key="k",
        transport=httpx.MockTransport(
            Upstream(
                {"request_id": "fal-1", "status": "COMPLETED"},
                {"video": None},
            )
        ),
    ) as adapter:
        with pytest.raises(ProviderCallError) as caught:
            await adapter.query("fal-1")
        assert caught.value.error.code == "QUERY_PROTOCOL_ERROR"


def test_the_adapter_rejects_a_request_of_another_provider():
    import asyncio

    async def run():
        async with KlingAdapter(api_key="k", transport=httpx.MockTransport(lambda _: httpx.Response(500))):
            with pytest.raises(AppError) as caught:
                await KlingAdapter(api_key="k").submit(fal_request(), "run:1:batch")
            assert caught.value.code in {"VIDEO_PROVIDER_UNAVAILABLE", "VIDEO_KEY_MISSING"}
        async with FalAdapter(api_key="k", transport=httpx.MockTransport(lambda _: httpx.Response(500))):
            with pytest.raises(AppError) as caught:
                await FalAdapter(api_key="k").submit(kling_request(), "run:1:batch")
            assert caught.value.code in {"VIDEO_PROVIDER_UNAVAILABLE"}

    asyncio.run(run())
