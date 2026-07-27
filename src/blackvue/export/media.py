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

import subprocess
import tempfile
from pathlib import Path

from ..generate.media import MediaToolError


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

# encode_with_nvenc_fallback()'s video_codec -> (nvenc encoder name,
# libx264-equivalent CPU encoder name, extra muxer args) - "h264" is
# the long-standing default every existing caller (map_video.py,
# gsensor_video.py, stitch.py) still gets with no change to their own
# call sites. "hevc" exists specifically for parking_transition.py's
# placeholder clip, which has to match whatever codec the real
# recording it's standing in for actually uses: ffmpeg's concat
# demuxer (concatenate_media(), used to splice the placeholder in
# alongside real front.mp4/rear.mp4 segments) stream-copies rather
# than re-encodes, and simply declaring two different codecs' raw
# bitstreams under one container without re-encoding produces a file
# that muxes without complaint but is unparseable by any real decoder
# from the splice point onward - confirmed against a real trip from
# Christer's own 4K HEVC camera: "Invalid NAL unit size" errors and a
# frozen picture (with audio, muxed separately, playing on normally)
# starting exactly at a mid-trip Parking placeholder that had been
# unconditionally encoded as H.264. `-tag:v hvc1` matches the fourcc
# BlackVue's own HEVC recordings use (confirmed via ffprobe on
# Christer's real front.mp4: "hevc (Main) (hvc1 / 0x31637668)") -
# ffmpeg's own libx265/hevc_nvenc default to "hev1" otherwise, a
# different (also valid, but non-matching) way of signaling the same
# codec that risks the exact same kind of splice mismatch this exists
# to avoid.
_VIDEO_CODEC_ENCODERS: dict[str, tuple[str, str, list[str]]] = {
    "h264": ("h264_nvenc", "libx264", []),
    "hevc": ("hevc_nvenc", "libx265", ["-tag:v", "hvc1"]),
}


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
    *,
    video_codec: str = "h264",
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

    `video_codec` selects which codec to encode as - "h264" (the
    default every existing caller still gets, mapping to
    h264_nvenc/libx264) or "hevc" (hevc_nvenc/libx265, tagged "hvc1" -
    see _VIDEO_CODEC_ENCODERS' own comment) - for
    parking_transition.py's placeholder clip, which has to match
    whatever codec the real recording it stands in for actually uses,
    not always assume H.264. `_nvenc_available()` itself only ever
    checks for h264_nvenc specifically (see its own docstring) - a
    machine with h264_nvenc but not hevc_nvenc would still attempt an
    NVENC hevc encode first, but that failure is caught exactly the
    same way an unavailable/broken h264_nvenc already is, falling
    through to the CPU encoder - no separate hevc_nvenc capability
    check needed for correctness, just a possible one wasted attempt.

    `extra_codec_args`, if given, are appended after the base codec
    args on *both* the NVENC and CPU attempts (e.g. a bitrate cap) -
    encoder-agnostic settings the caller wants regardless of which of
    the two actually ends up encoding.

    Unless `extra_codec_args` already contains its own rate-control
    flag (see _CALLER_RATE_CONTROL_FLAGS), a default quality target
    (_DEFAULT_NVENC_QUALITY_ARGS/_DEFAULT_LIBX264_QUALITY_ARGS) is
    applied instead of leaving it to nvenc/libx264's own internal
    defaults - see those constants' own comment for why that turned
    out to matter for real. The same two constants are reused for
    "hevc" too - -cq/-crf are generic rate-control flags both
    hevc_nvenc and libx265 also understand, so no separate HEVC-tuned
    quality constants were needed.

    Shared by every "encode a video via ffmpeg" caller in bv-export
    (map_video.py/gsensor_video.py's frame sequences via
    encode_frame_sequence() below, stitch.py's camera composition,
    parking_transition.py's placeholder clip) so they all get the same
    NVENC-then-CPU fallback behavior, and the same default quality
    safety net, for free.
    """

    extra_codec_args = extra_codec_args or []
    destination.parent.mkdir(parents=True, exist_ok=True)
    nvenc_encoder, cpu_encoder, tag_args = _VIDEO_CODEC_ENCODERS[video_codec]

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
                    "-c:v", nvenc_encoder, "-pix_fmt", "yuv420p", *tag_args,
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
                "-c:v", cpu_encoder, "-pix_fmt", "yuv420p", *tag_args,
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


def concatenate_media(sources: list[Path], destination: Path) -> None:
    """Concatenate video or audio files, in order, into `destination`
    via ffmpeg's concat demuxer, copying streams without re-encoding.

    Works for a single source too (a plain stream copy). Does nothing
    if `sources` is empty.
    """

    if not sources:
        return

    destination.parent.mkdir(parents=True, exist_ok=True)

    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".txt", delete=False, encoding="utf-8"
    ) as list_file:
        for source in sources:
            list_file.write(f"file '{_escape_concat_path(source)}'\n")
        list_path = Path(list_file.name)

    try:
        subprocess.run(
            [
                "ffmpeg",
                "-y",
                "-f", "concat",
                "-safe", "0",
                "-i", str(list_path),
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
            f"ffmpeg concat failed for {destination.name}: "
            f"{exc.stderr.strip()}"
        ) from exc
    finally:
        list_path.unlink(missing_ok=True)
