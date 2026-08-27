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

import asyncio
import contextlib
import hashlib
import os
import subprocess
import sys
import time
import uuid
from collections.abc import AsyncIterator
from pathlib import Path

from ..generate.cache_utils import enforce_cache_size_cap
from ..generate.media import MediaToolError
from ..generate.media import probe_video_codec
from .media import build_ffmpeg_encode_command
from .media import encode_with_nvenc_fallback

_HEVC_CODEC_NAMES = {"hevc", "h265"}

# Cached after the first check (per process) - same pattern as
# media.py's own _NVENC_AVAILABLE and stitch.py's own _NVDEC_AVAILABLE.
# Kept as its own local copy rather than imported from stitch.py -
# these two modules already don't share this kind of small hwaccel
# probe with each other (media.py's _nvenc_available() isn't imported
# by stitch.py either, which has its own separate copy), so this
# follows the same established convention rather than introducing a
# new cross-module dependency for a five-line probe.
_NVDEC_AVAILABLE: bool | None = None


def _nvdec_available() -> bool:
    """Return True if this machine's ffmpeg build lists "cuda" among
    its hwaccels (NVIDIA's hardware video decoder, NVDEC).

    Being listed doesn't guarantee this specific source will actually
    decode via NVDEC (codec/profile support varies) - a failed attempt
    just falls back to plain CPU decode (see load_or_transcode_hevc_
    preview()'s own try/except around the NVDEC attempt), so a wrong
    "True" here costs one failed attempt, not a broken preview.
    """

    global _NVDEC_AVAILABLE

    if _NVDEC_AVAILABLE is None:
        try:
            result = subprocess.run(
                ["ffmpeg", "-hide_banner", "-hwaccels"],
                capture_output=True,
                text=True,
                check=False,
            )
            _NVDEC_AVAILABLE = "cuda" in result.stdout
        except FileNotFoundError:
            _NVDEC_AVAILABLE = False

    return _NVDEC_AVAILABLE


# 2026-08-27: real-hardware report from Christer - plain H.264
# recordings (the vast majority of his archive, never touched by the
# transcode logic below at all) were slow to play via bv-web. Root
# cause: both load_or_transcode_hevc_preview() and open_hevc_preview_
# stream() unconditionally called probe_video_codec() - a fresh
# ffprobe subprocess spawn - at the very top, on *every single call*,
# before either function could even determine the source isn't HEVC
# and bail out to a plain FileResponse. A browser issues many
# overlapping Range requests per video while buffering/seeking (often
# several per second), so scrubbing an H.264 recording meant re-
# spawning ffprobe from scratch for every one of those - and since
# open_hevc_preview_stream() is an async route handler while
# probe_video_codec() calls the blocking subprocess.run() directly
# (not asyncio.create_subprocess_exec()), each of those probes stalled
# bv-web's single-threaded event loop for its full duration, queuing
# up every other concurrent request behind it too - not just slow for
# this one video, but for the whole server while it ran. Same class of
# bug task #425 already fixed once for the archive-browser's own O(N)
# rescan-per-request pattern, just resurfaced in this later HEVC-
# preview code path (task #704+), which never got the same treatment.
#
# Fixed by caching the probe result per (resolved path, mtime_ns,
# size) - same staleness-detection triple already used for this
# module's own cache_path naming a few lines below, so a file
# replaced/re-encoded in place still gets a fresh probe - and by
# running a cache-miss probe via asyncio.to_thread() in the async
# caller so it can't block the event loop even on the very first
# request for a given file. A cache hit (every request after the
# first, for the life of the process) costs a plain dict lookup -
# no subprocess, no thread hop - which is what makes scrubbing fast
# again regardless of codec.
_CODEC_PROBE_CACHE: dict[tuple[str, int, int], str | None] = {}


