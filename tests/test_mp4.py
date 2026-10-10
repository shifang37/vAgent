import io
import struct

import pytest
from media_support import SAMPLE

from vagent.video.mp4 import MP4, inspect_mp4


def track_boxes(data, index=0):
    parser = MP4(io.BytesIO(data), len(data))
    movie = parser.one(parser.boxes(0, len(data)), b"moov")
    track = [box for box in parser.children(movie) if box.kind == b"trak"][index]
    mdia = parser.one(parser.children(track), b"mdia")
    minf = parser.one(parser.children(mdia), b"minf")
    stbl = parser.one(parser.children(minf), b"stbl")
    return parser, mdia, minf, parser.children(stbl)


def test_fixture_contains_measured_avc_audio_and_sample_ranges():
    data = SAMPLE.read_bytes()
    metadata = inspect_mp4(io.BytesIO(data), len(data))
    assert metadata.model_dump() == {
        "width": 1280,
        "height": 720,
        "duration_seconds": 5.0,
        "video_codec": "h264",
        "has_audio": True,
        "audio_codec": "aac",
        "frame_rate": {"numerator": 10, "denominator": 1},
    }


@pytest.mark.parametrize(
    "mutation",
    [
        "truncated",
        "header_only",
        "oversized_box",
        "short_box",
        "fragmented",
        "empty_samples",
        "outside_video",
        "outside_audio",
        "external_reference",
        "invalid_chunk_map",
        "wrong_codec",
        "missing_avcc",
        "zero_duration",
    ],
)
def test_invalid_or_unsupported_mp4_cannot_pass_validation(mutation):
    data = bytearray(SAMPLE.read_bytes())
    parser, mdia, minf, tables = track_boxes(data, index=1 if mutation == "outside_audio" else 0)
    if mutation == "truncated":
        data = data[:-100]
    elif mutation == "header_only":
        data = data[:32]
    elif mutation == "oversized_box":
        struct.pack_into(">I", data, 0, len(data) + 1)
    elif mutation == "short_box":
        struct.pack_into(">I", data, 0, 4)
    elif mutation == "fragmented":
        data.extend(struct.pack(">I4s", 8, b"moof"))
    elif mutation == "empty_samples":
        box = parser.one(tables, b"stsz")
        struct.pack_into(">I", data, box.start + 8, 0)
    elif mutation in {"outside_video", "outside_audio"}:
        box = parser.one(tables, b"stco")
        struct.pack_into(">I", data, box.start + 8, len(data) - 1)
    elif mutation == "external_reference":
        dinf = parser.one(parser.children(minf), b"dinf")
        dref = parser.one(parser.children(dinf), b"dref")
        entry = parser.children(dref, 8)[0]
        data[entry.start : entry.start + 4] = b"\0\0\0\0"
    elif mutation == "invalid_chunk_map":
        box = parser.one(tables, b"stsc")
        struct.pack_into(">I", data, box.start + 12, 0)
    elif mutation in {"wrong_codec", "missing_avcc"}:
        entry = parser.children(parser.one(tables, b"stsd"), 8)[0]
        if mutation == "wrong_codec":
            data[entry.start - 4 : entry.start] = b"hvc1"
        else:
            config = parser.one(parser.children(entry, 78), b"avcC")
            data[config.start - 4 : config.start] = b"free"
    elif mutation == "zero_duration":
        box = parser.one(parser.children(mdia), b"mdhd")
        struct.pack_into(">I", data, box.start + 16, 0)
    with pytest.raises(ValueError):
        inspect_mp4(io.BytesIO(data), len(data))


@pytest.mark.parametrize("extended", [False, True])
def test_final_zero_sized_and_extended_64_bit_mdat(extended):
    data = bytearray(SAMPLE.read_bytes())
    parser = MP4(io.BytesIO(data), len(data))
    mdat = parser.one(parser.boxes(0, len(data)), b"mdat")
    assert mdat.end == len(data)
    if not extended:
        struct.pack_into(">I", data, mdat.start - 8, 0)
    else:
        for index in range(2):
            track_parser, _, _, tables = track_boxes(data, index)
            offsets = track_parser.one(tables, b"stco")
            for i, (offset,) in enumerate(track_parser.table(offsets)):
                struct.pack_into(">I", data, offsets.start + 8 + i * 4, offset + 8)
        start = mdat.start - 8
        data[start : mdat.start] = struct.pack(">I4sQ", 1, b"mdat", mdat.end - start + 8)
    assert inspect_mp4(io.BytesIO(data), len(data)).duration_seconds == 5.0
