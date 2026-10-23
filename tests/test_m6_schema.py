"""M6: schema 7, the v6->v7 migration, the audio media union and baseline drift."""

import hashlib
import importlib
import json
from decimal import Decimal
from pathlib import Path

import pytest
from shot_support import mock_tools, shot
from subtitle_support import plan, save_episode

from vagent.errors import AppError
from vagent.storage import DatabaseV8, FileStore
from vagent.video.audio import AudioAsset, AudioAssetVersion, AudioMedia, ProviderAudioSource
from vagent.video.films import parse_media
from vagent.video.jobs import JobService
from vagent.video.providers.mock import MockVideoAdapter
from vagent.video.subtitles import find_track, save_subtitles

ROOT = Path(__file__).resolve().parents[1]
BASELINE = json.loads((ROOT / "docs" / "M6_E0_BASELINE.json").read_text(encoding="utf-8"))
CONTRACT_MODULES = (
    "vagent.storage",
    "vagent.video.contracts",
    "vagent.video.films",
    "vagent.video.episodes",
    "vagent.video.storyboards",
    "vagent.video.audio",
    "vagent.video.subtitles",
)
# E3 extends the frozen render records (episode scope, optional subtitle, render-native
# assembly); these entries are the whole declared drift against the E0 baseline.
ALLOWED_CHANGED = {
    ("vagent.video.films", "FilmVersion"),
    ("vagent.video.films", "Film"),
    ("vagent.video.films", "FilmAssembly"),
    ("vagent.video.films", "RenderSources"),
    ("vagent.video.films", "RenderSubtitle"),
    ("vagent.storage", "DatabaseV5"),
    ("vagent.storage", "Database"),
    ("vagent.storage", "DatabaseV7"),
}


def contract_schemas():
    from pydantic import BaseModel

    result = {}
    for module_name in CONTRACT_MODULES:
        module = importlib.import_module(module_name)
        models = {}
        for name, value in vars(module).items():
            if not isinstance(value, type) or not issubclass(value, BaseModel):
                continue
            if value.__module__ != module_name:
                continue
            canonical = json.dumps(value.model_json_schema(), sort_keys=True, separators=(",", ":"))
            models[name] = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
        result[module_name] = models
    return result


def seed_v7_state(store):
    registry, _service, _adapter, _context = mock_tools(store)
    plan_value = plan()
    episode = save_episode(registry, store, plan_value)
    saved = registry.execute(
        "storyboard_save",
        {
            "title": "雨夜咖啡店",
            "sceneId": episode["sceneIds"][1],
            "shots": [shot("雨夜街道"), shot("推门入店")],
        },
        store=store,
        project_id="coffee",
        operation_key="m6-schema-storyboard",
    )
    assert saved["ok"], saved
    state = store.snapshot()
    track = save_subtitles(
        {"scope": "storyboard", "storyboardId": saved["data"]["storyboardId"]}, state, "coffee"
    )
    store.transaction(
        lambda draft: draft["subtitleTracks"].__setitem__(
            track["trackId"], state["subtitleTracks"][track["trackId"]]
        )
    )
    return {"storyboardId": saved["data"]["storyboardId"], "episodeId": episode["episodeId"]}


def dump_v6(state: dict) -> bytes:
    legacy = json.loads(json.dumps(state))
    legacy["schemaVersion"] = 6
    legacy.pop("audio", None)
    legacy.pop("subtitleTracks", None)
    legacy.pop("renders", None)
    return json.dumps(legacy, ensure_ascii=False, indent=2, allow_nan=False).encode("utf-8")


async def test_fresh_store_is_schema_eight_with_three_empty_collections(store):
    state = store.snapshot()
    assert state["schemaVersion"] == 8
    assert state["audio"] == {} and state["subtitleTracks"] == {} and state["renders"] == {}
    DatabaseV8.model_validate(state)
    assert (store.home / "state.json").is_file()


