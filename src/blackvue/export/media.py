"""
Media concatenation for bv-export (ffmpeg concat demuxer), plus the
shared frame-sequence-to-video encoder map_video.py/gsensor_video.py
both use - which tries NVIDIA's hardware h264_nvenc encoder when
available, falling back to the CPU libx264 encoder otherwise (see
encode_frame_sequence()).

Copyright (C) 2026 Christer R. (sssreh)

SPDX-License-Identifier: GPL-3.0-or-later
"""

from __future__ import annotations

import contextlib
import json
import subprocess
import tempfile
from pathlib import Path

from ..generate.media import MediaToolError
from ..generate.media import probe


class ExportCancelled(Exception):
    """Raised mid-export when a caller-supplied `should_continue()`
    callable (see trip_export.py's own `export_trip()` docstring)
    returns False - bv-web's job runner uses this to make Cancel
    actually stop new work, instead of only hiding a still-running
    job's output from the browser while the real background thread
    keeps going to completion regardless (see WORKING_CONTEXT.md,
    "bv-export cancellation doesn't stop it").

    Deliberately its own exception, not MediaToolError - a cancelled
    export isn't a failure to report as a warning the way a bad ffmpeg
    call is; callers should let it propagate and treat it as a clean,
    deliberate stop.

    Lives here (export/media.py), the shared low-level module both
    map_video.py and gsensor_graph_video.py already import from,
    rather than in trip_export.py itself - trip_export.py imports
    *from* both of those, so putting it there would be a circular
    import.
    """


# How often (in frames) map_video.py's render_map_video()/
# render_intro_flyover() and gsensor_graph_video.py's
# render_gsensor_graph_video() call their own `should_continue()`
# during a frame-rendering loop. Checking every single frame would
# work too (a Python callable call is negligible next to the PIL
# drawing/ffmpeg work each frame already does) but batching the check
# avoids paying even that negligible cost 25-30 times a second for no
# real gain - a several-minute render still stops within a couple of
# seconds either way.
_FRAME_CHECKPOINT_INTERVAL = 30


# Forced onto every source `_strip_audio_stream_copy()` normalizes
# before a `video_only=True` concat - see that function's own
# docstring for why. 90000 is the standard MPEG PTS clock rate
# (90kHz), a safe, universally-supported choice unrelated to any
# particular source's own frame rate.
_CONCAT_VIDEO_TRACK_TIMESCALE = 90000

# _concat_filter_reencode()'s own fallback target frame rate, used only
# if probing the first source (its own preferred target - see that
# function's own docstring) fails outright. 30fps is a safe, ordinary
# default for dashcam footage generally, not tied to any particular
# camera model.
_DEFAULT_CONCAT_FILTER_FPS = 30.0


def _escape_concat_path(path: Path) -> str:
    """Escape a path for use inside a single-quoted entry in an
    ffmpeg concat-demuxer list file.

    Everything inside single quotes is literal to ffmpeg's mini
    parser (including backslashes, so Windows paths need no
    escaping) - the one exception is a literal single quote, which
    has to close the quote, insert an escaped quote, and reopen it:
    the same trick shell single-quoting uses.
    """

    return str(path).replace("'", "'\\''")


# Cached after the first check (per process) - which encoders this
# machine's ffmpeg build has is a fixed fact for the life of the run,
# not something worth re-shelling-out to ffmpeg to ask for every
# single trip's map.mp4/gsensor.mp4.
_NVENC_AVAILABLE: bool | None = None


def _nvenc_available() -> bool:
    """Return True if this machine's ffmpeg build lists h264_nvenc
    (NVIDIA's hardware H.264 encoder) among its encoders.

    Just having the encoder listed doesn't guarantee it'll actually
    work (a real NVIDIA GPU + driver + ffmpeg built with NVENC support
    all have to line up) - encode_frame_sequence() falls back to the
    CPU encoder if an NVENC attempt fails for any reason, so a wrong
    "True" here costs one failed attempt, not a broken export.
    """

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
            # No ffmpeg at all - encode_frame_sequence()'s own attempt
            # below will raise the usual clean "not found" error.
            _NVENC_AVAILABLE = False

    return _NVENC_AVAILABLE


# Applied by encode_with_nvenc_fallback() whenever the caller hasn't
# already asked for their own rate control (typically stitch.py's
# --stitch-bitrate, via _bitrate_args()) - see that function's own
# docstring for why leaving rate control entirely up to nvenc/libx264's
# own internal defaults turned out to be a real problem, not just a
# theoretical one: confirmed on Christer's real archive that with no
# bitrate given at all, h264_nvenc's own default landed at ~23Mbps for
# one native-resolution stitch.mp4, but only ~1.9Mbps - visibly
# grainy - for a later rearview_mirror+map+gsensor+subtitles one same
# machine, same "no bitrate given" input. -cq/-crf 19 is a "high
# quality, roughly visually lossless" target for real camera footage
# (dashcam grain/detail is exactly the kind of content low CQ/CRF
# values are meant for) - independent of resolution or how much filter
# -graph compositing happens to precede the final encode, unlike
# whatever heuristic nvenc's own unset-bitrate default uses.
_DEFAULT_NVENC_QUALITY_ARGS = ["-rc", "vbr", "-cq", "19", "-b:v", "0"]
_DEFAULT_LIBX264_QUALITY_ARGS = ["-crf", "19"]

# extra_codec_args flags that mean "the caller already specified their
# own rate control" - _DEFAULT_NVENC_QUALITY_ARGS/
# _DEFAULT_LIBX264_QUALITY_ARGS are skipped whenever any of these is
# already present, so an explicit --stitch-bitrate (-b:v, via
# stitch.py's _bitrate_args()) isn't fought by a competing default
# quality target on top of it.
_CALLER_RATE_CONTROL_FLAGS = ("-b:v", "-crf", "-cq", "-qp")

