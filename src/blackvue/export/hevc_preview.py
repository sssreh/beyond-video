"""
Transcode HEVC/H.265 recordings into a browser-playable H.264 preview
copy, cached on disk, for bv-web's video player.

Copyright (C) 2026 Christer R. (sssreh)

SPDX-License-Identifier: GPL-3.0-or-later

Background: Christer reported a recording that played audio only, no
picture, in bv-web (see WORKING_CONTEXT.md, task #704). He ran ffprobe
against the file himself and confirmed codec_name=hevc. HEVC/H.265 is
a licensing-encumbered codec that Chrome and Firefox never decode in
their built-in <video> element, regardless of what codec packs are
installed at the OS level (Edge can, via the free Microsoft Store
"HEVC Video Extensions" package; Safari has native support) - the
browser still plays the file's AAC audio track fine, which is exactly
the "sound only, no picture" symptom he saw. Only a handful of
Christer's early recordings from when the camera was new are affected
(he was experimenting with HEVC, expecting smaller files, and switched
back once it didn't pan out) - most of his archive is already H.264.

This module follows the exact same load-if-cached-else-transcode-and-
cache pattern this codebase already uses for repairing Parking-mode
containers (mp4_repair.load_or_repair_parking_video()), fetching OSM
road data (osm_roads.load_or_fetch_roads()), reverse-geocoding
(geocoding.load_or_reverse_geocode()), and computing a recording's
real duration (generate.media.load_or_compute_duration()). Like the
Parking-repair cache, this preview cache lives entirely outside the
archive/Asset-registry system - it's a derived, disposable artifact,
not something bv-generate/bv-scribe or bv-export ever need to know
about.
"""

from __future__ import annotations

import hashlib
import os
import sys
import time
import uuid
from pathlib import Path

from ..generate.cache_utils import enforce_cache_size_cap
from ..generate.media import MediaToolError
from ..generate.media import probe_video_codec
from .media import encode_with_nvenc_fallback

_HEVC_CODEC_NAMES = {"hevc", "h265"}

# Enforced via enforce_cache_size_cap() right after every new cache
# write - see that function's own module docstring for the eviction
# policy itself (LRU by mtime, opportunistic, .tmp-safe). 5GiB is
# generous for "a handful of early recordings from when the camera
# was new" (see this module's own background paragraph - only a small
# fraction of Christer's archive is even HEVC to begin with), while
# still being a real, enforced bound rather than the "never evicted,
# clear it by hand" posture this cache had before Christer reported
# it "still needed to be purged after a while" even after the bitrate
# cap shrank individual previews to ~10% of their prior size.
_MAX_CACHE_BYTES = 5 * 1024 ** 3

# This preview exists purely so a browser can decode something - it's
# never the file Christer actually watches for real, archival-quality
# review (that's still the original recording, or a bv-export stitch).
# So unlike every other encode_with_nvenc_fallback() caller in this
# codebase, it deliberately does NOT use the shared default CQ/CRF 19
# quality target: on a real 4K HEVC recording, that target alone blew
# a 189MB HEVC source up to a 511MB H.264 preview (H.264 needs a
# significantly higher bitrate than HEVC for equivalent quality, on
# top of CQ 19 already being tuned for archival fidelity, not preview
# size) - a real problem given these caches are never evicted (see
# WORKING_CONTEXT.md, "Note: no eviction/size cap..."). Capping to a
# fixed target bitrate instead (Christer's explicit choice: prioritize
# smaller files) keeps 4K previews clearly watchable while landing far
# closer to the source's own size. -b:v/-maxrate/-bufsize all set to
# the same value is the same "cap it for real, not just target it on
# average" pattern stitch.py's own _bitrate_args() already uses for
# --stitch-bitrate.
_PREVIEW_TARGET_BITRATE = "8M"

