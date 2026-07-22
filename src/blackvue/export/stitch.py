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

import concurrent.futures
import json
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from ..generate.media import MediaToolError
from ..telemetry.gps_reader import GpsFix
from .map_video import render_map_video
from .media import concatenate_media
from .media import encode_with_nvenc_fallback
from .osm_roads import Road
from .osm_roads import aspect_ratio_of
from .osm_roads import bounding_box_for_fixes

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

# --stitch-map's default panel side when --stitch-map-side isn't given
# explicitly, keyed by camera `layout` - per the agreed spec: a
# top_down (tall) camera column gets its map panel on the left (itself
# free to be any height, camera column stays the tall piece); a
# side_by_side (wide) camera row gets its map panel on the bottom -
# nested perpendicular to the camera arrangement so the final frame
# doesn't turn into a long thin ribbon in either direction.
# rearview_mirror isn't in STACK_LAYOUTS yet (not implemented), so it
# has no entry here either.
_DEFAULT_MAP_SIDE_FOR_LAYOUT = {
    "side_by_side": "down",
    "top_down": "left",
}

# The map panel's free dimension (the one not forced to match the
# camera composite - see _map_panel_dimensions()) is clamped to this
# fraction range of the composite's own corresponding dimension, so a
# near-straight-line trip (real-world aspect ratio close to 0 or
# infinite) can't produce a degenerate sliver or an oversized panel
# that dominates the frame - the camera footage is meant to stay the
# primary content.
_MIN_MAP_PANEL_FRACTION = 0.2
_MAX_MAP_PANEL_FRACTION = 0.5

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
    map_mode: str | None = None,
    map_side: str | None = None,
    map_zoom_meters: float | None = None,
    map_fixes: tuple[GpsFix, ...] = (),
    map_roads: tuple[Road, ...] = (),
    map_icon: Path | None = None,
    debug: bool = False,
    warnings: list[str] | None = None,
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

    When both front and rear exist, each is decoded (and, for rear,
    scaled to match front on the one axis hstack/vstack requires) in
    its own separate ffmpeg process, run concurrently, before a final
    CPU-only pass combines the two results - not one ffmpeg process
    handling both hardware-decoded inputs at once. That's a deliberate
    choice, not an implementation detail: a controlled test on a real
    archive found one process with two unshared -hwaccel cuda inputs
    cost ~5x the sum of decoding each alone, and even pinning both to
    one shared CUDA device only recovered a small fraction of that -
    ffmpeg's filter graph engine appears to serialize hardware-decoded
    frame handling across simultaneous inputs within a single process.
    See _decode_camera()'s docstring and WORKING_CONTEXT.md's --stitch
    NVDEC follow-ups for the full investigation.

    No audio track is carried into the stitched video yet - trip-level
    audio already lives in its own audio.aac (see trip_export.py),
    muxing that back in is a later --stitch pass, not this one.

    `debug=True` prints which decode method (nvdec or cpu) was
    attempted, whether it succeeded or fell back, and how long that
    ffmpeg call took, to stderr - see bv_export.py's --debug flag.

    When both front and rear exist, a requested `bitrate` is also
    checked against a ceiling: the two intermediates hstack/vstack
    actually combines (see _stack()) are already resolution- and
    bitrate-reduced from the original source, so they're the true
    information ceiling for the final combine - not the original
    cameras' own native bitrates, which the final pass never sees
    again. A `bitrate` request above the sum of the two intermediates'
    own actual bitrates is silently spending bits the encoder can't
    use to recover detail that was already discarded upstream - capped
    to that sum instead, with a message appended to `warnings` (if
    given) explaining the cap. Skipped entirely (no probing, no
    warning) if `bitrate` is None, or if either intermediate's own
    bitrate can't be determined.

    `map_mode` ('map' or 'zoom', matching --stitch-map's values),
    if given, additionally composes a map panel alongside the camera
    composite - see _map_panel_dimensions()/_render_map_panel(). Only
    meaningful when both front and rear exist (the single-camera
    fallback below ignores it entirely, same as `layout` - not yet
    built for that simpler path). `map_side` overrides the panel's
    default side (see _DEFAULT_MAP_SIDE_FOR_LAYOUT); `map_zoom_meters`
    is required when `map_mode == "zoom"` (reused as the panel's
    follow-camera radius - normally whatever --map-zoom METERS was
    also given). `map_fixes`/`map_roads` are the trip's already-loaded
    GPS fixes/OSM road geometry (see trip_export.py's
    _load_trip_roads()) - `map_mode` is a no-op if `map_fixes` is
    empty. `map_icon` is the same custom position-marker image
    --map/--map-zoom accept. Any map-panel problem (no GPS data, no
    default side for an unrecognized layout, a missing zoom radius, an
    image-load failure) degrades to a `warnings` entry and no panel,
    never a failed stitch - the camera composite alone is still worth
    having.
    """

    if front is not None and rear is not None:
        return _stack(
            front, rear, destination,
            layout=layout, resolution=resolution, bitrate=bitrate,
            map_mode=map_mode, map_side=map_side,
            map_zoom_meters=map_zoom_meters, map_fixes=map_fixes,
            map_roads=map_roads, map_icon=map_icon,
            debug=debug, warnings=warnings,
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


def _video_bitrate(path: Path) -> int | None:
    """Return `path`'s own container-level bit rate in bits/second, or
    None if ffprobe can't report one (a very short clip, an unusual
    container, etc.) - used by _stack() to work out a sensible ceiling
    for a requested --stitch-bitrate, never to fail the export over.

    Deliberately not raising MediaToolError here (unlike
    _video_dimensions()) - a missing bitrate just means skipping the
    ceiling check, not aborting the whole stitch.
    """

    try:
        result = subprocess.run(
            [
                "ffprobe", "-v", "error",
                "-show_entries", "format=bit_rate",
                "-of", "json",
                str(path),
            ],
            capture_output=True,
            text=True,
            check=True,
        )
    except (FileNotFoundError, subprocess.CalledProcessError):
        return None

    try:
        return int(json.loads(result.stdout)["format"]["bit_rate"])
    except (KeyError, TypeError, ValueError):
        return None


def _parse_bitrate_bps(value: str) -> int | None:
    """Parse an ffmpeg-style bitrate string ("256k", "2M", "1500000")
    into plain bits/second, or None if it doesn't parse - used to
    compare a requested --stitch-bitrate against a computed ceiling.
    Mirrors ffmpeg's own suffix convention (k/K = x1000, m/M = x1e6).
    """

    value = value.strip()
    multiplier = 1

    if value and value[-1] in "kK":
        multiplier = 1_000
        value = value[:-1]
    elif value and value[-1] in "mM":
        multiplier = 1_000_000
        value = value[:-1]

    try:
        return int(float(value) * multiplier)
    except ValueError:
        return None


def _bitrate_args(bitrate: str | None) -> list[str]:
    """ffmpeg codec args constraining the encode to `bitrate` (e.g.
    "256k") - -b:v alone is only a target/average for most encoders,
    so -maxrate/-bufsize are set to the same value to actually cap it,
    which matters for a deliberately-small test render."""

    if bitrate is None:
        return []
    return ["-b:v", bitrate, "-maxrate", bitrate, "-bufsize", bitrate]


def _fit_and_pad(
    input_label: str, output_label: str, width: int, height: int, *, prefix: str = ""
) -> str:
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

    `prefix`, if given, is an extra comma-chained filter (or filters)
    inserted right after the `[input_label]` reference and before
    `scale=` - e.g. "hwdownload,format=nv12," to bring a hardware-
    decoded stream back to CPU frames before scaling touches it. Must
    go *inside* the bracketed label reference, not before it - ffmpeg
    requires the input label first in a filter-chain link; putting a
    filter name before "[input_label]" is a syntax error, which is
    exactly the bug this parameter fixes (an earlier version built
    `prefix + _fit_and_pad(...)`, landing "hwdownload,format=nv12,"
    *before* "[0:v]" instead of after it - ffmpeg rejected that
    instantly rather than actually attempting NVDEC decode).
    """

    return (
        f"[{input_label}]{prefix}scale=w={width}:h={height}:"
        "force_original_aspect_ratio=decrease,"
        f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2[{output_label}]"
    )


