"""M6 E3 render domain: effective timeline, transitions, cue remap and task limits."""

from fractions import Fraction
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from vagent.video.films import Rational
from vagent.video.renders import (
    FilmRenderArgs,
    RenderSceneProgress,
    RenderTask,
    RenderTitleCardArg,
    RenderTransitionArg,
    ShotEntry,
    effective_duration,
    effective_entries,
    remap_cues,
    validate_title_cards,
    validate_transitions,
)
from vagent.video.subtitles import SubtitleCue

SHA = "0" * 64


def entry(shot_id: str, index: int, duration: int = 5) -> ShotEntry:
    return ShotEntry(
        shot_id=shot_id,
        storyboard_id="board",
        scene_id="scene",
        start=Fraction(index * duration),
        duration=Fraction(duration),
    )


def cue(shot_id: str, start: int, end: int, text: str = "台词") -> SubtitleCue:
    return SubtitleCue(
        start=Rational.microseconds(start),
        end=Rational.microseconds(end),
        text=text,
        shot_id=shot_id,
        scene_id="scene",
        source_sha256=SHA,
    )


def test_crossfade_shortens_only_the_outgoing_tail():
    entries = [entry("a", 0), entry("b", 1), entry("c", 2)]
    active = validate_transitions(
        entries, (RenderTransitionArg(afterShotId="a", type="crossfade", durationMs=500),)
    )
    effective = effective_entries(entries, active)
    assert [item.start for item in effective] == [Fraction(0), Fraction(9, 2), Fraction(19, 2)]
    assert [item.duration for item in effective] == [Fraction(9, 2), Fraction(5), Fraction(5)]
    assert effective_duration(effective) == Fraction(29, 2)
    # The next shot keeps its full length; only the outgoing tail is dropped.
    assert effective[1].start == Fraction(9, 2)


def test_fade_and_crossfade_share_the_same_time_base():
    entries = [entry("a", 0), entry("b", 1), entry("c", 2)]
    fade = validate_transitions(entries, (RenderTransitionArg(afterShotId="a", type="fade", durationMs=300),))
    cross = validate_transitions(
        entries, (RenderTransitionArg(afterShotId="a", type="crossfade", durationMs=300),)
    )
    assert fade["a"][1] == cross["a"][1] == Fraction(3, 10)
    assert fade["a"][0] == "fade" and cross["a"][0] == "crossfade"


def test_a_cut_is_not_recorded_and_does_not_shorten():
    entries = [entry("a", 0), entry("b", 1)]
    active = validate_transitions(entries, (RenderTransitionArg(afterShotId="a", type="cut"),))
    assert active == {}
    assert effective_duration(effective_entries(entries, active)) == 10


def test_transition_limits_and_boundaries_are_enforced():
    entries = [entry("a", 0), entry("b", 1)]
    with pytest.raises(Exception) as error:
        validate_transitions(entries, (RenderTransitionArg(afterShotId="b", type="fade", durationMs=500),))
    assert getattr(error.value, "code", None) == "TRANSITION_INVALID"
    with pytest.raises(Exception):
        validate_transitions(
            entries, (RenderTransitionArg(afterShotId="missing", type="fade", durationMs=500),)
        )
    with pytest.raises(ValidationError):
        RenderTransitionArg(afterShotId="a", type="fade", durationMs=2000)
    # A one-second short shot cannot host a one-second transition.
    short = [entry("a", 0, 2), entry("b", 1, 2)]
    with pytest.raises(Exception):
        validate_transitions(short, (RenderTransitionArg(afterShotId="a", type="fade", durationMs=1000),))


def test_cue_remap_clamps_to_effective_windows_without_overlap():
    entries = [entry("a", 0), entry("b", 1)]
    active = validate_transitions(
        entries, (RenderTransitionArg(afterShotId="a", type="crossfade", durationMs=500),)
    )
    effective = effective_entries(entries, active)
    cues = [
        cue("a", 0, 2_000_000, "第一句"),
        cue("a", 4_700_000, 5_000_000, "尾句"),
        cue("b", 5_000_000, 7_000_000, "第二句"),
    ]
    remapped = remap_cues(cues, entries, effective)
    assert [(item.start.total_microseconds(), item.end.total_microseconds()) for item in remapped] == [
        (0, 2_000_000),
        (4_500_000, 6_500_000),
    ]
    assert remapped[-1].end.total_microseconds() <= round(effective_duration(effective) * 1_000_000)
    assert all(
        previous.end.total_microseconds() <= current.start.total_microseconds()
        for previous, current in zip(remapped, remapped[1:])
    )


def test_a_dropped_tail_cue_is_removed_instead_of_crossing_the_next_shot():
    entries = [entry("a", 0), entry("b", 1)]
    active = validate_transitions(
        entries, (RenderTransitionArg(afterShotId="a", type="crossfade", durationMs=500),)
    )
    effective = effective_entries(entries, active)
    cues = [cue("a", 4_800_000, 5_000_000, "尾"), cue("b", 5_000_000, 5_500_000, "头")]
    remapped = remap_cues(cues, entries, effective)
    assert [item.text for item in remapped] == ["头"]


