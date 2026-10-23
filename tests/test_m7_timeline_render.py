"""M7 E3 timeline render: trim/reorder/crossfade, subtitle remap and idempotency."""

import io
import wave

import pytest
from timeline_support import PROJECT, canonical, clips_for, render, render_args, renderable_episode, save

from vagent.video.audio_import import import_audio
from vagent.video.films import RENDER_TIMELINE_ASSEMBLY, Film
from vagent.video.renders import remap_timeline_cues, rendered_cues
from vagent.video.subtitles import SubtitleCue, SubtitleTimelineEntry
from vagent.video.timelines import Timeline, TimelineClip, effective_timeline, timeline_duration_ms


def wav_bytes(seconds: float = 1.0, sample_rate: int = 48000) -> bytes:
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(sample_rate)
        handle.writeframes(b"\x00\x00" * int(sample_rate * seconds))
    return buffer.getvalue()


async def fixture(store):
    return await renderable_episode(store)


def test_remap_keeps_the_cue_inside_its_trimmed_shot():
    clips = (
        TimelineClip(shotId="shot-a", shotVersionId="v-a", storyboardId="board", inMs=1000, outMs=5000),
        TimelineClip(shotId="shot-b", shotVersionId="v-b", storyboardId="board", inMs=0, outMs=5000),
    )
    _entries, _active, effective, total = effective_timeline(clips, ())
    assert total == 9
    # Source track: shot-a 0..5 s, shot-b 5..10 s.
    track = (
        SubtitleTimelineEntry(
            shotId="shot-a",
            start={"numerator": 0, "denominator": 1},
            duration={"numerator": 5, "denominator": 1},
        ),
        SubtitleTimelineEntry(
            shotId="shot-b",
            start={"numerator": 5, "denominator": 1},
            duration={"numerator": 5, "denominator": 1},
        ),
    )
    cue_a = SubtitleCue(
        start={"numerator": 2_000_000, "denominator": 1_000_000},
        end={"numerator": 4_000_000, "denominator": 1_000_000},
        text="第一句",
        shotId="shot-a",
        sourceSha256="0" * 64,
    )
    cue_b = SubtitleCue(
        start={"numerator": 6_000_000, "denominator": 1_000_000},
        end={"numerator": 8_000_000, "denominator": 1_000_000},
        text="第二句",
        shotId="shot-b",
        sourceSha256="0" * 64,
    )
    dropped = SubtitleCue(
        start={"numerator": 500_000, "denominator": 1_000_000},
        end={"numerator": 900_000, "denominator": 1_000_000},
        text="被裁掉",
        shotId="shot-a",
        sourceSha256="0" * 64,
    )
    remapped = remap_timeline_cues([dropped, cue_a, cue_b], track, clips, effective)
    # The pre-trim cue is entirely outside [1000, 5000] ms and is dropped.
    assert [cue.text for cue in remapped] == ["第一句", "第二句"]
    assert remapped[0].start.total_microseconds() == 1_000_000
    assert remapped[0].end.total_microseconds() == 3_000_000
    assert remapped[1].start.total_microseconds() == 5_000_000
    assert remapped[1].end.total_microseconds() == 7_000_000


