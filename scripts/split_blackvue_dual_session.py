"""
Split a BlackVue MP4 that packs multiple complete recordings into one
file back to back (ftyp/free/mdat/moov, then another full
ftyp/free/mdat/moov, ...) into separate, individually valid MP4 files.

Read-only, no dependencies beyond the standard library (doesn't need
ffmpeg/ffprobe or the beyond-video venv) - same spirit as
check_mp4_recoverability.py and dump_parking_container.py in this same
directory.

Usage:
    python scripts/split_blackvue_dual_session.py "/path/to/20260301_145621_NF.mp4"

Background: while trying to recover a truncated ("moov atom not
found") recording with untrunc, a "healthy" same-camera reference file
kept crashing untrunc with "Found duplicated MOOV Atom" / "multiple
mdats detected", even in read-only analyze mode (-ia). Confirmed via
check_mp4_recoverability.py's own top-level box walk against the
reference file that this isn't corruption at all: the file is two
*complete*, independently valid MP4s concatenated - a full
ftyp/free/mdat/moov session, immediately followed by another full
ftyp/free/mdat/moov session, both under one filename. untrunc (like
most MP4 tools) assumes a file holds exactly one session and chokes on
the second 'ftyp' as if it were garbage instead of a fresh start.

Since each half is independently well-formed on its own, splitting the
file at each 'ftyp' boundary (a byte-exact slice - nothing is parsed
into memory or rewritten, every session is copied through untouched)
produces ordinary single-session MP4s any tool can open cleanly,
suitable as an untrunc reference (or for their own sake - e.g. if
Christer only wants the footage from one half).
"""

import struct
import sys
from pathlib import Path


def read_box_header(f, pos: int, file_size: int):
    """Return (box_type, declared_size) for the box starting at `pos`,
    or None if there isn't a complete 8-byte header left to read."""

    if pos + 8 > file_size:
        return None

    f.seek(pos)
    header = f.read(8)
    if len(header) < 8:
        return None

    size = struct.unpack(">I", header[0:4])[0]
    box_type = header[4:8].decode("latin-1", errors="replace")

    if size == 1:
        if pos + 16 > file_size:
            return None
        f.seek(pos + 8)
        large = f.read(8)
        if len(large) < 8:
            return None
        size = struct.unpack(">Q", large)[0]
    elif size == 0:
        size = file_size - pos

    return box_type, size


def find_session_starts(path: Path) -> list[int]:
    """Return the byte offset of every top-level 'ftyp' box - each one
    marks the start of a new, independent MP4 session packed into this
    file."""

    file_size = path.stat().st_size
    starts = []

    with path.open("rb") as f:
        pos = 0
        while pos < file_size:
            header = read_box_header(f, pos, file_size)
            if header is None:
                break
            box_type, declared_size = header
            if box_type == "ftyp":
                starts.append(pos)
            pos += declared_size if declared_size > 0 else 8

    return starts


def main():
    if len(sys.argv) != 2:
        print(f"usage: python {Path(sys.argv[0]).name} <path-to-mp4>")
        sys.exit(1)

    path = Path(sys.argv[1])
    if not path.exists():
        print(f"File not found: {path}")
        sys.exit(1)

    starts = find_session_starts(path)
    file_size = path.stat().st_size

    if len(starts) <= 1:
        print(
            f"Only found {len(starts)} 'ftyp' box(es) - this file already "
            f"holds a single session (or none at all). Nothing to split."
        )
        return

    print(f"File: {path}")
    print(f"Found {len(starts)} sessions packed into this one file.\n")

    boundaries = starts + [file_size]

    with path.open("rb") as f:
        for i, (start, end) in enumerate(zip(boundaries, boundaries[1:]), start=1):
            out_path = path.with_name(f"{path.stem}_session{i}{path.suffix}")
            length = end - start
            f.seek(start)
            remaining = length
            with out_path.open("wb") as out:
                while remaining > 0:
                    chunk = f.read(min(remaining, 8 * 1024 * 1024))
                    if not chunk:
                        break
                    out.write(chunk)
                    remaining -= len(chunk)
            print(
                f"  Session {i}: bytes {start:,}-{end:,} "
                f"({length / (1024 * 1024):.1f} MB) -> {out_path.name}"
            )

    print(
        "\nEach session file above should now open cleanly (single "
        "ftyp/free/mdat/moov each) - try one as untrunc's reference "
        "against your broken file."
    )


if __name__ == "__main__":
    main()