def _build_codec_args(codec: str, extra_codec_args: list[str]) -> list[str]:
    """The `-c:v`/`-pix_fmt`/quality-target/`extra_codec_args` args for
    a single encode attempt at `codec` (`"h264_nvenc"` or `"libx264"`)
    - the inner piece `encode_with_nvenc_fallback()` builds for each of
    its own two attempts, factored out here so
    `build_ffmpeg_encode_command()` below can build the exact same
    thing for a caller that needs Popen-level control over the process
    instead of this module's own synchronous `subprocess.run()`.
    """

    caller_set_rate_control = any(
        flag in extra_codec_args for flag in _CALLER_RATE_CONTROL_FLAGS
    )
    if codec == "h264_nvenc":
        quality_args = (
            [] if caller_set_rate_control else _DEFAULT_NVENC_QUALITY_ARGS
        )
    else:
        quality_args = (
            [] if caller_set_rate_control else _DEFAULT_LIBX264_QUALITY_ARGS
        )
    return ["-c:v", codec, "-pix_fmt", "yuv420p", *quality_args, *extra_codec_args]


def build_ffmpeg_encode_command(
    input_args: list[str],
    destination: Path,
    codec: str,
    extra_codec_args: list[str] | None = None,
) -> list[str]:
    """Return the full ffmpeg argv `encode_with_nvenc_fallback()` would
    run for `codec` (`"h264_nvenc"` or `"libx264"`), without executing
    it.

    Built for a caller that needs Popen-level control over the actual
    process rather than this module's own blocking `subprocess.run()`
    - bv-web's progressive HEVC-preview streaming (`export/
    hevc_preview.py`) has to keep the process running while it reads
    bytes off its stdout as they're produced (piping the encode
    straight to the browser instead of waiting for the whole file), so
    it needs the exact argv this module's own quality-arg/rate-control
    policy (`_build_codec_args()`) would produce, without also getting
    `_run_ffmpeg_encode()`'s blocking wait baked in. `destination` can
    be a real path or `Path("-")` for stdout - this function doesn't
    care, it just builds the argv the same way `_run_ffmpeg_encode()`
    would.

    Unlike `encode_with_nvenc_fallback()`'s own automatic NVENC-then-
    CPU probe/fallback, `codec` here is the caller's own explicit
    choice - a caller managing its own subprocess for a live stream
    can't cleanly discard bytes it's already sent to a client and
    restart with a different encoder after the fact, so it has to
    decide up front (see hevc_preview.py's own streaming function for
    how it picks, and how it still recovers from a bad choice as long
    as nothing's been streamed yet).
    """

    extra_codec_args = extra_codec_args or []
    return [
        "ffmpeg", "-y", *input_args,
        *_build_codec_args(codec, extra_codec_args),
        str(destination),
    ]


def _run_ffmpeg_encode(
    codec_args: list[str], input_args: list[str], destination: Path
) -> None:
    subprocess.run(
        ["ffmpeg", "-y", *input_args, *codec_args, str(destination)],
        capture_output=True,
        text=True,
        check=True,
    )


def encode_with_nvenc_fallback(
    input_args: list[str],
    destination: Path,
    extra_codec_args: list[str] | None = None,
) -> None:
    """Run ffmpeg with `input_args` (whatever inputs/filters/maps the
    caller needs - a frame-sequence input, a multi-video
    filter_complex composition, etc.), encoding video with NVIDIA's
    hardware NVENC encoder when this machine's ffmpeg build supports
    it (see _nvenc_available()), falling back to the equivalent
    software encoder otherwise - always used directly if NVENC isn't
    available, and also if an NVENC attempt itself fails (e.g. the
    encoder is listed but no compatible GPU/driver is actually
    present) - so this always produces a video either way, just faster
    when a real NVIDIA GPU is there to use.

    `extra_codec_args`, if given, are appended after the base codec
    args on *both* the NVENC and CPU attempts (e.g. a bitrate cap) -
    encoder-agnostic settings the caller wants regardless of which of
    the two actually ends up encoding.

    Unless `extra_codec_args` already contains its own rate-control
    flag (see _CALLER_RATE_CONTROL_FLAGS), a default quality target
    (_DEFAULT_NVENC_QUALITY_ARGS/_DEFAULT_LIBX264_QUALITY_ARGS) is
    applied instead of leaving it to nvenc/libx264's own internal
    defaults - see those constants' own comment for why that turned
    out to matter for real.

    Shared by every "encode a video via ffmpeg" caller in bv-export
    (map_video.py/gsensor_video.py's frame sequences via
    encode_frame_sequence() below, stitch.py's camera composition) so
    they all get the same NVENC-then-CPU fallback behavior, and the
    same default quality safety net, for free.

    (An earlier version of this function also accepted a `video_codec`
    parameter, switching between H.264 and HEVC output for a since
    -removed placeholder-clip feature - see WORKING_CONTEXT.md.
    Reverted once that feature's removal left it with no caller.)
    """

    extra_codec_args = extra_codec_args or []
    destination.parent.mkdir(parents=True, exist_ok=True)

    if _nvenc_available():
        try:
            _run_ffmpeg_encode(
                _build_codec_args("h264_nvenc", extra_codec_args),
                input_args, destination,
            )
            return
        except FileNotFoundError as exc:
            raise MediaToolError("ffmpeg not found on PATH") from exc
        except subprocess.CalledProcessError:
            pass  # fall through to the CPU encoder below

    try:
        _run_ffmpeg_encode(
            _build_codec_args("libx264", extra_codec_args),
            input_args, destination,
        )
    except FileNotFoundError as exc:
        raise MediaToolError("ffmpeg not found on PATH") from exc
    except subprocess.CalledProcessError as exc:
        raise MediaToolError(
            f"ffmpeg encode failed for {destination.name}: "
            f"{exc.stderr.strip()}"
        ) from exc