# Christer asked to speed up the wait for a HEVC preview to finish
# transcoding (a Chrome/Firefox compatibility copy only - never what
# he reviews footage on for real, see this module's background
# paragraph).
#
# First tried "fast" - Christer reported no visible difference, and
# separately mentioned a concurrent bv-generate run that could have
# masked any effect through CPU contention alone. Wrongly concluded
# from that (plus bv-web's NAS deployment having no GPU passthrough in
# docker-compose.yml) that NVENC must be unreachable, and switched to
# libx264's fastest preset, "ultrafast" - but Christer's actual test
# was bv-web running natively on his own PC (real NVIDIA GPU, see
# stitch.py's own NVDEC/RTX 5090 references), not the NAS deployment
# assumed above.
#
# That WAS a real regression: encode_with_nvenc_fallback() applies
# extra_codec_args identically to whichever encoder it tries (NVENC
# first, libx264 fallback), so the preset name has to be one both
# accept. "ultrafast" isn't a valid h264_nvenc preset at all - ffmpeg
# rejects it outright ("Unable to parse option value") - so on a
# machine where NVENC is actually available, that NVENC attempt would
# fail immediately and silently fall through to CPU libx264 every
# time, never touching the GPU. Confirmed directly: `ffmpeg ... -c:v
# h264_nvenc -preset ultrafast ...` errors on the option itself, while
# `-preset fast` is accepted (only fails afterward for lack of a GPU
# in this sandbox - a different, later failure mode). "fast" is the
# one preset name genuinely valid for both encoders - not NVENC's
# fastest tier (that's p1; "fast" maps to NVENC's "hp 1 pass"), but a
# real improvement over no preset at all (NVENC's own unset default is
# roughly p4/"medium"), and it actually lets NVENC run instead of
# silently falling back to CPU.
_PREVIEW_PRESET = "fast"


