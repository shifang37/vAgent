"""M7 E3 timelines: append-only edit versions, validation and Store invariants."""

import copy
from uuid import uuid4

import pytest
from timeline_support import PROJECT, clips_for, read, renderable_episode, save

from vagent.errors import AppError
from vagent.storage import FileStore
from vagent.video.subtitles import find_track
from vagent.video.timelines import (
    TIMELINE_CLIP_LIMIT,
    Timeline,
    effective_timeline,
    timeline_duration_ms,
)


def tool(registry, store, name, args, *, key=None):
    return registry.execute(
        name, args, store=store, project_id=PROJECT, operation_key=key or f"m7e3-{uuid4()}"
    )


def failure(registry, store, name, args):
    result = tool(registry, store, name, args)
    return result


async def fixture(store):
    return await renderable_episode(store)


def test_save_freezes_the_ordered_timeline_and_replays_the_same_content(store):
    import asyncio

    fixture_value = asyncio.run(fixture(store))
    state = store.snapshot()
    clips = clips_for(state, fixture_value["boards"])
    first = save(fixture_value["registry"], store, fixture_value, clips, key="m7e3-save-1")
    assert first["currentVersion"] == 1 and first["revision"] == 0
    assert first["replayed"] is False
    latest = first["latest"]
    assert latest["contract"] == "timeline-zh-v1"
    assert latest["clipCount"] == len(clips)
    assert latest["effectiveDurationMs"] == sum(item["outMs"] - item["inMs"] for item in clips)
    assert latest["rawDurationMs"] == latest["effectiveDurationMs"]
    assert latest["stale"] is False
    assert [item["shotId"] for item in latest["clips"]] == [item["shotId"] for item in clips]
    assert all(item["mediaAvailable"] for item in latest["clips"])
    assert latest["subtitle"]["cueCount"] > 0

    # The identical content replays the same version even without expectedRevision.
    replayed = save(fixture_value["registry"], store, fixture_value, clips, key="m7e3-save-2")
    assert replayed["replayed"] is True
    assert replayed["currentVersion"] == 1
    assert store.snapshot()["jobs"].keys() == state["jobs"].keys()
    assert store.snapshot()["timelines"].keys() == {first["timelineId"]}


def test_a_change_appends_a_version_and_keeps_the_old_bytes(store):
    import asyncio

    fixture_value = asyncio.run(fixture(store))
    clips = clips_for(store.snapshot(), fixture_value["boards"])
    first = save(fixture_value["registry"], store, fixture_value, clips, key="m7e3-a")
    old_version = copy.deepcopy(store.snapshot()["timelines"][first["timelineId"]]["versions"][0])
    changed = [dict(item) for item in clips]
    changed[0]["inMs"] = 1000
    second = save(
        fixture_value["registry"],
        store,
        fixture_value,
        changed,
        key="m7e3-b",
        timelineId=first["timelineId"],
        expectedRevision=0,
    )
    assert second["currentVersion"] == 2 and second["revision"] == 1
    assert second["replayed"] is False
    record = store.snapshot()["timelines"][first["timelineId"]]
    assert record["versions"][0] == old_version
    assert record["currentVersion"] == 2
    # A missing or stale expectedRevision refuses the change.
    stale = tool(
        fixture_value["registry"],
        store,
        "timeline_save",
        {
            "episodeId": fixture_value["episode"]["episodeId"],
            "timelineId": first["timelineId"],
            "clips": clips,
            "subtitle": {"trackId": fixture_value["trackId"], "trackVersion": 1},
        },
    )
    assert stale["ok"] is False and stale["error"]["code"] == "TIMELINE_VERSION_CONFLICT"
    wrong = tool(
        fixture_value["registry"],
        store,
        "timeline_save",
        {
            "episodeId": fixture_value["episode"]["episodeId"],
            "timelineId": first["timelineId"],
            "clips": clips,
            "subtitle": {"trackId": fixture_value["trackId"], "trackVersion": 1},
            "expectedRevision": 0,
        },
    )
    assert wrong["ok"] is False and wrong["error"]["code"] == "TIMELINE_VERSION_CONFLICT"


