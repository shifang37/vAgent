"""Frozen fal queue protocol for the Kling v3 standard silent relay.

The queue host, endpoint and app root are fixed: no user-supplied base URL, no
callback and no cancel. A `COMPLETED` queue entry is not a delivery; the result is
read from the app-root path (never a URL echoed by the response) and its video URL
binds the exact HTTPS origin for the download source policy.
"""

from datetime import UTC
from urllib.parse import quote

from vagent.errors import AppError
from vagent.video.contracts import (
    FAL_ADAPTER_VERSION,
    FAL_ENDPOINT_PROFILE,
    FAL_MODEL,
    FAL_PRICE_VERSION,
    FAL_REGION,
    FAL_SOURCE_POLICY_VERSION,
    JobErrorV2,
    ProviderCallError,
    ProviderOutputV2,
    ProviderTaskHandleV3,
    ProviderTaskSnapshotV3,
    VideoCapabilitiesV4,
    VideoRequestV6,
    bound_result_source,
    fal_capabilities,
)
from vagent.video.providers.protocol import ProviderHttpAdapter, safe_identifier

BASE_URL = "https://queue.fal.run"
SUBMIT_PATH = "/fal-ai/kling-video/v3/standard/text-to-video"
APP_ROOT = "/fal-ai/kling-video"


class FalAdapter(ProviderHttpAdapter):
    provider = "fal"
    base_url = BASE_URL

    @staticmethod
    def _authorization(key: str) -> str:
        return f"Key {key}"

    def capabilities(self) -> VideoCapabilitiesV4:
        return fal_capabilities()

    def check_configuration(self, request=None) -> None:
        self.require_key()
        if request is None:
            return
        if (
            request.provider != self.provider
            or request.model != FAL_MODEL
            or request.region != FAL_REGION
            or request.endpoint_profile != FAL_ENDPOINT_PROFILE
            or request.adapter_version != FAL_ADAPTER_VERSION
            or request.price_version != FAL_PRICE_VERSION
            or request.source_policy_version != FAL_SOURCE_POLICY_VERSION
        ):
            raise AppError("VIDEO_PROVIDER_UNAVAILABLE", "缺少原任务的 fal 冻结配置。")

    def _body(self, request: VideoRequestV6) -> dict:
        parameters = request.parameters
        return {
            "prompt": request.provider_prompt,
            "duration": parameters.duration,
            "aspect_ratio": parameters.aspect_ratio,
            "generate_audio": parameters.generate_audio,
            "shot_type": parameters.shot_type,
            "enable_safety_checker": parameters.enable_safety_checker,
            "cfg_scale": parameters.cfg_scale,
            "negative_prompt": parameters.negative_prompt,
        }

    def _rejection(self, status: int, payload: dict) -> str | None:
        if status == 401:
            return "PROVIDER_AUTH_FAILED"
        if status == 403:
            return "PROVIDER_ACCESS_DENIED"
        if status == 404:
            return "PROVIDER_MODEL_UNAVAILABLE"
        if status == 429:
            return "PROVIDER_RATE_LIMITED"
        if status in {400, 422}:
            detail = payload.get("detail")
            if isinstance(detail, str) and "safety" in detail.lower():
                return "PROVIDER_CONTENT_REJECTED"
            return "PROVIDER_INVALID_REQUEST"
        return None

    def _query_error(self, status, payload, retry_after_at=None):
        if status in {401, 403}:
            raise ProviderCallError(
                self._error(
                    "query",
                    "PROVIDER_AUTH_FAILED" if status == 401 else "PROVIDER_ACCESS_DENIED",
                    status=status,
                    payload=payload,
                ),
                pause_reason="configuration",
            )
        if status == 404:
            raise ProviderCallError(
                self._error("query", "PROVIDER_TASK_UNAVAILABLE", status=status, payload=payload),
                pause_reason="task_unavailable",
            )
        if status == 429:
            raise ProviderCallError(
                self._error("query", "PROVIDER_RATE_LIMITED", status=status, payload=payload),
                retry_after_at=retry_after_at,
            )
        if 500 <= status < 600:
            raise ProviderCallError(
                self._error("query", "QUERY_HTTP_ERROR", status=status, payload=payload),
                retry_after_at=retry_after_at,
            )
        raise self._call_error("query", "QUERY_PROTOCOL_ERROR", status=status, payload=payload)

    async def submit(self, request, operation_key: str) -> ProviderTaskHandleV3:
        request = VideoRequestV6.model_validate(request)
        self.check_configuration(request)
        status, payload, retry_after_at = await self._request(
            "POST", SUBMIT_PATH, stage="submit", body=self._body(request)
        )
        task_id = safe_identifier(payload.get("request_id"))
        if 200 <= status < 300 and task_id:
            return ProviderTaskHandleV3(task_id=task_id)
        code = self._rejection(status, payload)
        if code:
            # M8 E2: a proven 429 reports Retry-After to the local submit gate.
            raise ProviderCallError(
                self._error("submit", code, status=status, payload=payload),
                submission_outcome="not_accepted",
                retry_after_at=retry_after_at,
            )
        raise self._call_error("submit", "SUBMISSION_UNKNOWN", status=status, payload=payload)

    async def query(self, task_id: str, *, endpoint_profile: str | None = None) -> ProviderTaskSnapshotV3:
        if safe_identifier(task_id) is None:
            raise self._call_error("query", "QUERY_PROTOCOL_ERROR")
        path = f"{APP_ROOT}/requests/{quote(task_id, safe='')}"
        status, payload, retry_after_at = await self._request("GET", f"{path}/status", stage="query")
        if not (200 <= status < 300):
            self._query_error(status, payload, retry_after_at)
        remote = payload.get("status")
        if remote == "IN_QUEUE":
            return ProviderTaskSnapshotV3(task_id=task_id, status="queued")
        if remote == "IN_PROGRESS":
            return ProviderTaskSnapshotV3(task_id=task_id, status="running")
        if remote != "COMPLETED":
            raise self._call_error("query", "QUERY_PROTOCOL_ERROR", status=status, payload=payload)
        # The status body may echo a response URL; only the frozen app-root path is read.
        status, payload, retry_after_at = await self._request("GET", path, stage="query")
        if not (200 <= status < 300):
            self._query_error(status, payload, retry_after_at)
        if payload.get("error"):
            return ProviderTaskSnapshotV3(
                task_id=task_id,
                status="failed",
                error=JobErrorV2(
                    stage="generate",
                    code="PROVIDER_GENERATION_FAILED",
                    message="上游已确认视频生成失败，不会自动修改内容或重新提交。",
                ),
            )
        video = payload.get("video")
        url = video.get("url") if isinstance(video, dict) else None
        if not isinstance(url, str):
            raise self._call_error("query", "QUERY_PROTOCOL_ERROR", status=status, payload=payload)
        try:
            source = bound_result_source(task_id, url, FAL_SOURCE_POLICY_VERSION)
        except ValueError:
            raise self._call_error("query", "QUERY_PROTOCOL_ERROR", status=status, payload=payload) from None
        output = ProviderOutputV2(
            task_id=task_id,
            received_at=self.clock().astimezone(UTC).isoformat(),
            video_url=url,
            result_source=source,
            usage=None,
        )
        return ProviderTaskSnapshotV3(task_id=task_id, status="succeeded", output=output)