def load_or_transcode_hevc_preview(source: Path, cache_dir: Path) -> Path:
    """Return a path to a browser-playable copy of `source`, using
    NVENC hardware encoding (falling back to libx264 - see
    encode_with_nvenc_fallback()) to transcode into `cache_dir` on
    first use and reusing that copy on every later call.

    Returns `source` itself, unchanged, doing no ffmpeg work at all,
    for anything that isn't HEVC/H.265 in the first place (the normal
    case for the bulk of Christer's archive), or if the codec probe
    itself fails for any reason (a missing/corrupt file, ffprobe not
    installed) - callers should serve whatever path comes back exactly
    the same way either way, falling back to the original file rather
    than raising is the same "never make an already-broken situation
    worse" posture load_or_repair_parking_video() takes.

    The cache file name is derived from `source`'s own resolved path
    plus its mtime and size (not just its filename), so a re-
    downloaded or re-encoded recording never serves a stale preview.
    Bounded to `_MAX_CACHE_BYTES` total, via `enforce_cache_size_cap()`
    right after every new entry is written - see that function's own
    docstring for the eviction policy (oldest-by-mtime first).

    The audio track is copied through as-is (`-c:a copy`) rather than
    re-encoded - it's already AAC, which every target browser already
    decodes fine, so only the video needs re-encoding.

    Also passes `-movflags +faststart`, moving the `moov` atom (the
    file's sample-table metadata) to the front of the output instead
    of ffmpeg's default of leaving it at the end. Christer's first
    real preview (a 515MB, ~200s 4K clip) confirmed valid via ffprobe
    (`codec_name=h264`) but still played audio-only in Chrome - the
    classic symptom of a browser's <video> element being unable to
    reliably locate a moov atom parked at the very end of a large
    file via range requests alone. No other caller of
    encode_with_nvenc_fallback() in this codebase has ever needed
    this (their outputs are much smaller/shorter), so it's passed
    here rather than added to that function's own defaults.

    Also caps the encode to `_PREVIEW_TARGET_BITRATE` instead of the
    shared default CQ/CRF 19 quality target every other caller of
    encode_with_nvenc_fallback() uses - see that constant's own
    comment for why (Christer's own numbers: a 189MB HEVC source
    became a 511MB H.264 preview at CQ 19).

    Also passes `-preset _PREVIEW_PRESET` ("fast") to speed up the
    transcode itself - see that constant's own comment for the full
    story (including a since-reverted "ultrafast" attempt that broke
    NVENC on a real GPU by passing it an invalid preset name and
    silently falling back to CPU). "fast" is valid for both NVENC and
    libx264, since extra_codec_args is applied to both attempts
    identically.

    Transcodes into a private per-call temp file inside `cache_dir`,
    then atomically renames it to `cache_path` only once the encode
    has fully finished - never writes directly to `cache_path` itself.
    Christer hit exactly the failure mode this guards against: a
    browser issues several overlapping requests for the same video
    while buffering/seeking, so two requests can both see no cache
    file yet and both start transcoding; without this, they'd race to
    write the same destination and could leave a corrupted or half-
    written file sitting at `cache_path` forever (nothing re-checks an
    existing cache file's validity, so it'd keep getting served,
    audio-only-again, until someone noticed and deleted it by hand -
    which is exactly what he had to do). With the temp-file rename,
    the worst case is wasted duplicate encoding work, never a broken
    cache entry - `cache_path` is always either fully absent or fully
    valid.

    Passes `-f mp4` explicitly for this same reason: the temp file's
    own name ends in `.tmp`, not `.mp4`, and ffmpeg normally infers
    its output *muxer* (container format) from the destination
    filename's extension - given a `.tmp` name it can't guess at all
    and refuses to write anything ("Unable to choose an output format
    ... use a standard extension ... or specify the format manually" -
    exactly the error Christer hit on his very first real HEVC source
    after this temp-file rename landed). `-f mp4` sidesteps that by
    telling ffmpeg the container format directly, regardless of what
    the destination happens to be named.
    """

    try:
        codec = probe_video_codec(source)
    except MediaToolError as exc:
        print(
            f"HEVC preview: codec probe failed for {source.name}, "
            f"serving the original file unchanged: {exc}",
            file=sys.stderr,
        )
        return source

    if codec is None or codec.lower() not in _HEVC_CODEC_NAMES:
        return source

    stat = source.stat()
    digest = hashlib.sha256(str(source.resolve()).encode("utf-8")).hexdigest()[:16]
    cache_path = cache_dir / f"{digest}-{stat.st_mtime_ns}-{stat.st_size}.mp4"

    if cache_path.is_file():
        print(
            f"HEVC preview: reusing cached preview for {source.name} "
            f"({cache_path.name})",
            file=sys.stderr,
        )
        return cache_path

    print(
        f"HEVC preview: {source.name} is {codec} - transcoding to H.264 "
        f"(this can take a while for a large file, and blocks this request "
        f"until it finishes)...",
        file=sys.stderr,
    )
    cache_dir.mkdir(parents=True, exist_ok=True)
    tmp_path = cache_path.with_name(f"{cache_path.stem}.{uuid.uuid4().hex[:8]}.tmp")
    start = time.monotonic()
    try:
        try:
            encode_with_nvenc_fallback(
                ["-i", str(source)],
                tmp_path,
                extra_codec_args=[
                    "-preset", _PREVIEW_PRESET,
                    "-c:a", "copy",
                    "-movflags", "+faststart",
                    "-b:v", _PREVIEW_TARGET_BITRATE,
                    "-maxrate", _PREVIEW_TARGET_BITRATE,
                    "-bufsize", _PREVIEW_TARGET_BITRATE,
                    "-f", "mp4",
                ],
            )
        except MediaToolError as exc:
            elapsed = time.monotonic() - start
            print(
                f"HEVC preview: transcode failed for {source.name} after "
                f"{elapsed:.1f}s, serving the original (audio-only-playable) "
                f"file unchanged: {exc}",
                file=sys.stderr,
            )
            return source

        os.replace(tmp_path, cache_path)
    finally:
        tmp_path.unlink(missing_ok=True)

    elapsed = time.monotonic() - start
    print(
        f"HEVC preview: transcode finished in {elapsed:.1f}s, cached as "
        f"{cache_path.name}",
        file=sys.stderr,
    )
    enforce_cache_size_cap(cache_dir, _MAX_CACHE_BYTES)
    return cache_path