def test_trim_and_transition_define_the_effective_timeline(store):
    import asyncio

    fixture_value = asyncio.run(fixture(store))
    clips = clips_for(store.snapshot(), fixture_value["boards"])
    trimmed = [dict(item) for item in clips]
    trimmed[0]["inMs"] = 1000
    transitions = [
        {"afterShotId": trimmed[0]["shotId"], "type": "crossfade", "durationMs": 500},
        {"afterShotId": trimmed[1]["shotId"], "type": "fade", "durationMs": 200},
    ]
    saved = save(
        fixture_value["registry"],
        store,
        fixture_value,
        trimmed,
        key="m7e3-trim",
        transitions=transitions,
    )
    latest = saved["latest"]
    raw = sum(item["outMs"] - item["inMs"] for item in trimmed)
    assert latest["rawDurationMs"] == raw
    assert latest["effectiveDurationMs"] == raw - 700
    assert latest["transitionMs"] == 700
    by_shot = {item["shotId"]: item for item in latest["clips"]}
    assert by_shot[trimmed[0]["shotId"]]["effectiveDurationMs"] == 4000 - 500
    assert by_shot[trimmed[1]["shotId"]]["effectiveDurationMs"] == 5000 - 200
    # The transition tail is inside the first clip's effective window.
    assert by_shot[trimmed[0]["shotId"]]["effectiveStartMs"] == 0
    assert by_shot[trimmed[1]["shotId"]]["effectiveStartMs"] == 3500


def test_invalid_trims_references_and_transitions_are_rejected(store):
    import asyncio

    fixture_value = asyncio.run(fixture(store))
    registry = fixture_value["registry"]
    episode_id = fixture_value["episode"]["episodeId"]
    state = store.snapshot()
    clips = clips_for(state, fixture_value["boards"])

    def attempt(changes):
        return tool(
            registry,
            store,
            "timeline_save",
            {
                "episodeId": episode_id,
                "clips": clips,
                "subtitle": {"trackId": fixture_value["trackId"], "trackVersion": 1},
                **changes,
            },
        )

    duplicate = attempt({"clips": [*clips, clips[0]]})
    assert duplicate["ok"] is False and duplicate["error"]["code"] == "TIMELINE_INVALID"

    too_long = [dict(item) for item in clips]
    too_long[0]["outMs"] = too_long[0]["outMs"] + 1
    assert attempt({"clips": too_long})["error"]["code"] == "TIMELINE_TRIM_INVALID"

    too_short = [dict(item) for item in clips]
    too_short[0]["inMs"] = too_short[0]["outMs"] - 500
    assert attempt({"clips": too_short})["error"]["code"] == "TIMELINE_TRIM_INVALID"

    reversed_window = [dict(item) for item in clips]
    reversed_window[0]["inMs"] = reversed_window[0]["outMs"]
    assert attempt({"clips": reversed_window})["error"]["code"] == "TIMELINE_TRIM_INVALID"

    last_shot = attempt(
        {"transitions": [{"afterShotId": clips[-1]["shotId"], "type": "crossfade", "durationMs": 300}]}
    )
    assert last_shot["error"]["code"] == "TIMELINE_INVALID"

    duplicate_boundary = attempt(
        {
            "transitions": [
                {"afterShotId": clips[0]["shotId"], "type": "crossfade", "durationMs": 200},
                {"afterShotId": clips[0]["shotId"], "type": "fade", "durationMs": 200},
            ]
        }
    )
    assert duplicate_boundary["error"]["code"] == "TIMELINE_INVALID"

    unknown_shot = [dict(clips[0], shotId=str(uuid4()))]
    assert attempt({"clips": unknown_shot})["error"]["code"] == "TIMELINE_INVALID"

    wrong_version = [dict(clips[0], shotVersionId=str(uuid4()))]
    assert attempt({"clips": wrong_version})["error"]["code"] == "TIMELINE_INVALID"

    wrong_board = [dict(clips[0], storyboardId=str(uuid4()))]
    assert attempt({"clips": wrong_board})["error"]["code"] == "TIMELINE_INVALID"

    assert store.snapshot()["timelines"] == {}


