"""M6 E2 ``audio_generate``: TTS budgets, voice binding, media and offline/live modes."""

import hashlib
import json
from decimal import Decimal
from types import SimpleNamespace

import httpx
from conftest import VideoClock, video_run
from shot_support import mock_tools
from subtitle_support import plan, save_episode

from vagent.video.tts import synthesize_local_wav

PROJECT = "coffee"


def call(registry, store, context, args, *, key="audio-1", step=1):
    scoped = context.model_copy(update={"tool_call_id": key, "model_step": step})
    return registry.aexecute(
        "audio_generate",
        args,
        store=store,
        project_id=PROJECT,
        operation_key=scoped.operation_key,
        context=scoped,
    )


def episode_case(store, *, clock=None):
    """A real planned episode plus the selected scene's frozen dialogue."""
    registry, service, _adapter, context = mock_tools(store, clock=clock or VideoClock())
    value = plan()
    episode = save_episode(registry, store, value)
    scene_index = next(index for index, scene in enumerate(value["scenes"]) if scene["dialogue"])
    scene = value["scenes"][scene_index]
    scene_id = episode["sceneIds"][scene_index]
    speakers = sorted({(line["speaker"] or "画外") for line in scene["dialogue"]})
    voices = [{"speaker": speaker, "voiceId": "local-narrator"} for speaker in speakers]
    args = {
        "episodeId": episode["episodeId"],
        "voices": voices,
        "confirm": True,
        "lines": [{"sceneId": scene_id, "paragraph": line["paragraph"]} for line in scene["dialogue"]],
    }
    return {
        "registry": registry,
        "service": service,
        "context": context,
        "episode": episode,
        "scene": scene,
        "sceneId": scene_id,
        "speakers": speakers,
        "voices": voices,
        "args": args,
        "characters": sum(len(line["text"]) for line in scene["dialogue"]),
    }


def _handler(audio: bytes):
    def handler(request: httpx.Request):
        if request.url.path.endswith("/SpeechSynthesizer"):
            body = json.loads(request.content)
            characters = len(body["input"]["text"])
            return httpx.Response(
                200,
                json={
                    "request_id": "tts-req",
                    "output": {
                        "audio": {"url": "https://dashscope-result-bj.oss-cn-beijing.aliyuncs.com/a.wav"}
                    },
                    "usage": {"characters": characters},
                },
            )
        return httpx.Response(200, content=audio, headers={"content-type": "audio/wav"})

    return handler


def use_live_cosyvoice(store, service, run_id="video-run"):
    service.config = SimpleNamespace(video_api_key="sk-test", video_workspace_id=None, video_mode="live")
    service.audio.transport = httpx.MockTransport(
        _handler(synthesize_local_wav("你好，世界。", "local-female"))
    )

    def upgrade(draft):
        draft["runs"][run_id]["videoMode"] = "live"

    store.transaction(upgrade)


async def test_audio_generate_requires_explicit_confirmation(store):
    case = episode_case(store)
    args = {**case["args"], "confirm": False}
    result = await call(case["registry"], store, case["context"], args)
    assert result["ok"] is False
    assert result["error"]["code"] == "CONFIRMATION_REQUIRED"
    assert store.snapshot()["audio"] == {}
    assert store.snapshot()["media"] == {}


async def test_audio_generate_writes_assets_media_and_episode_usage(store):
    case = episode_case(store)
    result = await call(case["registry"], store, case["context"], case["args"])
    assert result["ok"], result
    data = result["data"]
    assert data["provider"]["provider"] == "local"
    assert data["provider"]["priceKnown"] is False
    assert len(data["lines"]) == len(case["scene"]["dialogue"])
    assert data["usage"]["characters"] == case["characters"]
    assert data["usage"]["requests"] == len(data["lines"])
    assert data["usage"]["charactersLimit"] == 2000
    assert all(line["reused"] is False for line in data["lines"])
    state = store.snapshot()
    assert len(state["audio"]) == len(data["lines"])
    assert len(state["media"]) == len(data["lines"])
    assert state["jobs"] == {}
    asset = next(iter(state["audio"].values()))
    version = asset["versions"][0]
    assert version["source"]["source"] == "provider"
    assert version["source"]["episodeId"] == case["episode"]["episodeId"]
    assert version["source"]["speaker"] in case["speakers"]
    media = next(iter(state["media"].values()))
    assert media["kind"] == "audio" and media["mimeType"] == "audio/wav"
    assert (media["sampleRate"], media["channels"]) == (48000, 1)
    view = case["service"].media.view(media["id"])
    assert view["kind"] == "audio" and view["mediaAvailable"] is True
    resolved, lease = case["service"].media.open_content(media["id"])
    with lease:
        payload = lease.file.read()
    assert hashlib.sha256(payload).hexdigest() == media["sha256"]
    assert payload[:4] == b"RIFF"
    read = case["registry"].execute(
        "episode_read",
        {"episodeId": case["episode"]["episodeId"]},
        store=store,
        project_id=PROJECT,
        operation_key="audio-episode-read",
    )
    assert read["ok"], read
    assert read["data"]["audio"]["usage"]["requests"] == len(data["lines"])
    assert len(read["data"]["audio"]["assets"]) == len(data["lines"])
    assert read["data"]["audio"]["provider"]["voices"][0]["voiceId"] == "local-narrator"


