"""
Camera composition for bv-export --stitch: combines a trip's
front/rear footage into one video via ffmpeg's hstack/vstack filters.

This is the first --stitch building block - see WORKING_CONTEXT.md for
the full agreed spec. Only the two camera layouts that are a straight
stack of unmodified footage are built so far. rearview_mirror (flip +
scale + overlay), the map panel, the g-sensor overlay, subtitle
burn-in, and auto-picking a layout from the trip's own geometry all
come in later passes.

Copyright (C) 2026 Christer R. (sssreh)

SPDX-License-Identifier: GPL-3.0-or-later
"""

from __future__ import annotations

import json
import subprocess
import sys
import time
from pathlib import Path

from ..generate.media import MediaToolError
from .media import concatenate_media
from .media import encode_with_nvenc_fallback

# side_by_side places front and rear next to each other (ffmpeg
# hstack) - per the agreed --stitch spec, this is the layout a trip
# that runs mostly east-west will eventually auto-pick. top_down
# stacks them one above the other (vstack) - the north-south pick.
# Auto-picking from the trip's own geometry isn't built yet, so
# `layout` is always explicit for now.
STACK_LAYOUTS = {
    "side_by_side": "hstack",
    "top_down": "vstack",
}

# Cached after the first check, same pattern as media.py's
# _NVENC_AVAILABLE - which hwaccels this machine's ffmpeg build has is
# a fixed fact for the life of the run.
_NVDEC_AVAILABLE: bool | None = None