def encode_frame_sequence(frame_dir: Path, destination: Path, fps: int) -> None:
    """Encode a directory of frame_%06d.png images (map_video.py,
    gsensor_video.py) into a video at `destination`, in order, at
    `fps` frames/second - see encode_with_nvenc_fallback() for the
    actual encode/fallback behavior.
    """

    encode_with_nvenc_fallback(
        ["-framerate", str(fps), "-i", str(frame_dir / "frame_%06d.png")],
        destination,
    )


def check_readable(path: Path) -> None:
    """Raise MediaToolError if `path` can't even be opened/demuxed by
    ffprobe - a lightweight, container-level check meant to be run
    against every source before handing them all to
    concatenate_media(), which otherwise fails for *all* of them in
    one shot the moment ffmpeg's concat demuxer hits a single
    unreadable file (most often one whose moov atom never got
    written, e.g. the camera lost power mid-recording).

    Deliberately doesn't select a stream type the way
    generate/media.py's probe() does (`-select_streams v:0`, since
    that function only ever cares about a video stream's own duration/
    frame rate) - concatenate_media() is used for audio-only sources
    (audio.aac) too, which have no video stream at all and would
    otherwise always fail this check. Just asking for the container's
    own duration is enough to prove ffprobe can actually open and
    parse the file.
    """

    try:
        subprocess.run(
            [
                "ffprobe",
                "-v", "error",
                "-show_entries", "format=duration",
                "-of", "json",
                str(path),
            ],
            capture_output=True,
            text=True,
            check=True,
        )
    except FileNotFoundError as exc:
        raise MediaToolError("ffprobe not found on PATH") from exc
    except subprocess.CalledProcessError as exc:
        raise MediaToolError(
            f"ffprobe failed for {path.name}: {exc.stderr.strip()}"
        ) from exc


def _normalize_source_for_concat(
    source: Path, destination: Path, *, strip_audio: bool
) -> None:
    """Stream-copy `source` into `destination`, forcing a consistent
    `-video_track_timescale` (`_CONCAT_VIDEO_TRACK_TIMESCALE`) onto
    every source before it reaches the final concat, and additionally
    dropping any audio track (`-an`) when `strip_audio` is True.

    The audio-track drop is `concatenate_media()`'s own pre-existing
    `video_only=True` behavior (FRONT only - see that function's own
    docstring for why a repaired Parking recording's own video-only
    container otherwise corrupts the concat). The timescale forcing is
    separate and applies unconditionally, to both FRONT *and* REAR:
    Christer, running `--parking-speed 0.1` for real, "map, gps and
    sound good, but video freeze after parking." Root cause, confirmed
    by inspecting the concatenated front.mp4's own raw packet
    timestamps: `change_playback_speed()`'s re-encode (`export/
    media.py`) lands on a different internal MP4 timescale than the
    camera's own original recordings (libx264/NVENC pick one based on
    the encoded stream's own frame rate, and a slowed-down Parking
    clip's effective rate differs from a normal recording's), and
    ffmpeg's concat demuxer's stream-copy path (`-f concat -c copy`)
    doesn't correctly reconcile mismatched timescales across segments
    in the ffmpeg version this was tested against - the segment
    *after* the mismatched one gets its own real frames collapsed into
    a fractions-of-a-millisecond sliver right at the transition point,
    which is exactly what "frozen video, but audio/map/gsensor still
    advancing" looks like (those three are built independently of
    front.mp4's own container, so they're unaffected and stay
    correct - matching Christer's own report). Applies to REAR too,
    not just FRONT: `_apply_parking_speed()` speeds up both sides of a
    Parking recording identically, so a rear-camera trip would hit the
    exact same freeze on rear.mp4 if only FRONT were normalized here.
    `-video_track_timescale` rewrites only the output container's
    declared timescale, not the codec data - safe on a plain stream
    copy, no re-encode needed - and forcing every source onto the
    *same* one before the final concat sidesteps the whole class of
    mismatch regardless of which source (any future one, not just a
    sped-up Parking segment) picked a different one on its own. 90000
    (the standard MPEG PTS clock rate) is a safe,
    arbitrary-but-conventional common choice, not matched to any
    particular source.
    """

    destination.parent.mkdir(parents=True, exist_ok=True)

    command = [
        "ffmpeg", "-y",
        "-i", str(source),
    ]
    if strip_audio:
        command.append("-an")
    command += [
        "-c", "copy",
        "-video_track_timescale", str(_CONCAT_VIDEO_TRACK_TIMESCALE),
        str(destination),
    ]

    try:
        subprocess.run(
            command,
            capture_output=True,
            text=True,
            check=True,
        )
    except FileNotFoundError as exc:
        raise MediaToolError("ffmpeg not found on PATH") from exc
    except subprocess.CalledProcessError as exc:
        raise MediaToolError(
            f"ffmpeg source-normalize failed for {source.name}: "
            f"{exc.stderr.strip()}"
        ) from exc


