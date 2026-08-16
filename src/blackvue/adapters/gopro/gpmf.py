"""
GPMF (GoPro Metadata Format) parser - locates and decodes the
telemetry stream embedded directly in a GoPro video file, with no
ffprobe/ffmpeg dependency and no sidecar file.

Two layers:

1. MP4 sample-table parsing (`locate_gpmf_stream()`): a GoPro .MP4 has
   an extra track, alongside its video and audio tracks, whose sample
   description fourcc is 'gpmd' - GPMF's raw KLV byte stream, one
   sample per roughly one second of recording. Locating and reading it
   means walking the same stsd/stsz/stsc/stco(64) sample-table boxes
   any MP4 demuxer would, which this module does directly rather than
   shelling out to ffmpeg: an earlier attempt to test this by muxing a
   synthetic 'gpmd' stream via ffmpeg 4.4.2 failed outright ("Tag gpmd
   incompatible with output codec id '0'"), and no MP4Box/gpac
   alternative was available either - reusing
   generate/mp4_box_reader.py's own box-walking helpers (its existing
   precedent for exactly this "don't trust ffprobe/ffmpeg to handle
   every real-world MP4 shape" situation, already reused the same way
   by generate/mp4_repair.py) turned out to be both more testable
   (pure Python, no external tool needed for synthetic fixtures) and
   more robust than depending on any one ffmpeg build's tag handling.

2. GPMF KLV decoding (`extract_gps_fixes()`/`extract_gsensor_samples()`):
   GPMF is nested Key-Length-Value data - repeated DEVC (device)
   containers, each holding STRM (stream) containers, each holding one
   sensor-data key (GPS5, ACCL, ...) plus sibling metadata keys (SCAL
   = scale factor(s), STMP = stream-relative microsecond timestamp of
   this block's first sample, GPSF = GPS fix type 0/2/3, GPSU = a
   16-byte ASCII UTC anchor "YYMMDDhhmmss.sss" for this block's first
   GPS row). A KLV item's header is 8 bytes: 4-byte ASCII FourCC key,
   1-byte type char ('\\x00' = nested container), 1-byte per-sample
   size, 2-byte big-endian repeat count, then size*repeat bytes of
   payload padded to 4-byte alignment.

Known gaps (see docs/CAMERA_ADAPTERS.md's gopro section):

- GPS9 (Hero11+'s replacement for GPS5, a complex '?'-typed/
  TYPE-string-described format) isn't parsed - only GPS5. A Hero11+
  clip that only wrote GPS9 will read as having no GPS data, the same
  as a clip with GPS lock lost throughout, not as an error.
- GPS5 carries no heading/course field (lat, lon, altitude, speed2d,
  speed3d only) - every GpsFix this module returns has course=None.
- Within-block row timestamps/offsets are linearly interpolated across
  one assumed second per DEVC block (GoPro's real per-block cadence in
  practice), not derived from an explicit per-sample rate - adequate
  for this project's existing GPS-track/g-sensor-variance uses
  (trip building, map rendering, gsensor graph), not a claim of
  sub-second timestamp precision.

Copyright (C) 2026 Christer R. (sssreh)

SPDX-License-Identifier: GPL-3.0-or-later
"""

from __future__ import annotations

import struct
from dataclasses import dataclass
from datetime import datetime
from datetime import timedelta
from pathlib import Path

from ...generate.media import MediaToolError
from ...generate.mp4_box_reader import _find_box
from ...generate.mp4_box_reader import _find_top_level_box
from ...generate.mp4_box_reader import _iter_boxes
from ...generate.mp4_box_reader import _parse_hdlr_type
from ...telemetry.gps_reader import GpsFix
from ...telemetry.gsensor_reader import GSensorSample

# ---------------------------------------------------------------------------
# MP4 sample-table parsing: locate the 'gpmd' track's raw sample bytes.
# ---------------------------------------------------------------------------


def _stsd_fourcc(data: bytes, start: int, end: int) -> str | None:
    """Return the raw fourcc of an stsd payload's first sample entry -
    e.g. 'gpmd' for a GPMF metadata track. Unlike mp4_box_reader.py's
    own _parse_stsd_codec(), this doesn't filter through a
    known-video-codec allowlist - a GPMF track's fourcc isn't a video
    codec at all."""

    if end - start < 8:
        return None
    for fourcc, _entry_start, _entry_end in _iter_boxes(data, start + 8, end):
        return fourcc
    return None


