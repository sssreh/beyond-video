"""
Zoom/crop rendering for bv-search's GPS-proximity matches.

Experimental (Christer, 2026-08-11: "let's try your zoom crop thing,
we can always delete it later"). Given a --near/--place match - a
recording's GPS track came within --radius of a target coordinate -
this renders a cropped, zoomed-in thumbnail and short clip centered
on roughly where the target should appear in the front camera's own
wide frame, so the thing that was searched for isn't just a speck
somewhere in a mostly-irrelevant wide dashcam shot.

This is a heuristic, not a photometrically calibrated projection:
BlackVue doesn't publish per-model lens calibration data (focal
length, distortion coefficients), so the horizontal-FOV/crop-size
math here assumes a simple rectilinear (pinhole) camera model rather
than correcting for the real lens's fisheye-like barrel distortion,
especially near the wide ~136-degree edges. It gets roughly the right
direction and a roughly sensible zoom level, not pixel-precise
framing - good enough for "which direction do I look," not "read the
sign."

Copyright (C) 2026 Christer R. (sssreh)

SPDX-License-Identifier: GPL-3.0-or-later
"""

from __future__ import annotations

import json
import math
import subprocess
from dataclasses import dataclass
from pathlib import Path

from .generate.media import MediaToolError

# BlackVue DR900S/DR900X-series front camera's published horizontal
# field of view - confirmed against manufacturer specs and directly
# by Christer for his own DR900S-2CH. Other camera models may differ;
# bv-search's own --fov flag overrides this when a given camera's
# real horizontal FOV is known to be different.
DEFAULT_HORIZONTAL_FOV_DEGREES = 136.0

# Below this speed, a GPS receiver's course-over-ground reading gets
# noisy or effectively meaningless (NMEA course is derived from
# consecutive fixes' own movement, not a compass) - cropping toward a
# "heading" computed near a stop would just be noise, so matches at
# or under this speed are reported without a crop rather than a
# misleading one.
MIN_SPEED_FOR_HEADING_KMH = 5.0

# Crop-width heuristic, not a real lens calibration (see module
# docstring): _REFERENCE_DISTANCE_METERS is the distance at which the
# crop covers _REFERENCE_CROP_FRACTION of the frame's own width. Crop
# width then scales as _REFERENCE_CROP_FRACTION *
# _REFERENCE_DISTANCE_METERS / distance_meters - closer subjects need
# less zoom (a wider fraction of the frame), farther ones need a
# tighter crop blown back up to full size - clamped to a sane range
# either way.
_REFERENCE_DISTANCE_METERS = 15.0
_REFERENCE_CROP_FRACTION = 0.5
_MIN_CROP_FRACTION = 0.12
_MAX_CROP_FRACTION = 0.9

# Default length of the rendered preview clip, centered on the
# match's own timestamp (half before, half after).
DEFAULT_CLIP_SECONDS = 4.0


@dataclass(frozen=True)
class CropBox:
    """A crop region as fractions (0..1) of the source frame's own
    width/height - resolution-independent, converted to pixels only
    at render time. `width_fraction` and `height_fraction` are always
    equal, so the crop itself keeps the source frame's own aspect
    ratio."""

    x_fraction: float
    y_fraction: float
    width_fraction: float
    height_fraction: float


@dataclass(frozen=True)
class ZoomOutputs:
    """Paths to the two files rendered for one match."""

    thumbnail: Path
    clip: Path


def _bearing_degrees(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Initial great-circle bearing from (lat1, lon1) to (lat2, lon2),
    in compass degrees (0 = north, 90 = east) - the standard forward-
    azimuth formula."""

    lat1_rad, lat2_rad = math.radians(lat1), math.radians(lat2)
    dlon_rad = math.radians(lon2 - lon1)

    x = math.sin(dlon_rad) * math.cos(lat2_rad)
    y = math.cos(lat1_rad) * math.sin(lat2_rad) - math.sin(lat1_rad) * math.cos(
        lat2_rad
    ) * math.cos(dlon_rad)

    return (math.degrees(math.atan2(x, y)) + 360) % 360


def _relative_bearing_degrees(heading: float, target_bearing: float) -> float:
    """Signed bearing of `target_bearing` relative to `heading`,
    normalized to (-180, 180] - positive means the target is to the
    right of straight-ahead, negative means to the left."""

    return ((target_bearing - heading + 180) % 360) - 180


def compute_crop_box(
    *,
    car_lat: float,
    car_lon: float,
    heading: float | None,
    speed_kmh: float | None,
    target_lat: float,
    target_lon: float,
    distance_meters: float,
    fov_degrees: float = DEFAULT_HORIZONTAL_FOV_DEGREES,
) -> CropBox | None:
    """Compute a crop box centered on the search target's direction,
    as seen from the car's position/heading at the moment of the
    match - or None if the target can't be reasonably placed in
    frame: no heading available, the car was too slow for a
    meaningful course-over-ground reading (see
    MIN_SPEED_FOR_HEADING_KMH), or the target's bearing falls outside
    the camera's own horizontal field of view entirely (behind or to
    the side of the car, out of shot no matter how the frame is
    cropped).
    """

    if heading is None:
        return None
    if speed_kmh is None or speed_kmh < MIN_SPEED_FOR_HEADING_KMH:
        return None

    target_bearing = _bearing_degrees(car_lat, car_lon, target_lat, target_lon)
    relative_bearing = _relative_bearing_degrees(heading, target_bearing)

    if abs(relative_bearing) > fov_degrees / 2:
        return None

    if distance_meters <= 0:
        width_fraction = _MAX_CROP_FRACTION
    else:
        width_fraction = (
            _REFERENCE_CROP_FRACTION * _REFERENCE_DISTANCE_METERS / distance_meters
        )
    width_fraction = max(_MIN_CROP_FRACTION, min(_MAX_CROP_FRACTION, width_fraction))

    # x_fraction follows the target's relative bearing across the
    # frame (0 = left edge, 1 = right edge, assuming a simple
    # rectilinear mapping - see module docstring), then gets clamped
    # so the crop box's own edges never run off the frame.
    x_fraction = 0.5 + relative_bearing / fov_degrees
    half_width = width_fraction / 2
    x_fraction = max(half_width, min(1 - half_width, x_fraction))

    return CropBox(
        x_fraction=x_fraction,
        y_fraction=0.5,
        width_fraction=width_fraction,
        height_fraction=width_fraction,
    )


def _video_dimensions(path: Path) -> tuple[int, int]:
    """Probe a video's pixel width/height via ffprobe."""

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


