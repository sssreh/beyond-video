"""
Check whether a "moov atom not found" MP4 is actually recoverable.

Usage (PowerShell):
    python scripts\check_mp4_recoverability.py "x:\files\20260731_173318_NR.mp4"

No dependencies beyond the standard library - doesn't need ffmpeg,
ffprobe, or the beyond-video venv at all. Just walks the file's raw
top-level box structure (the same size-prefixed layout ffprobe itself
reads) and reports what's actually there.

Background: an MP4 is a sequence of boxes. A dashcam recording
normally writes 'ftyp' (file type) first, then 'mdat' (the actual
raw video/audio frame data) as it records, and only writes 'moov'
(the index - which frame lives at which byte offset) once at the very
end, when the recording is properly finalized. If the camera loses
power or is unplugged mid-recording, 'mdat' can be sitting there
complete (or partially complete) but 'moov' never gets written at
all - that's exactly what "moov atom not found" means. The raw frame
data usually isn't gone; ffmpeg just has no index telling it how to
read it.

Whether it's worth attempting recovery (with a tool like untrunc)
depends on what this script reports:

  - mdat present, and its declared size roughly matches the actual
    bytes remaining in the file: the recording data is very likely
    intact, and a moov-reconstruction tool has a real shot at
    rebuilding a working index.
  - mdat present but its declared size is much larger than what's
    actually in the file (fewer bytes on disk than mdat claims): the
    file was cut off mid-write, partway through mdat itself - some
    footage from partway through onward is genuinely gone, but
    whatever came before the cutoff may still be recoverable.
  - no mdat at all, or the file is only a few KB: there's nothing
    meaningful to recover - this is likely just a stub file the
    camera created and never wrote real data into before the failure.

Written for a real case Christer hit on 2026-07-31/08-01: bv-export's
concat step failing with "moov atom not found" on a rear-camera
recording (20260731_173318_NR.mp4) after the camera apparently lost
power mid-write - see WORKING_CONTEXT.md for the full story, including
bv-export's own separate fix (skip an unreadable source with a
warning instead of losing the whole trip's asset) that this script's
findings didn't change the need for.
"""

import struct
import sys
from pathlib import Path


def read_box_header(f, pos: int, file_size: int):
    """Return (box_type, header_size, declared_size) for the box
    starting at `pos`, or None if there isn't a complete 8-byte header
    left to read."""

    if pos + 8 > file_size:
        return None

    f.seek(pos)
    header = f.read(8)
    if len(header) < 8:
        return None

    size = struct.unpack(">I", header[0:4])[0]
    box_type = header[4:8].decode("latin-1", errors="replace")
    header_size = 8

    if size == 1:
        # 64-bit "largesize" follows immediately after the normal header.
        if pos + 16 > file_size:
            return None
        f.seek(pos + 8)
        large = f.read(8)
        if len(large) < 8:
            return None
        size = struct.unpack(">Q", large)[0]
        header_size = 16
    elif size == 0:
        # size == 0 is legal and means "this box runs to end of file" -
        # normal for an mdat still being written when power was lost.
        size = file_size - pos

    return box_type, header_size, size


def main():
    if len(sys.argv) != 2:
        print(f"usage: python {Path(sys.argv[0]).name} <path-to-mp4>")
        sys.exit(1)

    path = Path(sys.argv[1])
    if not path.exists():
        print(f"File not found: {path}")
        sys.exit(1)

    file_size = path.stat().st_size
    print(f"File: {path}")
    print(f"Size: {file_size:,} bytes ({file_size / (1024 * 1024):.1f} MB)\n")

    boxes = []
    with path.open("rb") as f:
        pos = 0
        while pos < file_size:
            header = read_box_header(f, pos, file_size)
            if header is None:
                print(
                    f"[!] Could not read a valid box header at offset "
                    f"{pos:,} - the file is truncated or corrupted at "
                    f"this point, {file_size - pos:,} bytes before EOF."
                )
                break

            box_type, header_size, declared_size = header
            box_end = pos + declared_size
            truncated = box_end > file_size
            boxes.append((box_type, pos, declared_size, truncated))
            pos = min(box_end, file_size)

    if not boxes:
        print("No readable boxes found at all - this file is empty or not an MP4.")
        return

    print("Top-level boxes found:")
    for box_type, offset, declared_size, truncated in boxes:
        flag = "  <-- extends past end of file (truncated here)" if truncated else ""
        print(f"  {box_type!r:8s} offset={offset:>12,}  declared_size={declared_size:>14,}{flag}")

    box_types = [b[0] for b in boxes]
    has_ftyp = "ftyp" in box_types
    has_moov = "moov" in box_types
    mdat_boxes = [b for b in boxes if b[0] == "mdat"]

    print()
    print(f"ftyp present: {has_ftyp}")
    print(f"moov present: {has_moov}" + ("" if not has_moov else " (unexpected - re-check the ffmpeg error)"))

    if not mdat_boxes:
        print("mdat present: False")
        print(
            "\nVerdict: no mdat box at all. There's no real recording "
            "data in this file to recover - not worth pursuing untrunc "
            "or any other repair tool."
        )
        return

    box_type, offset, declared_size, truncated = mdat_boxes[0]
    actual_bytes_available = file_size - offset
    print(f"mdat present: True (offset={offset:,}, declared_size={declared_size:,})")
    print(f"Bytes actually available for mdat: {actual_bytes_available:,}")

    if has_moov:
        print(
            "\nVerdict: moov is actually present - this doesn't match "
            "the 'moov atom not found' error you saw. Worth re-running "
            "ffprobe on this exact file again to double check."
        )
    elif truncated:
        shortfall = declared_size - actual_bytes_available
        print(
            f"\nVerdict: mdat claims to be {shortfall:,} bytes larger "
            f"than what's actually in the file - the recording was cut "
            f"off mid-write, partway through the raw frame data itself. "
            f"Whatever was recorded before the cutoff is a real repair "
            f"candidate; anything after the cutoff point is genuinely "
            f"gone. Worth trying untrunc against a reference file from "
            f"the same camera/settings, but expect the recovered video "
            f"to run shorter than the original."
        )
    elif actual_bytes_available < 100_000:
        print(
            "\nVerdict: mdat is present but tiny (under 100 KB) - "
            "probably only a fraction of a second of real footage, if "
            "any. Likely not worth the effort of a repair attempt."
        )
    else:
        print(
            "\nVerdict: mdat looks complete (its declared size matches "
            "what's actually in the file) and moov is simply missing. "
            "This is the good case - the raw recording data is very "
            "likely intact, just missing its index. A tool like untrunc, "
            "given a healthy reference file from the same camera/codec "
            "settings, has a real shot at rebuilding a working moov and "
            "recovering the full recording."
        )


if __name__ == "__main__":
    main()
