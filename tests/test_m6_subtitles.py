"""M6 E1: deterministic subtitle alignment, append-only tracks and SRT/ASS bytes."""

from vagent.video.episodes import SceneDialogue
from vagent.video.films import Rational
from vagent.video.subtitles import (
    SUBTITLE_LINE_MAX_CHARS,
    SubtitleTimelineEntry,
    align_subtitles,
    render_ass,
    render_srt,
    split_dialogue,
    wrap_lines,
)

DIALOGUE = [
    SceneDialogue(text="冰箱上的便条，写着：别忘了买牛奶。", speaker="林晚", paragraph=0),
    SceneDialogue(text="她推门进来，雨还在下。", speaker="旁白", paragraph=1),
]


def timeline(count=6, seconds=10, scene_id="scene-1"):
    entries, offset = [], 0
    for index in range(count):
        entries.append(
            SubtitleTimelineEntry(
                scene_id=scene_id,
                shot_id=f"shot-{index + 1}",
                start=Rational.microseconds(offset),
                duration=Rational.microseconds(seconds * 1_000_000),
            )
        )
        offset += seconds * 1_000_000
    return entries


def align(entries=None, dialogue=None, payload=None):
    entries = entries or timeline()
    dialogue = dialogue if dialogue is not None else {"scene-1": DIALOGUE}
    cues, sha = align_subtitles(entries, dialogue, source_payload=payload or {"fixture": "m6"})
    return entries, cues, sha


def test_split_keeps_every_character_and_respects_the_limits():
    text = "第一句话说完了。第二句很长，需要在次级标点处切开，保证每条不超过二十四个字符！第三句？"
    pieces = split_dialogue(text)
    assert "".join(pieces) == text
    assert all(0 < len(piece) <= 48 for piece in pieces)
    for piece in pieces:
        assert sum(len(line) for line in wrap_lines(piece)) == len(piece)
        assert all(len(line) <= SUBTITLE_LINE_MAX_CHARS for line in wrap_lines(piece))
        assert len(wrap_lines(piece)) <= 2


def test_unbreakable_run_becomes_one_two_line_cue():
    text = "这" * 30 + "。"
    pieces = split_dialogue(text)
    assert pieces == [text]
    assert [len(line) for line in wrap_lines(pieces[0])] == [16, 15]


def test_alignment_is_deterministic_and_keeps_the_dialogue():
    entries, first, sha = align()
    _entries, second, sha2 = align()
    assert [cue.model_dump() for cue in first] == [cue.model_dump() for cue in second]
    assert sha == sha2
    assert "".join(cue.text for cue in first) == "".join(line.text for line in DIALOGUE)
    assert {cue.shot_id for cue in first} <= {entry.shot_id for entry in entries}
    assert {cue.speaker for cue in first} == {"林晚", "旁白"}


def test_sixty_second_sample_has_zero_drift():
    entries, cues, _sha = align()
    assert cues
    assert cues[0].start.total_microseconds() == 0
    assert cues[-1].end.total_microseconds() == 60_000_000
    previous_end = 0
    for cue in cues:
        assert cue.end.total_microseconds() > cue.start.total_microseconds()
        assert cue.start.total_microseconds() >= previous_end
        previous_end = cue.end.total_microseconds()
    # Every shot's allocated cues exactly fill the shot budget: no seam drift.
    by_shot: dict[str, int] = {}
    for cue in cues:
        by_shot[cue.shot_id] = (
            by_shot.get(cue.shot_id, 0) + cue.end.total_microseconds() - cue.start.total_microseconds()
        )
    assert all(microseconds <= 10_000_000 for microseconds in by_shot.values())
    assert sum(len(cue.text) for cue in cues) == sum(len(line.text) for line in DIALOGUE)


def test_cue_never_crosses_a_shot_boundary():
    entries = timeline(count=2, seconds=2)
    long_dialogue = [SceneDialogue(text="一" * 40 + "。" * 20, speaker="甲", paragraph=0)]
    _entries, cues, _sha = align(entries, {"scene-1": long_dialogue})
    for cue in cues:
        entry = next(item for item in entries if item.shot_id == cue.shot_id)
        start = entry.start.total_microseconds()
        assert start <= cue.start.total_microseconds()
        assert cue.end.total_microseconds() <= start + entry.duration.total_microseconds()


def test_srt_and_ass_are_deterministic_and_use_the_exact_timeline():
    _entries, cues, _sha = align()
    srt = render_srt(cues)
    assert render_srt(cues) == srt
    assert srt.startswith("1\n00:00:00,000 --> ")
    first_end = cues[0].end.total_microseconds() // 1000
    assert f"--> 00:00:{first_end // 1000:02d},{first_end % 1000:03d}" in srt
    ass = render_ass(cues)
    assert "[V4+ Styles]" in ass and "VAgent Subtitle SC" in ass
    assert ass.count("Dialogue:") == len(cues)
    assert "0:00:00.00" in ass
