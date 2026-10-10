"""Frozen Beijing workspace HTTP protocol. No implicit submission retries."""

import asyncio
import json
import re
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from email.utils import parsedate_to_datetime
from urllib.parse import parse_qs, quote, urlsplit

import httpx
from pydantic import TypeAdapter

from vagent.errors import AppError
from vagent.video.contracts import (
    WAN_ADAPTER_VERSION,
    WAN_ENDPOINT_PROFILE,
    WAN_MODEL,
    WAN_REGION,
    JobErrorV2,
    ProviderCallError,
    ProviderOutput,
    ProviderTaskHandleV2,
    ProviderTaskSnapshotV2,
    ProviderTimes,
    ProviderUsage,
    VideoRequestV2,
    WorkspaceId,
    wan_capabilities,
)

MAX_RESPONSE_BYTES = 1024 * 1024
SUBMIT_PATH = "/api/v1/services/aigc/video-generation/video-synthesis"
_IDENTIFIER = re.compile(r"[A-Za-z0-9_-]{1,256}\Z", re.ASCII)
_CODE = re.compile(r"[A-Za-z][A-Za-z0-9_.-]{0,127}\Z", re.ASCII)

_REJECTIONS = {
    (400, code): "PROVIDER_INVALID_REQUEST"
    for code in ("InvalidParameter", "InvalidInputLength", "BadRequest.EmptyModel")
}
_REJECTIONS.update({(401, code): "PROVIDER_AUTH_FAILED" for code in ("InvalidApiKey", "invalid_api_key")})
_REJECTIONS.update(
    {
        (403, code): "PROVIDER_ACCESS_DENIED"
        for code in (
            "AccessDenied",
            "access_denied",
            "AccessDenied.Unpurchased",
            "Model.AccessDenied",
            "Workspace.AccessDenied",
            "Endpoint.AccessDenied",
        )
    }
)
_REJECTIONS.update(
    {
        (404, code): "PROVIDER_MODEL_UNAVAILABLE"
        for code in ("ModelNotFound", "model_not_found", "WorkSpaceNotFound")
    }
)
_REJECTIONS.update(
    {
        (400, "Arrearage"): "PROVIDER_BILLING_BLOCKED",
        (403, "AllocationQuota.FreeTierOnly"): "PROVIDER_BILLING_BLOCKED",
        (429, "BudgetLimitExceeded"): "PROVIDER_BILLING_BLOCKED",
    }
)
_REJECTIONS.update(
    {
        (429, code): "PROVIDER_RATE_LIMITED"
        for code in (
            "Throttling",
            "Throttling.RateQuota",
            "Throttling.BurstRate",
            "Throttling.AllocationQuota",
            "Throttling.Concurrency",
            "LimitRequests",
            "limit_requests",
        )
    }
)
_MESSAGES = {
    "PROVIDER_INVALID_REQUEST": "视频服务明确拒绝了请求参数，请检查原任务规格。",
    "PROVIDER_AUTH_FAILED": "视频凭证无效，请检查北京地域与原业务空间的 Key。",
    "PROVIDER_ACCESS_DENIED": "视频服务拒绝访问，请检查原业务空间、模型授权和 Key 权限。",
    "PROVIDER_MODEL_UNAVAILABLE": "原视频模型或业务空间不可用，请核对配置。",
    "PROVIDER_BILLING_BLOCKED": "视频服务因余额、额度或账单上限拒绝请求，请检查账号费用设置。",
    "PROVIDER_RATE_LIMITED": "视频服务限流，保留原任务并按已保存的策略处理。",
    "SUBMISSION_UNKNOWN": "提交结果不确定，未取得已确认的上游 ID；不会自动重新提交。",
    "QUERY_PROTOCOL_ERROR": "视频服务返回了无效查询结果，保留原任务与生成状态。",
    "QUERY_NETWORK": "原视频任务查询连接中断，按已保存的策略重试。",
    "QUERY_TIMEOUT": "原视频任务查询超时，按已保存的策略重试。",
    "QUERY_HTTP_ERROR": "原视频任务暂时无法查询，按已保存的策略重试。",
    "PROVIDER_TASK_UNAVAILABLE": "原视频任务不可查询或已过保留期限，查询已暂停。",
    "PROVIDER_GENERATION_FAILED": "上游已确认视频生成失败，不会自动修改内容或重新提交。",
    "PROVIDER_CANCELED": "上游已确认视频任务取消，不会创建替代任务。",
}