def _concat_filter_reencode(sources: list[Path], destination: Path) -> None:
    """Concatenate `sources` into `destination` by decoding every one
    of them to raw frames and re-encoding the result as a single,
    continuous stream (ffmpeg's `concat` *filter*, not the concat
    *demuxer* `concatenate_media()` normally uses) - video only, no
    audio is mapped from any source.

    `concatenate_media()`'s normal path is a plain stream copy: it
    never touches a single encoded frame, which is fast but means
    every source has to already agree on how it was encoded (SPS/PPS
    parameter sets, GOP structure, B-frame usage, container
    timescale...) for the demuxer's naive packet concatenation to
    produce something a real decoder can play back correctly. That's
    always true for two recordings straight off the same camera - they
    share one encoder session's own settings - but stops being true
    the moment one segment was re-encoded by something else entirely.
    `change_playback_speed()` (this module, `--parking-speed`'s own
    per-segment re-encode) is exactly that: its libx264/NVENC output
    picks its own profile/level/GOP/B-frame settings, independently of
    whatever the camera's own hardware encoder used for every
    recording around it. `_normalize_source_for_concat()`'s
    `-video_track_timescale` forcing (see its own docstring) fixed the
    specific "whole next segment collapses into a near-zero-duration
    sliver right at the transition" symptom this first surfaced as,
    but Christer's real camera footage kept freezing after that fix
    too - now a few frames *into* the segment after the sped-up one,
    not right at the boundary, both at `--parking-speed 3` and `0.1`,
    on front.mp4, rear.mp4, *and* stitch.mp4 alike. That pattern - a
    handful of frames decode fine (reusing whatever reference frames
    already exist), then the picture freezes while audio/map/gsensor
    keep advancing - is the same signature already diagnosed once
    before in this codebase, for a completely different feature: see
    `_concatenate_asset()`'s own docstring on the removed parking
    -transition-clip feature, which hit "Invalid NAL unit size"/"No
    ref lists in the SPS" from mixing the dashcam's own encoder output
    with anything bv-export rendered itself, via this same stream-copy
    concat path. A timescale fix alone can't reach that: it rewrites
    container metadata, not the actual encoded bitstream, so a real
    SPS/PPS/GOP mismatch survives it untouched.

    That earlier feature sidestepped the problem by simply never
    generating a locally-re-encoded segment to splice in - Christer's
    own call at the time was "we don't want time consuming stuff if it
    not gives us something great back. Just skip it altogether."
    `--parking-speed` can't take that way out: speeding footage up *is*
    the feature, so a re-encoded segment always has to be joined back
    into the surrounding untouched camera footage somehow. Decoding
    everything to raw frames and re-encoding the whole thing as one
    new, internally-consistent stream sidesteps every one of these
    mismatches at once (no two segments' SPS/PPS/GOP/timescale ever
    have to agree, because nothing downstream of this ever sees the
    original encoded bitstreams again) - at real re-encode cost, unlike
    a stream copy, which is exactly why `concatenate_media()` only
    takes this path when a caller explicitly opts in via
    `force_reencode` (trip_export.py's `_concatenate_asset()` does,
    only for the specific asset(s) `_apply_parking_speed()` actually
    touched - most trips never use `--parking-speed` and never pay
    this cost).

    Reuses `encode_with_nvenc_fallback()` for the actual encode, same
    as every other real encode in this module, so this gets the same
    NVENC-with-CPU-fallback behavior and default quality target as
    everything else.

    Every source is also run through its own `fps=<target>` filter
    before the concat filter proper, all normalized to the *first*
    source's own probed frame rate (falling back to
    `_DEFAULT_CONCAT_FILTER_FPS` if probing it fails). Discovered the
    hard way, testing this function directly: without it, ffmpeg's
    concat *filter* (unlike the concat *demuxer* elsewhere in this
    module) doesn't just get a source's own real frame spacing wrong
    when inputs disagree on frame rate - a 30fps/2fps/30fps sequence
    (the exact drive/park/drive shape `--parking-speed` produces)
    turned into hundreds of thousands of duplicated frames and an
    effective hang, the filter graph trying to reconcile the frame
    -rate mismatch by holding/duplicating frames rather than just
    concatenating cleanly. `change_playback_speed()`'s own `setpts`
    -based re-encode changes a segment's *effective* frame rate exactly
    this way (that's the whole point - speeding footage up packs the
    same frame count into less time), so this is a real, not
    theoretical, case for `--parking-speed`'s own re-encoded segment
    sitting between two untouched camera recordings. First source's own
    rate (not a hardcoded constant) because the untouched camera
    recordings on either side of a sped-up Parking segment are the
    ones that should keep their real fps - forcing them onto some
    unrelated fixed value would resample (and subtly alter the
    playback speed of) footage `--parking-speed` was never asked to
    touch.
    """

    input_args: list[str] = []
    for source in sources:
        input_args += ["-i", str(source)]

    target_fps = _DEFAULT_CONCAT_FILTER_FPS
    try:
        probed_fps = probe(sources[0]).frame_rate
        if probed_fps > 0:
            target_fps = probed_fps
    except MediaToolError:
        pass

    per_source_filters = "".join(
        f"[{index}:v:0]fps={target_fps}[v{index}];"
        for index in range(len(sources))
    )
    filter_inputs = "".join(f"[v{index}]" for index in range(len(sources)))
    filter_complex = (
        f"{per_source_filters}{filter_inputs}concat=n={len(sources)}:v=1:a=0[outv]"
    )
    input_args += ["-filter_complex", filter_complex, "-map", "[outv]"]

    encode_with_nvenc_fallback(input_args, destination)