def _parse_stsz(data: bytes, start: int, end: int) -> list[int] | None:
    """Return one entry per sample: its byte size. stsz's payload is
    version/flags(4) + sample_size(4) + sample_count(4), then, only if
    sample_size is 0 (samples vary in size - always true for a GPMF
    track, whose per-block payload size isn't fixed), sample_count
    4-byte per-sample size entries."""

    if end - start < 12:
        return None

    sample_size = int.from_bytes(data[start + 4:start + 8], "big")
    sample_count = int.from_bytes(data[start + 8:start + 12], "big")

    if sample_size != 0:
        return [sample_size] * sample_count

    entries_start = start + 12
    if end - entries_start < sample_count * 4:
        return None

    return [
        int.from_bytes(data[entries_start + i * 4:entries_start + i * 4 + 4], "big")
        for i in range(sample_count)
    ]


def _parse_stsc(data: bytes, start: int, end: int) -> list[tuple[int, int]] | None:
    """Return (first_chunk, samples_per_chunk) per stsc entry - the
    third field (sample_description_index) is irrelevant here, a GPMF
    track only ever has one sample description."""

    if end - start < 8:
        return None

    entry_count = int.from_bytes(data[start + 4:start + 8], "big")
    entries = []
    pos = start + 8

    for _ in range(entry_count):
        if end - pos < 12:
            return None
        first_chunk = int.from_bytes(data[pos:pos + 4], "big")
        samples_per_chunk = int.from_bytes(data[pos + 4:pos + 8], "big")
        entries.append((first_chunk, samples_per_chunk))
        pos += 12

    return entries


def _parse_chunk_offsets(
    data: bytes, start: int, end: int, *, wide: bool
) -> list[int] | None:
    """Return chunk byte offsets from an stco (32-bit, wide=False) or
    co64 (64-bit, wide=True) payload."""

    if end - start < 8:
        return None

    entry_count = int.from_bytes(data[start + 4:start + 8], "big")
    entry_size = 8 if wide else 4
    entries_start = start + 8

    if end - entries_start < entry_count * entry_size:
        return None

    return [
        int.from_bytes(
            data[entries_start + i * entry_size:entries_start + (i + 1) * entry_size],
            "big",
        )
        for i in range(entry_count)
    ]


def _sample_byte_ranges(
    sizes: list[int],
    stsc_entries: list[tuple[int, int]],
    chunk_offsets: list[int],
) -> list[tuple[int, int]]:
    """Standard ISO-BMFF algorithm: expand stsc's (first_chunk,
    samples_per_chunk) entries across the real chunk count (from
    chunk_offsets), then walk chunks in file order assigning
    consecutive stsz sizes to each chunk's samples - the same
    algorithm any MP4 demuxer uses to locate a track's samples,
    implemented directly rather than through a library."""

    ranges: list[tuple[int, int]] = []
    sample_index = 0

    for chunk_number, chunk_offset in enumerate(chunk_offsets, start=1):
        samples_per_chunk = 1
        for i, (first_chunk, spc) in enumerate(stsc_entries):
            next_first_chunk = (
                stsc_entries[i + 1][0] if i + 1 < len(stsc_entries) else None
            )
            if chunk_number >= first_chunk and (
                next_first_chunk is None or chunk_number < next_first_chunk
            ):
                samples_per_chunk = spc
                break

        offset = chunk_offset
        for _ in range(samples_per_chunk):
            if sample_index >= len(sizes):
                break
            size = sizes[sample_index]
            ranges.append((offset, size))
            offset += size
            sample_index += 1

    return ranges