def _nvdec_available() -> bool:
    """Return True if this machine's ffmpeg build lists "cuda" among
    its hwaccels (NVIDIA's hardware video decoder, NVDEC).

    Same caveat as media.py's _nvenc_available(): being listed doesn't
    guarantee a specific file will actually decode via NVDEC (codec/
    profile support varies) - a failed attempt just falls back to
    plain CPU decode, so a wrong "True" here costs one failed attempt,
    not a broken stitch.
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


def stitch_cameras(
    front: Path | None,
    rear: Path | None,
    destination: Path,
    *,
    layout: str,
    resolution: tuple[int, int] | None = None,
    bitrate: str | None = None,
    debug: bool = False,
) -> Path | None:
    """Compose a trip's front/rear footage into one video at
    `destination`.

    `layout` must be one of STACK_LAYOUTS's keys ('side_by_side' or
    'top_down' - 'rearview_mirror' isn't built yet). Only meaningful
    when both front and rear exist; a trip with just one of the two
    (the common single-front-camera case) falls back to a plain copy
    of whichever one is available, ignoring `layout` entirely - the
    same "don't fail, just do the sensible thing" convention the rest
    of bv-export follows for a missing optional input - unless
    `resolution`/`bitrate` are given too, in which case the single
    camera still gets re-encoded to honor them (a plain stream copy
    can't resize or re-bitrate). Returns None if neither exists.

    `resolution`, if given, is an (width, height) pixel pair the final
    output is scaled to (preserving aspect ratio, letterboxed to
    exactly fill it - see _fit_and_pad()) - handy for a fast, small
    test render (e.g. (320, 240)) instead of waiting on a
    full-resolution encode. `bitrate`, if given, is passed straight to
    ffmpeg as `-b:v` (plus matching `-maxrate`/`-bufsize` to actually
    constrain it - e.g. "256k", "2M").

    Decoding the source video(s) tries NVIDIA's hardware decoder
    (NVDEC) first when available (see _nvdec_available()), falling
    back to plain CPU decode if that fails for real - independent of
    encode_with_nvenc_fallback()'s own NVENC/libx264 choice for the
    *encode* side, so decode and encode each pick GPU-vs-CPU on their
    own. Only the encode side was GPU-accelerated before this; decode
    is real, unavoidable per-frame work for the source video's full
    length regardless of how small the requested output is, so it was
    the dominant cost of a --stitch run on a real (especially 4K)
    front camera.

    No audio track is carried into the stitched video yet - trip-level
    audio already lives in its own audio.aac (see trip_export.py),
    muxing that back in is a later --stitch pass, not this one.

    `debug=True` prints which decode method (nvdec or cpu) was
    attempted, whether it succeeded or fell back, and how long that
    ffmpeg call took, to stderr - see bv_export.py's --debug flag.
    """

    if front is not None and rear is not None:
        return _stack(
            front, rear, destination,
            layout=layout, resolution=resolution, bitrate=bitrate,
            debug=debug,
        )

    only = front or rear
    if only is None:
        return None

    if resolution is None and bitrate is None:
        concatenate_media([only], destination)
        return destination

    _reencode_single(
        only, destination,
        resolution=resolution, bitrate=bitrate, debug=debug,
    )
    return destination


def _video_dimensions(path: Path) -> tuple[int, int]:
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


def _bitrate_args(bitrate: str | None) -> list[str]:
    """ffmpeg codec args constraining the encode to `bitrate` (e.g.
    "256k") - -b:v alone is only a target/average for most encoders,
    so -maxrate/-bufsize are set to the same value to actually cap it,
    which matters for a deliberately-small test render."""

    if bitrate is None:
        return []
    return ["-b:v", bitrate, "-maxrate", bitrate, "-bufsize", bitrate]


def _fit_and_pad(input_label: str, output_label: str, width: int, height: int) -> str:
    """A filter-chain fragment scaling `input_label` to fit inside a
    width x height box without distorting its own aspect ratio
    (force_original_aspect_ratio=decrease - shrinks to fit, never
    stretches past the box), then pads with black bars to exactly
    reach width x height (letterboxed or pillarboxed, whichever axis
    ends up smaller than the box).

    Confirmed against Christer's real archive: a plain scale=W:H
    (stretching to the exact requested size regardless of the
    source's own shape) visibly distorted the picture whenever the
    stitched composite's natural aspect ratio didn't match the
    requested one - e.g. two 16:9 cameras side by side come out
    ultra-wide (~3.56:1), and forcing that into a --stitch-resolution
    like 320x240 (4:3) squeezed the width far more than the height.
    This fit-then-pad idiom keeps the file's own output dimensions
    exactly what was asked for, without warping the actual picture.
    """

    return (
        f"[{input_label}]scale=w={width}:h={height}:"
        "force_original_aspect_ratio=decrease,"
        f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2[{output_label}]"
    )


def _hwaccel_input_args(source: Path, *, hw_decode: bool) -> list[str]:
    """The -i args for one input, with NVDEC decode flags prepended
    when `hw_decode` is True. -hwaccel_output_format cuda keeps
    decoded frames in GPU memory - a later "hwdownload,format=nv12" in
    the filter graph (see _hw_predecode_filter()) is what brings them
    back to normal CPU frames for the (CPU-only) scale/stack/pad
    filters this module uses."""

    if hw_decode:
        return ["-hwaccel", "cuda", "-hwaccel_output_format", "cuda", "-i", str(source)]
    return ["-i", str(source)]


def _hw_predecode_filter(hw_decode: bool) -> str:
    """A filter-chain prefix bringing a hardware-decoded (GPU-resident)
    stream back to normal CPU frames, or nothing at all for a plain
    CPU-decoded stream which is already in that form. See
    _hwaccel_input_args()."""

    return "hwdownload,format=nv12," if hw_decode else ""


def _report_decode_timing(
    label: str, method: str, seconds: float, *, failed: bool, debug: bool
) -> None:
    """A one-line stderr timing report for a decode attempt - not
    warnings (nothing went wrong from the user's point of view if a
    GPU attempt fails and falls back), just diagnostic breadcrumbs so
    "the whole run got slower/faster" can be traced back to which
    decode path was actually used and how long ffmpeg itself spent on
    it. See WORKING_CONTEXT.md's NVDEC follow-up for why this was
    added - the first real-archive run showed NVDEC decode succeeding
    but the overall stitch step still coming out slower than plain CPU
    decode, and there was no way to tell from the outside whether that
    was hwdownload's GPU->CPU copy cost, two simultaneous NVDEC
    sessions contending for one decoder engine, or something else
    without this breakdown.

    Silent unless `debug` is True (bv_export.py's --debug flag) - most
    runs don't want this on stderr.
    """

    if not debug:
        return

    outcome = "failed" if failed else "succeeded"
    print(
        f"stitch: {label} decode={method} {outcome} in {seconds:.1f}s",
        file=sys.stderr,
    )


def _reencode_single(
    source: Path,
    destination: Path,
    *,
    resolution: tuple[int, int] | None,
    bitrate: str | None,
    debug: bool = False,
) -> None:
    if _nvdec_available():
        start = time.monotonic()
        try:
            _run_reencode_single(
                source, destination,
                resolution=resolution, bitrate=bitrate, hw_decode=True,
            )
        except MediaToolError:
            _report_decode_timing(
                destination.name, "nvdec", time.monotonic() - start,
                failed=True, debug=debug,
            )
            # fall through to plain CPU decode below
        else:
            _report_decode_timing(
                destination.name, "nvdec", time.monotonic() - start,
                failed=False, debug=debug,
            )
            return

    start = time.monotonic()
    _run_reencode_single(
        source, destination,
        resolution=resolution, bitrate=bitrate, hw_decode=False,
    )
    _report_decode_timing(
        destination.name, "cpu", time.monotonic() - start,
        failed=False, debug=debug,
    )


def _run_reencode_single(
    source: Path,
    destination: Path,
    *,
    resolution: tuple[int, int] | None,
    bitrate: str | None,
    hw_decode: bool,
) -> None:
    input_args = _hwaccel_input_args(source, hw_decode=hw_decode)
    predecode = _hw_predecode_filter(hw_decode)

    if resolution is not None:
        width, height = resolution
        input_args += [
            "-filter_complex", predecode + _fit_and_pad("0:v", "v", width, height),
            "-map", "[v]",
        ]
    elif hw_decode:
        input_args += [
            "-filter_complex", f"[0:v]{predecode}[v]",
            "-map", "[v]",
        ]
    else:
        input_args += ["-map", "0:v"]

    encode_with_nvenc_fallback(
        input_args, destination, extra_codec_args=_bitrate_args(bitrate)
    )


def _stack(
    front: Path,
    rear: Path,
    destination: Path,
    *,
    layout: str,
    resolution: tuple[int, int] | None,
    bitrate: str | None,
    debug: bool = False,
) -> Path:
    if layout not in STACK_LAYOUTS:
        raise ValueError(
            f"unknown stitch layout: {layout!r} "
            f"(expected one of {sorted(STACK_LAYOUTS)})"
        )

    filter_name = STACK_LAYOUTS[layout]
    front_width, front_height = _video_dimensions(front)

    if _nvdec_available():
        start = time.monotonic()
        try:
            _run_stack(
                front, rear, destination,
                filter_name=filter_name,
                front_width=front_width, front_height=front_height,
                resolution=resolution, bitrate=bitrate, hw_decode=True,
            )
        except MediaToolError:
            _report_decode_timing(
                destination.name, "nvdec", time.monotonic() - start,
                failed=True, debug=debug,
            )
            # fall through to plain CPU decode below
        else:
            _report_decode_timing(
                destination.name, "nvdec", time.monotonic() - start,
                failed=False, debug=debug,
            )
            return destination

    start = time.monotonic()
    _run_stack(
        front, rear, destination,
        filter_name=filter_name,
        front_width=front_width, front_height=front_height,
        resolution=resolution, bitrate=bitrate, hw_decode=False,
    )
    _report_decode_timing(
        destination.name, "cpu", time.monotonic() - start,
        failed=False, debug=debug,
    )
    return destination


def _run_stack(
    front: Path,
    rear: Path,
    destination: Path,
    *,
    filter_name: str,
    front_width: int,
    front_height: int,
    resolution: tuple[int, int] | None,
    bitrate: str | None,
    hw_decode: bool,
) -> None:
    predecode = _hw_predecode_filter(hw_decode)

    # hstack only requires matching *heights* (it concatenates
    # horizontally, combined width is whatever the two widths sum to);
    # vstack only requires matching *widths*. Only scale rear on the
    # one dimension that actually needs to match front (probed
    # directly - see _video_dimensions()), leaving the other free via
    # ffmpeg's "-2" (auto-computed, rounded to an even number for H.264)
    # so rear's own aspect ratio is preserved rather than forced to
    # front's. (An earlier version scaled rear to front's exact width
    # *and* height, which happened to look fine only because a real
    # front/rear pair tested had the same aspect ratio as each other -
    # not a safe assumption in general.)
    if filter_name == "hstack":
        rear_scale = f"scale=-2:{front_height}"
    else:
        rear_scale = f"scale={front_width}:-2"

    clauses = [f"[1:v]{predecode}{rear_scale}[rear_scaled]"]
    front_label = "0:v"
    if predecode:
        clauses.insert(0, f"[0:v]{predecode}null[front_predecoded]")
        front_label = "front_predecoded"

    clauses.append(f"[{front_label}][rear_scaled]{filter_name}=inputs=2[stacked]")
    output_label = "stacked"

    # A second pass on the finished composite, if a specific output
    # resolution was requested (e.g. a fast small test render) -
    # independent of the front/rear-matching scale above, and using
    # the aspect-preserving fit-then-pad idiom rather than a plain
    # stretch, since the composite's own shape (e.g. ultra-wide for
    # side_by_side) rarely matches an arbitrary requested resolution.
    if resolution is not None:
        out_width, out_height = resolution
        clauses.append(_fit_and_pad("stacked", "final", out_width, out_height))
        output_label = "final"

    encode_with_nvenc_fallback(
        [
            *_hwaccel_input_args(front, hw_decode=hw_decode),
            *_hwaccel_input_args(rear, hw_decode=hw_decode),
            "-filter_complex", ";".join(clauses),
            "-map", f"[{output_label}]",
        ],
        destination,
        extra_codec_args=_bitrate_args(bitrate),
    )