# The named CUDA device every hwaccel input is pinned to via
# -hwaccel_device (see _hwaccel_input_args()/_shared_hw_device_args())
# - an arbitrary but consistent label, not a magic ffmpeg constant.
_HW_DEVICE_NAME = "cu"


def _shared_hw_device_args(hw_decode: bool) -> list[str]:
    """-init_hw_device args creating one explicit, named CUDA device
    ("cu", device index 0) up front - go once at the very start of the
    ffmpeg command, before any -i. Every hwaccel input then references
    this same device via -hwaccel_device (see _hwaccel_input_args())
    instead of implicitly creating its own separate CUDA context.

    This matters for real, not just in theory: a controlled test on
    Christer's real archive (RTX 5090 laptop GPU) found NVDEC-decoding
    front and rear concurrently in two SEPARATE ffmpeg processes cost
    barely more than decoding each alone (44.8s vs a 38.5s solo
    baseline for front; 19.0s vs 17.0s solo for rear - modest overlap
    overhead). But decoding both in ONE ffmpeg process with two
    *unshared* -hwaccel cuda inputs (this module's original behavior)
    cost 276.2s for the same two files - roughly 5x the sum of the two
    solo times, far more than real GPU decoder-engine contention would
    explain (the two-separate-processes result rules that out: if the
    NVDEC hardware itself were the bottleneck, running two decodes at
    once - in any process arrangement - would cost close to what was
    measured there, not 5x worse). The likely explanation: without an
    explicit shared device, ffmpeg opens two independent CUDA contexts
    (one per input) and pays real cross-context synchronization
    overhead once both feed into the same filter graph - a known rough
    edge in ffmpeg's multi-input hwaccel handling, and this is its
    documented fix.
    """

    if not hw_decode:
        return []
    return ["-init_hw_device", f"cuda={_HW_DEVICE_NAME}:0"]