def test_subtitle_audio_and_title_card_bindings_are_validated(store):
    import asyncio

    fixture_value = asyncio.run(fixture(store))
    registry = fixture_value["registry"]
    episode_id = fixture_value["episode"]["episodeId"]
    state = store.snapshot()
    clips = clips_for(state, fixture_value["boards"])

    def attempt(**changes):
        args = {"episodeId": episode_id, "clips": clips, **changes}
        return tool(registry, store, "timeline_save", args)

    index = next(number for number, scene in enumerate(fixture_value["plan"]["scenes"]) if scene["dialogue"])
    storyboard = fixture_value["boards"][index]
    created = tool(
        registry,
        store,
        "subtitle_save",
        {"scope": "storyboard", "storyboardId": storyboard["storyboardId"]},
        key="m7e3-storyboard-subtitle",
    )
    assert created["ok"], created
    storyboard_track = find_track(store.snapshot(), PROJECT, "storyboard", storyboard["storyboardId"])
    assert storyboard_track is not None
    rejected = attempt(subtitle={"trackId": storyboard_track.id, "trackVersion": 1})
    assert rejected["error"]["code"] == "SUBTITLE_TRACK_MISSING"

    missing_version = attempt(subtitle={"trackId": fixture_value["trackId"], "trackVersion": 9})
    assert missing_version["error"]["code"] == "SUBTITLE_TRACK_MISSING"

    voice = fixture_value["audio"]["assets"][0]
    no_subtitle = attempt(
        audio=[
            {
                "audioId": voice["audioId"],
                "audioVersion": voice["audioVersion"],
                "kind": "voice",
            }
        ]
    )
    assert no_subtitle["error"]["code"] == "TIMELINE_INVALID"

    voice_offset = attempt(
        subtitle={"trackId": fixture_value["trackId"], "trackVersion": 1},
        audio=[
            {
                "audioId": voice["audioId"],
                "audioVersion": voice["audioVersion"],
                "kind": "voice",
                "startMs": 500,
            }
        ],
    )
    assert voice_offset["error"]["code"] == "INVALID_ARGUMENTS"

    bad_kind = attempt(
        audio=[
            {
                "audioId": voice["audioId"],
                "audioVersion": voice["audioVersion"],
                "kind": "bgm",
            }
        ]
    )
    assert bad_kind["error"]["code"] == "TIMELINE_INVALID"

    overlap_titles = attempt(
        titleCards=[
            {"text": "第一张", "startMs": 0, "durationMs": 3000},
            {"text": "第二张", "startMs": 2000, "durationMs": 3000},
        ]
    )
    assert overlap_titles["error"]["code"] == "TIMELINE_INVALID"

    overflow_title = attempt(titleCards=[{"text": "越界", "startMs": 80_000, "durationMs": 20_000}])
    assert overflow_title["error"]["code"] == "TIMELINE_INVALID"

    assert store.snapshot()["timelines"] == {}


def test_a_changed_episode_marks_the_timeline_stale(store):
    import asyncio

    from subtitle_support import episode_args, plan

    fixture_value = asyncio.run(fixture(store))
    clips = clips_for(store.snapshot(), fixture_value["boards"])
    saved = save(fixture_value["registry"], store, fixture_value, clips, key="m7e3-stale")
    assert (
        read(fixture_value["registry"], store, fixture_value["episode"]["episodeId"])["latest"]["stale"]
        is False
    )
    episode = store.snapshot()["episodes"][fixture_value["episode"]["episodeId"]]
    updated = tool(
        fixture_value["registry"],
        store,
        "episode_save",
        {
            **episode_args(plan()),
            "title": "第二版标题",
            "episodeId": episode["id"],
            "expectedRevision": episode["revision"],
        },
        key="m7e3-episode-v2",
    )
    assert updated["ok"], updated
    view = read(fixture_value["registry"], store, fixture_value["episode"]["episodeId"], key="m7e3-stale-2")
    assert view["latest"]["stale"] is True
    # The old version stays readable and is still the current timeline version.
    assert view["latest"]["version"] == 1
    assert saved["latest"]["clipCount"] == len(clips)