def safe_identifier(value) -> str | None:
    return value if isinstance(value, str) and _IDENTIFIER.fullmatch(value) else None


def safe_provider_code(value) -> str | None:
    return value if isinstance(value, str) and _CODE.fullmatch(value) else None


def retry_after_timestamp(value: str | None, received_at: datetime) -> str | None:
    """Accept delta-seconds or HTTP dates; never shorten the local retry window."""
    if not isinstance(value, str) or len(value) > 128:
        return None
    try:
        if re.fullmatch(r"[0-9]{1,10}", value.strip()):
            deadline = received_at + timedelta(seconds=int(value))
        else:
            deadline = parsedate_to_datetime(value)
            if deadline.utcoffset() is None:
                return None
        return max(received_at, deadline).astimezone(UTC).isoformat()
    except (ValueError, TypeError, OverflowError):
        return None


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate response key")
        result[key] = value
    return result


def _invalid_number(_value):
    raise ValueError("Non-finite response number")


def _has_acceptance(value) -> bool:
    if isinstance(value, dict):
        return any(
            key in {"task_id", "taskId", "task_status", "taskStatus"}
            or (key == "output" and item not in (None, {}))
            or _has_acceptance(item)
            for key, item in value.items()
        )
    if isinstance(value, list):
        return any(_has_acceptance(item) for item in value)
    return False


