"""
Minimal, tolerant MP4 (ISO base media file format) box reader.

Copyright (C) 2026 Christer R. (sssreh)

SPDX-License-Identifier: GPL-3.0-or-later

Used as a fallback when ffprobe refuses to open a file because one
track's sample tables are malformed - a real-world quirk on some
dashcam recordings, especially audio-less parking-mode clips: the
firmware sometimes still writes an empty/broken audio "trak" that
trips ffmpeg's stricter container validation, even though the video
track itself is intact.

This reader never validates anything. It only walks the box
structure using each box's own declared size (so a semantically
broken table inside one box doesn't stop it from skipping past that
box to its siblings), and reads a handful of fixed, well-defined
fields directly:

- moov/mvhd: overall movie duration and timescale.
- moov/trak/mdia/hdlr: which track is the video track ('vide').
- that track's moov/trak/tkhd: width/height.
- that track's moov/trak/mdia/minf/stbl/stsz: sample (frame) count.
- that track's moov/trak/mdia/minf/stbl/stsd: codec (H.264/HEVC).

It never looks at the audio track's contents at all.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .media import MediaToolError


@dataclass(frozen=True)
class Mp4Info:
    """What could be read directly from the container's box structure."""

    duration_seconds: float
    frame_count: int | None
    width: int | None = None
    height: int | None = None
    # Normalized ("h264"/"hevc") from the video track's stsd sample
    # entry fourcc - see _parse_stsd_codec(). None if the fourcc isn't
    # one this reader recognizes, or no stsd was found - same
    # "non-fatal, caller decides" treatment as width/height above.
    codec: str | None = None


def _read_box_header(
    f, pos: int, end: int
) -> tuple[str, int, int] | None:
    """Return (box_type, payload_start, box_end) for the box at pos.

    Reads from an open binary file handle so callers never have to
    load an entire (potentially huge) file into memory just to walk
    its top-level boxes.
    """

    if pos + 8 > end:
        return None

    f.seek(pos)
    header = f.read(8)

    if len(header) < 8:
        return None

    size = int.from_bytes(header[0:4], "big")
    box_type = header[4:8].decode("latin-1", errors="replace")
    payload_start = pos + 8

    if size == 1:
        if pos + 16 > end:
            return None

        f.seek(pos + 8)
        large = f.read(8)

        if len(large) < 8:
            return None

        size = int.from_bytes(large, "big")
        payload_start = pos + 16
    elif size == 0:
        size = end - pos

    if size < (payload_start - pos):
        return None

    return box_type, payload_start, min(pos + size, end)


def _find_top_level_box(path: Path, box_type: str) -> tuple[int, int] | None:
    """Return (payload_start, payload_end) of the first top-level box
    of box_type, without reading the file's other top-level boxes
    (e.g. a multi-gigabyte 'mdat') into memory."""

    end = path.stat().st_size

    with path.open("rb") as f:
        pos = 0

        while pos < end:
            header = _read_box_header(f, pos, end)

            if header is None:
                return None

            found_type, payload_start, box_end = header

            if found_type == box_type:
                return payload_start, box_end

            pos = box_end

    return None


def _iter_boxes(data: bytes, start: int, end: int):
    """Yield (box_type, payload_start, payload_end) within data[start:end]."""

    pos = start

    while pos + 8 <= end:
        size = int.from_bytes(data[pos:pos + 4], "big")
        box_type = data[pos + 4:pos + 8].decode("latin-1", errors="replace")
        header_size = 8

        if size == 1:
            if pos + 16 > end:
                break

            size = int.from_bytes(data[pos + 8:pos + 16], "big")
            header_size = 16
        elif size == 0:
            size = end - pos

        if size < header_size:
            break

        box_end = min(pos + size, end)
        yield box_type, pos + header_size, box_end
        pos = box_end


def _find_box(
    data: bytes, start: int, end: int, box_type: str
) -> tuple[int, int] | None:
    """Return the payload (start, end) of the first direct child box
    of box_type within data[start:end], or None."""

    for found_type, payload_start, payload_end in _iter_boxes(
        data, start, end
    ):
        if found_type == box_type:
            return payload_start, payload_end

    return None


def _parse_mvhd(data: bytes, start: int, end: int) -> tuple[float, int] | None:
    """Return (duration_seconds, timescale) from an mvhd payload."""

    if end - start < 4:
        return None

    version = data[start]

    if version == 1:
        if end - start < 4 + 16 + 4 + 8:
            return None

        timescale = int.from_bytes(data[start + 20:start + 24], "big")
        duration = int.from_bytes(data[start + 24:start + 32], "big")
    else:
        if end - start < 4 + 8 + 4 + 4:
            return None

        timescale = int.from_bytes(data[start + 12:start + 16], "big")
        duration = int.from_bytes(data[start + 16:start + 20], "big")

    if timescale == 0:
        return None

    return duration / timescale, timescale


def _parse_hdlr_type(data: bytes, start: int, end: int) -> str | None:
    """Return the 4-character handler_type from an hdlr payload
    ('vide' for video, 'soun' for audio, ...)."""

    if end - start < 12:
        return None

    return data[start + 8:start + 12].decode("latin-1", errors="replace")


def _parse_stsz_sample_count(data: bytes, start: int, end: int) -> int | None:
    """Return the sample (frame) count from an stsz payload."""

    if end - start < 12:
        return None

    return int.from_bytes(data[start + 8:start + 12], "big")


