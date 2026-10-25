"""M6 E2 TTS providers: the frozen ``tts-providers-v1`` table and two adapters.

``local-tts-v1`` is a deterministic offline fixture: it never opens a socket and
turns the exact text into a mono 48 kHz PCM WAV so the whole mixing/render suite
can run without credentials. ``cosyvoice-v2`` follows the official DashScope
speech-synthesis HTTP protocol (shared host or workspace host); its capabilities
stay marked "documented, no real sample" until an explicitly authorized run.

Official protocol (verified 2026-10-22):
https://help.aliyun.com/zh/model-studio/cosyvoice-tts-http-api
POST {base}/api/v1/services/audio/tts/SpeechSynthesizer
{"model": "cosyvoice-v2", "input": {"text", "voice", "format": "wav",
 "sample_rate": 48000, "rate"}}
-> {"request_id", "output": {"finish_reason", "audio": {"url", "id",
    "expires_at"}}, "usage": {"characters"}}
"""

import io
import math
import struct
import wave
from dataclasses import dataclass
from decimal import Decimal
from typing import Annotated, Literal
from urllib.parse import urlsplit

import httpx
from pydantic import Field, model_validator

from vagent.contracts import Contract, Fingerprint, Identifier, UtcTimestamp
from vagent.errors import AppError
from vagent.video.audio import (
    NARRATOR_SPEAKER,
    TTS_EPISODE_CHARACTER_LIMIT,
    TTS_EPISODE_COST_LIMIT,
    TTS_EPISODE_REQUEST_LIMIT,
    TTS_PROVIDERS_VERSION,
    TTS_REQUEST_CHARACTER_LIMIT,
)

LOCAL_PROVIDER = "local"
LOCAL_MODEL = "local-tts-v1"
COSYVOICE_PROVIDER = "cosyvoice"
COSYVOICE_MODEL = "cosyvoice-v2"
COSYVOICE_PRICE_PER_THOUSAND = Decimal("0.20")
COSYVOICE_PRICE_VERSION = "cosyvoice-v2-beijing-20261022"
COSYVOICE_PRICE_SOURCE = "https://help.aliyun.com/zh/model-studio/cosyvoice-v2"
COSYVOICE_RPM = 180
COSYVOICE_SAMPLE_RATE = 48000
COSYVOICE_SUBMIT_PATH = "/api/v1/services/audio/tts/SpeechSynthesizer"
SHARED_HOST = "dashscope.aliyuncs.com"
WORKSPACE_SUFFIX = ".cn-beijing.maas.aliyuncs.com"
LOCAL_SAMPLE_RATE = 48000
LOCAL_CHANNELS = 1
MAX_SUBMIT_BYTES = 1024 * 1024
MAX_AUDIO_BYTES = 25 * 1024 * 1024
TTS_TIMEOUT_SECONDS = 60.0
WAV_MIME_TYPES = {"audio/wav", "audio/x-wav", "audio/wave", "application/octet-stream"}
# Streaming WAV placeholders: a real CosyVoice object declared 0x7FFFFFxx sizes.
STREAMING_SIZE_SENTINEL = 0x7F000000

MESSAGES = {
    "TTS_PROVIDER_UNKNOWN": "供应商、模型或音色未在 tts-providers-v1 中核实。",
    "TTS_KEY_MISSING": "未配置可用的 TTS Key；CosyVoice 复用视频 Key。",
    "TTS_TEXT_LIMIT": "单条台词超过 500 字符或本集累计超过 2000 字符。",
    "TTS_BUDGET_LIMIT": "本集 TTS 请求次数或金额额度不足。",
    "TTS_EMPTY_EPISODE": "该分集没有可配音的台词。",
    "AUDIO_FORMAT_UNSUPPORTED": "TTS 音频不是受支持的 48 kHz PCM WAV。",
    "PROVIDER_AUTH_FAILED": "TTS 凭证无效，请检查视频 Key。",
    "PROVIDER_ACCESS_DENIED": "TTS 服务拒绝访问，请检查模型授权。",
    "PROVIDER_BILLING_BLOCKED": "TTS 服务因余额或账单上限拒绝请求。",
    "PROVIDER_INVALID_REQUEST": "TTS 服务明确拒绝了请求参数。",
    "PROVIDER_MODEL_UNAVAILABLE": "TTS 模型或业务空间不可用。",
    "PROVIDER_RATE_LIMITED": "TTS 服务限流，请稍后按已保存策略重试。",
    "PROVIDER_CONTENT_REJECTED": "TTS 内容审核拒绝了该台词。",
    "PROVIDER_UPSTREAM_ERROR": "TTS 服务暂时不可用，请保留原因后重试。",
    "PROVIDER_NETWORK": "TTS 网络连接中断，未产生可交付资产。",
    "PROVIDER_RESULT_UNTRUSTED": "TTS 返回的音频地址不在受信任域名内。",
}


