"""
Dump the raw box structure of a BlackVue Parking-mode MP4's sample
tables - a read-only diagnostic, no dependencies beyond the standard
library (doesn't need ffmpeg/ffprobe or the beyond-video venv), in the
same spirit as check_mp4_recoverability.py in this same directory.

Usage:
    python scripts/dump_parking_container.py "/path/to/20260726_144116_PF.mp4"

Background: Christer reported Parking-mode (P) recordings won't play
in bv-web's archive browser. A related, already-diagnosed issue (see
WORKING_CONTEXT.md, "Correction: the ffprobe failures aren't per-file
corruption, they're a known BlackVue container quirk") found that
ffprobe/ffmpeg refuse to open *every* Parking-mode file outright, with
errors like "contradictionary STSC and STCO" / "STSC entry 0 is
invalid" - a strict-validation quirk in ffmpeg's own mov demuxer, not
real file corruption (other players - VLC, Windows Media Player, MPC -
open these files fine). Chrome/Firefox's built-in <video> decoder is
suspected of hitting the same wall, independent of the recording's
1fps timelapse rate.

This codebase's own mp4_box_reader.py module docstring already
theorizes *why*: "the firmware sometimes still writes an empty/broken
audio 'trak' that trips ffmpeg's stricter container validation, even
though the video track itself is intact." If true, the real fix is
narrow and safe: strip (or repair) just the broken audio track's index
tables from the file's 'moov' box, leaving the video track and all of
'mdat' (the actual frame data) completely untouched - Parking
recordings are already silent timelapses with no meaningful audio to
lose (see bv-generate's own "--extract-audio: skipped for Parking-mode
recordings" behavior).

That theory has never been checked against one of Christer's own real
Parking files - this script does exactly that, without needing the
video content itself (just table sizes, safe to paste back as text).
It reports, per track (video vs audio):

  - stsc (sample-to-chunk): entry count, and the last entry's
    first_chunk value - the exact number ffmpeg's own mov.c compares
    against stco/co64's entry count to raise "contradictionary STSC
    and STCO".
  - stco/co64 (chunk offsets): entry count (the "chunk_count" ffmpeg
    compares against).
  - stsz (sample sizes): sample count.
  - stts (time-to-sample): entry count, and whether its sample counts
    sum to the same total stsz reports (a second, independent
    consistency check ffmpeg performs).

If the audio track's numbers are the ones that don't add up while the
video track's do, that confirms the theory and points at exactly what
a repair needs to fix. If it's the reverse (or both), the fix needs to
be different (and more invasive) than "just drop the audio track".
"""

from __future__ import annotations

import struct
import sys
from pathlib import Path


def read_box_header(data: bytes, pos: int, end: int):
    """Return (box_type, payload_start, box_end, header_size) for the
    box at pos within data[0:end], or None if there isn't a complete
    header left to read."""

    if pos + 8 > end:
        return None

    size = struct.unpack(">I", data[pos:pos + 4])[0]
    box_type = data[pos + 4:pos + 8].decode("latin-1", errors="replace")
    header_size = 8

    if size == 1:
        if pos + 16 > end:
            return None
        size = struct.unpack(">Q", data[pos + 8:pos + 16])[0]
        header_size = 16
    elif size == 0:
        size = end - pos

    if size < header_size:
        return None

    return box_type, pos + header_size, min(pos + size, end), header_size


def iter_boxes(data: bytes, start: int, end: int):
    """Yield (box_type, payload_start, payload_end) for each direct
    child box within data[start:end]."""

    pos = start
    while pos < end:
        header = read_box_header(data, pos, end)
        if header is None:
            return
        box_type, payload_start, box_end, _header_size = header
        yield box_type, payload_start, box_end
        pos = box_end


def find_box(data: bytes, start: int, end: int, box_type: str):
    for found_type, payload_start, payload_end in iter_boxes(data, start, end):
        if found_type == box_type:
            return payload_start, payload_end
    return None


def find_top_level_box(path: Path, box_type: str):
    """Return (payload_start, payload_end) of the first top-level box
    of box_type, reading only what's needed rather than the whole
    (potentially huge) file."""

    file_size = path.stat().st_size

    with path.open("rb") as f:
        pos = 0
        while pos < file_size:
            f.seek(pos)
            header_bytes = f.read(16)
            if len(header_bytes) < 8:
                return None

            size = struct.unpack(">I", header_bytes[0:4])[0]
            box_type_found = header_bytes[4:8].decode("latin-1", errors="replace")
            header_size = 8

            if size == 1:
                if len(header_bytes) < 16:
                    return None
                size = struct.unpack(">Q", header_bytes[8:16])[0]
                header_size = 16
            elif size == 0:
                size = file_size - pos

            if size < header_size:
                return None

            payload_start = pos + header_size
            box_end = min(pos + size, file_size)

            if box_type_found == box_type:
                return payload_start, box_end

            pos = box_end

    return None


def parse_hdlr_type(data: bytes, start: int, end: int):
    if end - start < 12:
        return None
    return data[start + 8:start + 12].decode("latin-1", errors="replace")


