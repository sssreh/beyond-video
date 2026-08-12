"""
Repair a BlackVue Parking-mode MP4 whose (empty, unused) audio track
trips ffmpeg's/browsers' strict container validation, so the intact
video track can still be played/probed normally.

Copyright (C) 2026 Christer R. (sssreh)

SPDX-License-Identifier: GPL-3.0-or-later

Background: every Parking-mode (P) recording fails ffprobe/ffmpeg
outright with "contradictionary STSC and STCO" / "error reading
header" (see WORKING_CONTEXT.md, "Correction: the ffprobe failures
aren't per-file corruption, they're a known BlackVue container
quirk"). Other players (VLC, Windows Media Player, MPC) open these
files fine - Chrome/Firefox's built-in <video> decoder was suspected
of hitting the same wall, independent of the recording's 1fps
timelapse rate (see "Parking-mode recordings won't play in the
archive browser").

Confirmed against one of Christer's own real Parking recordings
(2026-08-08, via scripts/dump_parking_container.py): the video track's
sample tables are completely self-consistent (647 samples agreeing
across stsz/stco/stsc/stts). The audio track is an empty stub - zero
samples, zero chunks (stco chunk_count=0) - but still carries a stray
stsc entry (entry_count=1, first_chunk=0), which is exactly what trips
ffmpeg's validation: reproduced directly by building a synthetic file
with those exact numbers and running the real ffprobe binary against
it (fails with the identical "contradictionary STSC and STCO" error;
succeeds once that one audio trak is dropped, reporting the video
stream normally). Parking recordings are already silent timelapses -
bv-generate's own --extract-audio explicitly skips them ("no audio
track worth extracting") - so dropping an already-empty, already-
broken audio track costs nothing real.

This module is deliberately narrow: repair_parking_container() only
drops an audio ('soun') track whose own chunk-offset table (stco/co64)
reports zero chunks - the one specific, now-confirmed pattern. Any
other kind of broken track (a broken *video* track, or an audio track
that actually has samples but is broken some other way) is left
completely untouched, and the function reports back that it found
nothing to fix rather than guessing at a problem it hasn't been shown
real data for yet.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

from .cache_utils import enforce_cache_size_cap
from .mp4_box_reader import _find_box
from .mp4_box_reader import _parse_hdlr_type
from .mp4_box_reader import _read_box_header

# Enforced via enforce_cache_size_cap() right after every new cache
# write - see that function's own module docstring for the eviction
# policy (LRU by mtime, opportunistic, .tmp-safe). Smaller than
# hevc_preview.py's own 5GiB cap: a repaired copy is a near-verbatim
# byte-for-byte copy of its source Parking recording (only the small
# 'moov' box is rewritten - see repair_parking_container()'s own
# docstring), so it never gets any *smaller* than the original the
# way an HEVC-to-H.264 preview does, but Parking recordings are also
# comparatively rare and this cache only grows for the specific
# broken-audio-track shape this module knows how to fix - 2GiB is a
# reasonable bound for that narrower case.
_MAX_CACHE_BYTES = 2 * 1024 ** 3


def _iter_top_level_boxes(path: Path):
    """Yield (box_type, box_start, box_end) for every top-level box in
    the file at `path`, in order.

    Unlike mp4_box_reader.py's _find_top_level_box() (which stops at
    the first match of one type), this walks all of them - repairing
    the container needs to know exactly where 'moov' sits relative to
    'mdat', since 'mdat' (the actual frame data, potentially
    gigabytes) must never be read into memory, moved, or have its
    absolute byte position change.
    """

    file_size = path.stat().st_size

    with path.open("rb") as f:
        pos = 0
        while pos < file_size:
            header = _read_box_header(f, pos, file_size)
            if header is None:
                return
            box_type, _payload_start, box_end = header
            yield box_type, pos, box_end
            pos = box_end


def _iter_boxes_with_start(data: bytes, start: int, end: int):
    """Like mp4_box_reader.py's _iter_boxes(), but also yields each
    child box's own start offset (header included), not just its
    payload bounds - needed here to copy a kept child byte-for-byte
    rather than only read fields out of it."""

    pos = start
    while pos + 8 <= end:
        box_start = pos
        size = int.from_bytes(data[pos:pos + 4], "big")
        box_type = data[pos + 4:pos + 8].decode("latin-1", errors="replace")
        header_size = 8

        if size == 1:
            if pos + 16 > end:
                return
            size = int.from_bytes(data[pos + 8:pos + 16], "big")
            header_size = 16
        elif size == 0:
            size = end - pos

        if size < header_size:
            return

        box_end = min(pos + size, end)
        yield box_type, box_start, pos + header_size, box_end
        pos = box_end


def _chunk_count(data: bytes, start: int, end: int) -> int | None:
    """entry_count from an stco or co64 payload - the same offset for
    both, only the per-entry size (4 vs 8 bytes), which this doesn't
    need to read, differs."""

    if end - start < 8:
        return None
    return int.from_bytes(data[start + 4:start + 8], "big")


def _has_empty_audio_track(data: bytes, trak_start: int, trak_end: int) -> bool:
    """True if this trak is an audio ('soun') track whose stco/co64
    reports zero chunks - the confirmed "empty, broken stub" shape a
    repair should drop. False for anything else, including a trak
    this can't fully inspect - repair only ever acts when it's sure,
    same "non-fatal, caller decides" posture mp4_box_reader.py's own
    field-by-field parsing already uses."""

    mdia = _find_box(data, trak_start, trak_end, "mdia")
    if mdia is None:
        return False

    hdlr = _find_box(data, *mdia, "hdlr")
    if hdlr is None or _parse_hdlr_type(data, *hdlr) != "soun":
        return False

    minf = _find_box(data, *mdia, "minf")
    if minf is None:
        return False

    stbl = _find_box(data, *minf, "stbl")
    if stbl is None:
        return False

    chunk_box = _find_box(data, *stbl, "stco") or _find_box(data, *stbl, "co64")
    if chunk_box is None:
        return False

    return _chunk_count(data, *chunk_box) == 0


def _box(box_type: str, payload: bytes) -> bytes:
    return (8 + len(payload)).to_bytes(4, "big") + box_type.encode("latin-1") + payload


def _pad_moov_to_original_size(new_moov: bytes, old_moov_total_size: int) -> bytes | None:
    """Pad new_moov with a trailing 'free' box (a box type the MP4
    spec defines as always-ignorable filler - the standard, safe way
    to pad) so its total size exactly matches old_moov_total_size.

    Used when 'moov' can't simply shrink - some other box's absolute
    file offset (in practice, always 'mdat': see
    repair_parking_container()'s safe_to_shrink check) would otherwise
    shift, silently invalidating the kept video track's own stco/co64
    entries. Returns None if the size difference is too small (1-7
    bytes) to hold a valid empty 'free' box (its own header alone
    needs 8 bytes) - callers should treat that as "can't safely repair
    this file" rather than risk writing a malformed pad."""

    pad_needed = old_moov_total_size - len(new_moov)
    if pad_needed == 0:
        return new_moov
    if pad_needed < 8:
        return None
    return new_moov + _box("free", b"\x00" * (pad_needed - 8))


def repair_parking_container(source: Path, destination: Path) -> bool:
    """Write a browser/ffmpeg-openable copy of `source` to
    `destination`, by dropping any empty, broken audio track (see
    module docstring) from its 'moov' box. The video track and all of
    'mdat' (the real frame data) are never read into memory, edited,
    or moved - only 'moov' (a few KB to tens of KB, never the
    recording's actual footage) is rewritten, and everything else is
    copied through byte for byte.

    Returns True if a repaired copy was written to `destination`
    (i.e. `source` matched the confirmed empty-audio-track pattern).
    Returns False, writing nothing at all, if it didn't - either
    because `source` has no 'moov' box, or because no trak matched the
    narrow pattern this function knows how to fix (see
    _has_empty_audio_track()'s own docstring on why this doesn't try
    to guess at other kinds of brokenness). Callers should fall back
    to serving `source` unchanged in that case.
    """

    top_level = list(_iter_top_level_boxes(source))

    moov_entry = next((entry for entry in top_level if entry[0] == "moov"), None)
    if moov_entry is None:
        return False

    _box_type, moov_start, moov_end = moov_entry

    with source.open("rb") as f:
        f.seek(moov_start)
        old_moov_bytes = f.read(moov_end - moov_start)

    header_size = 16 if int.from_bytes(old_moov_bytes[0:4], "big") == 1 else 8

    kept_children = []
    dropped_any = False

    for (
        box_type,
        child_box_start,
        child_payload_start,
        child_payload_end,
    ) in _iter_boxes_with_start(old_moov_bytes, header_size, len(old_moov_bytes)):
        if box_type == "trak" and _has_empty_audio_track(
            old_moov_bytes, child_payload_start, child_payload_end
        ):
            dropped_any = True
            continue
        kept_children.append(old_moov_bytes[child_box_start:child_payload_end])

    if not dropped_any:
        return False

    new_moov = _box("moov", b"".join(kept_children))
    old_moov_total_size = moov_end - moov_start

    # Shrinking 'moov' in place is only safe if nothing after it has
    # its absolute file position relied upon - in practice, only
    # 'mdat' (referenced by the kept video track's own stco/co64
    # offsets) matters. If every 'mdat' in the file already sits
    # before 'moov', shrinking can't move any of them; otherwise the
    # new, smaller 'moov' must be padded back out to its original
    # size instead, so every later byte's absolute position - and
    # every offset pointing at it - stays exactly where it was.
    mdat_starts = [
        box_start for box_type, box_start, _box_end in top_level if box_type == "mdat"
    ]
    safe_to_shrink = all(start < moov_start for start in mdat_starts)

    if len(new_moov) < old_moov_total_size and not safe_to_shrink:
        padded = _pad_moov_to_original_size(new_moov, old_moov_total_size)
        if padded is None:
            return False
        new_moov = padded

    file_size = source.stat().st_size

    with source.open("rb") as f:
        f.seek(0)
        before = f.read(moov_start)
        f.seek(moov_end)
        after = f.read(file_size - moov_end)

    destination.write_bytes(before + new_moov + after)
    return True


def load_or_repair_parking_video(source: Path, cache_dir: Path) -> Path:
    """Return a path to a browser/ffmpeg-openable copy of `source`,
    repairing it into `cache_dir` on first use and reusing that copy
    on every later call - the same load-if-cached-else-fetch-and-cache
    pattern this codebase already uses for OSM road data
    (osm_roads.load_or_fetch_roads()), reverse-geocoded addresses
    (geocoding.load_or_reverse_geocode()), and a recording's own real
    duration (media.load_or_compute_duration()).

    Returns `source` itself, unchanged, if repair_parking_container()
    found nothing to fix (see its own docstring) - the natural
    "nothing wrong here" case for any Parking recording whose audio
    track isn't the specific broken shape this module knows how to
    repair. Callers should serve whatever path comes back exactly the
    same way either way; they don't need to know which case they got.

    The cache file name is derived from `source`'s own resolved path
    plus its mtime and size (not just its filename), so: a
    re-downloaded or re-encoded recording (same filename, different
    bytes) never serves a stale repaired copy; and two different
    cameras/archives that happen to produce a recording with the same
    id (an unlikely but not impossible timestamp coincidence) never
    collide in the shared cache. Bounded to `_MAX_CACHE_BYTES` total,
    via `enforce_cache_size_cap()` right after every new entry is
    written - see that function's own docstring for the eviction
    policy (oldest-by-mtime first). The OSM road/geocode caches under
    `.osm_cache` are a separate concern, not covered by this - they're
    keyed by map area/address, not by a specific source recording, so
    the same per-recording eviction policy doesn't directly apply.
    """

    stat = source.stat()
    digest = hashlib.sha256(str(source.resolve()).encode("utf-8")).hexdigest()[:16]
    cache_path = cache_dir / f"{digest}-{stat.st_mtime_ns}-{stat.st_size}.mp4"

    if cache_path.is_file():
        return cache_path

    cache_dir.mkdir(parents=True, exist_ok=True)
    repaired = repair_parking_container(source, cache_path)
    if repaired:
        enforce_cache_size_cap(cache_dir, _MAX_CACHE_BYTES)
    return cache_path if repaired else source
