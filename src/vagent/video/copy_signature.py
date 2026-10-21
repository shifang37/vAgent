"""`mp4-copy-signature-v1`: prove that two AVC/AAC files may be stream-copied together.

Structural validation (`inspect_mp4`) only proves a file is a complete self-contained
MP4. Concatenating with ``-c copy`` additionally requires identical codec
configuration, track layout and a computable edit-list/priming relationship, because
FFmpeg will otherwise silently repair non-monotonic DTS instead of failing.

This module parses exactly the boxes that decide copy compatibility, records the
per-file timeline (sample durations, edit list, priming) and refuses any other
timeline structure it cannot explain. It performs no decoding and no FFmpeg call.
"""

import struct
from dataclasses import dataclass
from fractions import Fraction

from vagent.video.contracts import canonical_fingerprint
from vagent.video.mp4 import MP4

SIGNATURE_VERSION = "mp4-copy-signature-v1"
MAX_SAMPLE_RUNS = 4096
MAX_TIMELINE_SAMPLES = 1_000_000

# Boxes that carry no copy-relevant timeline semantics. Anything outside these sets
# in a track or timeline position is refused instead of being ignored.
TOP_LEVEL = {b"ftyp", b"moov", b"mdat", b"free", b"skip", b"wide", b"udta", b"meta"}
MOVIE = {b"mvhd", b"trak", b"udta", b"meta"}
TRACK = {b"tkhd", b"mdia", b"edts", b"udta"}
MEDIA = {b"mdhd", b"hdlr", b"minf", b"elng"}
MEDIA_INFO = {b"vmhd", b"smhd", b"dinf", b"stbl", b"nmhd"}
SAMPLE_TABLE = {b"stsd", b"stts", b"stsc", b"stsz", b"stco", b"co64", b"stss", b"ctts", b"sgpd", b"sbgp"}
VIDEO_ENTRY = {b"avcC", b"pasp", b"colr", b"btrt", b"clap"}
AUDIO_ENTRY = {b"esds", b"btrt", b"wave"}


class SignatureError(ValueError):
    """The file uses a copy structure this version does not explain."""


@dataclass(frozen=True)
class Edit:
    """One edit-list entry; ``media_time`` -1 marks an empty edit."""

    segment_duration: int
    media_time: int
    rate: tuple[int, int]


@dataclass(frozen=True)
class TrackTimeline:
    kind: str
    timescale: int
    movie_timescale: int
    samples: int
    runs: tuple[tuple[int, int], ...]
    duration_ticks: int
    edits: tuple[Edit, ...]
    composition_offsets: str
    sync_samples: int

    @property
    def raw_end(self) -> Fraction:
        return Fraction(self.duration_ticks, self.timescale)

    @property
    def media_time(self) -> int:
        real = [edit for edit in self.edits if edit.media_time >= 0]
        return real[0].media_time if real else 0

    @property
    def empty_seconds(self) -> Fraction:
        empty = sum(edit.segment_duration for edit in self.edits if edit.media_time < 0)
        return Fraction(empty, self.movie_timescale)

    @property
    def frame_ticks(self) -> int | None:
        """The constant per-sample duration, or None when the track is variable."""
        if len(self.runs) != 1:
            return None
        count, delta = self.runs[0]
        return delta if count == self.samples else None

    @property
    def frame_seconds(self) -> Fraction:
        return Fraction(self.frame_ticks or 0, self.timescale)

    @property
    def priming_seconds(self) -> Fraction:
        return Fraction(self.media_time, self.timescale)


@dataclass(frozen=True)
class CopySignature:
    version: str
    video: TrackTimeline
    audio: TrackTimeline | None
    config: dict
    timeline: dict

    @property
    def has_audio(self) -> bool:
        return self.audio is not None

    @property
    def duration_seconds(self) -> Fraction:
        """The segment duration: the longest raw track, priming included."""
        return max([self.video.raw_end, *([self.audio.raw_end] if self.audio else [])])

    @property
    def max_frame_seconds(self) -> Fraction:
        values = [self.video.frame_seconds]
        if self.audio is not None and self.audio.frame_ticks:
            values.append(self.audio.frame_seconds)
        return max(values)

    def config_sha256(self) -> str:
        return canonical_fingerprint(self.config)

    def sha256(self) -> str:
        return canonical_fingerprint({"config": self.config, "timeline": self.timeline})

    def priming(self) -> Fraction:
        """The presentation offset this input contributes at output position zero."""
        return self.video.priming_seconds