def test_editor_view_lists_sources_tracks_and_audio(store):
    import asyncio

    from vagent.video.timelines import editor_view

    fixture_value = asyncio.run(fixture(store))
    view = editor_view(store.snapshot(), PROJECT, fixture_value["episode"]["episodeId"])
    assert view["timeline"] is None
    assert len(view["shots"]) == sum(len(board["shotIds"]) for board in fixture_value["boards"])
    assert all(item["shotVersionId"] for item in view["shots"])
    assert all(item["mediaAvailable"] for item in view["shots"])
    assert view["subtitleTracks"][0]["trackId"] == fixture_value["trackId"]
    assert any(item["kind"] == "voice" for item in view["audio"])

    clips = clips_for(store.snapshot(), fixture_value["boards"])
    saved = save(fixture_value["registry"], store, fixture_value, clips, key="m7e3-editor")
    view = editor_view(store.snapshot(), PROJECT, fixture_value["episode"]["episodeId"])
    assert view["timeline"]["timelineId"] == saved["timelineId"]
    assert view["timeline"]["latest"]["clipCount"] == len(clips)


def test_store_rejects_deletion_and_history_rewrites(store):
    import asyncio

    fixture_value = asyncio.run(fixture(store))
    clips = clips_for(store.snapshot(), fixture_value["boards"])
    saved = save(fixture_value["registry"], store, fixture_value, clips, key="m7e3-store")
    timeline_id = saved["timelineId"]

    def delete(draft):
        draft["timelines"].pop(timeline_id)

    with pytest.raises(AppError) as error:
        store.transaction(delete)
    assert error.value.code == "TIMELINE_VERSION_CONFLICT"

    def rewrite(draft):
        raw = draft["timelines"][timeline_id]
        record = Timeline.model_validate(raw)
        version = record.latest.model_copy(update={"clips": record.latest.clips[:-1]})
        draft["timelines"][timeline_id] = record.model_copy(
            update={"versions": (*record.versions[:-1], version)}
        ).model_dump(mode="json", by_alias=True)

    with pytest.raises(AppError) as error:
        store.transaction(rewrite)
    assert error.value.code == "TIMELINE_INVALID"

    def fingerprint(draft):
        raw = draft["timelines"][timeline_id]
        record = Timeline.model_validate(raw)
        version = record.latest.model_copy(update={"fingerprint": "0" * 64})
        draft["timelines"][timeline_id] = record.model_copy(update={"versions": (version,)}).model_dump(
            mode="json", by_alias=True
        )

    with pytest.raises(AppError) as error:
        store.transaction(fingerprint)
    assert error.value.code == "TIMELINE_INVALID"
    # The real record survived all rejected commits.
    assert store.snapshot()["timelines"][timeline_id]["currentVersion"] == 1


def test_store_open_revalidates_timelines(tmp_path):
    import asyncio

    with FileStore.open(tmp_path / "source") as source:
        fixture_value = asyncio.run(fixture(source))
        clips = clips_for(source.snapshot(), fixture_value["boards"])
        saved = save(fixture_value["registry"], source, fixture_value, clips, key="m7e3-open")
        state = source.snapshot()
    assert saved["timelineId"] in state["timelines"]
    (tmp_path / "copy").mkdir()
    import json

    (tmp_path / "copy" / "state.json").write_text(
        json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    with FileStore.open(tmp_path / "copy") as reopened:
        assert Timeline.model_validate(reopened.snapshot()["timelines"][saved["timelineId"]])
    broken = copy.deepcopy(state)
    broken["timelines"][saved["timelineId"]]["projectId"] = "other"
    (tmp_path / "broken").mkdir()
    (tmp_path / "broken" / "state.json").write_text(
        json.dumps(broken, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    with pytest.raises(AppError) as error:
        FileStore.open(tmp_path / "broken")
    assert error.value.code == "INVALID_STORE"


def test_duration_helpers_are_exact(store):
    import asyncio

    fixture_value = asyncio.run(fixture(store))
    clips = clips_for(store.snapshot(), fixture_value["boards"])
    saved = save(fixture_value["registry"], store, fixture_value, clips, key="m7e3-duration")
    record = Timeline.model_validate(store.snapshot()["timelines"][saved["timelineId"]])
    version = record.latest
    entries, active, effective, total = effective_timeline(version.clips, version.transitions)
    assert len(entries) == len(clips) == len(effective)
    assert active == {}
    assert timeline_duration_ms(version) == round(total * 1000) == saved["latest"]["effectiveDurationMs"]
    assert len(version.clips) <= TIMELINE_CLIP_LIMIT