async def test_replaying_the_same_operation_returns_the_same_assets(store):
    case = episode_case(store)
    first = await call(case["registry"], store, case["context"], case["args"])
    assert first["ok"], first
    ids = [line["audioId"] for line in first["data"]["lines"]]
    again = await call(case["registry"], store, case["context"], case["args"])
    assert again["ok"], again
    assert [line["audioId"] for line in again["data"]["lines"]] == ids
    assert len(store.snapshot()["audio"]) == len(ids)


async def test_a_partial_line_selection_generates_only_that_line(store):
    case = episode_case(store)
    line = case["scene"]["dialogue"][0]
    args = {
        **case["args"],
        "lines": [{"sceneId": case["sceneId"], "paragraph": line["paragraph"]}],
    }
    result = await call(case["registry"], store, case["context"], args, key="audio-partial")
    assert result["ok"], result
    assert len(result["data"]["lines"]) == 1
    assert result["data"]["usage"]["requests"] == 1
    assert len(store.snapshot()["audio"]) == 1


async def test_the_same_operation_with_changed_arguments_conflicts(store):
    case = episode_case(store)
    first = await call(case["registry"], store, case["context"], case["args"], key="audio-same")
    assert first["ok"], first
    changed = {
        **case["args"],
        "voices": [{"speaker": voice["speaker"], "voiceId": "local-female"} for voice in case["voices"]],
    }
    result = await call(case["registry"], store, case["context"], changed, key="audio-same")
    assert result["ok"] is False and result["error"]["code"] == "OPERATION_CONFLICT"


async def test_a_new_call_reuses_identical_lines_without_new_requests(store):
    case = episode_case(store)
    first = await call(case["registry"], store, case["context"], case["args"], key="audio-1")
    assert first["ok"], first
    media_before = len(store.snapshot()["media"])
    second = await call(case["registry"], store, case["context"], case["args"], key="audio-2")
    assert second["ok"], second
    assert [line["audioId"] for line in second["data"]["lines"]] == [
        line["audioId"] for line in first["data"]["lines"]
    ]
    assert all(line["reused"] is True for line in second["data"]["lines"])
    assert second["data"]["usage"] == first["data"]["usage"]
    assert len(store.snapshot()["media"]) == media_before


async def test_voice_binding_rejects_a_different_voice_for_the_same_speaker(store):
    case = episode_case(store)
    first = await call(case["registry"], store, case["context"], case["args"], key="audio-1")
    assert first["ok"], first
    conflicting = dict(case["args"])
    conflicting["voices"] = [
        {"speaker": voice["speaker"], "voiceId": "local-female"} for voice in case["voices"]
    ]
    result = await call(case["registry"], store, case["context"], conflicting, key="audio-2")
    assert result["ok"] is False
    assert result["error"]["code"] == "VOICE_CONFLICT"
    assert len(store.snapshot()["audio"]) == len(first["data"]["lines"])


async def test_unknown_voice_and_missing_speaker_mapping_are_rejected(store):
    case = episode_case(store)
    unknown = dict(case["args"])
    unknown["voices"] = [{"speaker": case["speakers"][0], "voiceId": "longxiaochun_v2"}]
    result = await call(case["registry"], store, case["context"], unknown, key="audio-unknown")
    assert result["ok"] is False and result["error"]["code"] == "TTS_PROVIDER_UNKNOWN"
    missing = dict(case["args"])
    missing["voices"] = [{"speaker": "另一个人", "voiceId": "local-narrator"}]
    result = await call(case["registry"], store, case["context"], missing, key="audio-missing")
    assert result["ok"] is False and result["error"]["code"] == "TTS_PROVIDER_UNKNOWN"
    assert store.snapshot()["audio"] == {}


async def test_a_read_only_run_cannot_generate_audio(store):
    case = episode_case(store)
    read_only = video_run(store, "audio-read-only", project_id=PROJECT, read_only=True)
    result = await call(case["registry"], store, read_only, case["args"], key="audio-ro")
    assert result["ok"] is False and result["error"]["code"] == "READ_ONLY"
    assert store.snapshot()["audio"] == {}


def long_episode(registry, store, *, lines: int, text_length: int):
    counts = []
    remaining = lines
    for index in range(6):
        take = min(40, max(1, remaining - (5 - index)))
        counts.append(take)
        remaining -= take
    assert remaining == 0 and len(counts) == 6
    scenes = []
    for index, count in enumerate(counts):
        dialogue = [
            {"text": "一" * text_length, "paragraph": number, "speaker": "旁白"} for number in range(count)
        ]
        scenes.append(
            {
                "heading": f"场 {index + 1}",
                "synopsis": "预算测试场记。",
                "characterIds": [],
                "dialogue": dialogue,
                "shotCount": 2,
                "durationSeconds": 10,
            }
        )
    saved = registry.execute(
        "episode_save",
        {"title": "长集", "targetDurationSeconds": 60, "scenes": scenes},
        store=store,
        project_id=PROJECT,
        operation_key="audio-long-episode",
    )
    assert saved["ok"], saved
    return saved["data"]