class TtsError(AppError):
    def __init__(self, code: str, detail: str = "", *, stage: str = "provider"):
        message = MESSAGES[code]
        super().__init__(code, f"{message}{detail}" if detail else message)
        self.stage = stage


def local_voices() -> tuple[dict, ...]:
    return (
        {
            "voiceId": "local-narrator",
            "name": "本地旁白",
            "language": "zh",
            "description": "离线确定性旁白音色，仅用于验收与测试。",
        },
        {
            "voiceId": "local-female",
            "name": "本地女声",
            "language": "zh",
            "description": "离线确定性女声音色，仅用于验收与测试。",
        },
        {
            "voiceId": "local-male",
            "name": "本地男声",
            "language": "zh",
            "description": "离线确定性男声音色，仅用于验收与测试。",
        },
    )


def cosyvoice_voices() -> tuple[dict, ...]:
    """The verified Beijing subset from the official CosyVoice v2 voice list."""
    return (
        {
            "voiceId": "longxiaochun_v2",
            "name": "龙小淳",
            "language": "zh",
            "description": "知性积极女",
        },
        {
            "voiceId": "longxiaoxia_v2",
            "name": "龙小夏",
            "language": "zh",
            "description": "沉稳权威女",
        },
        {
            "voiceId": "longyichen",
            "name": "龙逸尘",
            "language": "zh",
            "description": "洒脱活力男",
        },
        {
            "voiceId": "longwanjun",
            "name": "龙婉君",
            "language": "zh",
            "description": "细腻柔声女",
        },
        {
            "voiceId": "longlaobo",
            "name": "龙老伯",
            "language": "zh",
            "description": "沧桑岁月爷",
        },
        {
            "voiceId": "longlaoyi",
            "name": "龙老姨",
            "language": "zh",
            "description": "烟火从容阿姨",
        },
        {
            "voiceId": "longsanshu",
            "name": "龙三叔",
            "language": "zh",
            "description": "沉稳质感男",
        },
        {
            "voiceId": "longxiaocheng_v2",
            "name": "龙小诚",
            "language": "zh",
            "description": "磁性低音男",
        },
    )


def tts_capabilities(mode: str) -> list[dict]:
    """Per-mode provider table; the live table is documentation-verified, not sampled."""
    if mode == "live":
        return [
            {
                "provider": COSYVOICE_PROVIDER,
                "model": COSYVOICE_MODEL,
                "mode": "live",
                "capabilitiesVersion": TTS_PROVIDERS_VERSION,
                "priceKnown": True,
                "currency": "CNY",
                "pricePerThousandCharacters": "0.20",
                "priceVersion": COSYVOICE_PRICE_VERSION,
                "priceSourceUrl": COSYVOICE_PRICE_SOURCE,
                "rpm": COSYVOICE_RPM,
                "sampleRate": COSYVOICE_SAMPLE_RATE,
                "channels": 1,
                "realSample": False,
                "verification": "documented-no-real-sample",
                "endpoint": f"https://{{WorkspaceId}}{WORKSPACE_SUFFIX}{COSYVOICE_SUBMIT_PATH}",
                "voices": list(cosyvoice_voices()),
            }
        ]
    return [
        {
            "provider": LOCAL_PROVIDER,
            "model": LOCAL_MODEL,
            "mode": "mock",
            "capabilitiesVersion": TTS_PROVIDERS_VERSION,
            "priceKnown": False,
            "currency": None,
            "pricePerThousandCharacters": None,
            "priceVersion": None,
            "priceSourceUrl": None,
            "rpm": None,
            "sampleRate": LOCAL_SAMPLE_RATE,
            "channels": LOCAL_CHANNELS,
            "realSample": False,
            "verification": "offline-fixture",
            "endpoint": None,
            "voices": list(local_voices()),
        }
    ]