def _boxes(mp4: MP4, start: int, end: int, allowed: set[bytes]) -> list:
    boxes = mp4.boxes(start, end)
    unknown = {box.kind for box in boxes} - allowed
    if unknown:
        raise SignatureError("Unknown MP4 structure in a copy-relevant position")
    return boxes


def _only(boxes, kind: bytes, *, required=True):
    matches = [box for box in boxes if box.kind == kind]
    if len(matches) > 1 or (required and not matches):
        raise SignatureError("Missing or repeated required MP4 box")
    return matches[0] if matches else None


def _header(mp4: MP4, box) -> tuple[int, int]:
    value = mp4.read(box.start, 4)
    return value[0], int.from_bytes(value[1:4], "big")


def _timing_runs(mp4: MP4, box) -> tuple[tuple[int, int], ...]:
    version, _ = _header(mp4, box)
    if version != 0:
        raise SignatureError("Unsupported sample timing version")
    value = mp4.data(box, 8)
    count = struct.unpack_from(">I", value, 4)[0]
    if not count or count > MAX_SAMPLE_RUNS or len(value) != 8 + count * 8:
        raise SignatureError("Unsupported sample timing table")
    runs = tuple(struct.iter_unpack(">II", value[8:]))
    if any(not run_count or not delta for run_count, delta in runs):
        raise SignatureError("Empty sample timing run")
    if sum(run_count for run_count, _ in runs) > MAX_TIMELINE_SAMPLES:
        raise SignatureError("Excessive timeline samples")
    return runs


def _edit_list(mp4: MP4, trak) -> tuple[Edit, ...]:
    """One optional leading empty edit plus one real edit; nothing else is explained."""
    edts = _only(_boxes(mp4, trak.start, trak.end, TRACK), b"edts", required=False)
    if edts is None:
        return ()
    elst = _only(_boxes(mp4, edts.start, edts.end, {b"elst"}), b"elst")
    version, _ = _header(mp4, elst)
    value = mp4.data(elst, 8)
    count = struct.unpack_from(">I", value, 4)[0]
    width = 12 if version == 0 else 20
    if not 1 <= count <= 2 or len(value) != 8 + count * width:
        raise SignatureError("Only one empty edit and one real edit are supported")
    edits = []
    for index in range(count):
        base = 8 + index * width
        if version == 0:
            segment_duration, media_time = struct.unpack_from(">Ii", value, base)
            rate = struct.unpack_from(">hh", value, base + 8)
        else:
            segment_duration, media_time = struct.unpack_from(">Qq", value, base)
            rate = struct.unpack_from(">hh", value, base + 16)
        if rate != (1, 0):
            raise SignatureError("Only a 1.0 media rate is supported")
        if segment_duration <= 0 or media_time < -1:
            raise SignatureError("Empty or negative edit list entry")
        edits.append(Edit(segment_duration=segment_duration, media_time=media_time, rate=rate))
    if count == 2 and (edits[0].media_time != -1 or edits[1].media_time < 0):
        raise SignatureError("Only a leading empty edit is explained")
    if any(edit.media_time == -1 for edit in edits[1:]):
        raise SignatureError("An empty edit must be the first entry")
    return tuple(edits)


def _composition(mp4: MP4, tables) -> str:
    box = _only(tables, b"ctts", required=False)
    if box is None:
        return "none"
    version, _ = _header(mp4, box)
    if version not in {0, 1}:
        raise SignatureError("Unsupported composition offset version")
    value = mp4.data(box, 8)
    count = struct.unpack_from(">I", value, 4)[0]
    width = 8 if version == 0 else 12
    if not count or count > MAX_SAMPLE_RUNS or len(value) != 8 + count * width:
        raise SignatureError("Unsupported composition offset table")
    offsets = [
        struct.unpack_from(">I" if version == 0 else ">i", value, 8 + index * width)[0]
        for index in range(count)
    ]
    if any(offset < 0 for offset in offsets):
        raise SignatureError("Negative composition offsets are unsupported")
    return f"ctts-v{version}"


