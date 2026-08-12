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
import sys
from pathlib import Path

from ..generate.media import MediaToolError
from ..generate.media import probe_video_codec
from .media import encode_with_nvenc_fallback

_HEVC_CODEC_NAMES = {"hevc", "h265"}


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
    Never evicted - left for a human to clear manually if it ever
    grows large enough to matter, same as this codebase's other small
    on-disk derived-artifact caches.

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
    try:
        encode_with_nvenc_fallback(
            ["-i", str(source)],
            cache_path,
            extra_codec_args=["-c:a", "copy", "-movflags", "+faststart"],
        )
    except MediaToolError as exc:
        print(
            f"HEVC preview: transcode failed for {source.name}, serving "
            f"the original (audio-only-playable) file unchanged: {exc}",
            file=sys.stderr,
        )
        return source

    print(f"HEVC preview: transcode finished, cached as {cache_path.name}", file=sys.stderr)
    return cache_path
