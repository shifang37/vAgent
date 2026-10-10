import asyncio
import json
from datetime import UTC, datetime

import httpx
import pytest
from wan_support import VIDEO_URL, frozen_request, provider_response

from vagent.errors import AppError
from vagent.video.contracts import ProviderCallError
from vagent.video.providers.wan import MAX_RESPONSE_BYTES, WanAdapter, retry_after_timestamp


def adapter(handler, **kwargs):
    return WanAdapter(
        api_key="video-secret",
        workspace_id="test-workspace",
        transport=httpx.MockTransport(handler),
        **kwargs,
    )


async def test_exact_frozen_protocol_and_original_task_identity(video_clock):
    calls = []

    def handler(request):
        calls.append(request)
        if request.method == "POST":
            return httpx.Response(200, json=provider_response())
        return httpx.Response(
            200,
            json={
                **provider_response(
                    "SUCCEEDED",
                    request_id="query-trace",
                    video_url=VIDEO_URL + "&Expires=1893456300",
                    end_time="2030-01-01 08:00:01",
                ),
                "usage": {
                    "duration": 5,
                    "output_video_duration": 5,
                    "input_video_duration": 0,
                    "video_count": 1,
                    "SR": "720P",
                    "ratio": "16:9",
                    "bill": "ignored",
                },
                "unknown_future_field": "ignored",
            },
        )

    async with adapter(handler, clock=video_clock) as provider:
        request = frozen_request(prompt="  连续镜头\n  内部空白保留。  ")
        handle = await provider.submit(request, "local-operation-do-not-send")
        assert handle.task_id == "wan-original" and handle.request_id == "trace-original"
        assert handle.snapshot.status == "queued"
        snapshot = await provider.query(handle.task_id)
        assert snapshot.status == "succeeded" and snapshot.request_id == "query-trace"
        assert snapshot.output.video_url.startswith(VIDEO_URL)
        assert snapshot.output.url_expires_at == "2030-01-01T00:05:00+00:00"
        assert snapshot.output.expiry_source == "signature"
        assert snapshot.output.provider_times.end_time == "2030-01-01 08:00:01"
        assert snapshot.output.usage.duration == 5 and snapshot.output.usage.video_count == 1
        assert "private-signature" not in repr(snapshot)
        assert not provider.client.follow_redirects and not provider.client.trust_env
        assert provider.client.timeout.connect == 10
    assert provider.client.is_closed
    assert len(calls) == 2
    post, query = calls
    assert (
        post.method == "POST"
        and str(post.url)
        == "https://test-workspace.cn-beijing.maas.aliyuncs.com/api/v1/services/aigc/video-generation/video-synthesis"
    )
    assert post.headers["authorization"] == "Bearer video-secret"
    assert post.headers["X-DashScope-Async"] == "enable"
    assert post.headers["content-type"] == "application/json"
    assert "local-operation" not in str(post.headers) + post.content.decode()
    assert json.loads(post.content) == {
        "model": "wan2.7-t2v-2026-06-12",
        "input": {"prompt": "生成单镜头视频。\n连续镜头\n  内部空白保留。"},
        "parameters": {
            "resolution": "720P",
            "ratio": "16:9",
            "duration": 5,
            "prompt_extend": False,
            "watermark": True,
            "seed": 0,
        },
    }
    assert query.method == "GET" and query.url.path == "/api/v1/tasks/wan-original"
    assert not query.content and "X-DashScope-Async" not in query.headers


@pytest.mark.parametrize(
    "output",
    [
        {"task_id": "wan-original"},
        {"task_id": "wan-original", "task_status": "NEW_STATUS"},
        {"task_id": "wan-original", "task_status": "SUCCEEDED"},
        {"task_id": "wan-original", "task_status": ["RUNNING"]},
        {"task_id": "wan-original", "task_status": "UNKNOWN"},
        {"task_id": "wan-original", "task_status": "SUCCEEDED", "video_url": 42},
    ],
)
async def test_acceptance_survives_invalid_optional_initial_snapshot(output):
    async with adapter(lambda _: httpx.Response(200, json={"output": output})) as provider:
        handle = await provider.submit(frozen_request(), "op")
        assert handle.task_id == "wan-original" and handle.snapshot is None