def parse_stsc(data: bytes, start: int, end: int):
    """Return (entry_count, last_first_chunk) from an stsc payload -
    last_first_chunk is None if entry_count is 0."""

    if end - start < 8:
        return None
    entry_count = struct.unpack(">I", data[start + 4:start + 8])[0]
    if entry_count == 0:
        return entry_count, None
    last_entry_start = start + 8 + (entry_count - 1) * 12
    if last_entry_start + 4 > end:
        return entry_count, "TRUNCATED"
    last_first_chunk = struct.unpack(
        ">I", data[last_entry_start:last_entry_start + 4]
    )[0]
    return entry_count, last_first_chunk


def parse_chunk_count(data: bytes, start: int, end: int):
    """entry_count from an stco or co64 payload - same offset for
    both, only the per-entry size (4 vs 8 bytes) differs, which this
    doesn't need to read."""

    if end - start < 8:
        return None
    return struct.unpack(">I", data[start + 4:start + 8])[0]


def parse_stsz_sample_count(data: bytes, start: int, end: int):
    if end - start < 12:
        return None
    return struct.unpack(">I", data[start + 8:start + 12])[0]


def parse_stts(data: bytes, start: int, end: int):
    """Return (entry_count, total_sample_count) from an stts payload."""

    if end - start < 8:
        return None
    entry_count = struct.unpack(">I", data[start + 4:start + 8])[0]
    total = 0
    for i in range(entry_count):
        entry_start = start + 8 + i * 8
        if entry_start + 4 > end:
            return entry_count, "TRUNCATED"
        total += struct.unpack(">I", data[entry_start:entry_start + 4])[0]
    return entry_count, total


def report_track(data: bytes, trak_start: int, trak_end: int, label: str):
    print(f"\n--- {label} track ---")

    mdia = find_box(data, trak_start, trak_end, "mdia")
    if mdia is None:
        print("  no mdia box - can't inspect further")
        return

    hdlr = find_box(data, *mdia, "hdlr")
    handler = parse_hdlr_type(data, *hdlr) if hdlr else None
    print(f"  handler_type: {handler!r}")

    minf = find_box(data, *mdia, "minf")
    if minf is None:
        print("  no minf box - can't inspect sample tables")
        return

    stbl = find_box(data, *minf, "stbl")
    if stbl is None:
        print("  no stbl box - can't inspect sample tables")
        return

    stsc = find_box(data, *stbl, "stsc")
    if stsc is not None:
        result = parse_stsc(data, *stsc)
        if result is not None:
            entry_count, last_first_chunk = result
            print(f"  stsc: entry_count={entry_count}  last_entry.first_chunk={last_first_chunk}")
        else:
            print("  stsc: present but too short to parse")
    else:
        print("  stsc: MISSING")

    stco = find_box(data, *stbl, "stco")
    co64 = find_box(data, *stbl, "co64")
    if stco is not None:
        chunk_count = parse_chunk_count(data, *stco)
        print(f"  stco: chunk_count={chunk_count}")
    elif co64 is not None:
        chunk_count = parse_chunk_count(data, *co64)
        print(f"  co64: chunk_count={chunk_count}")
    else:
        print("  stco/co64: MISSING")
        chunk_count = None

    stsz = find_box(data, *stbl, "stsz")
    sample_count = None
    if stsz is not None:
        sample_count = parse_stsz_sample_count(data, *stsz)
        print(f"  stsz: sample_count={sample_count}")
    else:
        print("  stsz: MISSING")

    stts = find_box(data, *stbl, "stts")
    if stts is not None:
        result = parse_stts(data, *stts)
        if result is not None:
            entry_count, total = result
            match = "" if total == sample_count else "  <-- MISMATCHES stsz sample_count!"
            print(f"  stts: entry_count={entry_count}  total_sample_count={total}{match}")
        else:
            print("  stts: present but too short to parse")
    else:
        print("  stts: MISSING")

    if isinstance(stsc, tuple):
        result = parse_stsc(data, *stsc) if stsc else None
        if result is not None and chunk_count is not None:
            entry_count, last_first_chunk = result
            if isinstance(last_first_chunk, int) and last_first_chunk > chunk_count:
                print(
                    f"  ==> CONTRADICTION: stsc's last first_chunk "
                    f"({last_first_chunk}) exceeds stco/co64's chunk_count "
                    f"({chunk_count}) - this is ffmpeg's own "
                    f"'contradictionary STSC and STCO' check, right here."
                )


def main():
    if len(sys.argv) != 2:
        print(f"usage: python {Path(sys.argv[0]).name} <path-to-mp4>")
        sys.exit(1)

    path = Path(sys.argv[1])
    if not path.exists():
        print(f"File not found: {path}")
        sys.exit(1)

    print(f"File: {path}")
    print(f"Size: {path.stat().st_size:,} bytes\n")

    moov = find_top_level_box(path, "moov")
    if moov is None:
        print("No moov box found at the top level - can't inspect sample tables.")
        sys.exit(1)

    moov_start, moov_end = moov
    with path.open("rb") as f:
        f.seek(moov_start)
        data = f.read(moov_end - moov_start)

    size = len(data)
    trak_index = 0
    for box_type, trak_start, trak_end in iter_boxes(data, 0, size):
        if box_type != "trak":
            continue
        trak_index += 1
        report_track(data, trak_start, trak_end, f"#{trak_index}")

    if trak_index == 0:
        print("No trak boxes found inside moov.")

    print(
        "\nPaste this whole output back - no video content is included, "
        "just table sizes/counts from the file's index."
    )


if __name__ == "__main__":
    main()