def all_line_refs(registry, store, episode_id):
    read = registry.execute(
        "episode_read",
        {"episodeId": episode_id},
        store=store,
        project_id=PROJECT,
        operation_key="audio-long-read",
    )
    assert read["ok"], read
    refs = []
    for scene in read["data"]["scenes"]:
        for line in scene["dialogue"]:
            refs.append({"sceneId": scene["sceneId"], "paragraph": line["paragraph"]})
    return refs


async def test_episode_character_limit_rejects_the_whole_batch_before_tts(store):
    registry, _service, _adapter, context = mock_tools(store)
    episode = long_episode(registry, store, lines=240, text_length=10)
    refs = all_line_refs(registry, store, episode["episodeId"])
    assert len(refs) == 240
    args = {
        "episodeId": episode["episodeId"],
        "voices": [{"speaker": "旁白", "voiceId": "local-narrator"}],
        "confirm": True,
        "lines": refs,
    }
    result = await call(registry, store, context, args, key="audio-chars")
    assert result["ok"] is False and result["error"]["code"] == "TTS_TEXT_LIMIT"
    assert store.snapshot()["audio"] == {} and store.snapshot()["media"] == {}


async def test_episode_request_limit_rejects_the_whole_batch_before_tts(store):
    registry, _service, _adapter, context = mock_tools(store)
    episode = long_episode(registry, store, lines=41, text_length=2)
    refs = all_line_refs(registry, store, episode["episodeId"])
    args = {
        "episodeId": episode["episodeId"],
        "voices": [{"speaker": "旁白", "voiceId": "local-narrator"}],
        "confirm": True,
        "lines": refs,
    }
    result = await call(registry, store, context, args, key="audio-requests")
    assert result["ok"] is False and result["error"]["code"] == "TTS_BUDGET_LIMIT"
    assert store.snapshot()["audio"] == {}


async def test_audio_media_serves_the_audio_content_type_with_ranges(store):
    from vagent.video.media_response import media_response

    case = episode_case(store)
    result = await call(case["registry"], store, case["context"], case["args"], key="audio-range")
    assert result["ok"], result
    media_id = next(iter(store.snapshot()["media"]))
    media, lease = case["service"].media.open_content(media_id)
    try:
        response = media_response(
            media,
            lease,
            method="GET",
            request_headers={"range": "bytes=0-99"},
            download=False,
            content_type=media.mime_type,
            extension="wav",
        )
        assert response.status_code == 206
        assert response.headers["content-type"].startswith("audio/wav")
        assert response.headers["content-range"] == f"bytes 0-99/{media.size_bytes}"
        assert response.headers["accept-ranges"] == "bytes"
        assert response.headers["etag"] == f'"{media.sha256}"'
    finally:
        lease.close()


async def test_live_mode_uses_cosyvoice_and_records_the_verified_price(store):
    case = episode_case(store)
    use_live_cosyvoice(store, case["service"])
    live_args = dict(case["args"])
    live_args["voices"] = [
        {"speaker": voice["speaker"], "voiceId": "longxiaochun_v2"} for voice in case["voices"]
    ]
    result = await call(case["registry"], store, case["context"], live_args, key="audio-live")
    assert result["ok"], result
    data = result["data"]
    assert data["provider"]["provider"] == "cosyvoice"
    assert data["provider"]["pricePerThousandCharacters"] == "0.20"
    assert data["usage"]["currency"] == "CNY"
    expected = format(Decimal(case["characters"]) * Decimal("0.0002"), ".4f")
    assert data["usage"]["estimatedCostCny"] == expected
    state = store.snapshot()
    assert state["jobs"] == {}
    source = next(iter(state["audio"].values()))["versions"][0]["source"]
    assert (source["provider"], source["model"]) == ("cosyvoice", "cosyvoice-v2")
    assert source["text"].strip()


async def test_a_cache_only_replay_does_not_need_a_live_key(store):
    case = episode_case(store)
    use_live_cosyvoice(store, case["service"])
    live_args = dict(case["args"])
    live_args["voices"] = [
        {"speaker": voice["speaker"], "voiceId": "longxiaochun_v2"} for voice in case["voices"]
    ]
    first = await call(case["registry"], store, case["context"], live_args, key="audio-live-1")
    assert first["ok"], first
    case["service"].config = SimpleNamespace(video_api_key="", video_workspace_id=None, video_mode="live")
    second = await call(case["registry"], store, case["context"], live_args, key="audio-live-2")
    assert second["ok"], second
    assert all(line["reused"] is True for line in second["data"]["lines"])
    assert [line["audioId"] for line in second["data"]["lines"]] == [
        line["audioId"] for line in first["data"]["lines"]
    ]
