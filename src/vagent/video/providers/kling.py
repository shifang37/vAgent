"""Frozen Kling Singapore API protocol: text-to-video only, one submit at most.

The host, path and settings block are fixed by the M3 E0 contract; no user-supplied
base URL, callback, image input or cancel is accepted. A successful task result binds
the exact HTTPS origin of its video URL for the download source policy (contract
§5.2); the adapter itself never downloads the file.
"""

import hashlib
from datetime import UTC
from urllib.parse import quote

from vagent.errors import AppError
from vagent.video.contracts import (
    KLING_ADAPTER_VERSION,
    KLING_ENDPOINT_PROFILE,
    KLING_MODEL,
    KLING_PRICE_VERSION,
    KLING_REGION,
    KLING_SOURCE_POLICY_VERSION,
    JobErrorV2,
    ProviderCallError,
    ProviderOutputV2,
    ProviderTaskHandleV3,
    ProviderTaskSnapshotV3,
    ProviderTimes,
    ProviderUsage,
    VideoCapabilitiesV4,
    VideoRequestV6,
    bound_result_source,
    kling_capabilities,
)
from vagent.video.providers.protocol import ProviderHttpAdapter, safe_identifier

BASE_URL = "https://api-singapore.klingai.com"
SUBMIT_PATH = "/text-to-video/kling-3.0-turbo"
QUERY_PATH = "/tasks"

# Kling answers a business rejection with HTTP 200 plus a non-zero `code`. Only
# responses listed here prove the request was refused; anything else is ambiguous.
_REJECTIONS = {
    "1101": "PROVIDER_BILLING_BLOCKED",
    "1102": "PROVIDER_BILLING_BLOCKED",
    "1201": "PROVIDER_CONTENT_REJECTED",
    "1301": "PROVIDER_CONTENT_REJECTED",
    "1302": "PROVIDER_RATE_LIMITED",
    "1303": "PROVIDER_RATE_LIMITED",
    "1304": "PROVIDER_ACCESS_DENIED",
    "1001": "PROVIDER_AUTH_FAILED",
    "1002": "PROVIDER_AUTH_FAILED",
    "1003": "PROVIDER_AUTH_FAILED",
    "1004": "PROVIDER_ACCESS_DENIED",
}

_STATUS_MAP = {"submitted": "queued", "processing": "running", "succeeded": "succeeded", "failed": "failed"}


def _payload_code(payload: dict) -> str | None:
    """Kling returns numeric business codes; the local error record keeps strings."""
    value = payload.get("code")
    if type(value) is int:
        return str(value)
    return value if isinstance(value, str) else None