def _sync_samples(mp4: MP4, tables, samples: int) -> int:
    box = _only(tables, b"stss", required=False)
    if box is None:
        return samples
    version, _ = _header(mp4, box)
    if version != 0:
        raise SignatureError("Unsupported sync sample table")
    value = mp4.data(box, 8)
    count = struct.unpack_from(">I", value, 4)[0]
    if not count or count > samples or len(value) != 8 + count * 4:
        raise SignatureError("Unsupported sync sample table")
    entries = [entry[0] for entry in struct.iter_unpack(">I", value[8:])]
    if entries[0] != 1 or entries != sorted(set(entries)) or any(index > samples for index in entries):
        raise SignatureError("Sync samples must start at the first sample")
    return count


def _optional_hex(mp4: MP4, boxes, kind: bytes) -> str | None:
    box = _only(boxes, kind, required=False)
    return None if box is None else mp4.data(box).hex()


def _sample_entry(mp4: MP4, entry, handler: bytes) -> dict:
    if handler == b"vide":
        if entry.kind != b"avc1":
            raise SignatureError("Only AVC video is supported")
        value = mp4.data(entry, 78)
        children = _boxes(mp4, entry.start + 78, entry.end, VIDEO_ENTRY)
        avcc = _only(children, b"avcC")
        return {
            "kind": "avc1",
            "width": struct.unpack_from(">H", value, 24)[0],
            "height": struct.unpack_from(">H", value, 26)[0],
            "horizontalResolution": struct.unpack_from(">I", value, 28)[0],
            "verticalResolution": struct.unpack_from(">I", value, 32)[0],
            "depth": struct.unpack_from(">H", value, 74)[0],
            "avcC": mp4.data(avcc).hex(),
            "pasp": _optional_hex(mp4, children, b"pasp"),
            "colr": _optional_hex(mp4, children, b"colr"),
            "clap": _optional_hex(mp4, children, b"clap"),
        }
    if entry.kind != b"mp4a":
        raise SignatureError("Only AAC audio is supported")
    value = mp4.data(entry, 28)
    children = _boxes(mp4, entry.start + 28, entry.end, AUDIO_ENTRY)
    return {
        "kind": "aac",
        "channels": struct.unpack_from(">H", value, 16)[0],
        "sampleRate": struct.unpack_from(">I", value, 24)[0] >> 16,
        "esds": _optional_hex(mp4, children, b"esds"),
        "wave": _optional_hex(mp4, children, b"wave"),
    }


def _identity_groups(mp4: MP4, tables) -> str:
    """Encoded AAC priming groups decide presentation; their bytes must match."""
    parts = []
    for kind in (b"sgpd", b"sbgp"):
        box = _only(tables, kind, required=False)
        if box is not None:
            parts.append(kind.decode("latin1") + ":" + mp4.data(box).hex())
    return "|".join(parts)


def _track_rotation(mp4: MP4, trak) -> dict:
    tkhd = _only(_boxes(mp4, trak.start, trak.end, TRACK), b"tkhd")
    version, _ = _header(mp4, tkhd)
    value = mp4.data(tkhd, 84 if version == 0 else 96)
    offset = 40 if version == 0 else 52
    matrix = struct.unpack_from(">9i", value, offset)
    return {"matrix": list(matrix), "width": struct.unpack_from(">I", value, offset + 36)[0]}