def concatenate_media(
    sources: list[Path],
    destination: Path,
    *,
    video_only: bool = False,
    force_reencode: bool = False,
) -> None:
    """Concatenate video or audio files, in order, into `destination`
    via ffmpeg's concat demuxer, copying streams without re-encoding.

    Works for a single source too (a plain stream copy). Does nothing
    if `sources` is empty.

    `force_reencode`, if True, bypasses the stream-copy concat
    entirely in favor of `_concat_filter_reencode()` - decoding every
    source and re-encoding the result as one continuous stream, video
    only (see that function's own docstring for why: a stream-copy
    concat needs every source to share compatible encoder settings,
    which no longer holds once one segment has been re-encoded by
    something other than the camera itself, e.g. `--parking-speed`'s
    `change_playback_speed()`). `video_only` is ignored when
    `force_reencode` is True - the re-encode path never maps audio
    from any source, the same effective result `video_only=True`
    produces on the normal path.

    `video_only`, if True, guarantees `destination` carries no audio
    stream at all, regardless of what any individual source has
    embedded. trip_export.py's own FRONT-asset concatenation passes
    this - a BlackVue FRONT recording normally embeds its own audio
    track alongside video, but a repaired Parking recording (see
    generate/mp4_repair.py's `repair_parking_container()`) has had its
    own broken, empty audio track dropped entirely, leaving it video
    -only while every other FRONT recording in the same trip still
    carries two streams.

    The first attempt at this fix just appended `-an` to the final
    concat command, relying on ffmpeg's concat demuxer to tolerate a
    mix of 1-stream and 2-stream segments (something its own docs
    claim is supported, as long as the streams that *are* present
    line up in the same order across files). In practice, on a real
    trip, that didn't fix anything: front.mp4 came back with the exact
    same corruption as before the `-an` was added - video stream
    reporting `time_base=1/16000` (a value that looks like it leaked
    from an audio sample rate) instead of the `1/1000` every
    individual source itself reports, and a duration ~16x too long
    (3682.512s reported vs. the ~230s every other camera/source agrees
    the trip actually runs), despite the real frame count surviving
    concatenation almost exactly right (6822 vs. rear's 6829). Since
    `-an` there only restricts what the *output* mapping selects, not
    what the concat demuxer itself has to reconcile while reading a
    virtual single stream out of files with different per-segment
    stream layouts, the corruption was happening upstream of that
    flag entirely.

    `video_only=True` now normalizes every source into its own
    video-only temp copy (`-an` applied per file, individually, before
    any of them reach the concat list) rather than trusting the concat
    demuxer to reconcile mixed layouts itself. This makes every listed
    file's own stream layout identical (exactly one video stream)
    before ffmpeg ever has to combine them, sidestepping the whole
    class of bug rather than depending on a specific ffmpeg version's
    handling of the mixed-layout case. trip_export.py's own
    `_concatenate_asset()` then remuxes the trip's separately-built,
    always-consistent `audio.aac` into the result afterward (see
    `mux_audio_track()` below), rather than depending on front.mp4's
    own raw per-recording audio surviving concatenation intact.

    Every source - regardless of `video_only` - also gets its own
    `-video_track_timescale` forced to a shared, consistent value
    before the final concat (see `_normalize_source_for_concat()`'s
    own docstring for the real "video freeze after parking" bug this
    fixes). Unlike the audio-stripping above, this isn't FRONT-only:
    REAR goes through the exact same per-source normalization pass
    now too, since a `--parking-speed`-sped Parking recording's REAR
    side needs it exactly as much as FRONT does.
    """

    if not sources:
        return

    destination.parent.mkdir(parents=True, exist_ok=True)

    if force_reencode:
        _concat_filter_reencode(sources, destination)
        return

    with contextlib.ExitStack() as stack:
        # ignore_cleanup_errors=True: a leftover lock on one of these
        # per-source normalized copies (e.g. antivirus - see
        # trip_export.py's own front.mp4 mux temp dir for the real
        # case Christer hit) would otherwise raise out of this
        # function on the way out, even after the concat itself
        # already succeeded and destination is already good.
        normalize_dir = Path(
            stack.enter_context(
                tempfile.TemporaryDirectory(
                    prefix="bv_export_strip_", ignore_cleanup_errors=True
                )
            )
        )
        concat_sources = []
        for index, source in enumerate(sources):
            normalized = normalize_dir / f"{index:04d}_{source.name}"
            _normalize_source_for_concat(
                source, normalized, strip_audio=video_only
            )
            concat_sources.append(normalized)

        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".txt", delete=False, encoding="utf-8"
        ) as list_file:
            for source in concat_sources:
                list_file.write(f"file '{_escape_concat_path(source)}'\n")
            list_path = Path(list_file.name)

        command = [
            "ffmpeg",
            "-y",
            "-f", "concat",
            "-safe", "0",
            "-i", str(list_path),
            "-c", "copy",
        ]
        if video_only:
            command.append("-an")
        command.append(str(destination))

        try:
            subprocess.run(command, capture_output=True, text=True, check=True)
        except FileNotFoundError as exc:
            raise MediaToolError("ffmpeg not found on PATH") from exc
        except subprocess.CalledProcessError as exc:
            raise MediaToolError(
                f"ffmpeg concat failed for {destination.name}: "
                f"{exc.stderr.strip()}"
            ) from exc
        finally:
            list_path.unlink(missing_ok=True)