def test_v6_state_migrates_to_v8_with_an_exact_backup(tmp_path):
    with FileStore.open(tmp_path / "source") as source:
        seed_v7_state(source)
        seeded = source.snapshot()
    raw = dump_v6(seeded)
    legacy = json.loads(raw)
    assert "audio" not in legacy and "subtitleTracks" not in legacy and "renders" not in legacy
    home = tmp_path / "migrated"
    home.mkdir()
    (home / "state.json").write_bytes(raw)
    # A domain-only checkpoint sentinel must survive the envelope migration.
    sentinel = b"domain-only-sentinel"
    (home / "checkpoints.sqlite").write_bytes(sentinel)
    with FileStore.open(home) as store:
        migrated = store.snapshot()
    assert migrated["schemaVersion"] == 8
    assert migrated["audio"] == {} and migrated["subtitleTracks"] == {} and migrated["renders"] == {}
    for key, value in legacy.items():
        if key == "schemaVersion":
            continue
        assert migrated[key] == value, key
    DatabaseV8.model_validate(migrated)
    backups = list(home.glob("state-v6-*.json"))
    assert len(backups) == 1 and backups[0].read_bytes() == raw
    assert (home / "checkpoints.sqlite").read_bytes() == sentinel
    with FileStore.open(home) as reopened:
        assert reopened.snapshot() == migrated
    assert len(list(home.glob("state-v*.json"))) == 1


def test_v7_state_migrates_to_v8_with_an_exact_backup(tmp_path):
    with FileStore.open(tmp_path / "source") as source:
        seed_v7_state(source)
        seeded = source.snapshot()
    legacy = json.loads(json.dumps(seeded))
    legacy["schemaVersion"] = 7
    legacy.pop("renders")
    raw = json.dumps(legacy, ensure_ascii=False, indent=2, allow_nan=False).encode("utf-8")
    home = tmp_path / "migrated"
    home.mkdir()
    (home / "state.json").write_bytes(raw)
    with FileStore.open(home) as store:
        migrated = store.snapshot()
    assert migrated["schemaVersion"] == 8
    assert migrated["renders"] == {}
    for key, value in legacy.items():
        if key != "schemaVersion":
            assert migrated[key] == value, key
    DatabaseV8.model_validate(migrated)
    backups = list(home.glob("state-v7-*.json"))
    assert len(backups) == 1 and backups[0].read_bytes() == raw


def test_audio_media_branch_round_trips_and_is_not_a_video(store):
    store.ensure_session("coffee")
    media_id = "11111111-1111-4111-8111-111111111111"
    asset_id = "22222222-2222-4222-8222-222222222222"
    data = b"RIFF" + b"\0" * 64
    directory = store.home / "media" / "coffee"
    directory.mkdir(parents=True, exist_ok=True)
    (directory / f"{media_id}.wav").write_bytes(data)
    media = AudioMedia(
        id=media_id,
        project_id="coffee",
        relative_path=f"media/coffee/{media_id}.wav",
        mime_type="audio/wav",
        size_bytes=len(data),
        sha256=hashlib.sha256(data).hexdigest(),
        duration_seconds=1.5,
        sample_rate=48000,
        channels=1,
        created_at="2030-01-01T00:00:00+00:00",
    )
    version = AudioAssetVersion(
        version=1,
        kind="voice",
        media_id=media_id,
        source=ProviderAudioSource(
            provider="local-tts",
            model="local-tts-v1",
            voice_id="narration",
            text="冰箱上的便条。",
        ),
        duration_seconds=1.5,
        sample_rate=48000,
        channels=1,
        sha256=media.sha256,
        size_bytes=media.size_bytes,
        created_at="2030-01-01T00:00:00+00:00",
    )
    asset = AudioAsset(
        id=asset_id,
        project_id="coffee",
        kind="voice",
        current_version=1,
        versions=(version,),
        revision=0,
        created_at="2030-01-01T00:00:00+00:00",
    )

    def insert(draft):
        draft["media"][media_id] = media.model_dump(mode="json", by_alias=True)
        draft["audio"][asset_id] = asset.model_dump(mode="json", by_alias=True)

    store.transaction(insert)
    state = store.snapshot()
    assert parse_media(state["media"][media_id]).__class__.__name__ == "AudioMedia"
    service = JobService(store, [MockVideoAdapter(store)])
    view = service.media.view(media_id)
    assert view["kind"] == "audio"
    assert view["mimeType"] == "audio/wav"
    assert view["durationSeconds"] == 1.5
    assert view["mediaAvailable"] is True
    # An audio record is never a film input: the union keeps the branches separate.
    assert state["audio"][asset_id]["versions"][0]["mediaId"] == media_id


