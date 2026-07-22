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


def _run_ffmpeg_encode(
    codec_args: list[str], input_args: list[str], destination: Path
) -> None:
    subprocess.run(
        ["ffmpeg", "-y", *input_args, *codec_args, str(destination)],
        capture_output=True,
        text=True,
        check=True,
    )


def encode_with_nvenc_fallback(input_args: list[str], destination: Path) -> None:
    """Run ffmpeg with `input_args` (whatever inputs/filters/maps the
    caller needs - a frame-sequence input, a multi-video
    filter_complex composition, etc.), encoding video with NVIDIA's
    hardware h264_nvenc encoder when this machine's ffmpeg build
    supports it (see _nvenc_available()), falling back to the software
    libx264 encoder otherwise - always used directly if NVENC isn't
    available, and also if an NVENC attempt itself fails (e.g. the
    encoder is listed but no compatible GPU/driver is actually
    present) - so this always produces a video either way, just faster
    when a real NVIDIA GPU is there to use.

    Shared by every "encode a video via ffmpeg" caller in bv-export
    (map_video.py/gsensor_video.py's frame sequences via
    encode_frame_sequence() below, stitch.py's camera composition) so
    they all get the same NVENC-then-CPU fallback behavior for free.
    """

    destination.parent.mkdir(parents=True, exist_ok=True)

    if _nvenc_available():
        try:
            _run_ffmpeg_encode(
                ["-c:v", "h264_nvenc", "-pix_fmt", "yuv420p"],
                input_args, destination,
            )
            return
        except FileNotFoundError as exc:
            raise MediaToolError("ffmpeg not found on PATH") from exc
        except subprocess.CalledProcessError:
            pass  # fall through to the CPU encoder below

    try:
        _run_ffmpeg_encode(
            ["-c:v", "libx264", "-pix_fmt", "yuv420p"],
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