def _find_gpmf_track_boxes(
    data: bytes, moov_start: int, moov_end: int
) -> tuple[int, int] | None:
    """Return (stbl_start, stbl_end) for the first trak whose handler
    type is 'meta' and whose stsd sample entry fourcc is 'gpmd', or
    None if this file has no GPMF track at all - a plain video with no
    embedded telemetry, or a non-video file that happened to match
    video_extensions (see this module's own docstring's known-gaps
    section and adapter.py's per-recording degradation contract)."""

    for box_type, trak_start, trak_end in _iter_boxes(data, moov_start, moov_end):
        if box_type != "trak":
            continue

        mdia = _find_box(data, trak_start, trak_end, "mdia")
        if mdia is None:
            continue

        hdlr = _find_box(data, *mdia, "hdlr")
        if hdlr is None or _parse_hdlr_type(data, *hdlr) != "meta":
            continue

        minf = _find_box(data, *mdia, "minf")
        if minf is None:
            continue

        stbl = _find_box(data, *minf, "stbl")
        if stbl is None:
            continue

        stsd = _find_box(data, *stbl, "stsd")
        if stsd is None or _stsd_fourcc(data, *stsd) != "gpmd":
            continue

        return stbl

    return None


def locate_gpmf_stream(path: Path) -> bytes:
    """Return the concatenated raw GPMF (KLV) byte stream from an MP4's
    'gpmd' metadata track, oldest sample first.

    Raises MediaToolError if this file has no moov box, no GPMF track,
    or its sample tables can't be read - the same "this one file is
    unusable, not the whole scan" contract as every other per-file
    MediaToolError in this project (see adapters/telemetry_bridge.py):
    a caller reading a mixed-content archive should catch this
    per-recording, not let one file with no embedded telemetry abort
    the scan.
    """

    moov = _find_top_level_box(path, "moov")
    if moov is None:
        raise MediaToolError(f"{path.name}: no moov box found")

    moov_start, moov_end = moov

    with path.open("rb") as f:
        f.seek(moov_start)
        data = f.read(moov_end - moov_start)

    stbl = _find_gpmf_track_boxes(data, 0, len(data))
    if stbl is None:
        raise MediaToolError(f"{path.name}: no GPMF ('gpmd') track found")

    stbl_start, stbl_end = stbl

    stsz = _find_box(data, stbl_start, stbl_end, "stsz")
    stsc = _find_box(data, stbl_start, stbl_end, "stsc")
    stco = _find_box(data, stbl_start, stbl_end, "stco")
    co64 = _find_box(data, stbl_start, stbl_end, "co64")

    if stsz is None or stsc is None or (stco is None and co64 is None):
        raise MediaToolError(
            f"{path.name}: GPMF track is missing a required sample table"
        )

    sizes = _parse_stsz(data, *stsz)
    stsc_entries = _parse_stsc(data, *stsc)
    chunk_offsets = (
        _parse_chunk_offsets(data, *co64, wide=True)
        if co64 is not None
        else _parse_chunk_offsets(data, *stco, wide=False)
    )

    if sizes is None or stsc_entries is None or chunk_offsets is None:
        raise MediaToolError(f"{path.name}: could not parse GPMF sample tables")

    ranges = _sample_byte_ranges(sizes, stsc_entries, chunk_offsets)

    chunks = []
    with path.open("rb") as f:
        for offset, size in ranges:
            f.seek(offset)
            chunk = f.read(size)
            if len(chunk) != size:
                raise MediaToolError(f"{path.name}: truncated GPMF sample data")
            chunks.append(chunk)

    return b"".join(chunks)


# ---------------------------------------------------------------------------
# GPMF KLV decoding.
# ---------------------------------------------------------------------------

# Maps a GPMF type character to (struct format char, item byte size).
# 'c' (ASCII char array, e.g. GPSU) and '\x00' (nested container) are
# handled separately, never through this table.
_TYPE_FORMAT = {
    "b": ("b", 1), "B": ("B", 1),
    "s": ("h", 2), "S": ("H", 2),
    "l": ("i", 4), "L": ("I", 4),
    "f": ("f", 4),
    "d": ("d", 8),
    "j": ("q", 8), "J": ("Q", 8),
}


@dataclass(frozen=True)
class _KlvItem:
    fourcc: str
    type_char: str
    sample_size: int
    repeat: int
    payload: bytes


def _iter_klv(data: bytes, start: int, end: int):
    """Yield each top-level KLV item within data[start:end] - see
    module docstring for GPMF's KLV shape."""

    pos = start
    while pos + 8 <= end:
        fourcc = data[pos:pos + 4].decode("ascii", errors="replace")
        type_char = chr(data[pos + 4])
        sample_size = data[pos + 5]
        repeat = int.from_bytes(data[pos + 6:pos + 8], "big")
        payload_start = pos + 8
        payload_len = sample_size * repeat
        payload_end = payload_start + payload_len

        if payload_end > end:
            break

        yield _KlvItem(
            fourcc=fourcc,
            type_char=type_char,
            sample_size=sample_size,
            repeat=repeat,
            payload=data[payload_start:payload_end],
        )

        padded_len = payload_len + ((-payload_len) % 4)
        pos = payload_start + padded_len