def change_playback_speed(source: Path, destination: Path, speed: float) -> None:
    """Re-encode `source`'s video at `speed`x its own natural pace
    (2.0 plays twice as fast/half as long, 0.5 plays half as fast/
    twice as long), via ffmpeg's `setpts` filter, dropping any audio
    track (`-an`).

    Built for `trip_export.py`'s `--parking-speed` (Christer's own
    request: Parking-mode footage is motion-triggered and sparse, so
    a long real-world span often compresses into a short, slow-to-
    watch clip - a speed control lets it play back faster without
    touching the rest of the trip). `-an` matches
    `concatenate_media(video_only=True)`'s own reasoning for FRONT
    (see that function's docstring): whether or not a Parking
    recording has its own real audio, this function's own caller
    (`_apply_parking_speed()` in trip_export.py) also drops
    `Asset.AUDIO` from any recording it speeds up here, so
    `_pad_missing_audio_with_silence()` fills the gap afterward with
    correctly-*sped-up*-duration silence (it reads the recording's own
    post-speed-change video duration via the same `duration_overrides`
    mechanism this function's own caller registers its output under) -
    so there's nothing this function itself needs to preserve or speed
    up on the audio side. Without that drop, a recording whose own
    audio survived would leave it at its original, now-longer length -
    muxing that stale audio against the shorter sped-up video later
    (`mux_audio_track()`, no `-shortest`) makes the muxed file's own
    reported duration follow the *longer* (audio) stream instead of
    the genuinely shorter video.

    Unlike every other per-source operation in this module
    (`trim_media()`, `trim_media_head()`, `_strip_audio_stream_copy()`),
    this can't be a stream copy: `setpts` rewrites presentation
    timestamps, which only makes sense on decoded frames, not raw
    packets. Reuses `encode_with_nvenc_fallback()` for the actual
    encode, so this gets the same NVENC-with-CPU-fallback behavior and
    default quality target (`_DEFAULT_NVENC_QUALITY_ARGS`/
    `_DEFAULT_LIBX264_QUALITY_ARGS`) every other real encode in this
    codebase already uses, rather than a bespoke re-encode path with
    its own quality quirks.

    Raises `ValueError` for a non-positive `speed` - `setpts=PTS/0` (or
    a negative divisor) has no sane meaning and would otherwise just
    surface as an opaque ffmpeg failure instead of a clear one. Range
    -checking the value against Christer's requested 0.10-10.0 window is
    the CLI layer's own job (`cli/bv_export.py`'s `_parse_parking_speed`),
    not this function's - a library function shouldn't bake in a UI
    -level policy choice about how extreme a speed is "reasonable."
    """

    if speed <= 0:
        raise ValueError(f"speed must be greater than 0, got {speed!r}")

    encode_with_nvenc_fallback(
        [
            "-i", str(source),
            "-filter:v", f"setpts=PTS/{speed}",
            "-an",
        ],
        destination,
    )


def generate_silence(
    destination: Path,
    duration_seconds: float,
    *,
    sample_rate: int = 48000,
    channels: int = 2,
) -> None:
    """Generate a silent AAC/ADTS clip exactly `duration_seconds` long,
    at `sample_rate`/`channels`.

    Built for trip_export.py's own audio/video sync fix: a Parking
    recording (or any other recording missing its own `.aac`) still
    takes up real time in front.mp4's video timeline, but contributes
    nothing to audio.aac, which simply leaves recordings without an
    audio asset out entirely (see `_ensure_recording_audio()`'s own
    docstring - Parking recordings never get one). Concatenated as-is,
    audio.aac ends up shorter than the video by exactly the skipped
    recordings' own durations - every recording *after* the first gap
    plays back against audio recorded for a different moment in the
    trip. Christer, on a real export: "audio it not in sync width
    front, its synching with the parking file." Filling the gap with
    real silence of the *exact* right duration keeps audio.aac's own
    timeline the same length as the video's, so everything after it
    stays lined up.

    `sample_rate`/`channels` default to a reasonable common format,
    but a caller filling a gap alongside *real* `.aac` files should
    always pass the real ones' own probed values (`generate/media.py`'s
    `probe_audio_format()`) instead of relying on these defaults -
    ffmpeg's concat demuxer doesn't harmonize mismatched parameters
    across `-c copy` segments, it just produces a corrupted result,
    exactly the class of bug `concatenate_media()`'s own docstring
    already covers in detail for video.
    """

    destination.parent.mkdir(parents=True, exist_ok=True)
    channel_layout = "mono" if channels == 1 else "stereo"

    try:
        subprocess.run(
            [
                "ffmpeg", "-y",
                "-f", "lavfi",
                "-i", f"anullsrc=r={sample_rate}:cl={channel_layout}",
                "-t", str(duration_seconds),
                "-c:a", "aac",
                str(destination),
            ],
            capture_output=True,
            text=True,
            check=True,
        )
    except FileNotFoundError as exc:
        raise MediaToolError("ffmpeg not found on PATH") from exc
    except subprocess.CalledProcessError as exc:
        raise MediaToolError(
            f"ffmpeg silence generation failed for {destination.name}: "
            f"{exc.stderr.strip()}"
        ) from exc


def mux_audio_track(
    video_source: Path, audio_source: Path, destination: Path
) -> None:
    """Remux `video_source`'s own video stream together with
    `audio_source`'s own audio stream into `destination`, both as a
    plain stream copy (no re-encode of either).

    Built for trip_export.py's own front.mp4 pipeline: `video_source`
    is the video-only concat `concatenate_media(..., video_only=True)`
    produces (see that function's own docstring for why front.mp4's
    raw per-recording audio can't be trusted to survive concatenation
    intact), and `audio_source` is the trip's separately-concatenated
    `audio.aac` - the same file already muxed into stitch.mp4 (see
    stitch.py) - giving front.mp4 real, correctly-timed audio again
    without ever routing it back through the concat demuxer's fragile
    same-stream-layout requirement.

    `destination` must not be the same file as `video_source` - ffmpeg
    can't read and write the same file in a single pass. Swapping the
    result into `video_source`'s own place, if that's the goal, is the
    caller's job (write to a temp path, then move it into place -
    trip_export.py does exactly this for front.mp4).
    """

    destination.parent.mkdir(parents=True, exist_ok=True)

    try:
        subprocess.run(
            [
                "ffmpeg", "-y",
                "-i", str(video_source),
                "-i", str(audio_source),
                "-map", "0:v:0",
                "-map", "1:a:0",
                "-c", "copy",
                str(destination),
            ],
            capture_output=True,
            text=True,
            check=True,
        )
    except FileNotFoundError as exc:
        raise MediaToolError("ffmpeg not found on PATH") from exc
    except subprocess.CalledProcessError as exc:
        raise MediaToolError(
            f"ffmpeg audio mux failed for {destination.name}: "
            f"{exc.stderr.strip()}"
        ) from exc