def _track(mp4: MP4, trak, movie_timescale: int) -> tuple[TrackTimeline, dict]:
    rotation = _track_rotation(mp4, trak)
    mdia = _only(_boxes(mp4, trak.start, trak.end, TRACK), b"mdia")
    mdia_boxes = _boxes(mp4, mdia.start, mdia.end, MEDIA)
    handler = mp4.data(_only(mdia_boxes, b"hdlr"), 12)[8:12]
    if handler not in {b"vide", b"soun"}:
        raise SignatureError("Only one video and at most one audio track are supported")
    timescale, duration = mp4.timing(_only(mdia_boxes, b"mdhd"))
    minf = _only(mdia_boxes, b"minf")
    stbl = _only(_boxes(mp4, minf.start, minf.end, MEDIA_INFO), b"stbl")
    tables = _boxes(mp4, stbl.start, stbl.end, SAMPLE_TABLE)
    stsd = _only(tables, b"stsd")
    header = mp4.read(stsd.start, 8)
    entries = mp4.children(stsd, 8)
    if (
        header[:4] != b"\0\0\0\0"
        or len(entries) != struct.unpack_from(">I", header, 4)[0]
        or len(entries) != 1
    ):
        raise SignatureError("Changing sample descriptions are unsupported")
    runs = _timing_runs(mp4, _only(tables, b"stts"))
    samples = sum(count for count, _ in runs)
    ticks = sum(count * delta for count, delta in runs)
    if abs(ticks - duration) > 1:
        raise SignatureError("Media duration disagrees with the sample timeline")
    edits = _edit_list(mp4, trak)
    media_time = next((edit.media_time for edit in edits if edit.media_time >= 0), 0)
    if media_time >= ticks:
        raise SignatureError("Edit list skips the whole track")
    timeline = TrackTimeline(
        kind="video" if handler == b"vide" else "audio",
        timescale=timescale,
        movie_timescale=movie_timescale,
        samples=samples,
        runs=runs,
        duration_ticks=ticks,
        edits=edits,
        composition_offsets=_composition(mp4, tables),
        sync_samples=_sync_samples(mp4, tables, samples),
    )
    config = {
        "handler": handler.decode("latin1"),
        **_sample_entry(mp4, entries[0], handler),
        "timescale": timescale,
        "identityGroups": _identity_groups(mp4, tables),
        "track": rotation,
    }
    return timeline, config


def read_copy_signature(file, size: int) -> CopySignature:
    """Parse one file into its shared copy configuration plus its own timeline."""
    mp4 = MP4(file, size)
    top = _boxes(mp4, 0, size, TOP_LEVEL)
    ftyp = mp4.data(_only(top, b"ftyp"))
    moov = _only(top, b"moov")
    movie = _boxes(mp4, moov.start, moov.end, MOVIE)
    movie_timescale, _ = mp4.timing(_only(movie, b"mvhd"))
    tracks = [box for box in movie if box.kind == b"trak"]
    if len(tracks) not in {1, 2}:
        raise SignatureError("Expected one video track and at most one audio track")
    parsed = [_track(mp4, track, movie_timescale) for track in tracks]
    videos = [item for item in parsed if item[0].kind == "video"]
    audios = [item for item in parsed if item[0].kind == "audio"]
    if len(videos) != 1 or len(audios) > 1:
        raise SignatureError("Expected one video track and at most one audio track")
    video, video_config = videos[0]
    audio = audios[0][0] if audios else None
    config = {
        "version": SIGNATURE_VERSION,
        "ftyp": ftyp.hex(),
        "video": video_config,
        "audio": audios[0][1] if audios else None,
    }
    timeline = {
        "version": SIGNATURE_VERSION,
        "video": _timeline_record(video),
        "audio": _timeline_record(audio) if audio is not None else None,
    }
    return CopySignature(
        version=SIGNATURE_VERSION,
        video=video,
        audio=audio,
        config=config,
        timeline=timeline,
    )


def _timeline_record(track: TrackTimeline | None) -> dict | None:
    if track is None:
        return None
    return {
        "timescale": track.timescale,
        "movieTimescale": track.movie_timescale,
        "samples": track.samples,
        "runs": [list(run) for run in track.runs],
        "durationTicks": track.duration_ticks,
        "edits": [
            {"segmentDuration": edit.segment_duration, "mediaTime": edit.media_time, "rate": list(edit.rate)}
            for edit in track.edits
        ],
        "compositionOffsets": track.composition_offsets,
        "syncSamples": track.sync_samples,
    }


def compatibility(signatures) -> tuple[dict, str]:
    """Require identical copy configuration and explain the first difference."""
    signatures = list(signatures)
    if not signatures:
        raise SignatureError("No inputs to compare")
    expected = signatures[0].config
    for index, signature in enumerate(signatures[1:], start=2):
        difference = _difference(expected, signature.config, "")
        if difference:
            raise SignatureError(f"输入 {index} 的编码配置不一致：{difference}")
    configs = [signature.config_sha256() for signature in signatures]
    joint = {"version": SIGNATURE_VERSION, "configs": configs}
    return joint, canonical_fingerprint(joint)


def _difference(expected, observed, path: str) -> str | None:
    if isinstance(expected, dict) and isinstance(observed, dict):
        if set(expected) != set(observed):
            return f"{path or 'config'} 字段集合不同"
        for key in expected:
            found = _difference(expected[key], observed[key], f"{path}.{key}".lstrip("."))
            if found:
                return found
        return None
    return None if expected == observed else (path or "config")