def _find_klv(items: list[_KlvItem], fourcc: str) -> _KlvItem | None:
    for item in items:
        if item.fourcc == fourcc:
            return item
    return None


def _unpack_rows(item: _KlvItem) -> list[tuple] | None:
    """Unpack a leaf KLV item's payload into `item.repeat` rows, each a
    tuple of `item.sample_size // item_size` numeric fields - e.g.
    GPS5's 20-byte, type 'l' rows unpack to 5 int32 fields each.
    Returns None for a type this reader doesn't recognize (including
    'c'/ASCII, read directly via .payload instead) or a sample_size
    that isn't an exact multiple of the type's item size."""

    fmt = _TYPE_FORMAT.get(item.type_char)
    if fmt is None:
        return None

    struct_char, item_size = fmt
    if item.sample_size % item_size != 0:
        return None

    fields_per_row = item.sample_size // item_size
    row_format = f">{fields_per_row}{struct_char}"

    rows = []
    for i in range(item.repeat):
        row_start = i * item.sample_size
        row_bytes = item.payload[row_start:row_start + item.sample_size]
        if len(row_bytes) != item.sample_size:
            break
        rows.append(struct.unpack(row_format, row_bytes))

    return rows


def _scale_factors(scal: _KlvItem | None, field_count: int) -> list[float]:
    """Return one scale factor per field - SCAL packs each scale as its
    own repeated row (repeat=5, one int32 per row for GPS5's 5 fields;
    repeat=1 for ACCL's single shared factor - see module docstring),
    not one row holding every field at once, so each row's first (and
    only) value is what's wanted here, gathered across all of SCAL's
    rows. Missing/unparseable SCAL falls back to 1.0 (no scaling)
    rather than failing the whole block: a scale factor this reader
    can't make sense of shouldn't be worse than not scaling at all."""

    if scal is None:
        return [1.0] * field_count

    rows = _unpack_rows(scal)
    if not rows:
        return [1.0] * field_count

    values = [float(row[0]) for row in rows]

    if len(values) == 1:
        return values * field_count
    if len(values) == field_count:
        return values
    return [1.0] * field_count


def _parse_gpsu(item: _KlvItem | None) -> datetime | None:
    """Parse GPSU's 'YYMMDDhhmmss.sss' ASCII anchor into a naive UTC
    datetime - see telemetry/gps_reader.py's own module docstring on
    why this project treats naive datetimes as UTC-equivalent
    throughout, so this stays directly comparable to RecordingId's own
    naive timestamps."""

    if item is None:
        return None

    text = item.payload.rstrip(b"\x00").decode("ascii", errors="replace").strip()

    try:
        return datetime.strptime(text, "%y%m%d%H%M%S.%f")
    except ValueError:
        return None


def _iter_devc_blocks(data: bytes):
    """Yield the child KLV items of each top-level DEVC (device)
    container in a GPMF stream."""

    for item in _iter_klv(data, 0, len(data)):
        if item.fourcc == "DEVC":
            yield list(_iter_klv(item.payload, 0, len(item.payload)))


def _iter_strm_blocks(devc_children: list[_KlvItem]):
    """Yield the child KLV items of each STRM (stream) container within
    one already-parsed DEVC block."""

    for item in devc_children:
        if item.fourcc == "STRM":
            yield list(_iter_klv(item.payload, 0, len(item.payload)))