def _cached_probe_video_codec(source: Path) -> str | None:
    """probe_video_codec(), memoized per (path, mtime, size) for the
    life of this process - see _CODEC_PROBE_CACHE's own comment above.

    Deliberately does NOT cache a MediaToolError (ffprobe missing or
    erroring outright) - that's a systemic problem, not a per-file
    fact, and should keep surfacing to the caller's own try/except
    every time rather than being memoized as a permanent "unknown"
    for one unlucky file.
    """

    stat = source.stat()
    key = (str(source.resolve()), stat.st_mtime_ns, stat.st_size)

    if key in _CODEC_PROBE_CACHE:
        return _CODEC_PROBE_CACHE[key]

    codec = probe_video_codec(source)
    _CODEC_PROBE_CACHE[key] = codec
    return codec


# Arbitrary but consistent label for the one CUDA device this module's
# (always single-source) transcodes are pinned to - see stitch.py's
# own _shared_hw_device_args() docstring for why an explicit named
# device matters when a single ffmpeg process decodes *multiple*
# hwaccel inputs at once (real 5x slowdown measured on Christer's own
# archive without one). Moot here - this module only ever decodes one
# source per call - but naming the device explicitly anyway costs
# nothing and keeps the pattern consistent with stitch.py's rather
# than silently diverging from a lesson that was expensive to learn
# once already.
_HW_DEVICE_NAME = "cu"


def _decode_input_args(source: Path, *, hw_decode: bool) -> list[str]:
    """The -i args for `source`, with NVDEC decode flags prepended and
    a CPU-downloading filter appended when `hw_decode` is True -
    mirrors stitch.py's _hwaccel_input_args()/_hw_predecode_filter().

    -hwaccel_output_format cuda keeps decoded frames in GPU memory;
    the trailing "-vf hwdownload,format=nv12" then brings them back to
    normal CPU frames before encode_with_nvenc_fallback()'s own
    "-pix_fmt yuv420p" (a software pixel format) gets applied to
    whichever encoder actually runs - left out, ffmpeg would have to
    silently negotiate that GPU-to-CPU conversion on its own, which
    stitch.py's own hard-won history (see its _hw_predecode_filter()
    docstring) says isn't safe to leave to chance.
    """

    if hw_decode:
        return [
            "-init_hw_device", f"cuda={_HW_DEVICE_NAME}:0",
            "-hwaccel", "cuda",
            "-hwaccel_device", _HW_DEVICE_NAME,
            "-hwaccel_output_format", "cuda",
            "-i", str(source),
            "-vf", "hwdownload,format=nv12",
        ]
    return ["-i", str(source)]


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

    Even with that preset fix confirmed correctly reaching NVENC,
    Christer's own before/after timings barely moved (28.4s -> 26.4s
    for similarly-sized ~280-290MB 4K HEVC sources) - too small a gap
    to be the encoder actually being the bottleneck. The source read
    is HEVC decode, which this function was, until now, always doing
    in plain software regardless of preset - a several-hundred-MB 4K
    HEVC decode is itself heavy enough to plausibly dominate the whole
    ~26-28s on its own. So this now also tries NVDEC (`_nvdec_available()`)
    hardware decode on the *input* side first - mirroring stitch.py's
    own proven `_hwaccel_input_args()`/`_hw_predecode_filter()` pattern
    - and falls back to plain CPU decode (catching `MediaToolError`) if
    that fails for any reason (unsupported profile, driver hiccup,
    etc.), so a bad NVDEC attempt costs one retry, never a broken
    preview. Unverified on real hardware from this end (no GPU
    available here to test against) - Christer's own retest is what
    will actually confirm whether this moves the needle.

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
        # 2026-08-27: cached per (path, mtime, size) - see
        # _CODEC_PROBE_CACHE's own comment near the top of this module.
        codec = _cached_probe_video_codec(source)
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
    extra_codec_args = [
        "-preset", _PREVIEW_PRESET,
        "-c:a", "copy",
        "-movflags", "+faststart",
        "-b:v", _PREVIEW_TARGET_BITRATE,
        "-maxrate", _PREVIEW_TARGET_BITRATE,
        "-bufsize", _PREVIEW_TARGET_BITRATE,
        "-f", "mp4",
    ]
    hw_decode = _nvdec_available()
    decode_method = "nvdec" if hw_decode else "cpu"
    start = time.monotonic()
    try:
        try:
            try:
                encode_with_nvenc_fallback(
                    _decode_input_args(source, hw_decode=hw_decode),
                    tmp_path,
                    extra_codec_args=extra_codec_args,
                )
            except MediaToolError:
                if not hw_decode:
                    raise
                # NVDEC decode itself failed (unsupported profile,
                # driver hiccup, etc.) - not the same thing as NVENC
                # vs. libx264 encoder fallback, which
                # encode_with_nvenc_fallback() already handles
                # internally. Retry once with plain CPU decode before
                # giving up on this source entirely.
                decode_method = "cpu"
                encode_with_nvenc_fallback(
                    _decode_input_args(source, hw_decode=False),
                    tmp_path,
                    extra_codec_args=extra_codec_args,
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
        f"HEVC preview: transcode finished in {elapsed:.1f}s ({decode_method} "
        f"decode), cached as {cache_path.name}",
        file=sys.stderr,
    )
    enforce_cache_size_cap(cache_dir, _MAX_CACHE_BYTES)
    return cache_path