def trim_media(source: Path, destination: Path, duration_seconds: float) -> None:
    """Cut `source` down to its own first `duration_seconds` via a
    plain ffmpeg stream copy - no re-encode, no filters, nothing
    generated. This only ever removes packets from the tail of a
    file that's already one continuous, valid encoder session, so
    it's safe in exactly the way padding the *shorter* side of a
    front/rear mismatch would not be: extending a file means
    splicing in synthetically generated frames from a different
    encoder session, the same class of corruption that
    concatenate_media()'s docstring (and the removed parking
    -placeholder feature - see WORKING_CONTEXT.md) already covers in
    detail. Shortening a file needs nothing new spliced in at all.

    Used by trip_export.py's _align_front_rear_durations() to bring
    a recording's longer side (front or rear) down to match its
    shorter side when the two differ by more than a small tolerance
    - see that function's own docstring for why the two can differ
    in the first place.
    """

    destination.parent.mkdir(parents=True, exist_ok=True)

    try:
        subprocess.run(
            [
                "ffmpeg", "-y",
                "-i", str(source),
                "-t", str(duration_seconds),
                "-c", "copy",
                str(destination),
            ],
            capture_output=True,
            text=True,
            check=True,
        )
    except FileNotFoundError as exc:
        raise MediaToolError("ffmpeg not found on PATH") from exc
    except subprocess.CalledProcessError as exc:
        raise MediaToolError(
            f"ffmpeg trim failed for {source.name}: {exc.stderr.strip()}"
        ) from exc


def trim_media_head(
    source: Path, destination: Path, offset_seconds: float
) -> None:
    """Cut the first `offset_seconds` off the *start* of `source`, via
    a plain ffmpeg stream copy (`-ss` before `-i`, the fast input-side
    seek) - the head-trimming counterpart to trim_media()'s own tail
    cut, used by trip_export.py's _trim_prebuffers() to remove a
    detected pre-record-buffer overlap from the front of an Event/
    Manual recording.

    Stream copy can't cut at an arbitrary byte-exact position - only a
    re-encode could guarantee that, at real time cost for what's
    normally a handful of seconds - so like every other trim in this
    module, this snaps to the nearest keyframe. Deliberately the
    *preceding* keyframe (ffmpeg's own default for an input-side seek
    with `-c copy`): worst case, this leaves a small residual sliver
    of the original duplicate content still in place, rather than
    ever risking a seek that overshoots into genuinely new, no-longer-
    duplicate footage. Christer, on the resulting jump/glitch this can
    still leave at the cut point: "It would be nice to [not] have a
    visible jump/glitch, but if it can't be avoided its ok" - so this
    trades a small amount of trim precision for a fast, lossless cut,
    the same trade-off trim_media() itself already makes.
    """

    destination.parent.mkdir(parents=True, exist_ok=True)

    try:
        subprocess.run(
            [
                "ffmpeg", "-y",
                "-ss", str(offset_seconds),
                "-i", str(source),
                "-c", "copy",
                str(destination),
            ],
            capture_output=True,
            text=True,
            check=True,
        )
    except FileNotFoundError as exc:
        raise MediaToolError("ffmpeg not found on PATH") from exc
    except subprocess.CalledProcessError as exc:
        raise MediaToolError(
            f"ffmpeg head trim failed for {source.name}: {exc.stderr.strip()}"
        ) from exc


def probe_video_dimensions(path: Path) -> tuple[int, int]:
    """Return `path`'s own (width, height) via ffprobe.

    Used by trip_export.py to size render_missing_camera_placeholder()
    below to exactly match a trip's real rear.mp4 sources - see that
    function's own docstring for why an exact match matters."""

    try:
        result = subprocess.run(
            [
                "ffprobe", "-v", "error",
                "-select_streams", "v:0",
                "-show_entries", "stream=width,height",
                "-of", "json",
                str(path),
            ],
            capture_output=True,
            text=True,
            check=True,
        )
    except FileNotFoundError as exc:
        raise MediaToolError("ffprobe not found on PATH") from exc
    except subprocess.CalledProcessError as exc:
        raise MediaToolError(
            f"ffprobe failed for {path.name}: {exc.stderr.strip()}"
        ) from exc

    try:
        stream = json.loads(result.stdout)["streams"][0]
        return int(stream["width"]), int(stream["height"])
    except (KeyError, IndexError, ValueError) as exc:
        raise MediaToolError(
            f"could not parse ffprobe output for {path.name}"
        ) from exc


