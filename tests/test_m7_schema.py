"""M7 E1: schema 9, the v8->v9 migration and the frozen E0 baseline."""

import copy
import hashlib
import importlib
import json
from pathlib import Path

import pytest
from shot_support import mock_tools
from subtitle_support import episode_args, plan

from vagent.errors import AppError
from vagent.storage import DatabaseV8, DatabaseV9, FileStore, migrated_state
from vagent.video import tools
from vagent.video.seasons import SEASON_EPISODE_LIMIT, SEASON_PER_PROJECT_LIMIT

ROOT = Path(__file__).resolve().parents[1]
BASELINE = json.loads((ROOT / "docs" / "M7_E0_BASELINE.json").read_text(encoding="utf-8"))
CONTRACT_MODULES = (
    "vagent.storage",
    "vagent.video.contracts",
    "vagent.video.films",
    "vagent.video.episodes",
    "vagent.video.storyboards",
    "vagent.video.renders",
    "vagent.video.audio",
    "vagent.video.subtitles",
    "vagent.video.tts",
)
# E3 appends the timeline render source/assembly literal and the timeline inputs; the
# transitive ``$defs`` of Film/FilmVersion/Database are the whole declared drift.
E3_ALLOWED_CHANGED = {
    ("vagent.storage", "Database"),
    ("vagent.storage", "DatabaseV5"),
    ("vagent.storage", "DatabaseV7"),
    ("vagent.storage", "DatabaseV8"),
    ("vagent.video.films", "Film"),
    ("vagent.video.films", "FilmAssembly"),
    ("vagent.video.films", "FilmRender"),
    ("vagent.video.films", "FilmVersion"),
    ("vagent.video.films", "RenderSources"),
    ("vagent.video.renders", "FilmRenderArgs"),
    ("vagent.video.renders", "RenderReadArgs"),
    ("vagent.video.renders", "RenderTask"),
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


def seed_episode(store):
    registry, _service, _adapter, _context = mock_tools(store)
    args = episode_args(plan())
    args["title"] = "第一集"
    result = registry.execute(
        "episode_save", args, store=store, project_id="coffee", operation_key="m7-schema-episode"
    )
    assert result["ok"], result
    saved = registry.execute(
        "storyboard_save",
        {"title": "雨夜咖啡店", "shots": [{"description": "雨夜街道", "framing": "medium"}]},
        store=store,
        project_id="coffee",
        operation_key="m7-schema-board",
    )
    assert saved["ok"], saved
    return result["data"]["episodeId"]


def test_every_e0_baseline_schema_hash_is_unchanged():
    current = contract_schemas()
    for module_name, models in BASELINE["schemaSha256"].items():
        for name, digest in models.items():
            if (module_name, name) in E3_ALLOWED_CHANGED:
                assert current[module_name][name] != digest, f"{module_name}.{name} should change"
                continue
            assert current[module_name][name] == digest, f"{module_name}.{name} changed"
    # E1 only appends v9 models; the 165 baseline models keep their exact shape.
    assert {"DatabaseV9"} <= set(current["vagent.storage"])
    seasons = importlib.import_module("vagent.video.seasons")
    assert {"Season", "SeasonVersion"} <= {name for name in vars(seasons) if name[0].isupper()}
    # E2 appends the batch domain without touching any frozen baseline model.
    batches = importlib.import_module("vagent.video.batches")
    assert {"Batch", "BatchEpisode", "BatchEstimate"} <= {name for name in vars(batches) if name[0].isupper()}
    # E3 appends the timeline domain and the timeline render source.
    timelines = importlib.import_module("vagent.video.timelines")
    assert {
        "Timeline",
        "TimelineVersion",
        "TimelineClip",
        "TimelineTransition",
        "TimelineSubtitleRef",
        "TimelineAudioTrack",
        "TimelineTitleCard",
    } <= {name for name in vars(timelines) if name[0].isupper()}
    films = importlib.import_module("vagent.video.films")
    assert "RenderTimelineSource" in {name for name in vars(films) if name[0].isupper()}
    assert films.RENDER_TIMELINE_ASSEMBLY == "ffmpeg-render-timeline-v1"


def test_database_v9_adds_exactly_the_three_empty_collections():
    added = set(DatabaseV9.model_fields) - set(DatabaseV8.model_fields)
    # The version field exists in every envelope; only these three are new.
    assert added == {"seasons", "batches", "timelines"}
    assert DatabaseV9.model_fields["schema_version"].annotation == __import__("typing").Literal[9]
    database = DatabaseV9(
        schemaVersion=9,
        projects={},
        sessions={},
        artifacts={},
        runs={},
        operations={},
        jobs={},
        waits={},
        media={},
        storyboards={},
        assets={},
        films={},
        episodes={},
        scenes={},
        characters={},
        styles={},
        audio={},
        subtitleTracks={},
        renders={},
        seasons={},
        batches={},
        timelines={},
    )
    assert database.seasons == {} and database.batches == {} and database.timelines == {}


def test_frozen_stage_limits_and_tool_groups():
    limits = BASELINE["limits"]
    assert limits["storageSchemaVersion"] == 8
    assert limits["tools"]["videoToolsVersion"] == 13
    assert (tools.VIDEO_TOOLS_VERSION, tools.VIDEO_RULES_VERSION) == (18, 18)
    assert (tools.LIVE_VIDEO_TOOLS_VERSION, tools.LIVE_VIDEO_RULES_VERSION) == (18, 18)
    groups = {
        "M2": tools.M2_TOOL_NAMES,
        "M3": tools.M3_TOOL_NAMES,
        "M5": tools.M5_TOOL_NAMES,
        "M5E2": tools.M5_E2_TOOL_NAMES,
        "M5E3": tools.M5_E3_TOOL_NAMES,
        "M6E1": tools.M6_E1_TOOL_NAMES,
        "M6E2": tools.M6_E2_TOOL_NAMES,
        "M6E3": tools.M6_E3_TOOL_NAMES,
    }
    for group, names in limits["toolGroups"].items():
        assert list(groups[group]) == names
    # E1 appends exactly the season group and leaves every earlier group intact;
    # E2 appends only the season batch tool; E3 appends the timeline pair.
    assert tools.M7_E1_TOOL_NAMES == ("season_save", "season_read")
    assert tools.M7_E2_TOOL_NAMES == ("season_generate",)
    assert tools.M7_E3_TOOL_NAMES == ("timeline_save", "timeline_read")
    assert tools.M7_TOOL_NAMES == (
        *tools.M7_E1_TOOL_NAMES,
        *tools.M7_E2_TOOL_NAMES,
        *tools.M7_E3_TOOL_NAMES,
    )
    assert tools.WAIT_SET_LIMIT == limits["wait"]["setLimit"] == 20
    assert tools.WAIT_TIMEOUT_SECONDS == limits["wait"]["timeoutSeconds"] == 600
    # M7 freezes the season limits named in the contract.
    assert (SEASON_PER_PROJECT_LIMIT, SEASON_EPISODE_LIMIT) == (8, 4)
    # E3 timeline limits are the contract's 20 clips / 19 transitions / 1–90 s.
    from vagent.video import timelines

    assert (timelines.TIMELINE_CLIP_LIMIT, timelines.TIMELINE_TRANSITION_LIMIT) == (20, 19)
    assert (timelines.TIMELINE_MIN_SECONDS, timelines.TIMELINE_MAX_SECONDS) == (1, 90)
    assert timelines.TIMELINE_CONTRACT == "timeline-zh-v1"


def test_fresh_store_is_schema_nine_with_three_empty_collections(store):
    state = store.snapshot()
    assert state["schemaVersion"] == 9
    assert state["seasons"] == {} and state["batches"] == {} and state["timelines"] == {}
    DatabaseV9.model_validate(state)
    assert (store.home / "state.json").is_file()


def test_v8_state_migrates_to_v9_with_an_exact_backup(tmp_path):
    with FileStore.open(tmp_path / "source") as source:
        episode_id = seed_episode(source)
        seeded = source.snapshot()
    legacy = copy.deepcopy(seeded)
    legacy["schemaVersion"] = 8
    legacy.pop("seasons")
    legacy.pop("batches")
    legacy.pop("timelines")
    raw = json.dumps(legacy, ensure_ascii=False, indent=2, allow_nan=False).encode("utf-8")
    home = tmp_path / "migrated"
    home.mkdir()
    (home / "state.json").write_bytes(raw)
    sentinel = b"m7-domain-only-sentinel"
    (home / "checkpoints.sqlite").write_bytes(sentinel)
    with FileStore.open(home) as store:
        migrated = store.snapshot()
    assert migrated["schemaVersion"] == 9
    assert migrated["seasons"] == {} and migrated["batches"] == {} and migrated["timelines"] == {}
    for key, value in legacy.items():
        if key == "schemaVersion":
            continue
        assert migrated[key] == value, key
    assert migrated["episodes"][episode_id]["title"] == "第一集"
    DatabaseV9.model_validate(migrated)
    backups = list(home.glob("state-v8-*.json"))
    assert len(backups) == 1 and backups[0].read_bytes() == raw
    assert (home / "checkpoints.sqlite").read_bytes() == sentinel
    with FileStore.open(home) as reopened:
        assert reopened.snapshot() == migrated
    assert len(list(home.glob("state-v*.json"))) == 1


def test_migrated_state_is_mechanical_for_schema_eight():
    state = {
        "schemaVersion": 8,
        "projects": {},
        "sessions": {},
        "artifacts": {},
        "runs": {},
        "operations": {},
        "jobs": {},
        "waits": {},
        "media": {},
        "storyboards": {},
        "assets": {},
        "films": {},
        "episodes": {},
        "scenes": {},
        "characters": {},
        "styles": {},
        "audio": {},
        "subtitleTracks": {},
        "renders": {},
    }
    upgraded = migrated_state(state)
    assert upgraded["schemaVersion"] == 9
    assert upgraded["seasons"] == {} and upgraded["batches"] == {} and upgraded["timelines"] == {}
    assert state["schemaVersion"] == 8 and "seasons" not in state
    DatabaseV9.model_validate(upgraded)


@pytest.mark.parametrize("version", [0, 10, True, "9", None])
def test_unsupported_versions_are_still_rejected(tmp_path, version):
    raw = json.dumps({"schemaVersion": version, "projects": {}}).encode()
    (tmp_path / "state.json").write_bytes(raw)
    with pytest.raises(AppError) as error:
        FileStore.open(tmp_path)
    assert error.value.code == "INVALID_STORE"
    assert (tmp_path / "state.json").read_bytes() == raw