def extract_gps_fixes(data: bytes) -> tuple[GpsFix, ...]:
    """Extract every GPS5 fix from a raw GPMF stream (see
    locate_gpmf_stream()), oldest first - see module docstring's
    known-gaps section for GPS9/course-field/interpolation caveats.

    A block with no GPS5 item, no usable GPSU anchor, or an
    unparseable SCAL/GPS5 payload is skipped, not fatal - real GPMF
    streams routinely have stretches with no GPS lock (the camera just
    stops writing meaningful fixes for those seconds), the same
    per-block tolerance extract_gsensor_samples() applies. Never
    raises on its own - this function only ever sees data
    locate_gpmf_stream() has already validated as a real GPMF byte
    stream.
    """

    fixes: list[GpsFix] = []

    for devc_children in _iter_devc_blocks(data):
        for strm_children in _iter_strm_blocks(devc_children):
            gps5 = _find_klv(strm_children, "GPS5")
            if gps5 is None:
                continue

            anchor = _parse_gpsu(_find_klv(strm_children, "GPSU"))
            if anchor is None:
                continue

            rows = _unpack_rows(gps5)
            if not rows:
                continue

            scales = _scale_factors(_find_klv(strm_children, "SCAL"), 5)

            gpsf_item = _find_klv(strm_children, "GPSF")
            gpsf_rows = _unpack_rows(gpsf_item) if gpsf_item is not None else None
            fix_type = gpsf_rows[0][0] if gpsf_rows else None

            row_count = len(rows)
            for i, row in enumerate(rows):
                lat_raw, lon_raw, _alt_raw, _speed2d_raw, speed3d_raw = row
                timestamp = anchor + timedelta(seconds=i / row_count)

                latitude = lat_raw / scales[0] if scales[0] else None
                longitude = lon_raw / scales[1] if scales[1] else None
                speed3d = speed3d_raw / scales[4] if scales[4] else None
                speed_kmh = speed3d * 3.6 if speed3d is not None else None

                fixes.append(
                    GpsFix(
                        timestamp=timestamp,
                        valid=(fix_type != 0) if fix_type is not None else True,
                        latitude=latitude,
                        longitude=longitude,
                        speed_kmh=speed_kmh,
                        course=None,
                    )
                )

    return tuple(fixes)


def extract_gsensor_samples(data: bytes) -> tuple[GSensorSample, ...]:
    """Extract every ACCL sample from a raw GPMF stream, oldest first.

    ACCL rows are 3 raw (unscaled) int16 values (x, y, z) - deliberately
    NOT run through ACCL's own SCAL factor, matching this project's
    existing telemetry/gsensor_reader.py contract for BlackVue's own
    .3gf format: GSensorSample carries raw values because the physical
    unit isn't confirmed for either camera's raw axis data (see that
    module's own docstring), and everything downstream (movement.py's
    heuristics, the g-sensor overlay/graph renderers) already works
    off relative variance, not a calibrated g-force threshold.

    Each row's `offset` (time since this recording's own start) comes
    from its STRM block's STMP field (GPMF's own microsecond-since-
    stream-start timestamp for that block's first sample), with rows
    within a block spread evenly across the following second - see
    module docstring's known-gaps section.

    A block with no ACCL item or no usable STMP is skipped, not fatal -
    same per-block tolerance extract_gps_fixes() applies.
    """

    samples: list[GSensorSample] = []

    for devc_children in _iter_devc_blocks(data):
        for strm_children in _iter_strm_blocks(devc_children):
            accl = _find_klv(strm_children, "ACCL")
            if accl is None:
                continue

            stmp_item = _find_klv(strm_children, "STMP")
            if stmp_item is None:
                continue

            stmp_rows = _unpack_rows(stmp_item)
            if not stmp_rows:
                continue

            block_start = timedelta(microseconds=stmp_rows[0][0])

            rows = _unpack_rows(accl)
            if not rows:
                continue

            row_count = len(rows)
            for i, row in enumerate(rows):
                x, y, z = row
                offset = block_start + timedelta(seconds=i / row_count)
                samples.append(GSensorSample(offset=offset, x=x, y=y, z=z))

    return tuple(samples)


def read_gps(path: Path) -> tuple[GpsFix, ...]:
    """Read every GPS5 fix embedded in a GoPro video's GPMF stream -
    GoProAdapter.read_gps()'s implementation (see adapter.py). Raises
    MediaToolError under the same conditions as locate_gpmf_stream()."""

    data = locate_gpmf_stream(path)
    return extract_gps_fixes(data)


def read_gsensor(path: Path) -> tuple[GSensorSample, ...]:
    """Read every ACCL sample embedded in a GoPro video's GPMF stream -
    GoProAdapter.read_gsensor()'s implementation (see adapter.py).
    Raises MediaToolError under the same conditions as
    locate_gpmf_stream()."""

    data = locate_gpmf_stream(path)
    return extract_gsensor_samples(data)