def _hwaccel_input_args(source: Path, *, hw_decode: bool) -> list[str]:
    """The -i args for one input, with NVDEC decode flags prepended
    when `hw_decode` is True. -hwaccel_device pins this input to the
    one shared CUDA device created by _shared_hw_device_args() (must
    be called once, before any -i, in the same ffmpeg command) rather
    than letting this input open its own separate context - see that
    function's docstring for why that distinction turned out to matter
    a lot in practice. -hwaccel_output_format cuda keeps decoded
    frames in GPU memory - a later "hwdownload,format=nv12" in the
    filter graph (see _hw_predecode_filter()) is what brings them back
    to normal CPU frames for the (CPU-only) scale/stack/pad filters
    this module uses."""

    if hw_decode:
        return [
            "-hwaccel", "cuda",
            "-hwaccel_device", _HW_DEVICE_NAME,
            "-hwaccel_output_format", "cuda",
            "-i", str(source),
        ]
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
    input_args = _shared_hw_device_args(hw_decode) + _hwaccel_input_args(
        source, hw_decode=hw_decode
    )
    predecode = _hw_predecode_filter(hw_decode)

    if resolution is not None:
        width, height = resolution
        input_args += [
            "-filter_complex",
            _fit_and_pad("0:v", "v", width, height, prefix=predecode),
            "-map", "[v]",
        ]
    elif hw_decode:
        # predecode ("hwdownload,format=nv12,") ends with a trailing
        # comma, meant as a separator before whatever filter follows
        # it (see _fit_and_pad()'s `prefix` param above). With nothing
        # following it here, that trailing comma has to be stripped -
        # left in, "[0:v]hwdownload,format=nv12,[v]" is a malformed
        # filter chain (a dangling comma right before the output
        # label) that ffmpeg rejects instantly - the same class of bug
        # as the _fit_and_pad prefix-ordering one fixed earlier, just
        # in the one branch that historically never got exercised
        # (every real run so far always passed --stitch-resolution,
        # which takes the branch above instead).
        input_args += [
            "-filter_complex", f"[0:v]{predecode.rstrip(',')}[v]",
            "-map", "[v]",
        ]
    else:
        input_args += ["-map", "0:v"]

    encode_with_nvenc_fallback(
        input_args, destination, extra_codec_args=_bitrate_args(bitrate)
    )


