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
import subprocess
import tempfile
from pathlib import Path

from ..generate.media import MediaToolError

# Forced onto every source `_strip_audio_stream_copy()` normalizes
# before a `video_only=True` concat - see that function's own
# docstring for why. 90000 is the standard MPEG PTS clock rate
# (90kHz), a safe, universally-supported choice unrelated to any
# particular source's own frame rate.
_CONCAT_VIDEO_TRACK_TIMESCALE = 90000


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

    caller_set_rate_control = any(
        flag in extra_codec_args for flag in _CALLER_RATE_CONTROL_FLAGS
    )

    if _nvenc_available():
        quality_args = (
            [] if caller_set_rate_control else _DEFAULT_NVENC_QUALITY_ARGS
        )
        try:
            _run_ffmpeg_encode(
                [
                    "-c:v", "h264_nvenc", "-pix_fmt", "yuv420p",
                    *quality_args, *extra_codec_args,
                ],
                input_args, destination,
            )
            return
        except FileNotFoundError as exc:
            raise MediaToolError("ffmpeg not found on PATH") from exc
        except subprocess.CalledProcessError:
            pass  # fall through to the CPU encoder below

    quality_args = (
        [] if caller_set_rate_control else _DEFAULT_LIBX264_QUALITY_ARGS
    )
    try:
        _run_ffmpeg_encode(
            [
                "-c:v", "libx264", "-pix_fmt", "yuv420p",
                *quality_args, *extra_codec_args,
            ],
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


def concatenate_media(
    sources: list[Path], destination: Path, *, video_only: bool = False
) -> None:
    """Concatenate video or audio files, in order, into `destination`
    via ffmpeg's concat demuxer, copying streams without re-encoding.

    Works for a single source too (a plain stream copy). Does nothing
    if `sources` is empty.

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
    -checking the value against Christer's requested 0.10-5.0 window is
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