def test_title_cards_must_fit_and_not_overlap():
    validate_title_cards((RenderTitleCardArg(text="片头", startMs=0, durationMs=1000),), Fraction(10))
    with pytest.raises(Exception) as error:
        validate_title_cards(
            (
                RenderTitleCardArg(text="一", startMs=0, durationMs=1500),
                RenderTitleCardArg(text="二", startMs=1000, durationMs=1000),
            ),
            Fraction(10),
        )
    assert getattr(error.value, "code", None) == "TITLE_CARD_LIMIT"
    with pytest.raises(Exception):
        validate_title_cards((RenderTitleCardArg(text="片尾", startMs=9500, durationMs=1000),), Fraction(10))
    with pytest.raises(ValidationError):
        FilmRenderArgs(
            scope="storyboard",
            storyboardId="board",
            titleCards=[
                {"text": f"卡{index}", "startMs": index * 100, "durationMs": 100} for index in range(5)
            ],
        )


def test_render_args_require_one_target_and_a_track_for_burn():
    base = {"scope": "storyboard", "storyboardId": "board", "trackId": "track"}
    assert FilmRenderArgs(**base).subtitle_mode == "soft"
    with pytest.raises(ValidationError):
        FilmRenderArgs(**{**base, "subtitleMode": "burn", "trackId": None})
    with pytest.raises(ValidationError):
        FilmRenderArgs(scope="episode", storyboardId="board")
    with pytest.raises(ValidationError):
        FilmRenderArgs(scope="storyboard", storyboardId="board", trackVersion=1)


def test_task_status_and_scene_consistency_are_checked():
    scene = RenderSceneProgress(storyboard_id="board", shot_ids=("shot",), status="succeeded")
    base = {
        "id": "render",
        "projectId": "coffee",
        "scope": "storyboard",
        "storyboardId": "board",
        "profileName": "720p-h264-aac-v1",
        "subtitleMode": "soft",
        "fingerprint": SHA,
        "status": "running",
        "scenes": (scene,),
        "estimatedSeconds": 10,
        "createdAt": "2030-01-01T00:00:00+00:00",
        "updatedAt": "2030-01-01T00:00:00+00:00",
        "revision": 0,
    }
    assert RenderTask(**base).status == "running"
    with pytest.raises(ValidationError):
        RenderTask(**{**base, "status": "succeeded"})
    with pytest.raises(ValidationError):
        RenderTask(**{**base, "episodeId": "episode"})
    with pytest.raises(ValidationError):
        RenderSceneProgress(storyboard_id="board", shot_ids=("shot",), status="failed", error_code=None)


def test_film_timeline_rebuilds_rounded_segment_starts(monkeypatch):
    """AAC durations are exact rationals while assembly starts are microsecond-rounded.

    The render must rebuild starts from the durations, otherwise a subtitle track
    saved from the same film cannot match it exactly.
    """
    from vagent.video import render_service as module
    from vagent.video.render_service import RenderService

    duration = Fraction(55822, 11025)
    segments = [
        SimpleNamespace(
            shot_id="s1",
            start=Rational.microseconds(0),
            duration=Rational.of(duration),
        ),
        SimpleNamespace(
            shot_id="s2",
            start=Rational.microseconds(5_063_220),
            duration=Rational.of(duration),
        ),
    ]
    base = SimpleNamespace(
        inputs=[SimpleNamespace(shot_id="s1"), SimpleNamespace(shot_id="s2")],
        assembly=SimpleNamespace(timeline=segments),
        render=None,
    )
    monkeypatch.setattr(module, "find_film", lambda *_args: SimpleNamespace(versions=[base]))
    service = RenderService.__new__(RenderService)
    shots = [
        SimpleNamespace(shot_id="s1", start=Fraction(0), duration=Fraction(5)),
        SimpleNamespace(shot_id="s2", start=Fraction(0), duration=Fraction(5)),
    ]
    service._assign_timeline({}, "coffee", SimpleNamespace(id="board"), shots)
    assert [shot.start for shot in shots] == [Fraction(0), duration]
    assert [shot.duration for shot in shots] == [duration, duration]
    assert shots[0].start + shots[0].duration == shots[1].start


def test_title_cards_render_through_ass_without_drawtext():
    """Linux imageio-ffmpeg ships libass but not drawtext; title cards use ASS."""
    from vagent.video.subtitles import render_title_ass

    ass = render_title_ass(
        (
            RenderTitleCardArg(text="雨夜咖啡店", startMs=200, durationMs=1500),
            RenderTitleCardArg(text="未完待续", startMs=9_000, durationMs=1_000),
        ),
        width=1920,
        height=1080,
    )
    assert "Style: Title" in ass
    assert "PlayResX: 1920" in ass and "PlayResY: 1080" in ass
    assert "Dialogue: 0,0:00:00.20,0:00:01.70,Title" in ass
    assert "Dialogue: 0,0:00:09.00,0:00:10.00,Title" in ass
    assert "雨夜咖啡店" in ass and "未完待续" in ass
    assert "drawtext" not in ass