def _parse_tkhd_dimensions(data: bytes, start: int, end: int) -> tuple[int, int] | None:
    """Return (width, height) from a tkhd payload, rounded to the
    nearest pixel from their stored 16.16 fixed-point form.

    tkhd's version (0 or 1) only changes the size of the earlier
    creation/modification/duration timestamp fields - width and
    height are always the last two 32-bit fields in the payload
    (following a fixed 36-byte transformation matrix), so reading
    from the *end* of the payload works for either version without
    needing to branch on it at all, unlike _parse_mvhd() above.
    """

    if end - start < 8:
        return None

    width_raw = int.from_bytes(data[end - 8:end - 4], "big")
    height_raw = int.from_bytes(data[end - 4:end], "big")
    return round(width_raw / 65536), round(height_raw / 65536)


# Video sample entry fourccs, mapped to the same normalized codec
# names ffprobe's own "codec_name" reports ("h264"/"hevc") - so
# whichever probing path succeeds (ffprobe directly, or this reader as
# its fallback), the caller gets back the same vocabulary either way.
# "avc1"/"avc3" both mean H.264 (they only differ in how parameter
# sets are signaled, in-band vs out-of-band); "hvc1"/"hev1" both mean
# H.265/HEVC, same distinction. Anything else (e.g. "mp4v" for
# MPEG-4 Part 2, seen on some older/lower-res dashcam firmware) isn't
# mapped - callers treat an unrecognized fourcc the same as "unknown".
_CODEC_FOURCC_MAP = {
    "avc1": "h264",
    "avc3": "h264",
    "hvc1": "hevc",
    "hev1": "hevc",
}


def _parse_stsd_codec(data: bytes, start: int, end: int) -> str | None:
    """Return a normalized codec name ("h264"/"hevc") from a video
    track's stsd payload.

    stsd holds one "sample entry" per distinct codec/format the track
    uses (a BlackVue recording only ever has one) - each entry is
    itself box-shaped (a 4-byte size, then a 4-byte type that's the
    actual codec fourcc, e.g. "avc1" or "hvc1"), preceded by 4 bytes
    of stsd's own version/flags and a 4-byte entry_count. That means
    the entries can be walked with _iter_boxes() exactly like any
    other box list, just starting 8 bytes into the payload rather than
    at its very start.
    """

    if end - start < 8:
        return None

    for fourcc, _entry_start, _entry_end in _iter_boxes(data, start + 8, end):
        return _CODEC_FOURCC_MAP.get(fourcc)

    return None


def read_mp4_info(path: Path) -> Mp4Info:
    """Read duration (and, if available, video frame count) directly
    from an MP4's box structure, bypassing ffprobe entirely.

    Raises MediaToolError if the file doesn't look like a readable
    MP4, or the boxes needed aren't where expected.
    """

    moov = _find_top_level_box(path, "moov")

    if moov is None:
        raise MediaToolError(f"{path.name}: no moov box found")

    moov_start, moov_end = moov

    with path.open("rb") as f:
        f.seek(moov_start)
        data = f.read(moov_end - moov_start)

    size = len(data)

    mvhd = _find_box(data, 0, size, "mvhd")

    if mvhd is None:
        raise MediaToolError(f"{path.name}: no mvhd box in moov")

    parsed_mvhd = _parse_mvhd(data, *mvhd)

    if parsed_mvhd is None:
        raise MediaToolError(f"{path.name}: could not parse mvhd")

    duration_seconds, _timescale = parsed_mvhd

    frame_count = None
    width = None
    height = None
    codec = None

    for box_type, trak_start, trak_end in _iter_boxes(data, 0, size):
        if box_type != "trak":
            continue

        mdia = _find_box(data, trak_start, trak_end, "mdia")
        if mdia is None:
            continue

        hdlr = _find_box(data, *mdia, "hdlr")
        if hdlr is None:
            continue

        if _parse_hdlr_type(data, *hdlr) != "vide":
            continue

        # tkhd (track header - width/height) is a direct sibling of
        # mdia within this same trak, not something under mdia
        # itself. Missing/unparseable is non-fatal, same as a missing
        # stsz below - callers (e.g. parking_transition.py's
        # probe_video_properties()) decide for themselves whether
        # width/height being None still gives them enough to work
        # with.
        tkhd = _find_box(data, trak_start, trak_end, "tkhd")
        if tkhd is not None:
            dimensions = _parse_tkhd_dimensions(data, *tkhd)
            if dimensions is not None:
                width, height = dimensions

        minf = _find_box(data, *mdia, "minf")
        if minf is None:
            continue

        stbl = _find_box(data, *minf, "stbl")
        if stbl is None:
            continue

        # stsd (codec identification) is a sibling of stsz within the
        # same stbl, not dependent on stsz existing - read it even if
        # stsz turns out to be missing/unreadable below, same
        # "independent, both non-fatal" treatment tkhd already gets
        # above relative to mdia.
        stsd = _find_box(data, *stbl, "stsd")
        if stsd is not None:
            codec = _parse_stsd_codec(data, *stsd)

        stsz = _find_box(data, *stbl, "stsz")
        if stsz is None:
            continue

        frame_count = _parse_stsz_sample_count(data, *stsz)
        break  # first video track found is enough

    return Mp4Info(
        duration_seconds=duration_seconds,
        frame_count=frame_count,
        width=width,
        height=height,
        codec=codec,
    )