@pytest.mark.parametrize(
    "status,code,local",
    [
        (400, "InvalidParameter", "PROVIDER_INVALID_REQUEST"),
        (400, "InvalidInputLength", "PROVIDER_INVALID_REQUEST"),
        (401, "invalid_api_key", "PROVIDER_AUTH_FAILED"),
        (403, "AccessDenied.Unpurchased", "PROVIDER_ACCESS_DENIED"),
        (403, "Workspace.AccessDenied", "PROVIDER_ACCESS_DENIED"),
        (404, "WorkSpaceNotFound", "PROVIDER_MODEL_UNAVAILABLE"),
        (400, "Arrearage", "PROVIDER_BILLING_BLOCKED"),
        (403, "AllocationQuota.FreeTierOnly", "PROVIDER_BILLING_BLOCKED"),
        (429, "BudgetLimitExceeded", "PROVIDER_BILLING_BLOCKED"),
        (429, "Throttling.Concurrency", "PROVIDER_RATE_LIMITED"),
    ],
)
async def test_only_exact_documented_rejections_are_not_accepted(status, code, local):
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(
            status, json={"code": code, "message": "untrusted-video-secret", "request_id": "safe-trace"}
        )

    async with adapter(handler) as provider:
        with pytest.raises(ProviderCallError) as caught:
            await provider.submit(frozen_request(), "op")
        error = caught.value
        assert error.submission_outcome == "not_accepted" and error.error.code == local
        assert error.error.http_status == status and error.error.request_id == "safe-trace"
        assert "secret" not in str(error) + error.error.model_dump_json()
    assert len(calls) == 1


@pytest.mark.parametrize(
    "status,payload",
    [
        (500, {"code": "InvalidApiKey"}),
        (408, {"code": "InvalidParameter"}),
        (400, {"code": "DataInspectionFailed"}),
        (400, {"code": "InternalError.Algo.Invalid"}),
        (400, {"code": "InvalidApiKey"}),
        (401, {"code": "UnknownAuthCode"}),
        (200, {}),
        (200, {"output": {"task_id": "https://untrusted/task"}}),
        (401, {"code": "InvalidApiKey", "output": {"task_id": "accepted"}}),
        (401, {"code": "InvalidApiKey", "output": {"task_id": None}}),
        (401, {"code": "InvalidApiKey", "task_id": "accepted"}),
        (200, {"code": "InvalidApiKey", **provider_response()}),
        (302, provider_response()),
    ],
)
async def test_ambiguous_submission_never_retries_or_follows_redirects(status, payload):
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(status, json=payload, headers={"Location": "https://untrusted.example"})

    async with adapter(handler) as provider:
        with pytest.raises(ProviderCallError) as caught:
            await provider.submit(frozen_request(), "op")
        assert caught.value.submission_outcome == "unknown"
        assert caught.value.error.code == "SUBMISSION_UNKNOWN"
    assert len(calls) == 1


@pytest.mark.parametrize(
    "content",
    [
        b"not-json",
        b'{"output":{},"output":{"task_id":"ambiguous"}}',
        b'{"output":NaN}',
        b"x" * (MAX_RESPONSE_BYTES + 1),
    ],
    ids=["non-json", "duplicate-key", "nan", "oversized"],
)
async def test_bounded_complete_json_is_required(content):
    async with adapter(lambda _: httpx.Response(200, content=content)) as provider:
        with pytest.raises(ProviderCallError) as caught:
            await provider.submit(frozen_request(), "op")
        assert caught.value.submission_outcome == "unknown"
        with pytest.raises(ProviderCallError) as caught:
            await provider.query("wan-original")
        assert caught.value.error.code == "QUERY_PROTOCOL_ERROR"


@pytest.mark.parametrize(
    "error", [httpx.ConnectError, httpx.ReadError, httpx.WriteError, httpx.ConnectTimeout, httpx.ReadTimeout]
)
async def test_network_submission_errors_are_always_uncertain(error):
    calls = []

    def handler(request):
        calls.append(request)
        raise error("untrusted-video-secret", request=request)

    async with adapter(handler) as provider:
        with pytest.raises(ProviderCallError) as caught:
            await provider.submit(frozen_request(), "op")
        assert caught.value.submission_outcome == "unknown" and "secret" not in str(caught.value)
    assert len(calls) == 1


