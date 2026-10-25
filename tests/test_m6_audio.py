"""M6 E2 TTS contracts: the frozen provider table, local fixture and CosyVoice adapter."""

import hashlib
import json
import wave
from decimal import Decimal
from io import BytesIO

import httpx
import pytest

from vagent.video.audio import (
    TTS_EPISODE_CHARACTER_LIMIT,
    TTS_EPISODE_COST_LIMIT,
    TTS_EPISODE_REQUEST_LIMIT,
    TTS_PRICE_PER_THOUSAND_CHARACTERS,
    TTS_REQUEST_CHARACTER_LIMIT,
    TTS_REQUEST_COST_LIMIT,
    tts_cost,
)
from vagent.video.tts import (
    COSYVOICE_MODEL,
    COSYVOICE_PROVIDER,
    CosyVoiceAdapter,
    LocalTtsAdapter,
    TtsError,
    TtsRequestV1,
    capability_for,
    inspect_wav,
    local_duration_seconds,
    normalize_streaming_wav,
    synthesize_local_wav,
    tts_capabilities,
)


def test_frozen_tts_limits_and_price_are_the_contract():
    assert TTS_EPISODE_CHARACTER_LIMIT == 2000
    assert TTS_REQUEST_CHARACTER_LIMIT == 500
    assert TTS_EPISODE_REQUEST_LIMIT == 40
    assert TTS_PRICE_PER_THOUSAND_CHARACTERS == Decimal("0.20")
    assert TTS_REQUEST_COST_LIMIT == Decimal("0.10")
    assert TTS_EPISODE_COST_LIMIT == Decimal("0.40")
    assert tts_cost(0, TTS_PRICE_PER_THOUSAND_CHARACTERS) == Decimal("0.0000")
    assert tts_cost(500, TTS_PRICE_PER_THOUSAND_CHARACTERS) == Decimal("0.1000")
    assert tts_cost(2000, TTS_PRICE_PER_THOUSAND_CHARACTERS) == Decimal("0.4000")


def test_capability_tables_are_mode_specific_and_mark_the_cosyvoice_sample_status():
    live = tts_capabilities("live")
    mock = tts_capabilities("mock")
    assert len(live) == len(mock) == 1
    cosy = live[0]
    assert (cosy["provider"], cosy["model"]) == (COSYVOICE_PROVIDER, COSYVOICE_MODEL)
    assert cosy["priceKnown"] is True
    assert cosy["currency"] == "CNY"
    assert cosy["pricePerThousandCharacters"] == "0.20"
    assert cosy["rpm"] == 180
    assert cosy["realSample"] is False
    assert cosy["verification"] == "documented-no-real-sample"
    assert (
        "cosyvoice-tts-http-api" in tts_capabilities("live")[0]["endpoint"]
        or "WorkspaceId" in cosy["endpoint"]
    )
    voice_ids = {voice["voiceId"] for voice in cosy["voices"]}
    assert {"longxiaochun_v2", "longxiaoxia_v2", "longyichen", "longwanjun"} <= voice_ids
    local = mock[0]
    assert (local["provider"], local["model"]) == ("local", "local-tts-v1")
    assert local["priceKnown"] is False and local["pricePerThousandCharacters"] is None
    assert {voice["voiceId"] for voice in local["voices"]} == {
        "local-narrator",
        "local-female",
        "local-male",
    }
    assert capability_for("live", "longxiaochun_v2")[0]["provider"] == "cosyvoice"
    with pytest.raises(TtsError) as error:
        capability_for("mock", "longxiaochun_v2")
    assert error.value.code == "TTS_PROVIDER_UNKNOWN"


def test_local_tts_is_deterministic_and_matches_the_frozen_duration_formula():
    assert local_duration_seconds("一") == 1
    assert local_duration_seconds("一二三四五") == 1
    assert local_duration_seconds("一" * 10) == 2
    assert local_duration_seconds("一" * 44) == 8
    assert local_duration_seconds("一" * 500) == 8
    first = synthesize_local_wav("雨夜咖啡店，推门而入。", "local-narrator")
    second = synthesize_local_wav("雨夜咖啡店，推门而入。", "local-narrator")
    assert first == second
    facts = inspect_wav(first)
    assert (facts.sample_rate, facts.channels, facts.bits_per_sample) == (48000, 1, 16)
    assert facts.duration_seconds == 2.0
    with wave.open(BytesIO(first), "rb") as handle:
        frames = handle.readframes(handle.getnframes())
    amplitude = max(
        abs(int.from_bytes(frames[index : index + 2], "little", signed=True))
        for index in range(0, len(frames), 2)
    )
    assert 0 < amplitude < 32767 * 0.99