class KlingAdapter(ProviderHttpAdapter):
    provider = "kling"
    base_url = BASE_URL

    def capabilities(self) -> VideoCapabilitiesV4:
        return kling_capabilities()

    def check_configuration(self, request=None) -> None:
        self.require_key()
        if request is None:
            return
        if (
            request.provider != self.provider
            or request.model != KLING_MODEL
            or request.region != KLING_REGION
            or request.endpoint_profile != KLING_ENDPOINT_PROFILE
            or request.adapter_version != KLING_ADAPTER_VERSION
            or request.price_version != KLING_PRICE_VERSION
            or request.source_policy_version != KLING_SOURCE_POLICY_VERSION
        ):
            raise AppError("VIDEO_PROVIDER_UNAVAILABLE", "缺少原任务的 Kling 冻结配置。")

    @staticmethod
    def external_task_id(operation_key: str) -> str:
        """A deterministic, provider-safe association for the local submission."""
        return "vag-" + hashlib.sha256(operation_key.encode("utf-8")).hexdigest()[:32]

    def _body(self, request: VideoRequestV6, operation_key: str) -> dict:
        parameters = request.parameters
        return {
            "prompt": request.provider_prompt,
            "settings": {
                "duration": parameters.duration,
                "resolution": parameters.resolution,
                "aspect_ratio": parameters.aspect_ratio,
            },
            "options": {"external_task_id": self.external_task_id(operation_key)},
        }

    def _rejection(self, status: int, payload: dict) -> str | None:
        code = _REJECTIONS.get(_payload_code(payload) or "")
        if code:
            return code
        if status == 401:
            return "PROVIDER_AUTH_FAILED"
        if status == 403:
            return "PROVIDER_ACCESS_DENIED"
        if status == 404:
            return "PROVIDER_MODEL_UNAVAILABLE"
        if status == 429:
            return "PROVIDER_RATE_LIMITED"
        if status in {400, 422}:
            return "PROVIDER_INVALID_REQUEST"
        return None

    async def submit(self, request, operation_key: str) -> ProviderTaskHandleV3:
        request = VideoRequestV6.model_validate(request)
        self.check_configuration(request)
        status, payload, _ = await self._request(
            "POST", SUBMIT_PATH, stage="submit", body=self._body(request, operation_key)
        )
        data = payload.get("data")
        task_id = safe_identifier(data.get("id")) if isinstance(data, dict) else None
        if 200 <= status < 300 and _payload_code(payload) == "0" and task_id:
            return ProviderTaskHandleV3(task_id=task_id)
        code = self._rejection(status, payload)
        if code:
            raise ProviderCallError(
                self._error("submit", code, status=status, payload=payload),
                submission_outcome="not_accepted",
            )
        raise self._call_error("submit", "SUBMISSION_UNKNOWN", status=status, payload=payload)

    async def query(self, task_id: str, *, endpoint_profile: str | None = None) -> ProviderTaskSnapshotV3:
        if safe_identifier(task_id) is None:
            raise self._call_error("query", "QUERY_PROTOCOL_ERROR")
        status, payload, retry_after_at = await self._request(
            "GET", f"{QUERY_PATH}?task_ids={quote(task_id, safe='')}", stage="query"
        )
        if 200 <= status < 300 and _payload_code(payload) == "0":
            data = payload.get("data")
            entry = (
                next((item for item in data if isinstance(item, dict) and item.get("id") == task_id), None)
                if isinstance(data, list)
                else None
            )
            if entry is None:
                raise ProviderCallError(
                    self._error("query", "PROVIDER_TASK_UNAVAILABLE", status=status, payload=payload),
                    pause_reason="task_unavailable",
                )
            return self._snapshot(entry, task_id, status, payload)
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

    def _snapshot(self, entry: dict, task_id: str, status: int, payload: dict) -> ProviderTaskSnapshotV3:
        remote = entry.get("status")
        if remote not in _STATUS_MAP:
            raise self._call_error("query", "QUERY_PROTOCOL_ERROR", status=status, payload=payload)
        local = _STATUS_MAP[remote]
        if local in {"queued", "running"}:
            return ProviderTaskSnapshotV3(task_id=task_id, status=local)
        if local == "failed":
            return ProviderTaskSnapshotV3(
                task_id=task_id,
                status="failed",
                error=JobErrorV2(
                    stage="generate",
                    code="PROVIDER_GENERATION_FAILED",
                    message="上游已确认视频生成失败，不会自动修改内容或重新提交。",
                    provider_code=_payload_code(entry.get("error") or {}) or None,
                ),
            )
        outputs = entry.get("outputs")
        videos = (
            [item for item in outputs if isinstance(item, dict) and item.get("type") == "video"]
            if isinstance(outputs, list)
            else []
        )
        if len(videos) != 1 or not isinstance(videos[0].get("url"), str):
            raise self._call_error("query", "QUERY_PROTOCOL_ERROR", status=status, payload=payload)
        url = videos[0]["url"]
        try:
            source = bound_result_source(task_id, url, KLING_SOURCE_POLICY_VERSION)
        except ValueError:
            raise self._call_error("query", "QUERY_PROTOCOL_ERROR", status=status, payload=payload) from None
        usage = entry.get("usage")
        provider_usage = None
        if isinstance(usage, dict):
            duration = usage.get("duration")
            if isinstance(duration, (int, float)) and not isinstance(duration, bool) and duration >= 0:
                provider_usage = ProviderUsage(duration=float(duration))
        output = ProviderOutputV2(
            task_id=task_id,
            received_at=self.clock().astimezone(UTC).isoformat(),
            video_url=url,
            result_source=source,
            provider_times=ProviderTimes(
                submit_time=_text(entry.get("created_at")),
                end_time=_text(entry.get("updated_at")),
            ),
            usage=provider_usage,
        )
        return ProviderTaskSnapshotV3(task_id=task_id, status="succeeded", output=output)


def _text(value) -> str | None:
    return value if isinstance(value, str) and value else None