def test_audio_asset_rejects_a_mismatched_media_record(store):
    store.ensure_session("coffee")
    media_id = "33333333-3333-4333-8333-333333333333"
    data = b"RIFF" + b"\0" * 32
    directory = store.home / "media" / "coffee"
    directory.mkdir(parents=True, exist_ok=True)
    (directory / f"{media_id}.wav").write_bytes(data)
    media = AudioMedia(
        id=media_id,
        project_id="coffee",
        relative_path=f"media/coffee/{media_id}.wav",
        mime_type="audio/wav",
        size_bytes=len(data),
        sha256=hashlib.sha256(data).hexdigest(),
        duration_seconds=1.0,
        sample_rate=48000,
        channels=1,
        created_at="2030-01-01T00:00:00+00:00",
    )
    bad = media.model_copy(update={"sha256": "0" * 64})
    version = AudioAssetVersion(
        version=1,
        kind="bgm",
        media_id=media_id,
        source=ProviderAudioSource(provider="local-tts", model="local-tts-v1", voice_id="v", text="背景音乐"),
        duration_seconds=1.0,
        sample_rate=48000,
        channels=1,
        sha256=media.sha256,
        size_bytes=media.size_bytes,
        created_at="2030-01-01T00:00:00+00:00",
    )
    asset = AudioAsset(
        id="44444444-4444-4444-8444-444444444444",
        project_id="coffee",
        kind="bgm",
        current_version=1,
        versions=(version,),
        revision=0,
        created_at="2030-01-01T00:00:00+00:00",
    )

    def insert(draft):
        draft["media"][media_id] = bad.model_dump(mode="json", by_alias=True)
        draft["audio"][asset.id] = asset.model_dump(mode="json", by_alias=True)

    with pytest.raises(AppError) as error:
        store.transaction(insert)
    assert error.value.code == "AUDIO_ASSET_MISSING"


def test_subtitle_track_target_is_unique_per_storyboard(store):
    registry, _service, _adapter, _context = mock_tools(store)
    plan_value = plan()
    episode = save_episode(registry, store, plan_value)
    saved = registry.execute(
        "storyboard_save",
        {
            "title": "雨夜咖啡店",
            "sceneId": episode["sceneIds"][1],
            "shots": [shot("雨夜街道"), shot("推门入店")],
        },
        store=store,
        project_id="coffee",
        operation_key="m6-unique-board",
    )
    assert saved["ok"], saved
    storyboard_id = saved["data"]["storyboardId"]
    state = store.snapshot()
    track = save_subtitles({"scope": "storyboard", "storyboardId": storyboard_id}, state, "coffee")
    state["subtitleTracks"][track["trackId"]] = state["subtitleTracks"][track["trackId"]]
    store.transaction(
        lambda draft: draft["subtitleTracks"].__setitem__(
            track["trackId"], state["subtitleTracks"][track["trackId"]]
        )
    )
    # A second track for the same target cannot be committed.
    duplicate = dict(state["subtitleTracks"][track["trackId"]])
    duplicate["id"] = "55555555-5555-4555-8555-555555555555"
    with pytest.raises(AppError) as error:
        store.transaction(lambda draft: draft["subtitleTracks"].__setitem__(duplicate["id"], duplicate))
    assert error.value.code == "SUBTITLE_CONFLICT"
    assert find_track(store.snapshot(), "coffee", "storyboard", storyboard_id) is not None