@pytest.mark.parametrize(
    "status,local",
    [
        ("PENDING", "queued"),
        ("RUNNING", "running"),
        ("SUCCEEDED", "succeeded"),
        ("FAILED", "failed"),
        ("CANCELED", "failed"),
    ],
)
async def test_documented_states_and_immediate_terminal_submit(status, local):
    body = provider_response(
        status, video_url=VIDEO_URL, code="DataInspectionFailed", message="raw-provider-message"
    )
    async with adapter(lambda _: httpx.Response(200, json=body)) as provider:
        handle = await provider.submit(frozen_request(), "op")
        assert handle.snapshot.status == local
        result = await provider.query(handle.task_id)
        assert result.status == local and result.provider_status == status
        if status == "SUCCEEDED":
            assert result.output.url_expires_at is None and result.output.expiry_source == "unknown"
        if status in {"FAILED", "CANCELED"}:
            assert result.error.stage == "generate"
            assert result.error.code == (
                "PROVIDER_CANCELED" if status == "CANCELED" else "PROVIDER_GENERATION_FAILED"
            )
            assert "raw-provider-message" not in result.model_dump_json()


@pytest.mark.parametrize(
    "body",
    [
        provider_response("new-status"),
        provider_response(task_id="different-task"),
        provider_response("SUCCEEDED"),
        provider_response("SUCCEEDED", video_url="http://plain-http.example/test.mp4"),
        {**provider_response("SUCCEEDED", video_url=VIDEO_URL), "usage": {"duration": True}},
        provider_response("SUCCEEDED", video_url=VIDEO_URL, end_time=5),
    ],
)
async def test_invalid_query_fields_do_not_become_generation_failures(body):
    async with adapter(lambda _: httpx.Response(200, json=body)) as provider:
        with pytest.raises(ProviderCallError) as caught:
            await provider.query("wan-original")
        assert caught.value.error.stage == "query" and caught.value.error.code == "QUERY_PROTOCOL_ERROR"
        assert caught.value.submission_outcome is None


async def test_unknown_status_and_authentication_pause_original_query():
    for status, body, reason in [
        (200, provider_response("UNKNOWN"), "task_unavailable"),
        (401, {"code": "InvalidApiKey"}, "configuration"),
        (403, {"code": "AccessDenied"}, "configuration"),
    ]:
        async with adapter(lambda _, status=status, body=body: httpx.Response(status, json=body)) as provider:
            with pytest.raises(ProviderCallError) as caught:
                await provider.query("wan-original")
            assert caught.value.pause_reason == reason


async def test_rate_limit_retry_after_is_returned_for_persistent_scheduling(video_clock):
    async with adapter(
        lambda _: httpx.Response(429, json={"code": "Throttling"}, headers={"Retry-After": "90"}),
        clock=video_clock,
    ) as provider:
        with pytest.raises(ProviderCallError) as caught:
            await provider.query("wan-original")
        assert caught.value.retry_after_at == "2030-01-01T00:01:30+00:00"
        assert caught.value.error.code == "PROVIDER_RATE_LIMITED" and caught.value.pause_reason is None
    at = datetime(2030, 1, 1, tzinfo=UTC)
    assert retry_after_timestamp("Tue, 01 Jan 2030 00:02:00 GMT", at) == "2030-01-01T00:02:00+00:00"
    for invalid in ("-5", "1.5", "never", "9" * 100):
        assert retry_after_timestamp(invalid, at) is None


async def test_wrong_scope_and_path_are_rejected_without_http():
    calls = []
    async with adapter(lambda request: calls.append(request)) as provider:
        with pytest.raises(AppError) as caught:
            await provider.submit(frozen_request(workspace_id="other-workspace"), "op")
        assert caught.value.code == "VIDEO_PROVIDER_UNAVAILABLE"
        for task_id in ("../tasks", "https://untrusted", "汉字", "a" * 257):
            with pytest.raises(ProviderCallError):
                await provider.query(task_id)
    assert not calls


async def test_submit_cancellation_is_not_transformed_into_safe_rejection():
    started = asyncio.Event()

    async def handler(_):
        started.set()
        await asyncio.Event().wait()

    async with adapter(handler) as provider:
        task = asyncio.create_task(provider.submit(frozen_request(), "op"))
        await started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