class WanAdapter:
    adapter_version = WAN_ADAPTER_VERSION
    endpoint_profile = WAN_ENDPOINT_PROFILE

    def __init__(
        self,
        *,
        api_key: str | None = None,
        workspace_id: str | None = None,
        region: str = WAN_REGION,
        model: str = WAN_MODEL,
        transport: httpx.AsyncBaseTransport | None = None,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ):
        if workspace_id is not None:
            TypeAdapter(WorkspaceId).validate_python(workspace_id)
        if region != WAN_REGION or model != WAN_MODEL:
            raise AppError("VIDEO_CONFIG_INVALID", "视频服务仅支持已冻结的北京地域和万相模型。")
        self._key = api_key
        self.workspace_id, self.region, self.model = workspace_id, region, model
        self.clock = clock
        self.client = httpx.AsyncClient(
            transport=transport or httpx.AsyncHTTPTransport(retries=0),
            follow_redirects=False,
            trust_env=False,
            timeout=httpx.Timeout(30, connect=10),
        )

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        await self.aclose()

    async def aclose(self):
        await self.client.aclose()

    def capabilities(self):
        return wan_capabilities()

    def check_configuration(self, request: VideoRequestV2 | None = None):
        if not self._key:
            raise AppError("VIDEO_KEY_MISSING", "请配置视频服务 Key，文本模型 Key 不能代替视频 Key。")
        if not self.workspace_id:
            raise AppError("VIDEO_WORKSPACE_REQUIRED", "请配置北京视频 Key 对应的业务空间 ID。")
        if request is not None and (
            request.workspace_id != self.workspace_id
            or request.region != self.region
            or request.model != self.model
            or request.adapter_version != self.adapter_version
            or request.endpoint_profile != self.endpoint_profile
        ):
            raise AppError("VIDEO_PROVIDER_UNAVAILABLE", "缺少原视频任务的地域、业务空间或适配器配置。")

    def _error(self, stage, code, *, status=None, payload=None, request_id=None):
        payload = payload if isinstance(payload, dict) else {}
        return JobErrorV2(
            stage=stage,
            code=code,
            message=_MESSAGES[code],
            http_status=status,
            provider_code=safe_provider_code(payload.get("code")),
            request_id=safe_identifier(request_id or payload.get("request_id")),
        )

    def _call_error(self, stage, code, **kwargs):
        return ProviderCallError(
            self._error(stage, code, **kwargs), submission_outcome="unknown" if stage == "submit" else None
        )

    async def _request(self, method, path, *, stage, body=None):
        self.check_configuration()
        headers = {"Authorization": f"Bearer {self._key}", "Accept": "application/json"}
        if stage == "submit":
            headers["X-DashScope-Async"] = "enable"
        url = f"https://{self.workspace_id}.cn-beijing.maas.aliyuncs.com{path}"
        try:
            async with asyncio.timeout(30):
                async with self.client.stream(
                    method, url, headers=headers, json=body, follow_redirects=False, auth=None
                ) as response:
                    content = bytearray()
                    async for chunk in response.aiter_bytes(chunk_size=65536):
                        if len(content) + len(chunk) > MAX_RESPONSE_BYTES:
                            raise ValueError("Oversized provider response")
                        content.extend(chunk)
                    payload = json.loads(
                        content, object_pairs_hook=_unique_object, parse_constant=_invalid_number
                    )
                    if not isinstance(payload, dict):
                        raise ValueError("Provider response must be an object")
                    return (
                        response.status_code,
                        payload,
                        retry_after_timestamp(response.headers.get("Retry-After"), self.clock()),
                    )
        except (TimeoutError, httpx.TimeoutException):
            raise self._call_error(
                stage, "SUBMISSION_UNKNOWN" if stage == "submit" else "QUERY_TIMEOUT"
            ) from None
        except httpx.HTTPError:
            raise self._call_error(
                stage, "SUBMISSION_UNKNOWN" if stage == "submit" else "QUERY_NETWORK"
            ) from None
        except (ValueError, RecursionError):
            raise self._call_error(
                stage, "SUBMISSION_UNKNOWN" if stage == "submit" else "QUERY_PROTOCOL_ERROR"
            ) from None

    def _snapshot(self, payload: dict, task_id: str) -> ProviderTaskSnapshotV2:
        output = payload.get("output")
        if not isinstance(output, dict) or output.get("task_id") != task_id:
            raise ValueError("Missing or mismatched original task")
        request_id = payload.get("request_id")
        if request_id is not None and safe_identifier(request_id) is None:
            raise ValueError("Invalid request ID")
        status = output.get("task_status")
        if status == "UNKNOWN":
            raise ProviderCallError(
                self._error("query", "PROVIDER_TASK_UNAVAILABLE", request_id=request_id),
                pause_reason="task_unavailable",
            )
        if not isinstance(status, str) or status not in {
            "PENDING",
            "RUNNING",
            "SUCCEEDED",
            "FAILED",
            "CANCELED",
        }:
            raise ValueError("Unrecognized provider status")
        if status in {"FAILED", "CANCELED"}:
            return ProviderTaskSnapshotV2(
                task_id=task_id,
                request_id=request_id,
                status="failed",
                error=self._error(
                    "generate",
                    "PROVIDER_CANCELED" if status == "CANCELED" else "PROVIDER_GENERATION_FAILED",
                    payload=output,
                    request_id=request_id,
                ),
            )
        if status in {"PENDING", "RUNNING"}:
            return ProviderTaskSnapshotV2(
                task_id=task_id, request_id=request_id, status="queued" if status == "PENDING" else "running"
            )
        usage = payload.get("usage")
        if usage is not None:
            if not isinstance(usage, dict):
                raise ValueError("Invalid usage")
            usage = ProviderUsage.model_validate(
                {
                    local: usage.get(remote)
                    for local, remote in {
                        "duration": "duration",
                        "inputVideoDuration": "input_video_duration",
                        "outputVideoDuration": "output_video_duration",
                        "videoCount": "video_count",
                        "resolution": "SR",
                        "aspectRatio": "ratio",
                    }.items()
                }
            )
        video_url = output.get("video_url")
        expires_at = None
        if isinstance(video_url, str):
            expires = parse_qs(urlsplit(video_url).query).get("Expires", [])
            if len(expires) == 1 and re.fullmatch(r"[0-9]{1,12}", expires[0]):
                try:
                    expires_at = datetime.fromtimestamp(int(expires[0]), UTC).isoformat()
                except (ValueError, OverflowError, OSError):
                    pass
        provider_output = ProviderOutput(
            task_id=task_id,
            received_at=self.clock().astimezone(UTC).isoformat(),
            video_url=video_url,
            url_expires_at=expires_at,
            expiry_source="signature" if expires_at else "unknown",
            provider_times=ProviderTimes(
                submit_time=output.get("submit_time"),
                scheduled_time=output.get("scheduled_time"),
                end_time=output.get("end_time"),
            ),
            usage=usage,
        )
        return ProviderTaskSnapshotV2(
            task_id=task_id, request_id=request_id, status="succeeded", output=provider_output
        )

    async def submit(self, request: VideoRequestV2, operation_key: str) -> ProviderTaskHandleV2:
        request = VideoRequestV2.model_validate(request)
        self.check_configuration(request)
        status, payload, _ = await self._request(
            "POST",
            SUBMIT_PATH,
            stage="submit",
            body={
                "model": request.model,
                "input": {"prompt": request.provider_prompt},
                "parameters": request.parameters.model_dump(mode="json", by_alias=True),
            },
        )
        output = payload.get("output")
        task_id = safe_identifier(output.get("task_id")) if isinstance(output, dict) else None
        if 200 <= status < 300 and "code" not in payload and task_id:
            # Persistable acceptance survives absent/invalid optional snapshot fields.
            try:
                snapshot = self._snapshot(payload, task_id)
            except (ValueError, ProviderCallError):
                snapshot = None
            return ProviderTaskHandleV2(
                task_id=task_id, request_id=safe_identifier(payload.get("request_id")), snapshot=snapshot
            )
        code = _REJECTIONS.get((status, safe_provider_code(payload.get("code"))))
        if code and not _has_acceptance(payload):
            raise ProviderCallError(
                self._error("submit", code, status=status, payload=payload), submission_outcome="not_accepted"
            )
        raise self._call_error("submit", "SUBMISSION_UNKNOWN", status=status, payload=payload)

    async def query(self, task_id: str) -> ProviderTaskSnapshotV2:
        if safe_identifier(task_id) is None:
            raise self._call_error("query", "QUERY_PROTOCOL_ERROR")
        status, payload, retry_after_at = await self._request(
            "GET", f"/api/v1/tasks/{quote(task_id, safe='')}", stage="query"
        )
        if 200 <= status < 300 and "code" not in payload:
            try:
                return self._snapshot(payload, task_id)
            except ValueError:
                raise self._call_error(
                    "query", "QUERY_PROTOCOL_ERROR", status=status, payload=payload
                ) from None
        code = _REJECTIONS.get((status, safe_provider_code(payload.get("code"))))
        if status in {401, 403} or (code and code != "PROVIDER_RATE_LIMITED"):
            code = code or ("PROVIDER_AUTH_FAILED" if status == 401 else "PROVIDER_ACCESS_DENIED")
            raise ProviderCallError(
                self._error("query", code, status=status, payload=payload), pause_reason="configuration"
            )
        code = (
            "PROVIDER_RATE_LIMITED"
            if status == 429
            else "QUERY_HTTP_ERROR"
            if 500 <= status < 600
            else "QUERY_PROTOCOL_ERROR"
        )
        raise ProviderCallError(
            self._error("query", code, status=status, payload=payload), retry_after_at=retry_after_at
        )