def _decode_camera(
    source: Path,
    destination: Path,
    *,
    scale_filter: str | None,
    debug: bool = False,
) -> None:
    """Decode `source` (trying NVDEC first when available, falling
    back to plain CPU decode on a real failure - see
    _nvdec_available()) into a normal, CPU-readable intermediate video
    at `destination`, applying `scale_filter` (a raw ffmpeg
    "scale=..." expression) along the way if given, or leaving frame
    size untouched if not.

    Always its own ffmpeg process/call - by design, not an
    implementation detail. A controlled test on Christer's real
    archive (RTX 5090 laptop GPU) found decoding front and rear as two
    genuinely separate ffmpeg processes, run concurrently, cost barely
    more than decoding each alone. But combining both into ONE ffmpeg
    process - even after pinning both to a single shared CUDA device
    (see _shared_hw_device_args(), which helped only marginally, ~16%)
    - still cost roughly 4x the sum of the two solo decode times.
    ffmpeg's filter graph engine appears to serialize frame handling
    across simultaneous hardware-decoded inputs within one process;
    only real OS-level process parallelism avoided that. See
    WORKING_CONTEXT.md's --stitch NVDEC follow-ups for the full
    investigation this conclusion is based on.
    """

    if _nvdec_available():
        start = time.monotonic()
        try:
            _run_decode_camera(
                source, destination, scale_filter=scale_filter, hw_decode=True
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
    _run_decode_camera(
        source, destination, scale_filter=scale_filter, hw_decode=False
    )
    _report_decode_timing(
        destination.name, "cpu", time.monotonic() - start,
        failed=False, debug=debug,
    )


def _run_decode_camera(
    source: Path,
    destination: Path,
    *,
    scale_filter: str | None,
    hw_decode: bool,
) -> None:
    input_args = _shared_hw_device_args(hw_decode) + _hwaccel_input_args(
        source, hw_decode=hw_decode
    )
    predecode = _hw_predecode_filter(hw_decode)

    if scale_filter is not None:
        input_args += [
            "-filter_complex", f"[0:v]{predecode}{scale_filter}[v]",
            "-map", "[v]",
        ]
    elif hw_decode:
        # See the identical comment in _run_reencode_single() - same
        # bug, same fix: predecode's trailing comma needs stripping
        # when nothing follows it. This is front's own branch whenever
        # a --stitch-resolution isn't in play for the intermediate
        # scale (see _stack()'s scale-filter selection below) - it's
        # what actually fired on Christer's real archive this time.
        input_args += [
            "-filter_complex", f"[0:v]{predecode.rstrip(',')}[v]",
            "-map", "[v]",
        ]
    else:
        input_args += ["-map", "0:v"]

    encode_with_nvenc_fallback(input_args, destination)


def _map_panel_dimensions(
    comp_width: int,
    comp_height: int,
    *,
    side: str,
    fixes: tuple[GpsFix, ...],
) -> tuple[int, int] | None:
    """The (width, height) --stitch-map's panel should render at so it
    slots onto `side` of a comp_width x comp_height camera composite
    via a plain hstack ('left'/'right') or vstack ('top'/'down').

    The axis matching the composite is matched exactly (panel height
    == comp_height for hstack, panel width == comp_width for vstack -
    hstack/vstack both require that shared axis to line up). The other,
    *free* axis is sized from the trip's own real-world aspect ratio
    (see osm_roads.aspect_ratio_of()) - a north-south trip wants a
    taller panel, an east-west trip a wider one - clamped to between
    _MIN_MAP_PANEL_FRACTION and _MAX_MAP_PANEL_FRACTION of the
    composite's own corresponding dimension, so a near-straight-line
    trip can't ask for a degenerate sliver or an oversized panel.

    That clamp is relative to the camera composite alone, not the
    eventual composite+panel total (which would make this circular) -
    a deliberate simplification: when a map panel is also requested,
    --stitch-resolution bounds the camera portion, not necessarily the
    final file's own total dimensions, since the panel adds to it.

    Returns None if there isn't enough GPS data to compute a real
    -world bounding box at all (mirrors bounding_box_for_fixes()'s own
    "nothing to bound" convention).
    """

    bbox = bounding_box_for_fixes(fixes)
    if bbox is None:
        return None

    trip_ratio = aspect_ratio_of(bbox)

    if side in ("left", "right"):
        low = comp_width * _MIN_MAP_PANEL_FRACTION
        high = comp_width * _MAX_MAP_PANEL_FRACTION
        free_dimension = max(low, min(comp_height * trip_ratio, high))
        width, height = free_dimension, comp_height
    else:
        low = comp_height * _MIN_MAP_PANEL_FRACTION
        high = comp_height * _MAX_MAP_PANEL_FRACTION
        free_dimension = max(low, min(comp_width / trip_ratio, high))
        width, height = comp_width, free_dimension

    # Even dimensions for yuv420p encoding - same rounding convention
    # as _ideal_shared_dimension().
    return max(2, round(width / 2) * 2), max(2, round(height / 2) * 2)


def _render_map_panel(
    mode: str,
    fixes: tuple[GpsFix, ...],
    roads: tuple[Road, ...],
    destination: Path,
    *,
    width: int,
    height: int,
    zoom_meters: float | None,
    marker_image_path: Path | None,
) -> Path | None:
    """Render --stitch-map's panel (mode 'map' or 'zoom') at exactly
    width x height, shaped so combining it with the camera composite
    doesn't distort it - see osm_roads.bounding_box_for_fixes()'s
    `aspect_ratio` param. Returns None (writes nothing) if there isn't
    enough GPS data to draw a route from - the same convention
    render_map_video() itself uses.

    This is a dedicated render, separate from any general-purpose
    map.mp4/map_zoom_*m.mp4 --map/--map-zoom may also produce in the
    same run - those stay whatever shape/size they've always been
    (square by default); this one is sized specifically to fit the
    stitch composite.
    """

    if mode == "zoom":
        if zoom_meters is None:
            return None
        # bbox is a required render_map_video() param but unused
        # whenever zoom_meters is given (a fresh one is built every
        # frame instead) - any non-None placeholder works; reuse the
        # trip's own unshaped whole-trip box, same as
        # trip_export.py's _render_map_variant() does for the general
        # -purpose map_zoom_*m.mp4.
        bbox = bounding_box_for_fixes(fixes)
        if bbox is None:
            return None
        return render_map_video(
            fixes, roads, bbox, destination,
            marker_image_path=marker_image_path,
            zoom_meters=zoom_meters,
            width=width, height=height,
        )

    bbox = bounding_box_for_fixes(fixes, aspect_ratio=width / height)
    if bbox is None:
        return None
    return render_map_video(
        fixes, roads, bbox, destination,
        marker_image_path=marker_image_path,
        width=width, height=height,
    )


def _ideal_shared_dimension(
    front_width: int,
    front_height: int,
    rear_width: int,
    rear_height: int,
    *,
    filter_name: str,
    out_width: int,
    out_height: int,
) -> int:
    """The shared height (hstack) or width (vstack) both cameras'
    intermediates should be scaled to, chosen so the combined
    composite lands as close as possible to (out_width, out_height)
    without exceeding either dimension - never bigger than the final
    output will actually use.

    For hstack, both cameras share a height H; each contributes a
    width of H * its own aspect ratio, and the composite's total width
    is the sum of the two. Solving "combined width == out_width" for H
    gives out_width / (front_aspect + rear_aspect) - e.g. two same-
    aspect-ratio (16:9) cameras split an out_width of 1280 evenly,
    landing H at 360 (640-wide each), not out_height (720, which is
    what an earlier version of this function used - producing a
    combined width of 2560, exactly double what the final pass needed,
    wasting real decode/encode time on detail that just got thrown
    away one step later). Capped at out_height too, in case the
    cameras are narrow/tall enough that the width constraint alone
    would ask for an H bigger than the target frame itself.

    vstack is the mirror of this: both cameras share a width W, each
    contributes a height of W / its own aspect ratio, solving
    "combined height == out_height" for W, capped at out_width.

    Rounded to the nearest even number - unlike the "-2" ffmpeg uses
    for the *other*, free dimension in these scale filters (which
    self-rounds), this one is a literal scale=... value and needs to
    be even for yuv420p encoding on its own.
    """

    front_aspect = front_width / front_height
    rear_aspect = rear_width / rear_height

    if filter_name == "hstack":
        shared = min(out_width / (front_aspect + rear_aspect), out_height)
    else:
        shared = min(out_height / (1 / front_aspect + 1 / rear_aspect), out_width)

    return max(2, round(shared / 2) * 2)


def _stack(
    front: Path,
    rear: Path,
    destination: Path,
    *,
    layout: str,
    resolution: tuple[int, int] | None,
    bitrate: str | None,
    map_mode: str | None = None,
    map_side: str | None = None,
    map_zoom_meters: float | None = None,
    map_fixes: tuple[GpsFix, ...] = (),
    map_roads: tuple[Road, ...] = (),
    map_icon: Path | None = None,
    debug: bool = False,
    warnings: list[str] | None = None,
) -> Path:
    if layout not in STACK_LAYOUTS:
        raise ValueError(
            f"unknown stitch layout: {layout!r} "
            f"(expected one of {sorted(STACK_LAYOUTS)})"
        )

    filter_name = STACK_LAYOUTS[layout]

    # hstack only requires matching *heights* (it concatenates
    # horizontally, combined width is whatever the two widths sum to);
    # vstack only requires matching *widths*.
    #
    # When a final `resolution` is requested, scale BOTH cameras'
    # intermediates to the *ideal* shared height (hstack) or width
    # (vstack) - the one that makes the combined composite land as
    # close as possible to `resolution` without exceeding it - rather
    # than matching rear to front's full native size (the original
    # bug: an unnecessary upscale, fixed in the previous commit) or
    # even scaling both cameras straight to the target's own height/
    # width (fixed here: still wasteful, since two cameras stacked
    # side by side at height=out_height combine to roughly *twice*
    # out_width, so the final pass then has to shrink the whole
    # composite by about half again). Christer worked out the correct
    # target by hand for the common case (two same-aspect-ratio
    # cameras: roughly half of `resolution` per camera) and asked
    # whether that was right - see _ideal_shared_dimension() for the
    # general version that also handles cameras with *different*
    # aspect ratios from each other.
    #
    # When no `resolution` is given (full native-quality output),
    # front stays untouched and rear matches front's own native size
    # on the one axis that actually needs to match - unchanged.
    if resolution is not None:
        out_width, out_height = resolution
        front_width, front_height = _video_dimensions(front)
        rear_width, rear_height = _video_dimensions(rear)
        shared = _ideal_shared_dimension(
            front_width, front_height, rear_width, rear_height,
            filter_name=filter_name, out_width=out_width, out_height=out_height,
        )
        if filter_name == "hstack":
            front_scale_filter = f"scale=-2:{shared}"
            rear_scale_filter = f"scale=-2:{shared}"
        else:
            front_scale_filter = f"scale={shared}:-2"
            rear_scale_filter = f"scale={shared}:-2"
    else:
        front_scale_filter = None
        front_width, front_height = _video_dimensions(front)
        if filter_name == "hstack":
            rear_scale_filter = f"scale=-2:{front_height}"
        else:
            rear_scale_filter = f"scale={front_width}:-2"

    with tempfile.TemporaryDirectory(prefix="bv-stitch-") as tmp_dir:
        tmp_path = Path(tmp_dir)
        front_decoded = tmp_path / "front.mp4"
        rear_decoded = tmp_path / "rear.mp4"

        # Decode front and rear as two genuinely separate ffmpeg
        # processes, run concurrently via threads - safe for the same
        # reason front/rear/audio concatenation already is in
        # trip_export.py (each worker mostly blocks in
        # subprocess.run(), which releases the GIL while ffmpeg runs).
        # See _decode_camera()'s docstring for why this - rather than
        # one ffmpeg process handling both hardware-decoded inputs -
        # turned out to matter so much on real footage.
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
            front_future = executor.submit(
                _decode_camera, front, front_decoded,
                scale_filter=front_scale_filter, debug=debug,
            )
            rear_future = executor.submit(
                _decode_camera, rear, rear_decoded,
                scale_filter=rear_scale_filter, debug=debug,
            )
            front_future.result()
            rear_future.result()

        # A requested `bitrate` is capped to the sum of the two
        # intermediates' own actual bitrates, not the original
        # cameras' native bitrates - the final combine pass never sees
        # the originals again, only these already-reduced
        # intermediates, so that's the real information ceiling. Sum,
        # not the higher of the two: both intermediates are already
        # scaled to roughly the same size (see _ideal_shared_dimension()
        # above), so the composite has roughly double the pixel area of
        # either one alone - capping at just one intermediate's bitrate
        # would spread that same budget over twice the pixels, likely
        # looking worse than either intermediate on its own. Skipped
        # entirely if no bitrate was requested, or if either
        # intermediate's own bitrate can't be determined (never worth
        # failing the export over).
        effective_bitrate = bitrate
        if bitrate is not None:
            front_bps = _video_bitrate(front_decoded)
            rear_bps = _video_bitrate(rear_decoded)
            requested_bps = _parse_bitrate_bps(bitrate)

            if front_bps is not None and rear_bps is not None:
                ceiling_bps = front_bps + rear_bps
                if requested_bps is not None and requested_bps > ceiling_bps:
                    effective_bitrate = str(ceiling_bps)
                    if warnings is not None:
                        warnings.append(
                            f"stitch: requested bitrate {bitrate} exceeds "
                            "the two intermediates' combined bitrate "
                            f"(~{ceiling_bps // 1000}k) - capped to that "
                            "instead"
                        )

        # The expensive part - decoding the original source footage -
        # is already done above. Both intermediates are already
        # CPU-readable and already matched on the one axis
        # hstack/vstack needs, so this final pass is a plain CPU
        # decode + stack + (optional) resolution fit-and-pad + encode.
        # Deliberately no hwaccel here: there's nothing left to gain
        # from it on these much-smaller intermediates, and using it
        # would just reintroduce the two-hwaccel-input cost this
        # whole redesign exists to avoid.
        clauses = [f"[0:v][1:v]{filter_name}=inputs=2[stacked]"]
        camera_label = "stacked"

        if resolution is not None:
            out_width, out_height = resolution
            clauses.append(_fit_and_pad("stacked", "camera", out_width, out_height))
            camera_label = "camera"

        output_label = camera_label
        extra_inputs: list[str] = []

        if map_mode is not None and map_fixes:
            # The camera composite's own pixel dimensions are only
            # knowable *now*, and only worth computing when a map
            # panel is actually requested (an extra ffprobe call
            # otherwise, on top of everything already probed above) -
            # either exactly `resolution` (the fit-and-pad above
            # guarantees that), or, with no `resolution` given, front's
            # own decoded size plus whatever rear contributed on the
            # stacking axis (both already probed/matched above).
            if resolution is not None:
                comp_width, comp_height = resolution
            else:
                comp_width, comp_height = front_width, front_height
                rear_decoded_width, rear_decoded_height = _video_dimensions(
                    rear_decoded
                )
                if filter_name == "hstack":
                    comp_width += rear_decoded_width
                else:
                    comp_height += rear_decoded_height

            panel_side = map_side or _DEFAULT_MAP_SIDE_FOR_LAYOUT.get(layout)

            if panel_side is None:
                if warnings is not None:
                    warnings.append(
                        f"stitch map panel: no default side for layout "
                        f"{layout!r} - pass --stitch-map-side explicitly - "
                        "skipped"
                    )
            elif map_mode == "zoom" and map_zoom_meters is None:
                if warnings is not None:
                    warnings.append(
                        "stitch map panel: --stitch-map zoom requires "
                        "--map-zoom METERS to also be given (reused as the "
                        "panel's follow-camera radius) - skipped"
                    )
            else:
                panel_size = _map_panel_dimensions(
                    comp_width, comp_height, side=panel_side, fixes=map_fixes,
                )
                panel_path = tmp_path / "map_panel.mp4"
                rendered = None
                try:
                    rendered = _render_map_panel(
                        map_mode, map_fixes, map_roads, panel_path,
                        width=panel_size[0], height=panel_size[1],
                        zoom_meters=map_zoom_meters,
                        marker_image_path=map_icon,
                    ) if panel_size is not None else None
                except MediaToolError as exc:
                    if warnings is not None:
                        warnings.append(f"stitch map panel: {exc}")

                if rendered is None:
                    if warnings is not None and panel_size is None:
                        warnings.append(
                            "stitch map panel: no GPS data for this trip - "
                            "skipped"
                        )
                else:
                    panel_filter_name = (
                        "hstack" if panel_side in ("left", "right") else "vstack"
                    )
                    combine_inputs = (
                        f"[2:v][{camera_label}]"
                        if panel_side in ("left", "top")
                        else f"[{camera_label}][2:v]"
                    )
                    clauses.append(
                        f"{combine_inputs}{panel_filter_name}=inputs=2[withmap]"
                    )
                    output_label = "withmap"
                    extra_inputs = ["-i", str(rendered)]

        encode_with_nvenc_fallback(
            [
                "-i", str(front_decoded),
                "-i", str(rear_decoded),
                *extra_inputs,
                "-filter_complex", ";".join(clauses),
                "-map", f"[{output_label}]",
            ],
            destination,
            extra_codec_args=_bitrate_args(effective_bitrate),
        )

    return destination