def test_e1_keeps_every_baseline_schema_hash_except_the_declared_change():
    current = contract_schemas()
    for module_name, models in BASELINE["schemaSha256"].items():
        for name, digest in models.items():
            if (module_name, name) in ALLOWED_CHANGED:
                assert current[module_name][name] != digest
                continue
            assert current[module_name][name] == digest, f"{module_name}.{name} changed"
    # Only additive models appear in the new schema surface.
    assert {"DatabaseV7", "DatabaseV8"} <= set(current["vagent.storage"])
    assert {"FilmRender", "ExportProfile", "RenderMeasurements"} <= set(current["vagent.video.films"])
    assert {"AudioAsset", "AudioAssetVersion", "AudioMedia"} <= set(current["vagent.video.audio"])
    assert {"SubtitleTrack", "SubtitleTrackVersion", "SubtitleCue"} <= set(current["vagent.video.subtitles"])
    # Frozen limits and tool groups stay byte-identical; only the storage version moves.
    limits = BASELINE["limits"]
    assert limits["storageSchemaVersion"] == 6
    from vagent.storage import DatabaseV8 as _DatabaseV8  # noqa: F401
    from vagent.video import assembler, contracts, episodes, films, tools

    assert contracts.EPISODE_TOOLS_VERSION == limits["episode"]["toolsVersion"]
    assert films.FILM_INPUT_LIMIT == limits["film"]["inputLimit"]
    assert films.ASSEMBLY_VERSION == limits["film"]["assemblyVersion"]
    assert tools.WAIT_SET_LIMIT == limits["wait"]["setLimit"]
    assert tools.WAIT_TIMEOUT_SECONDS == limits["wait"]["timeoutSeconds"]
    assert assembler.ASSEMBLY_TIMEOUT_SECONDS == limits["film"]["assemblyTimeoutSeconds"]
    groups = {
        "M2": tools.M2_TOOL_NAMES,
        "M3": tools.M3_TOOL_NAMES,
        "M5": tools.M5_TOOL_NAMES,
        "M5E2": tools.M5_E2_TOOL_NAMES,
        "M5E3": tools.M5_E3_TOOL_NAMES,
    }
    for group, names in limits["toolGroups"].items():
        assert list(groups[group]) == names
    # E1 appends exactly the new M6 group; every older group is unchanged above.
    assert tools.M6_TOOL_NAMES == ("subtitle_save", "audio_generate", "film_render", "render_read")
    assert episodes.EPISODE_MAX_SHOTS == limits["episode"]["maxShots"]


def test_frozen_tts_and_subtitle_limits_match_the_contract():
    from vagent.video import audio, films
    from vagent.video.subtitles import (
        SUBTITLE_CUE_LIMIT,
        SUBTITLE_CUE_MAX_CHARS,
        SUBTITLE_LINE_MAX_CHARS,
        SUBTITLE_SCENE_CUE_LIMIT,
    )

    assert (SUBTITLE_CUE_LIMIT, SUBTITLE_CUE_MAX_CHARS, SUBTITLE_LINE_MAX_CHARS) == (200, 48, 24)
    assert SUBTITLE_SCENE_CUE_LIMIT == 40
    assert films.RENDER_MAX_SECONDS == 90
    assert films.RENDER_TIMEOUT_SECONDS == 120.0
    assert films.RENDER_JOB_TIMEOUT_SECONDS == 600.0
    assert films.TITLE_CARD_LIMIT == 4
    assert audio.TTS_PROVIDERS_VERSION == "tts-providers-v1"
    assert (
        audio.TTS_EPISODE_CHARACTER_LIMIT,
        audio.TTS_REQUEST_CHARACTER_LIMIT,
        audio.TTS_EPISODE_REQUEST_LIMIT,
    ) == (2000, 500, 40)
    assert str(audio.TTS_PRICE_PER_THOUSAND_CHARACTERS) == "0.20"
    assert str(audio.TTS_EPISODE_COST_LIMIT) == "0.40"
    assert str(audio.TTS_REQUEST_COST_LIMIT) == "0.10"
    assert audio.tts_cost(500, audio.TTS_PRICE_PER_THOUSAND_CHARACTERS) == Decimal("0.1000")


def test_bundled_font_matches_its_manifest_licence_and_pinned_digest():
    import hashlib

    from vagent.video import subtitle_font

    manifest = json.loads((subtitle_font.FONT_DIR / "manifest.json").read_text(encoding="utf-8"))
    font = subtitle_font.bundled_font_path()
    data = font.read_bytes()
    assert hashlib.sha256(data).hexdigest() == subtitle_font.FONT_SHA256
    assert len(data) == subtitle_font.FONT_SIZE_BYTES == manifest["subset"]["sizeBytes"]
    assert manifest["subset"]["sha256"] == subtitle_font.FONT_SHA256
    assert manifest["family"] == subtitle_font.FONT_NAME == "VAgent Subtitle SC"
    assert manifest["license"] == "OFL-1.1"
    licence = (subtitle_font.FONT_DIR / subtitle_font.LICENSE_FILE).read_text(encoding="utf-8")
    assert licence.startswith("This Font Software is licensed under the SIL Open Font License")
    assert "SIL OPEN FONT LICENSE Version 1.1" in licence


def test_a_tampered_font_digest_is_reported_as_font_missing(monkeypatch):
    from vagent.video import subtitle_font

    monkeypatch.setattr(subtitle_font, "FONT_SHA256", "0" * 64)
    with pytest.raises(AppError) as error:
        subtitle_font.bundled_font_path()
    assert error.value.code == "FONT_MISSING"