def capability_for(mode: str, voice_id: str) -> tuple[dict, dict]:
    for table in tts_capabilities(mode):
        for voice in table["voices"]:
            if voice["voiceId"] == voice_id:
                return table, voice
    raise TtsError("TTS_PROVIDER_UNKNOWN", f"（{voice_id}）")


def known_voice(mode: str, voice_id: str) -> bool:
    try:
        capability_for(mode, voice_id)
    except AppError:
        return False
    return True


# -- deterministic local fixture ------------------------------------------------


def local_duration_seconds(text: str) -> int:
    """Contract formula: clamp(ceil(characters * 0.18), 1, 8) whole seconds."""
    return min(8, max(1, math.ceil(len(text) * 0.18)))


def local_frequency(voice_id: str) -> float:
    return {
        "local-narrator": 220.0,
        "local-female": 330.0,
        "local-male": 165.0,
    }.get(voice_id, 220.0)


def synthesize_local_wav(text: str, voice_id: str) -> bytes:
    """A deterministic mono 48 kHz PCM WAV: one tone segment then silence.

    The same text and voice always produce the same bytes; there is no clock,
    random source or filesystem input. A 10 ms fade avoids a click at the edges.
    """
    seconds = local_duration_seconds(text)
    rate = LOCAL_SAMPLE_RATE
    samples = seconds * rate
    tone_samples = int(samples * 0.75)
    frequency = local_frequency(voice_id)
    amplitude = 0.35
    fade = max(1, int(rate * 0.01))
    frames = bytearray()
    for index in range(samples):
        if index >= tone_samples:
            value = 0.0
        else:
            envelope = 1.0
            if index < fade:
                envelope = index / fade
            elif index > tone_samples - fade:
                envelope = max(0.0, (tone_samples - index) / fade)
            value = amplitude * envelope * math.sin(2 * math.pi * frequency * index / rate)
        frames.extend(struct.pack("<h", int(round(value * 32767))))
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as handle:
        handle.setnchannels(LOCAL_CHANNELS)
        handle.setsampwidth(2)
        handle.setframerate(rate)
        handle.writeframes(bytes(frames))
    return buffer.getvalue()


@dataclass(frozen=True)
class WavFacts:
    duration_seconds: float
    sample_rate: int
    channels: int
    bits_per_sample: int
    size_bytes: int


def normalize_streaming_wav(data: bytes) -> bytes:
    """Rewrite the unknown-length sentinels of a streamed WAV to its real sizes.

    DashScope hands out the object before its length is known, so a real sample
    returned ``RIFF``/``data`` sizes of ``0x7FFFFFxx`` (2,147,483,583 and
    2,147,483,547 in the 2026-10-25 run). The downloaded object is complete; the
    two sizes are rewritten and every other header byte is preserved, so the
    stored fixture is a canonical PCM WAV. A payload whose sizes are merely too
    large for the file (a truncated download) is left untouched and keeps failing
    ``inspect_wav``.
    """
    if len(data) < 44 or data[:4] != b"RIFF" or data[8:12] != b"WAVE":
        return data
    riff_size = int.from_bytes(data[4:8], "little")
    offset = 12
    while offset + 8 <= len(data):
        chunk_id = data[offset : offset + 4]
        size = int.from_bytes(data[offset + 4 : offset + 8], "little")
        body = offset + 8
        if chunk_id == b"data":
            if riff_size < STREAMING_SIZE_SENTINEL or size < STREAMING_SIZE_SENTINEL:
                return data
            buffer = bytearray(data)
            buffer[4:8] = (len(data) - 8).to_bytes(4, "little")
            buffer[offset + 4 : offset + 8] = (len(data) - body).to_bytes(4, "little")
            return bytes(buffer)
        if body + size > len(data):
            return data
        offset = body + size + (size % 2)
    return data


