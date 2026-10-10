"""Bounded structural validation of self-contained, non-fragmented AVC MP4 files.

This checks sample tables and their actual byte ranges. Decoding remains a separate
browser acceptance check; neither a filename nor an ftyp header proves a video.
"""

import struct
from dataclasses import dataclass
from fractions import Fraction
from typing import BinaryIO

from vagent.video.contracts import FrameRate, MediaMetadata

MAX_TABLE_BYTES = 16 * 1024 * 1024
MAX_BOXES = 100_000


@dataclass(frozen=True)
class Box:
    kind: bytes
    start: int
    end: int


class MP4:
    def __init__(self, file: BinaryIO, size: int):
        self.file, self.size = file, size
        self.box_count = 0

    def read(self, offset: int, length: int) -> bytes:
        if offset < 0 or length < 0 or offset + length > self.size or length > MAX_TABLE_BYTES:
            raise ValueError("Invalid MP4 read boundary")
        self.file.seek(offset)
        value = self.file.read(length)
        if len(value) != length:
            raise ValueError("Truncated MP4")
        return value

    def boxes(self, start: int, end: int) -> list[Box]:
        result = []
        while start < end:
            self.box_count += 1
            if end - start < 8 or self.box_count > MAX_BOXES:
                raise ValueError("Invalid MP4 box table")
            size, kind = struct.unpack(">I4s", self.read(start, 8))
            header = 8
            if size == 1:
                if end - start < 16:
                    raise ValueError("Truncated extended box")
                size = struct.unpack(">Q", self.read(start + 8, 8))[0]
                header = 16
            elif size == 0:
                size = end - start
            if size < header or start + size > end:
                raise ValueError("MP4 box exceeds its parent")
            result.append(Box(kind, start + header, start + size))
            start += size
        return result

    def children(self, box: Box, skip=0) -> list[Box]:
        return self.boxes(box.start + skip, box.end)

    @staticmethod
    def one(boxes: list[Box], kind: bytes) -> Box:
        matches = [box for box in boxes if box.kind == kind]
        if len(matches) != 1:
            raise ValueError("Missing or repeated required MP4 box")
        return matches[0]

    def data(self, box: Box, minimum=0) -> bytes:
        value = self.read(box.start, box.end - box.start)
        if len(value) < minimum:
            raise ValueError("Truncated MP4 fields")
        return value

    def table(self, box: Box, columns=1, width=4) -> list[tuple[int, ...]]:
        value = self.data(box, 8)
        if value[:4] != b"\0\0\0\0":
            raise ValueError("Unsupported sample table version")
        count = struct.unpack_from(">I", value, 4)[0]
        if count == 0 or len(value) != 8 + count * columns * width:
            raise ValueError("Empty or truncated sample table")
        return list(struct.iter_unpack(">" + ("Q" if width == 8 else "I") * columns, value[8:]))

    def timing(self, box: Box) -> tuple[int, int]:
        value = self.data(box, 4)
        if value[1:4] != b"\0\0\0" or value[0] not in {0, 1}:
            raise ValueError("Unsupported media header")
        offset = 12 if value[0] == 0 else 20
        if len(value) < offset + (8 if value[0] == 0 else 12):
            raise ValueError("Truncated media duration")
        timescale, duration = struct.unpack_from(">II" if value[0] == 0 else ">IQ", value, offset)
        if not timescale or not duration or duration == (2 ** (32 if value[0] == 0 else 64) - 1):
            raise ValueError("Missing media duration")
        return timescale, duration

    def sample_sizes(self, boxes: list[Box]) -> list[int]:
        regular = [box for box in boxes if box.kind == b"stsz"]
        compact = [box for box in boxes if box.kind == b"stz2"]
        if len(regular) + len(compact) != 1:
            raise ValueError("Missing sample sizes")
        value = self.data((regular or compact)[0], 12)
        if value[:4] != b"\0\0\0\0":
            raise ValueError("Unsupported sample sizes")
        count = struct.unpack_from(">I", value, 8)[0]
        if not count or count > min(self.size, MAX_TABLE_BYTES // 4):
            raise ValueError("Empty or excessive samples")
        if regular:
            fixed = struct.unpack_from(">I", value, 4)[0]
            if len(value) != 12 + (0 if fixed else 4 * count):
                raise ValueError("Truncated sample sizes")
            sizes = [fixed] * count if fixed else [entry[0] for entry in struct.iter_unpack(">I", value[12:])]
        else:
            bits = value[7]
            if bits not in {4, 8, 16} or len(value) != 12 + (count * bits + 7) // 8:
                raise ValueError("Invalid compact sample sizes")
            if bits == 4:
                sizes = [(value[12 + i // 2] >> (4 if i % 2 == 0 else 0)) & 15 for i in range(count)]
            elif bits == 8:
                sizes = list(value[12:])
            else:
                sizes = [entry[0] for entry in struct.iter_unpack(">H", value[12:])]
        if any(size <= 0 for size in sizes):
            raise ValueError("Empty media sample")
        return sizes

    def descriptions(
        self, stsd: Box, handler: bytes, refs: int
    ) -> tuple[list[bytes], tuple[int, int] | None]:
        header = self.read(stsd.start, 8)
        if header[:4] != b"\0\0\0\0":
            raise ValueError("Unsupported sample descriptions")
        entries = self.children(stsd, 8)
        if not entries or len(entries) != struct.unpack_from(">I", header, 4)[0]:
            raise ValueError("Missing sample descriptions")
        dimensions = None
        codecs = []
        for entry in entries:
            value = self.data(entry, 8)
            reference = struct.unpack_from(">H", value, 6)[0]
            if not 1 <= reference <= refs:
                raise ValueError("External sample reference")
            if handler == b"vide":
                if entry.kind not in {b"avc1", b"avc3"} or len(value) < 78:
                    raise ValueError("Only AVC video is supported")
                current = struct.unpack_from(">HH", value, 24)
                if not all(current) or (dimensions is not None and current != dimensions):
                    raise ValueError("Inconsistent video dimensions")
                dimensions = current
                config = self.data(self.one(self.children(entry, 78), b"avcC"), 7)
                if config[0] != 1 or config[4] & 3 == 2:
                    raise ValueError("Invalid AVC configuration")
                # Both parameter-set lists must be present and fully bounded.
                position, count = 6, config[5] & 31
                for group in range(2):
                    if not count:
                        raise ValueError("Missing AVC parameter sets")
                    for _ in range(count):
                        if position + 2 > len(config):
                            raise ValueError("Truncated AVC parameter sets")
                        length = struct.unpack_from(">H", config, position)[0]
                        position += 2 + length
                        if not length or position > len(config):
                            raise ValueError("Truncated AVC parameter data")
                    if group == 0:
                        if position >= len(config):
                            raise ValueError("Missing AVC picture parameters")
                        count = config[position]
                        position += 1
            else:
                if len(value) < 28 or entry.kind not in {b"mp4a", b"Opus", b"ac-3", b"ec-3", b"alac"}:
                    raise ValueError("Unsupported audio sample description")
                # Versioned QuickTime audio layouts and encrypted entries are unsupported.
                if value[8:10] != b"\0\0":
                    raise ValueError("Unsupported audio layout")
                self.children(entry, 28)
            codecs.append(entry.kind)
        return codecs, dimensions

    def track(self, track: Box, mdats: list[Box]):
        mdia = self.children(self.one(self.children(track), b"mdia"))
        handler = self.data(self.one(mdia, b"hdlr"), 12)[8:12]
        if handler not in {b"vide", b"soun"}:
            raise ValueError("Unsupported media track")
        timescale, duration = self.timing(self.one(mdia, b"mdhd"))
        minf = self.children(self.one(mdia, b"minf"))
        dref = self.one(self.children(self.one(minf, b"dinf")), b"dref")
        refs = self.children(dref, 8)
        header = self.read(dref.start, 8)
        if header[:4] != b"\0\0\0\0" or len(refs) != struct.unpack_from(">I", header, 4)[0] or not refs:
            raise ValueError("Invalid data references")
        if any(ref.kind != b"url " or self.data(ref) != b"\0\0\0\1" for ref in refs):
            raise ValueError("External data references are forbidden")
        tables = self.children(self.one(minf, b"stbl"))
        codecs, dimensions = self.descriptions(self.one(tables, b"stsd"), handler, len(refs))
        sizes = self.sample_sizes(tables)
        timings = self.table(self.one(tables, b"stts"), 2)
        ticks = sum(count * delta for count, delta in timings)
        if any(not count or not delta for count, delta in timings) or sum(x[0] for x in timings) != len(
            sizes
        ):
            raise ValueError("Invalid sample timing")
        if abs(ticks - duration) > 1:
            raise ValueError("Media duration disagrees with samples")
        offsets = [box for box in tables if box.kind in {b"stco", b"co64"}]
        if len(offsets) != 1:
            raise ValueError("Missing chunk offsets")
        chunks = self.table(offsets[0], width=8 if offsets[0].kind == b"co64" else 4)
        mapping = self.table(self.one(tables, b"stsc"), 3)
        if mapping[0][0] != 1 or any(
            first > len(chunks)
            or count == 0
            or not 1 <= description <= len(codecs)
            or (i and first <= mapping[i - 1][0])
            for i, (first, count, description) in enumerate(mapping)
        ):
            raise ValueError("Invalid chunk mapping")
        sample, run, ranges = 0, 0, []
        for index, (offset,) in enumerate(chunks, 1):
            if run + 1 < len(mapping) and index == mapping[run + 1][0]:
                run += 1
            count = mapping[run][1]
            if sample + count > len(sizes):
                raise ValueError("Chunk exceeds sample table")
            end = offset + sum(sizes[sample : sample + count])
            if not any(mdat.start <= offset < end <= mdat.end for mdat in mdats):
                raise ValueError("Sample bytes are outside mdat")
            ranges.append((offset, end))
            sample += count
        if sample != len(sizes):
            raise ValueError("Unaddressed media samples")
        rate = Fraction(len(sizes) * timescale, ticks)
        return handler, dimensions, duration / timescale, rate, codecs, ranges

    def metadata(self) -> MediaMetadata:
        top = self.boxes(0, self.size)
        ftyp = self.data(self.one(top, b"ftyp"), 8)
        if (len(ftyp) - 8) % 4 or any(box.kind == b"moof" for box in top):
            raise ValueError("Unsupported MP4 container")
        movie = self.children(self.one(top, b"moov"))
        if any(box.kind == b"mvex" for box in movie):
            raise ValueError("Fragmented MP4 is unsupported")
        self.timing(self.one(movie, b"mvhd"))
        mdats = [box for box in top if box.kind == b"mdat" and box.end > box.start]
        tracks = [self.track(box, mdats) for box in movie if box.kind == b"trak"]
        videos = [track for track in tracks if track[0] == b"vide"]
        audio = [track for track in tracks if track[0] == b"soun"]
        if len(videos) != 1 or len(audio) > 1 or not mdats:
            raise ValueError("Expected one complete video track")
        ranges = sorted(span for track in tracks for span in track[5])
        if any(end > start for (_, end), (start, _) in zip(ranges, ranges[1:])):
            raise ValueError("Overlapping media samples")
        _, (width, height), duration, rate, _, _ = videos[0]
        audio_codec = None
        if audio:
            kinds = set(audio[0][4])
            if len(kinds) != 1:
                raise ValueError("Changing audio codecs are unsupported")
            kind = next(iter(kinds))
            audio_codec = "aac" if kind == b"mp4a" else kind.decode("ascii").lower()
        return MediaMetadata(
            width=width,
            height=height,
            duration_seconds=duration,
            video_codec="h264",
            has_audio=bool(audio),
            audio_codec=audio_codec,
            frame_rate=FrameRate(numerator=rate.numerator, denominator=rate.denominator),
        )


def inspect_mp4(file: BinaryIO, size: int) -> MediaMetadata:
    return MP4(file, size).metadata()