def _crop_filter(crop_box: CropBox, video_width: int, video_height: int) -> str:
    """Build an ffmpeg crop filter string for `crop_box`, converted to
    even pixel dimensions (required by most codecs) and clamped so
    the crop rectangle never runs outside the real frame."""

    width = max(2, round(crop_box.width_fraction * video_width))
    height = max(2, round(crop_box.height_fraction * video_height))
    width -= width % 2
    height -= height % 2

    x = round(crop_box.x_fraction * video_width - width / 2)
    y = round(crop_box.y_fraction * video_height - height / 2)
    x = max(0, min(video_width - width, x))
    y = max(0, min(video_height - height, y))

    return f"crop={width}:{height}:{x}:{y}"


def render_zoom_thumbnail(
    video_path: Path,
    offset_seconds: float,
    crop_box: CropBox,
    destination: Path,
) -> None:
    """Render a single cropped frame at `offset_seconds` into
    `video_path`, scaled back up to the source's own resolution so it
    reads as "zoomed in" rather than just a smaller image."""

    video_width, video_height = _video_dimensions(video_path)
    crop = _crop_filter(crop_box, video_width, video_height)

    try:
        subprocess.run(
            [
                "ffmpeg", "-y",
                "-ss", f"{max(0.0, offset_seconds):.3f}",
                "-i", str(video_path),
                "-frames:v", "1",
                "-vf", f"{crop},scale={video_width}:{video_height}",
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
            f"ffmpeg failed rendering {destination.name}: {exc.stderr.strip()}"
        ) from exc


def render_zoom_clip(
    video_path: Path,
    offset_seconds: float,
    crop_box: CropBox,
    destination: Path,
    *,
    clip_seconds: float = DEFAULT_CLIP_SECONDS,
) -> None:
    """Render a short cropped clip centered on `offset_seconds` (half
    `clip_seconds` before/after, clamped to not start before 0),
    scaled back up to the source's own resolution. Audio is dropped
    (`-an`) - this is a quick directional preview, not a polished
    export, and keeping it video-only avoids re-encoding an audio
    stream a recording might not even have."""

    video_width, video_height = _video_dimensions(video_path)
    crop = _crop_filter(crop_box, video_width, video_height)
    start = max(0.0, offset_seconds - clip_seconds / 2)

    try:
        subprocess.run(
            [
                "ffmpeg", "-y",
                "-ss", f"{start:.3f}",
                "-i", str(video_path),
                "-t", f"{clip_seconds:.3f}",
                "-vf", f"{crop},scale={video_width}:{video_height}",
                "-an",
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
            f"ffmpeg failed rendering {destination.name}: {exc.stderr.strip()}"
        ) from exc


def render_zoom_outputs(
    video_path: Path,
    offset_seconds: float,
    crop_box: CropBox,
    output_dir: Path,
    stem: str,
) -> ZoomOutputs:
    """Render both a thumbnail and a clip for one match into
    `output_dir`, named from `stem` (typically the recording id) -
    `<stem>_zoom.jpg`/`<stem>_zoom.mp4`. Raises MediaToolError if
    either render fails (ffmpeg/ffprobe missing or erroring); the
    caller decides whether that should interrupt the whole search or
    just be reported and skipped for this one match, same as any
    other per-recording MediaToolError in bv-search."""

    thumbnail_path = output_dir / f"{stem}_zoom.jpg"
    clip_path = output_dir / f"{stem}_zoom.mp4"

    render_zoom_thumbnail(video_path, offset_seconds, crop_box, thumbnail_path)
    render_zoom_clip(video_path, offset_seconds, crop_box, clip_path)

    return ZoomOutputs(thumbnail=thumbnail_path, clip=clip_path)