def inspect_wav(data: bytes) -> WavFacts:
    """Parse a canonical PCM RIFF/WAVE payload; reject anything the mixer cannot use."""
    if len(data) < 44 or data[:4] != b"RIFF" or data[8:12] != b"WAVE":
        raise TtsError("AUDIO_FORMAT_UNSUPPORTED", "（不是 RIFF/WAVE）", stage="validate")
    offset = 12
    format_tag = channels = sample_rate = bits = None
    data_size = None
    while offset + 8 <= len(data):
        chunk_id = data[offset : offset + 4]
        chunk_size = int.from_bytes(data[offset + 4 : offset + 8], "little")
        body = offset + 8
        if body + chunk_size > len(data):
            raise TtsError("AUDIO_FORMAT_UNSUPPORTED", "（块长度越界）", stage="validate")
        if chunk_id == b"fmt ":
            if chunk_size < 16:
                raise TtsError("AUDIO_FORMAT_UNSUPPORTED", "（fmt 块过短）", stage="validate")
            format_tag = int.from_bytes(data[body : body + 2], "little")
            channels = int.from_bytes(data[body + 2 : body + 4], "little")
            sample_rate = int.from_bytes(data[body + 4 : body + 8], "little")
            bits = int.from_bytes(data[body + 14 : body + 16], "little")
        elif chunk_id == b"data":
            data_size = chunk_size
            if format_tag is not None and data_size is not None:
                break
        offset = body + chunk_size + (chunk_size % 2)
    if not all((format_tag, channels, sample_rate, bits, data_size is not None)):
        raise TtsError("AUDIO_FORMAT_UNSUPPORTED", "（缺少必要的 WAVE 块）", stage="validate")
    if format_tag not in {1, 0xFFFE} or bits != 16:
        raise TtsError("AUDIO_FORMAT_UNSUPPORTED", "（只接受 16 位 PCM）", stage="validate")
    if sample_rate <= 0 or channels <= 0:
        raise TtsError("AUDIO_FORMAT_UNSUPPORTED", "（采样率或声道非法）", stage="validate")
    frame_bytes = channels * bits // 8
    if data_size % frame_bytes:
        raise TtsError("AUDIO_FORMAT_UNSUPPORTED", "（数据长度不是完整帧）", stage="validate")
    duration = data_size / (sample_rate * frame_bytes)
    if duration <= 0:
        raise TtsError("AUDIO_FORMAT_UNSUPPORTED", "（音频时长为零）", stage="validate")
    return WavFacts(
        duration_seconds=round(duration, 6),
        sample_rate=sample_rate,
        channels=channels,
        bits_per_sample=bits,
        size_bytes=len(data),
    )


# -- contracts -----------------------------------------------------------------


class TtsRequestV1(Contract):
    """One dialogue line, never a video Job; provider calls stay outside the Store lock."""

    contract_version: Literal[1] = 1
    provider: Literal["local", "cosyvoice"]
    model: str = Field(min_length=1, max_length=80)
    voice_id: str = Field(min_length=1, max_length=80)
    text: str = Field(min_length=1, max_length=TTS_REQUEST_CHARACTER_LIMIT)
    speed: float = Field(default=1.0, ge=0.5, le=2.0, allow_inf_nan=False)
    sample_rate: int = Field(default=LOCAL_SAMPLE_RATE, ge=8000, le=48000)
    format: Literal["wav"] = "wav"
    episode_id: Identifier | None = None
    speaker: str = Field(default=NARRATOR_SPEAKER, min_length=1, max_length=40)
    character_id: Identifier | None = None


class TtsResultV1(Contract):
    contract_version: Literal[1] = 1
    provider: str
    model: str
    voice_id: str
    characters: int = Field(ge=1)
    duration_seconds: float = Field(gt=0, allow_inf_nan=False)
    sample_rate: int = Field(gt=0)
    channels: int = Field(gt=0)
    sha256: Fingerprint
    size_bytes: int = Field(gt=0)
    audio: bytes = Field(exclude=True)
    request_id: str | None = None
    provider_url_expires_at: int | None = None


# -- CosyVoice adapter ----------------------------------------------------------

