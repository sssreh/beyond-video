"""
Shared size-cap eviction for this codebase's on-disk derived-artifact
caches (HEVC preview transcodes, Parking-mode repaired copies, ...).

Copyright (C) 2026 Christer R. (sssreh)

SPDX-License-Identifier: GPL-3.0-or-later

Background: both `export.hevc_preview.load_or_transcode_hevc_preview()`
and `generate.mp4_repair.load_or_repair_parking_video()` cache derived
video copies under `default_config_dir()`, keyed by the source
recording's own digest+mtime+size so a re-downloaded/re-encoded
recording never serves a stale entry - but neither ever removed an
old entry once written (see WORKING_CONTEXT.md, "Note: no eviction/
size cap on the Parking-repair or HEVC-preview caches"). Christer,
after the HEVC-preview bitrate cap brought individual files down to
~10% of their original size but the *cache directory itself* still
"needed to be purged after a while": confirmed he wants an actual
size bound, not just smaller individual files.

`enforce_cache_size_cap()` is a small, generic helper rather than
something baked into either cache module: both caches already follow
the same "check cache_dir, write on miss" shape, so the eviction
policy - sum the directory, and if over the cap, delete least-
recently-written entries until back under it - is identical for both
and shouldn't be implemented twice. Each caller passes its own
`cache_dir`/`max_bytes` (the two caches hold very different kinds of
content - full-length H.264 transcodes vs. byte-for-byte repaired
originals - so a shared *policy* makes sense, but a shared *limit*
would not).

Deliberately LRU by *mtime* (last-written time, since these entries
are never modified after creation - write time and "last touched"
time are the same thing here), not last-*accessed* time: this
codebase never touches `st_atime`, and relying on it would require
every read path (bv-web's `FileResponse`) to actually update it,
which isn't guaranteed across platforms/mount options (many real
deployments mount with `noatime` for performance, which would make
atime-based eviction silently do nothing). mtime-based "oldest
entry first" is a reasonable proxy in practice: an HEVC preview or
repaired copy that keeps getting requested belongs to a recording
Christer keeps coming back to, but a brand new entry is unlikely to
already need eviction, and an old, never-revisited one is exactly
the kind of entry a size cap should reclaim first.

Deliberately opportunistic - called once, right after each cache
module writes a fresh entry - rather than running on a schedule or a
background thread, matching every other "check/fix at the point of
use" pattern already in this codebase (self-healing `.duration.txt`,
sidecar probing, etc.). The worst case of only checking after a write
is a cache sitting slightly over its cap between writes, which is
harmless; a background sweep would be one more moving part (thread
lifecycle, shutdown handling) for a home-scale problem that doesn't
need one.

Explicitly skips any file ending in `.tmp` - both cache modules that
use a private per-call temp file before an atomic rename
(`hevc_preview.py`'s own concurrent-write-race fix) name it with a
`.tmp` final suffix specifically so this sweep - which could run
concurrently with another request's in-flight transcode - never
deletes out from under a write that hasn't finished yet. A cache
module that doesn't use a temp file at all (mp4_repair.py, as of this
writing) is unaffected either way, since it never produces a `.tmp`
file for this to see.
"""

from __future__ import annotations

import os
from pathlib import Path


def enforce_cache_size_cap(cache_dir: Path, max_bytes: int) -> None:
    """If the files directly inside `cache_dir` (skipping the `.tmp`
    files an in-progress write may have left there - see this
    module's own docstring) together exceed `max_bytes`, delete the
    least-recently-written ones until the total is back at or under
    the cap.

    Does nothing if `cache_dir` doesn't exist yet (nothing to enforce
    for a cache that's never been written to) or is already at/under
    the cap.

    Best-effort: a file that fails to delete (e.g. a transient
    Windows file lock - see `trip_export.py`'s own precedent for why
    that's a real, not theoretical, concern on Christer's machine) is
    skipped rather than raising, so one locked file can't block
    reclaiming space from every other evictable entry. The caller
    (already past its own real cache write) shouldn't have its own
    request fail over housekeeping.
    """

    if not cache_dir.is_dir():
        return

    entries: list[tuple[Path, os.stat_result]] = []
    for path in cache_dir.iterdir():
        if path.suffix == ".tmp" or not path.is_file():
            continue
        try:
            stat = path.stat()
        except OSError:
            # Vanished between the is_file() check and stat()-ing it -
            # another racing eviction sweep, or the file simply isn't
            # there anymore. Either way, nothing left here to evict.
            continue
        entries.append((path, stat))

    total_bytes = sum(stat.st_size for _path, stat in entries)
    if total_bytes <= max_bytes:
        return

    entries.sort(key=lambda entry: entry[1].st_mtime_ns)

    for path, stat in entries:
        if total_bytes <= max_bytes:
            break
        try:
            path.unlink()
        except OSError:
            continue
        total_bytes -= stat.st_size