# --- Progressive (streaming) preview transcode -----------------------
#
# Christer, once the NVDEC fix above landed and confirmed decode really
# was the bottleneck: "I guess its already running in parallel, so my
# question is. Can you convert the first 10 to 20%, start playing that
# and during that time convert the rest?" The reason today's transcode
# blocks the whole request isn't decode/encode speed as such - it's
# that a plain MP4's `+faststart` moov atom (the sample-table index a
# browser needs before it can play anything) can't be finalized until
# ffmpeg has already encoded the entire file, so nothing can play until
# 100% is done, not just the first 10-20%. A *fragmented* MP4 (an
# empty moov up front, then a stream of self-contained moof/mdat
# fragments) sidesteps that entirely - a browser can start decoding as
# soon as the first fragment arrives. Combined with NVDEC/NVENC
# comfortably outpacing real-time playback on Christer's own hardware
# (a multi-minute clip transcodes in ~20s, per his own retest above),
# streaming ffmpeg's output directly to the browser as it's produced
# means playback can start within a second or two, and by the time
# more bytes are needed, ffmpeg has usually already caught up.
#
# Deliberately a separate code path from load_or_transcode_hevc_
# preview() above, not a rewrite of it: that function's simple
# Path-in-Path-out contract is still exactly right for a cache hit or
# a non-HEVC/probe-failed passthrough (the overwhelming majority of
# requests), and it's an easy, fully-intact fallback to revert bv-web's
# route back to if this streaming path ever needs to be pulled -
# Christer's own words going in: "try it, we can always take it back."
#
# Second iteration, after Christer reported a real bug in the first:
# "I looks lile every time a look at the video, it does the same and
# not playing the cached file." Root cause: the first version tied the
# whole ffmpeg-reading loop directly to the HTTP response's own async
# generator - so as soon as Starlette closed that generator (which it
# does via GeneratorExit whenever the browser disconnects before
# draining the response to a natural EOF - pausing, seeking away,
# navigating off the page, or just not watching the whole clip
# start-to-finish, all extremely normal for real video playback), the
# generator's own `finally: tmp_path.unlink()` fired and the final
# rename-into-cache step was simply never reached. In practice this
# meant the cache almost never actually got populated - every viewing
# looked like a fresh transcode because, functionally, it usually was.
#
# Fixed by decoupling the background transcode's lifetime from any one
# request's own lifetime entirely: `_run_transcode_to_cache()` runs as
# an independent `asyncio.Task` (tracked in `_IN_PROGRESS`, keyed by
# `cache_path` - which doubles as free deduplication for two
# overlapping requests against the same not-yet-cached source), and
# keeps running to completion - writing to disk, renaming into the
# cache - regardless of whether the HTTP request that originally
# triggered it is still being read. Each HTTP-facing request instead
# gets its own `_consume_broadcast()` generator, subscribed to a
# `_TranscodeBroadcast`'s in-memory history-plus-live-feed: closing one
# subscriber's generator early now only stops *that* subscriber, never
# the shared background transcode underneath it.