def test_a_full_timeline_render_freezes_the_edit_and_replays(store):
    import asyncio

    fixture_value = asyncio.run(fixture(store))
    state = store.snapshot()
    clips = clips_for(state, fixture_value["boards"])
    audio = asyncio.run(
        import_audio(
            fixture_value["service"],
            project_id=PROJECT,
            data=wav_bytes(),
            kind="bgm",
            title="雨声",
            source_url="https://example.test/rain.wav",
            license="CC0",
        )
    )
    voice = fixture_value["audio"]["assets"][0]
    timeline = save(
        fixture_value["registry"],
        store,
        fixture_value,
        clips,
        key="m7e3-render-save",
        titleCards=[
            {"text": "第一场", "startMs": 1000, "durationMs": 3000},
            {"text": "尾声", "startMs": 80_000, "durationMs": 5000},
        ],
        audio=[
            {
                "audioId": audio["audioId"],
                "audioVersion": audio["audioVersion"],
                "kind": "bgm",
                "gainDb": -12,
                "startMs": 500,
            },
            {
                "audioId": voice["audioId"],
                "audioVersion": voice["audioVersion"],
                "kind": "voice",
            },
        ],
    )
    record = Timeline.model_validate(store.snapshot()["timelines"][timeline["timelineId"]])
    effective = timeline["latest"]["effectiveDurationMs"]
    assert effective == timeline_duration_ms(record.latest)

    result = asyncio.run(render(fixture_value, timeline["timelineId"], key="m7e3-render-1"))
    film = store.snapshot()["films"][result["filmId"]]
    frozen = Film.model_validate(film).versions[-1]
    assert frozen.assembly.assembly_version == RENDER_TIMELINE_ASSEMBLY
    assert frozen.render.sources.timeline is not None
    assert frozen.render.sources.timeline.timeline_id == timeline["timelineId"]
    assert frozen.render.sources.timeline.timeline_version == 1
    assert frozen.render.sources.timeline.timeline_sha256 == timeline["latest"]["sha256"]
    assert frozen.render.subtitle is not None and frozen.render.subtitle.retimed is True
    assert abs(frozen.metadata.duration_seconds - effective / 1000) <= 1 / 30 + 0.05
    assert [card.text for card in frozen.render.title_cards] == ["第一场", "尾声"]
    kinds = {track.kind: track for track in frozen.render.audio.tracks}
    assert set(kinds) == {"bgm", "voice"}
    assert kinds["bgm"].gain_db == -12
    assert kinds["bgm"].start.total_microseconds() == 500_000
    # Timeline renders are local: no provider Job is registered.
    assert store.snapshot()["jobs"].keys() == state["jobs"].keys()

    cues = rendered_cues(store.snapshot(), Film.model_validate(film), frozen)
    assert cues
    assert all(0 <= cue.start.total_microseconds() < effective * 1000 for cue in cues)
    assert all(
        before.end.total_microseconds() <= after.start.total_microseconds()
        for before, after in zip(cues, cues[1:], strict=False)
    )

    replay = asyncio.run(render(fixture_value, timeline["timelineId"], key="m7e3-render-2"))
    assert replay["replay"] is True and replay["filmVersion"] == 1

    read = fixture_value["service"].renders.read({"timelineId": timeline["timelineId"]}, PROJECT)
    assert read["render"]["renderId"] == result["renderId"]
    assert read["render"]["scope"] == "timeline"
    assert read["render"]["timelineId"] == timeline["timelineId"]


def test_edit_then_render_appends_a_new_version_and_keeps_the_old_one(store):
    import asyncio

    fixture_value = asyncio.run(fixture(store))
    clips = clips_for(store.snapshot(), fixture_value["boards"])
    timeline = save(fixture_value["registry"], store, fixture_value, clips, key="m7e3-v1-save")
    first = asyncio.run(render(fixture_value, timeline["timelineId"], key="m7e3-v1-render"))
    first_film = store.snapshot()["films"][first["filmId"]]
    first_sha = first_film["versions"][0]["sha256"]

    state = store.snapshot()
    record = Timeline.model_validate(state["timelines"][timeline["timelineId"]])
    reordered = [item.model_dump(mode="json", by_alias=True) for item in record.latest.clips]
    reordered[0], reordered[1] = reordered[1], reordered[0]
    reordered[0]["inMs"] = 1000
    transitions = [{"afterShotId": reordered[0]["shotId"], "type": "crossfade", "durationMs": 400}]
    edited = save(
        fixture_value["registry"],
        store,
        fixture_value,
        reordered,
        key="m7e3-v2-save",
        timelineId=timeline["timelineId"],
        expectedRevision=record.revision,
        transitions=transitions,
    )
    assert edited["currentVersion"] == 2
    expected_ms = sum(item["outMs"] - item["inMs"] for item in reordered) - 400
    assert edited["latest"]["effectiveDurationMs"] == expected_ms

    second = asyncio.run(render(fixture_value, timeline["timelineId"], key="m7e3-v2-render"))
    assert second["filmId"] == first["filmId"]
    assert second["filmVersion"] == 2
    film = Film.model_validate(store.snapshot()["films"][first["filmId"]])
    assert [item.version for item in film.versions] == [1, 2]
    assert film.versions[0].sha256 == first_sha
    assert film.versions[0].assembly.assembly_version == RENDER_TIMELINE_ASSEMBLY
    assert film.versions[1].render.sources.timeline.timeline_version == 2
    assert abs(film.versions[1].metadata.duration_seconds - expected_ms / 1000) <= 1 / 30 + 0.05

    # Rendering an explicit older version is a distinct, still-valid request.
    old = asyncio.run(render(fixture_value, timeline["timelineId"], key="m7e3-v1-again", timelineVersion=1))
    assert old["replay"] is True
    assert old["filmVersion"] == 1


