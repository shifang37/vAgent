"""Shared controlled-HTTP protocol pieces for the M3 E2 providers.

Everything here is deliberately narrow: one JSON object per response with unique
keys and finite numbers, a bounded body, no redirects, no environment proxy and no
implicit retries. Submission errors must state whether the request could have been
accepted; ambiguous outcomes are never retried automatically.
"""

import asyncio
import json
import re
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from email.utils import parsedate_to_datetime
from typing import Literal

import httpx

from vagent.errors import AppError
from vagent.video.contracts import JobErrorV2, ProviderCallError

MAX_RESPONSE_BYTES = 1024 * 1024
_IDENTIFIER = re.compile(r"[A-Za-z0-9_-]{1,256}\Z", re.ASCII)
_CODE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}\Z", re.ASCII)

PROVIDER_MESSAGES = {
    "PROVIDER_INVALID_REQUEST": "上游明确拒绝了请求参数，未创建替代任务。",
    "PROVIDER_CONTENT_REJECTED": "上游以内容政策拒绝了请求，不会自动改写提示词重提。",
    "PROVIDER_AUTH_FAILED": "视频凭证无效或已过期，请检查对应供应商 Key。",
    "PROVIDER_ACCESS_DENIED": "供应商拒绝访问，请检查模型授权、账号地域或 Key 权限。",
    "PROVIDER_MODEL_UNAVAILABLE": "上游模型或端点不可用，请核对已冻结的配置。",
    "PROVIDER_BILLING_BLOCKED": "供应商因余额、资源包或账单上限拒绝请求，请检查账号费用。",
    "PROVIDER_RATE_LIMITED": "供应商限流或并发受限，保留原任务并沿用已保存的策略。",
    "SUBMISSION_UNKNOWN": "提交结果不确定，未取得已确认的上游 ID；不会自动重新提交。",
    "QUERY_PROTOCOL_ERROR": "上游返回了无效查询结果，保留原任务与生成状态。",
    "QUERY_NETWORK": "原任务查询连接中断，按已保存的策略重试。",
    "QUERY_TIMEOUT": "原任务查询超时，按已保存的策略重试。",
    "QUERY_HTTP_ERROR": "原任务暂时无法查询，按已保存的策略重试。",
    "PROVIDER_TASK_UNAVAILABLE": "原任务不可查询或已过保留期限，查询已暂停。",
    "PROVIDER_GENERATION_FAILED": "上游已确认生成失败，不会自动修改内容或重新提交。",
    "PROVIDER_CANCELED": "上游已确认任务取消，不会创建替代任务。",
}

QueryPause = Literal["configuration", "task_unavailable"]


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


def load_json_object(content: bytes) -> dict:
    payload = json.loads(content, object_pairs_hook=_unique_object, parse_constant=_invalid_number)
    if not isinstance(payload, dict):
        raise ValueError("Provider response must be an object")
    return payload


class ProviderHttpAdapter:
    """One frozen provider host with bearer/key auth and bounded JSON responses."""

    provider: str = ""
    base_url: str = ""

    def __init__(
        self,
        *,
        api_key: str | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ):
        self._key = api_key
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

    def update_credentials(self, *, api_key: str | None) -> None:
        """Saved settings reach new registrations without a restart."""
        self._key = api_key

    def require_key(self) -> str:
        if not self._key:
            raise AppError("VIDEO_KEY_MISSING", f"请配置 {self.provider} 视频服务 Key。")
        return self._key

    def _headers(self) -> dict:
        return {"Accept": "application/json"}

    def _error(self, stage, code, *, status=None, payload=None, request_id=None):
        payload = payload if isinstance(payload, dict) else {}
        return JobErrorV2(
            stage=stage,
            code=code,
            message=PROVIDER_MESSAGES[code],
            http_status=status,
            provider_code=safe_provider_code(payload.get("code")),
            request_id=safe_identifier(request_id),
        )

    def _call_error(self, stage, code, **kwargs):
        return ProviderCallError(
            self._error(stage, code, **kwargs),
            submission_outcome="unknown" if stage == "submit" else None,
        )

    async def _request(self, method, path, *, stage, body=None):
        """Run one request and return (status, payload, retry_after_at)."""
        key = self.require_key()
        headers = {**self._headers(), "Authorization": self._authorization(key)}
        if body is not None:
            headers["Content-Type"] = "application/json"
        url = f"{self.base_url}{path}"
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
                    payload = load_json_object(bytes(content))
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

    @staticmethod
    def _authorization(key: str) -> str:
        return f"Bearer {key}"