_STATUS_CODES = {
    401: "PROVIDER_AUTH_FAILED",
    403: "PROVIDER_ACCESS_DENIED",
    404: "PROVIDER_MODEL_UNAVAILABLE",
    429: "PROVIDER_RATE_LIMITED",
}
_ERROR_CODES = {
    "InvalidApiKey": "PROVIDER_AUTH_FAILED",
    "invalid_api_key": "PROVIDER_AUTH_FAILED",
    "AccessDenied": "PROVIDER_ACCESS_DENIED",
    "ModelNotFound": "PROVIDER_MODEL_UNAVAILABLE",
    "Arrearage": "PROVIDER_BILLING_BLOCKED",
    "DataInspectionFailed": "PROVIDER_CONTENT_REJECTED",
    "Throttling": "PROVIDER_RATE_LIMITED",
    "Throttling.RateQuota": "PROVIDER_RATE_LIMITED",
    "InvalidParameter": "PROVIDER_INVALID_REQUEST",
    "CLIENT_ERROR": "PROVIDER_INVALID_REQUEST",
}


def _trusted_result_url(url: str) -> bool:
    parts = urlsplit(url)
    # The official response example uses an http OSS URL; the download carries no
    # credentials and the bytes are re-validated, so both schemes are accepted.
    if parts.scheme not in {"http", "https"} or not parts.hostname or parts.username or parts.password:
        return False
    host = parts.hostname.lower()
    return host == "aliyuncs.com" or host.endswith(".aliyuncs.com")


class CosyVoiceAdapter:
    """Controlled DashScope client: one submit, one bounded download, no redirects."""

    def __init__(
        self,
        *,
        api_key: str,
        workspace_id: str | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
        timeout: float = TTS_TIMEOUT_SECONDS,
    ):
        if not api_key:
            raise TtsError("TTS_KEY_MISSING")
        self.api_key = api_key
        self.workspace_id = workspace_id
        self.transport = transport
        self.timeout = timeout

    @property
    def model(self) -> str:
        return COSYVOICE_MODEL

    @property
    def provider(self) -> str:
        return COSYVOICE_PROVIDER

    def endpoint(self) -> str:
        if self.workspace_id:
            host = f"{self.workspace_id}{WORKSPACE_SUFFIX}"
        else:
            host = SHARED_HOST
        return f"https://{host}{COSYVOICE_SUBMIT_PATH}"

    async def synthesize(self, request: TtsRequestV1) -> TtsResultV1:
        payload = {
            "model": COSYVOICE_MODEL,
            "input": {
                "text": request.text,
                "voice": request.voice_id,
                "format": "wav",
                "sample_rate": COSYVOICE_SAMPLE_RATE,
                "rate": request.speed,
                "volume": 50,
            },
        }
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
        timeout = httpx.Timeout(self.timeout, connect=min(10.0, self.timeout))
        try:
            async with httpx.AsyncClient(
                transport=self.transport,
                timeout=timeout,
                follow_redirects=False,
                trust_env=False,
            ) as client:
                response = await client.post(self.endpoint(), headers=headers, json=payload)
                body = self._json_or_error(response)
                audio_url = self._audio_url(body)
                if not _trusted_result_url(audio_url):
                    raise TtsError("PROVIDER_RESULT_UNTRUSTED")
                chunks: list[bytes] = []
                total = 0
                async with client.stream(
                    "GET", audio_url, headers={"Accept": "audio/wav"}, follow_redirects=False
                ) as download:
                    if download.status_code != 200:
                        raise TtsError("PROVIDER_UPSTREAM_ERROR", f"（下载 {download.status_code}）")
                    async for chunk in download.aiter_bytes():
                        total += len(chunk)
                        if total > MAX_AUDIO_BYTES:
                            raise TtsError("PROVIDER_RESULT_UNTRUSTED", "（音频超过 25 MB）")
                        chunks.append(chunk)
        except TtsError:
            raise
        except httpx.TimeoutException:
            raise TtsError("PROVIDER_NETWORK", "（超时）") from None
        except httpx.HTTPError:
            raise TtsError("PROVIDER_NETWORK", "（连接失败）") from None
        data = normalize_streaming_wav(b"".join(chunks))
        facts = inspect_wav(data)
        characters = body.get("usage", {}).get("characters")
        if not isinstance(characters, int) or characters < 1:
            characters = len(request.text)
        audio = body.get("output", {}).get("audio", {})
        return TtsResultV1(
            provider=COSYVOICE_PROVIDER,
            model=COSYVOICE_MODEL,
            voice_id=request.voice_id,
            characters=characters,
            duration_seconds=facts.duration_seconds,
            sample_rate=facts.sample_rate,
            channels=facts.channels,
            sha256=_sha256(data),
            size_bytes=len(data),
            audio=data,
            request_id=body.get("request_id"),
            provider_url_expires_at=audio.get("expires_at")
            if isinstance(audio.get("expires_at"), int)
            else None,
        )

    @staticmethod
    def _json_or_error(response: httpx.Response) -> dict:
        if len(response.content) > MAX_SUBMIT_BYTES:
            raise TtsError("PROVIDER_RESULT_UNTRUSTED", "（响应超过 1 MB）")
        try:
            body = response.json()
        except ValueError:
            body = None
        if response.status_code != 200 or not isinstance(body, dict):
            code = body.get("code") if isinstance(body, dict) else None
            mapped = _ERROR_CODES.get(code) or _STATUS_CODES.get(response.status_code)
            if mapped is None:
                mapped = (
                    "PROVIDER_UPSTREAM_ERROR" if response.status_code >= 500 else "PROVIDER_INVALID_REQUEST"
                )
            raise TtsError(mapped)
        return body

    @staticmethod
    def _audio_url(body: dict) -> str:
        output = body.get("output")
        audio = output.get("audio") if isinstance(output, dict) else None
        url = audio.get("url") if isinstance(audio, dict) else None
        if not isinstance(url, str) or not url:
            raise TtsError("PROVIDER_RESULT_UNTRUSTED", "（缺少音频地址）")
        return url