def test_a_stale_timeline_refuses_before_encoding(store):
    import asyncio

    from subtitle_support import episode_args, plan

    fixture_value = asyncio.run(fixture(store))
    clips = clips_for(store.snapshot(), fixture_value["boards"])
    timeline = save(fixture_value["registry"], store, fixture_value, clips, key="m7e3-stale-save")
    episode = store.snapshot()["episodes"][fixture_value["episode"]["episodeId"]]
    changed = fixture_value["registry"].execute(
        "episode_save",
        {
            **episode_args(plan()),
            "title": "改过的标题",
            "episodeId": episode["id"],
            "expectedRevision": episode["revision"],
        },
        store=store,
        project_id=PROJECT,
        operation_key="m7e3-stale-episode",
    )
    assert changed["ok"], changed
    before = store.snapshot()
    args = canonical(render_args(timeline["timelineId"]))
    from vagent.video.renders import RenderError

    with pytest.raises(RenderError) as error:
        asyncio.run(
            fixture_value["service"].renders.render(
                args, project_id=PROJECT, operation_key="m7e3-stale-render", args=args
            )
        )
    assert error.value.code == "TIMELINE_SOURCE_MISSING"
    after = store.snapshot()
    assert after["films"] == before["films"]
    assert after["media"] == before["media"]
    assert after["renders"] == before["renders"]


def test_audio_import_requires_a_license_and_deduplicates(store):
    import asyncio

    from vagent.errors import AppError

    fixture_value = asyncio.run(fixture(store))
    data = wav_bytes(0.5)
    first = asyncio.run(
        import_audio(
            fixture_value["service"],
            project_id=PROJECT,
            data=data,
            kind="sfx",
            title="快门",
            source_url="https://example.test/sfx.wav",
            license="CC0",
        )
    )
    assert first["replayed"] is False
    again = asyncio.run(
        import_audio(
            fixture_value["service"],
            project_id=PROJECT,
            data=data,
            kind="sfx",
            title="另一个标题",
            source_url="https://example.test/other.wav",
            license="CC0",
        )
    )
    assert again["replayed"] is True and again["audioId"] == first["audioId"]
    with pytest.raises(AppError) as error:
        asyncio.run(
            import_audio(
                fixture_value["service"],
                project_id=PROJECT,
                data=data,
                kind="sfx",
                title="无许可",
                source_url="",
                license="",
            )
        )
    assert error.value.code == "AUDIO_LICENSE_MISSING"
    with pytest.raises(AppError) as error:
        asyncio.run(
            import_audio(
                fixture_value["service"],
                project_id=PROJECT,
                data=b"not audio",
                kind="bgm",
                title="坏文件",
                source_url="https://example.test/x.wav",
                license="CC0",
            )
        )
    assert error.value.code == "AUDIO_FORMAT_UNSUPPORTED"
    assert store.snapshot()["audio"][first["audioId"]]["kind"] == "sfx"