def render_missing_camera_placeholder(
    destination: Path,
    duration_seconds: float,
    *,
    width: int,
    height: int,
    fps: float = 30.0,
) -> None:
    """Render a black `duration_seconds`-long video with a red
    diagonal cross drawn corner-to-corner across it, at `width`x
    `height`/`fps` - a stand-in for a recording whose own rear video
    is simply missing (SD card fault, camera glitch, ...) while its
    front video still exists and is otherwise a normal part of the
    trip.

    Built for trip_export.py's own rear.mp4 pipeline (task #799).
    Christer, on a real export that logged "concatenated rear.mp4 from
    5 recording(s)" against "front.mp4 from 6 recording(s))": "So this
    will give a black rearview mirror for a while or?" Plain black was
    rejected in favor of reusing the same visual language the archive
    browser's thumbnail grid already uses for a recording with no
    video at all (see base.html's `.thumb-no-video` CSS, task #432) -
    a red diagonal cross reads unambiguously as "deliberately marked
    missing", not "camera glitched / lens cap / tunnel". Drawn corner-
    to-corner here rather than at the thumbnail's fixed +/-35 degrees -
    a thumbnail is always roughly the same aspect ratio, a dashcam
    video frame isn't guaranteed to be, and corner-to-corner always
    fills the frame correctly regardless of aspect ratio.

    Rendered as a single static PIL frame, looped by ffmpeg
    (`-loop 1`) for the whole duration, rather than writing out one
    PNG per held frame the way map_video.py's Parking frame-holding
    optimization does (see that function's own docstring) - nothing
    here changes frame to frame, so one input frame is enough even for
    a multi-minute gap.

    `width`/`height` must match the caller's other rear.mp4 sources'
    own resolution exactly - trip_export.py's own caller always forces
    `force_reencode=True` on the whole rear.mp4 concat whenever this
    placeholder is used (see concatenate_media()'s/
    `_concat_filter_reencode()`'s own docstrings for why: a file
    rendered here never shares the camera's own encoder session, so
    it can't safely go through the normal stream-copy concat path -
    exactly the corruption class the removed parking-transition-clip
    feature hit through that same path; see `_concatenate_asset()`'s
    own docstring). `_concat_filter_reencode()`'s own `concat` filter
    has no scale step of its own, so a mismatched width/height here
    would fail or misbehave rather than being silently corrected.
    """

    from PIL import Image
    from PIL import ImageDraw

    destination.parent.mkdir(parents=True, exist_ok=True)

    image = Image.new("RGB", (width, height), (0, 0, 0))
    draw = ImageDraw.Draw(image)
    # Same red as the archive browser's own .thumb-no-video CSS
    # (#e5484d), so the two "this footage is missing" markers in the
    # app read as the same visual language rather than two unrelated
    # ones.
    red = (229, 72, 77)
    line_width = max(6, min(width, height) // 40)
    draw.line([(0, 0), (width, height)], fill=red, width=line_width)
    draw.line([(width, 0), (0, height)], fill=red, width=line_width)

    with tempfile.TemporaryDirectory(
        prefix="bv_export_rear_placeholder_", ignore_cleanup_errors=True
    ) as frame_dir:
        frame_path = Path(frame_dir) / "placeholder.png"
        image.save(frame_path)

        encode_with_nvenc_fallback(
            [
                "-loop", "1",
                "-framerate", str(fps),
                "-i", str(frame_path),
                "-t", str(duration_seconds),
            ],
            destination,
        )


def render_image_as_video(
    source_image: Path,
    destination: Path,
    duration_seconds: float,
    *,
    width: int,
    height: int,
    fps: float = 30.0,
) -> None:
    """Render a still photo as a `duration_seconds`-long video at
    `width`x`height`/`fps`, held on the one real frame for the whole
    span - the concrete mechanics behind Christer's own framing of
    photo support: "a picture is also a video, but 1 frame only." See
    archive/photo.py's module docstring for the full "why store a
    photo under Asset.FRONT at all" design; this function is where
    that framing actually becomes a real video file, right before
    it's spliced into a trip's concatenated front.mp4 (see
    trip_export.py's `_photo_clip_overrides()`).

    Sibling to `render_missing_camera_placeholder()` just above,
    same `-loop 1` + `encode_with_nvenc_fallback()` shape, but reusing
    that function directly wasn't a fit: it always draws its own
    placeholder frame from scratch (nothing to scale/pad, since PIL
    controls the canvas size exactly), where this one has to start
    from an arbitrary real photo - almost never already `width`x
    `height`, or even the same aspect ratio (a portrait phone photo
    dropped into a landscape dashcam trip, for instance). ffmpeg does
    that scale/pad step itself (`scale=...:force_original_aspect_ratio
    =decrease,pad=...`) rather than PIL: PIL's own format support
    doesn't reliably cover every extension `archive/photo.py`'s
    PHOTO_EXTENSIONS recognizes (HEIC in particular needs a plugin PIL
    doesn't ship with; ffmpeg's own decoders are the more dependable
    common denominator across jpg/png/heic/gpr). Letterboxed onto a
    black background (the `pad` filter's own default) rather than
    cropped to fill - cropping a photo the person actually chose to
    keep risks cutting off the part of it they cared about; a photo
    momentarily in front of a real video, framed by black bars, reads
    as intentional rather than a bug the way a stretched or oddly
    cropped frame would.

    `width`/`height` must match the caller's other front.mp4 sources'
    own resolution exactly, same reasoning and same
    `force_reencode=True` requirement as
    `render_missing_camera_placeholder()`'s own docstring - a photo
    clip rendered here never shares the surrounding recordings' own
    encoder session either.
    """

    destination.parent.mkdir(parents=True, exist_ok=True)

    scale_pad_filter = (
        f"scale={width}:{height}:force_original_aspect_ratio=decrease,"
        f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2:color=black"
    )

    encode_with_nvenc_fallback(
        [
            "-loop", "1",
            "-framerate", str(fps),
            "-i", str(source_image),
            "-t", str(duration_seconds),
            "-vf", scale_pad_filter,
        ],
        destination,
    )