# Empty moov + self-contained per-fragment moof/mdat headers instead of
# a single moov built only once the whole encode finishes - see this
# section's own header comment for why plain `+faststart` (what
# load_or_transcode_hevc_preview() above still uses) can't support
# progressive playback at all. default_base_moof avoids duplicating a
# full absolute base-data-offset in every fragment header, a minor
# size/compatibility nicety recommended for streaming output generally.
_FRAGMENTED_MP4_MOVFLAGS = "frag_keyframe+empty_moov+default_base_moof"

# Read/yield/write granularity for the live ffmpeg-stdout-to-browser
# pipe below. Small enough that the browser starts seeing data
# promptly rather than waiting for a large buffer to fill, large enough
# that it isn't spending its time on per-chunk overhead against a
# multi-hundred-MB stream.
_STREAM_CHUNK_BYTES = 256 * 1024

# Cached after the first check (per process) - mirrors media.py's own
# _nvenc_available() exactly (same subprocess probe), but kept as its
# own local copy rather than imported - see _nvdec_available() above
# for why this module follows that convention for small hwaccel/
# encoder probes rather than cross-importing media.py's private one.
_NVENC_AVAILABLE: bool | None = None


def _nvenc_available() -> bool:
    """Return True if this machine's ffmpeg build lists h264_nvenc
    among its encoders - see media.py's own _nvenc_available() for the
    full reasoning (this is the same probe, duplicated per this
    module's own established convention - see _nvdec_available()
    above)."""

    global _NVENC_AVAILABLE

    if _NVENC_AVAILABLE is None:
        try:
            result = subprocess.run(
                ["ffmpeg", "-hide_banner", "-encoders"],
                capture_output=True,
                text=True,
                check=False,
            )
            _NVENC_AVAILABLE = "h264_nvenc" in result.stdout
        except FileNotFoundError:
            _NVENC_AVAILABLE = False

    return _NVENC_AVAILABLE


# Tracks every transcode currently running in the background, keyed by
# the cache path it's writing toward. Doubles as free deduplication:
# two overlapping requests for the same not-yet-cached source join the
# same _TranscodeBroadcast instead of each spawning a redundant ffmpeg
# process. See this module's own "Second iteration" header comment
# above for why this exists (the GeneratorExit-on-disconnect bug).
_IN_PROGRESS: dict[Path, "_TranscodeBroadcast"] = {}


class _TranscodeBroadcast:
    """Coordinates one in-flight transcode of `source` into
    `cache_path`, shared by every HTTP request currently watching or
    joining it - deliberately decoupled from any single request's own
    lifetime (see _run_transcode_to_cache() below, which is what
    actually owns and drives this).

    Every chunk ffmpeg produces is kept in `history` (in memory, not
    re-read from disk) so a request that subscribes after the transcode
    has already started still gets the complete stream from the very
    beginning - including the fragmented MP4's essential empty-moov
    header, without which nothing after it is playable - not just
    whatever's produced from the moment it joined.
    """

    def __init__(self, source: Path, cache_path: Path, tmp_path: Path) -> None:
        self.source = source
        self.cache_path = cache_path
        self.tmp_path = tmp_path
        self.history: list[bytes] = []
        self.done = False
        self.failed = False
        self.subscribers: list[asyncio.Queue] = []
        self.task: asyncio.Task | None = None

    def subscribe(self) -> asyncio.Queue:
        """Register a new consumer, immediately replaying everything
        produced so far into its own queue - see class docstring."""

        queue: asyncio.Queue = asyncio.Queue()
        for chunk in self.history:
            queue.put_nowait(chunk)
        if self.done:
            queue.put_nowait(None)
        self.subscribers.append(queue)
        return queue

    def publish(self, chunk: bytes) -> None:
        self.history.append(chunk)
        for queue in self.subscribers:
            queue.put_nowait(chunk)

    def finish(self) -> None:
        self.done = True
        for queue in self.subscribers:
            queue.put_nowait(None)


async def _spawn_ffmpeg(
    source: Path,
    extra_codec_args: list[str],
    *,
    hw_decode: bool,
    codec: str,
) -> asyncio.subprocess.Process:
    """Launch ffmpeg for `source`, muxing straight to stdout ("-") so
    _run_transcode_to_cache() below can read its output as it's
    produced, rather than waiting for a finished file on disk - the
    Popen-level control build_ffmpeg_encode_command() exists for (see
    that function's own docstring)."""

    command = build_ffmpeg_encode_command(
        _decode_input_args(source, hw_decode=hw_decode),
        Path("-"),
        codec,
        extra_codec_args=extra_codec_args,
    )
    return await asyncio.create_subprocess_exec(
        *command,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )


async def _run_transcode_to_cache(broadcast: _TranscodeBroadcast) -> None:
    """Drive one ffmpeg transcode to completion as an independent
    asyncio.Task, entirely decoupled from any single HTTP request's own
    lifetime - the actual fix for the bug described in this module's
    "Second iteration" header comment above. Publishes each chunk to
    `broadcast` as it's produced (for every current and future
    subscriber to see) and, on success, does the same temp-file-then-
    rename cache write load_or_transcode_hevc_preview() and the first
    streaming iteration both used - but now that rename is reached
    unconditionally once ffmpeg finishes, regardless of whether any
    particular browser request stuck around to watch the whole thing.
    """

    source, cache_path, tmp_path = (
        broadcast.source,
        broadcast.cache_path,
        broadcast.tmp_path,
    )

    hw_decode = _nvdec_available()
    use_nvenc = _nvenc_available()
    decode_method = "nvdec" if hw_decode else "cpu"
    encode_method = "nvenc" if use_nvenc else "libx264"

    extra_codec_args = [
        "-preset", _PREVIEW_PRESET,
        "-c:a", "copy",
        "-movflags", _FRAGMENTED_MP4_MOVFLAGS,
        "-b:v", _PREVIEW_TARGET_BITRATE,
        "-maxrate", _PREVIEW_TARGET_BITRATE,
        "-bufsize", _PREVIEW_TARGET_BITRATE,
        "-f", "mp4",
    ]

    start = time.monotonic()
    try:
        proc = await _spawn_ffmpeg(
            source, extra_codec_args,
            hw_decode=hw_decode, codec="h264_nvenc" if use_nvenc else "libx264",
        )
        first_chunk = await proc.stdout.read(_STREAM_CHUNK_BYTES)

        if not first_chunk and (hw_decode or use_nvenc):
            # Preferred combination produced nothing before exiting -
            # nothing published yet, so it's safe to retry with the
            # known-safe combination.
            await proc.wait()
            proc = await _spawn_ffmpeg(
                source, extra_codec_args, hw_decode=False, codec="libx264",
            )
            decode_method, encode_method = "cpu", "libx264"
            first_chunk = await proc.stdout.read(_STREAM_CHUNK_BYTES)

        if not first_chunk:
            await proc.wait()
            stderr = await proc.stderr.read() if proc.stderr else b""
            elapsed = time.monotonic() - start
            print(
                f"HEVC preview: transcode failed for {source.name} after "
                f"{elapsed:.1f}s (before any bytes were produced): "
                f"{stderr.decode(errors='replace').strip()}",
                file=sys.stderr,
            )
            broadcast.failed = True
            return

        total_bytes = 0
        try:
            tmp_path.parent.mkdir(parents=True, exist_ok=True)
            with open(tmp_path, "wb") as tmp_file:
                chunk = first_chunk
                while chunk:
                    tmp_file.write(chunk)
                    total_bytes += len(chunk)
                    broadcast.publish(chunk)
                    chunk = await proc.stdout.read(_STREAM_CHUNK_BYTES)

            returncode = await proc.wait()
            if returncode != 0:
                stderr = await proc.stderr.read() if proc.stderr else b""
                elapsed = time.monotonic() - start
                print(
                    f"HEVC preview: transcode failed for {source.name} after "
                    f"{elapsed:.1f}s, mid-stream after producing "
                    f"{total_bytes} bytes - any viewers already watching "
                    f"got a truncated preview, and nothing is cached: "
                    f"{stderr.decode(errors='replace').strip()}",
                    file=sys.stderr,
                )
                broadcast.failed = True
                return

            os.replace(tmp_path, cache_path)
        finally:
            tmp_path.unlink(missing_ok=True)

        elapsed = time.monotonic() - start
        print(
            f"HEVC preview: transcode finished in {elapsed:.1f}s "
            f"({decode_method} decode, {encode_method} encode, streamed), "
            f"cached as {cache_path.name}",
            file=sys.stderr,
        )
        enforce_cache_size_cap(cache_path.parent, _MAX_CACHE_BYTES)
    finally:
        # Runs no matter how the transcode ended (success, failure, or
        # even a bug raising an unexpected exception above) - both
        # steps are essential: finish() releases every subscriber
        # that's still waiting (otherwise a broken transcode would hang
        # any request watching it forever), and popping the registry
        # entry lets the *next* request for this source start a fresh
        # attempt instead of joining a dead broadcast.
        broadcast.finish()
        _IN_PROGRESS.pop(cache_path, None)