def _sha256(data: bytes) -> str:
    import hashlib

    return hashlib.sha256(data).hexdigest()


# -- local adapter --------------------------------------------------------------


class LocalTtsAdapter:
    """Deterministic fixture adapter; it never performs I/O outside the process."""

    provider = LOCAL_PROVIDER
    model = LOCAL_MODEL

    def __init__(self, *, transport: None = None):
        self.transport = transport

    async def synthesize(self, request: TtsRequestV1) -> TtsResultV1:
        data = synthesize_local_wav(request.text, request.voice_id)
        facts = inspect_wav(data)
        return TtsResultV1(
            provider=LOCAL_PROVIDER,
            model=LOCAL_MODEL,
            voice_id=request.voice_id,
            characters=len(request.text),
            duration_seconds=facts.duration_seconds,
            sample_rate=facts.sample_rate,
            channels=facts.channels,
            sha256=_sha256(data),
            size_bytes=len(data),
            audio=data,
        )


def local_capability() -> dict:
    return tts_capabilities("mock")[0]


def cosyvoice_capability() -> dict:
    return tts_capabilities("live")[0]


def limits_snapshot(*, characters: int, requests: int, cost: Decimal | None) -> dict:
    return {
        "characters": characters,
        "charactersLimit": TTS_EPISODE_CHARACTER_LIMIT,
        "requests": requests,
        "requestsLimit": TTS_EPISODE_REQUEST_LIMIT,
        "estimatedCostCny": None if cost is None else format(cost, ".4f"),
        "costLimitCny": None if cost is None else format(TTS_EPISODE_COST_LIMIT, ".4f"),
        "currency": None if cost is None else "CNY",
    }


class VoiceBinding(Contract):
    """Frozen per-episode voice choice; appended by ``audio_generate`` only."""

    speaker: str = Field(min_length=1, max_length=40)
    character_id: Identifier | None = None
    provider: str = Field(min_length=1, max_length=40)
    model: str = Field(min_length=1, max_length=80)
    voice_id: str = Field(min_length=1, max_length=80)
    speed: float = Field(default=1.0, ge=0.5, le=2.0, allow_inf_nan=False)
    created_at: UtcTimestamp

    @model_validator(mode="after")
    def speaker_is_not_blank(self):
        if not self.speaker.strip():
            raise ValueError("A voice binding needs a speaker")
        return self


TtsSpeaker = Annotated[str, Field(min_length=1, max_length=40)]