def test_inspect_wav_rejects_non_pcm_and_truncated_payloads():
    with pytest.raises(TtsError) as error:
        inspect_wav(b"RIFF" + b"\0" * 64)
    assert error.value.code == "AUDIO_FORMAT_UNSUPPORTED"
    buffer = BytesIO()
    with wave.open(buffer, "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(1)
        handle.setframerate(8000)
        handle.writeframes(b"\0" * 800)
    with pytest.raises(TtsError):
        inspect_wav(buffer.getvalue())
    with pytest.raises(TtsError):
        inspect_wav(b"not a wave file at all")


def _cosy_handler(audio: bytes, *, characters: int = 12):
    calls = []

    def handler(request: httpx.Request):
        calls.append(request)
        if request.url.path.endswith("/SpeechSynthesizer"):
            assert request.headers["authorization"] == "Bearer sk-test"
            body = json.loads(request.content)
            assert body["model"] == COSYVOICE_MODEL
            assert body["input"]["format"] == "wav"
            assert body["input"]["sample_rate"] == 48000
            assert body["input"]["voice"] == "longxiaochun_v2"
            return httpx.Response(
                200,
                json={
                    "request_id": "req-1",
                    "output": {
                        "finish_reason": "stop",
                        "audio": {
                            "url": "https://dashscope-result-bj.oss-cn-beijing.aliyuncs.com/a.wav",
                            "id": "audio-1",
                            "expires_at": 1772697707,
                        },
                    },
                    "usage": {"characters": characters},
                },
            )
        assert str(request.url).startswith("https://")
        assert request.headers.get("authorization") is None
        return httpx.Response(200, content=audio, headers={"content-type": "audio/wav"})

    return handler, calls


async def test_cosyvoice_adapter_uses_the_official_shape_and_downloads_the_audio():
    audio = synthesize_local_wav("你好，世界。", "local-female")
    handler, calls = _cosy_handler(audio, characters=6)
    adapter = CosyVoiceAdapter(api_key="sk-test", transport=httpx.MockTransport(handler))
    request = TtsRequestV1(
        provider="cosyvoice",
        model=COSYVOICE_MODEL,
        voice_id="longxiaochun_v2",
        text="你好，世界。",
    )
    result = await adapter.synthesize(request)
    assert result.provider == "cosyvoice"
    assert result.characters == 6
    assert result.request_id == "req-1"
    assert result.sha256 == hashlib.sha256(audio).hexdigest()
    assert result.duration_seconds == 2.0
    assert [call.method for call in calls] == ["POST", "GET"]
    assert adapter.endpoint() == "https://dashscope.aliyuncs.com/api/v1/services/audio/tts/SpeechSynthesizer"


def _streaming_wav(audio: bytes) -> bytes:
    """The 2026-10-25 real CosyVoice object: placeholder RIFF/data sizes."""
    buffer = bytearray(audio)
    buffer[4:8] = (2147483583).to_bytes(4, "little")
    data_at = audio.index(b"data")
    buffer[data_at + 4 : data_at + 8] = (2147483547).to_bytes(4, "little")
    return bytes(buffer)


def test_normalize_streaming_wav_rewrites_only_the_placeholder_sizes():
    audio = synthesize_local_wav("你好，世界。", "local-narrator")
    streamed = _streaming_wav(audio)
    with pytest.raises(TtsError) as error:
        inspect_wav(streamed)
    assert error.value.code == "AUDIO_FORMAT_UNSUPPORTED"
    assert normalize_streaming_wav(streamed) == audio
    assert normalize_streaming_wav(audio) == audio
    # A truncation keeps its real declared size and must stay rejected.
    assert normalize_streaming_wav(audio[:-1000]) == audio[:-1000]
    with pytest.raises(TtsError):
        inspect_wav(audio[:-1000])


async def test_cosyvoice_adapter_normalizes_the_streamed_wav_it_downloads():
    audio = synthesize_local_wav("你好，世界。", "local-narrator")
    streamed = _streaming_wav(audio)
    handler, _calls = _cosy_handler(streamed, characters=6)
    adapter = CosyVoiceAdapter(api_key="sk-test", transport=httpx.MockTransport(handler))
    request = TtsRequestV1(
        provider="cosyvoice",
        model=COSYVOICE_MODEL,
        voice_id="longxiaochun_v2",
        text="你好，世界。",
    )
    result = await adapter.synthesize(request)
    assert result.audio == audio
    assert result.sha256 == hashlib.sha256(audio).hexdigest()
    assert result.size_bytes == len(audio)
    assert result.duration_seconds == 2.0


def test_workspace_endpoint_uses_the_workspace_domain():
    adapter = CosyVoiceAdapter(api_key="sk-test", workspace_id="coffee-space", transport=None)
    assert adapter.endpoint() == (
        "https://coffee-space.cn-beijing.maas.aliyuncs.com/api/v1/services/audio/tts/SpeechSynthesizer"
    )


@pytest.mark.parametrize(
    ("status", "code", "expected"),
    [
        (401, "InvalidApiKey", "PROVIDER_AUTH_FAILED"),
        (403, "AccessDenied", "PROVIDER_ACCESS_DENIED"),
        (429, "Throttling.RateQuota", "PROVIDER_RATE_LIMITED"),
        (400, "DataInspectionFailed", "PROVIDER_CONTENT_REJECTED"),
        (400, "Arrearage", "PROVIDER_BILLING_BLOCKED"),
    ],
)
async def test_cosyvoice_adapter_maps_official_errors(status, code, expected):
    def handler(request: httpx.Request):
        return httpx.Response(status, json={"request_id": "r", "code": code, "message": "denied"})

    adapter = CosyVoiceAdapter(api_key="sk-test", transport=httpx.MockTransport(handler))
    request = TtsRequestV1(
        provider="cosyvoice",
        model=COSYVOICE_MODEL,
        voice_id="longxiaochun_v2",
        text="你好。",
    )
    with pytest.raises(TtsError) as error:
        await adapter.synthesize(request)
    assert error.value.code == expected


async def test_cosyvoice_adapter_rejects_an_untrusted_result_url():
    def handler(request: httpx.Request):
        return httpx.Response(
            200,
            json={
                "request_id": "req-2",
                "output": {"audio": {"url": "https://evil.example/a.wav"}},
                "usage": {"characters": 2},
            },
        )

    adapter = CosyVoiceAdapter(api_key="sk-test", transport=httpx.MockTransport(handler))
    request = TtsRequestV1(
        provider="cosyvoice",
        model=COSYVOICE_MODEL,
        voice_id="longxiaochun_v2",
        text="你好。",
    )
    with pytest.raises(TtsError) as error:
        await adapter.synthesize(request)
    assert error.value.code == "PROVIDER_RESULT_UNTRUSTED"


async def test_local_adapter_needs_no_network():
    adapter = LocalTtsAdapter()
    request = TtsRequestV1(
        provider="local",
        model="local-tts-v1",
        voice_id="local-narrator",
        text="雨夜咖啡店。",
        episode_id="episode-1",
        speaker="旁白",
    )
    result = await adapter.synthesize(request)
    assert result.provider == "local"
    assert result.characters == 6
    assert result.audio == synthesize_local_wav("雨夜咖啡店。", "local-narrator")


async def test_an_http_aliyun_result_url_is_still_accepted():
    audio = synthesize_local_wav("你好。", "local-female")

    def handler(request: httpx.Request):
        if request.url.path.endswith("/SpeechSynthesizer"):
            return httpx.Response(
                200,
                json={
                    "request_id": "req-http",
                    "output": {
                        "audio": {"url": "http://dashscope-result-bj.oss-cn-beijing.aliyuncs.com/a.wav"}
                    },
                    "usage": {"characters": 3},
                },
            )
        return httpx.Response(200, content=audio, headers={"content-type": "audio/wav"})

    adapter = CosyVoiceAdapter(api_key="sk-test", transport=httpx.MockTransport(handler))
    request = TtsRequestV1(
        provider="cosyvoice",
        model=COSYVOICE_MODEL,
        voice_id="longxiaochun_v2",
        text="你好。",
    )
    result = await adapter.synthesize(request)
    assert result.characters == 3