async def _consume_broadcast(broadcast: _TranscodeBroadcast) -> AsyncIterator[bytes]:
    """Per-request adapter: subscribes to `broadcast` and yields
    whatever it produces to this one HTTP response. If the browser
    disconnects, Starlette closes this generator early (GeneratorExit)
    - caught here only to unsubscribe this one queue, never to touch
    the shared `broadcast` or its background task underneath, which is
    the entire point of this second-iteration design (see this
    module's own header comment above)."""

    queue = broadcast.subscribe()
    try:
        while True:
            item = await queue.get()
            if item is None:
                return
            yield item
    finally:
        with contextlib.suppress(ValueError):
            broadcast.subscribers.remove(queue)


async def open_hevc_preview_stream(
    source: Path, cache_dir: Path
) -> Path | AsyncIterator[bytes]:
    """Progressive-streaming counterpart to load_or_transcode_hevc_
    preview() for bv-web's file route: returns a Path to serve
    directly (unchanged original file for anything non-HEVC/probe-
    failed, or an already-cached preview - both instant, no ffmpeg
    work at all) exactly like that function, or - the new case - an
    async byte generator when a fresh transcode is needed, so the
    caller can start streaming a growing preview to the browser
    immediately (StreamingResponse) instead of blocking the whole
    request until a full transcode finishes.

    Callers must check which of the two came back (e.g.
    `isinstance(result, Path)`) - a Path should be served as a normal
    static file (FileResponse, full Range-request support and all,
    since it's either the untouched original or an already-complete
    cached copy); an async generator should be streamed directly. The
    background transcode that feeds it (_run_transcode_to_cache()) runs
    as its own asyncio.Task, independent of this or any other request,
    and is what actually populates the cache - see this module's own
    "Second iteration" header comment above for why that separation
    matters.

    A coroutine (not a plain function) so `asyncio.create_task()` below
    is always called from inside a running event loop.
    """

    try:
        # 2026-08-27: cached per (path, mtime, size), and hopped onto a
        # worker thread so a cache miss's blocking ffprobe subprocess
        # can't stall this coroutine's event loop - see
        # _CODEC_PROBE_CACHE's own comment near the top of this module,
        # and load_or_transcode_hevc_preview()'s sync counterpart above,
        # which uses the same cache without the thread hop since it's
        # already outside any event loop.
        codec = await asyncio.to_thread(_cached_probe_video_codec, source)
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

    broadcast = _IN_PROGRESS.get(cache_path)
    if broadcast is not None:
        print(
            f"HEVC preview: {source.name} is {codec} - joining an "
            f"already in-progress live transcode...",
            file=sys.stderr,
        )
    else:
        print(
            f"HEVC preview: {source.name} is {codec} - streaming a live "
            f"H.264 transcode (playback can start before this finishes; "
            f"the rest keeps converting in the background, independently "
            f"of whether this particular request stays open)...",
            file=sys.stderr,
        )
        cache_dir.mkdir(parents=True, exist_ok=True)
        tmp_path = cache_path.with_name(
            f"{cache_path.stem}.{uuid.uuid4().hex[:8]}.tmp"
        )
        broadcast = _TranscodeBroadcast(source, cache_path, tmp_path)
        _IN_PROGRESS[cache_path] = broadcast
        broadcast.task = asyncio.create_task(_run_transcode_to_cache(broadcast))

    return _consume_broadcast(broadcast)
