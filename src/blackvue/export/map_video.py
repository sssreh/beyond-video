"""
Map-overlay video encoding for bv-export: turns a trip's merged GPS
fixes into map.mp4 - rendering one frame per interval (route driven
so far, current position/heading, speed, timestamp) against a
locally-drawn OSM-road basemap, then handing the frame sequence to
ffmpeg.

Copyright (C) 2026 Christer R. (sssreh)

SPDX-License-Identifier: GPL-3.0-or-later
"""

from __future__ import annotations

import math
import tempfile
from datetime import datetime
from datetime import timedelta
from pathlib import Path

from PIL import Image

from ..generate.media import MediaToolError
from ..telemetry.gps_reader import GpsFix
from .map_render import render_frame
from .media import encode_frame_sequence
from .osm_roads import BoundingBox
from .osm_roads import Road
from .osm_roads import bounding_box_around_point

# 5 frames/second is enough for the position dot to read as smooth
# motion without generating an excessive number of frames for a long
# trip. If this ever gets composited alongside real footage (the
# future --stitch item), ffmpeg can retime it there; map.mp4 doesn't
# need to match the front/rear video's own frame rate.
DEFAULT_FPS = 5


def _valid_positioned_fixes(fixes: tuple[GpsFix, ...]) -> tuple[GpsFix, ...]:
    return tuple(
        fix
        for fix in fixes
        if fix.valid and fix.latitude is not None and fix.longitude is not None
    )


def _interpolate_course(
    a: float | None, b: float | None, t: float
) -> float | None:
    """Interpolate between two compass courses (degrees, 0-360),
    correctly handling the 0/360 wraparound a plain linear
    interpolation would get wrong (e.g. 350 degrees -> 10 degrees
    should pass through 0/360, not swing back down through 180).

    Falls back to whichever course is given if only one is (a fix's
    course field can be empty in the raw NMEA data).
    """

    if a is None:
        return b
    if b is None:
        return a

    a_rad, b_rad = math.radians(a), math.radians(b)
    x = (1 - t) * math.cos(a_rad) + t * math.cos(b_rad)
    y = (1 - t) * math.sin(a_rad) + t * math.sin(b_rad)

    if x == 0.0 and y == 0.0:
        # Exactly opposite courses (e.g. interpolating across a
        # U-turn) - no single "average" direction is more correct
        # than the other; picking `a` is an arbitrary but stable
        # choice rather than an error.
        return a

    result = math.degrees(math.atan2(y, x)) % 360
    # A result that should mathematically be a hair below 0 (e.g.
    # -1e-15) can round to exactly 360.0 in floating point rather than
    # landing in [0, 360) - fold that edge case back to 0.0 so callers
    # never have to special-case 360 meaning the same thing as 0.
    return 0.0 if result == 360.0 else result


def interpolate_position(
    fixes: tuple[GpsFix, ...], timestamp: datetime
) -> tuple[float, float, float | None, float | None]:
    """Linearly interpolate (lat, lon, speed_kmh, course) at
    `timestamp` between the two fixes bracketing it (course uses
    circular interpolation - see _interpolate_course()).

    `fixes` must be sorted by timestamp and non-empty. A timestamp
    outside the fixes' own range clamps to the nearest end fix rather
    than extrapolating.
    """

    if timestamp <= fixes[0].timestamp:
        first = fixes[0]
        return first.latitude, first.longitude, first.speed_kmh, first.course

    if timestamp >= fixes[-1].timestamp:
        last = fixes[-1]
        return last.latitude, last.longitude, last.speed_kmh, last.course

    for previous, current in zip(fixes, fixes[1:]):
        if previous.timestamp <= timestamp <= current.timestamp:
            span = (current.timestamp - previous.timestamp).total_seconds()

            if span <= 0:
                return (
                    previous.latitude, previous.longitude,
                    previous.speed_kmh, previous.course,
                )

            t = (timestamp - previous.timestamp).total_seconds() / span
            lat = previous.latitude + (current.latitude - previous.latitude) * t
            lon = previous.longitude + (current.longitude - previous.longitude) * t

            if previous.speed_kmh is not None and current.speed_kmh is not None:
                speed = (
                    previous.speed_kmh
                    + (current.speed_kmh - previous.speed_kmh) * t
                )
            else:
                speed = previous.speed_kmh or current.speed_kmh

            course = _interpolate_course(previous.course, current.course, t)

            return lat, lon, speed, course

    # Unreachable given the clamp checks above, but keeps the return
    # type honest if it's ever reached.
    last = fixes[-1]
    return last.latitude, last.longitude, last.speed_kmh, last.course


def render_map_video(
    fixes: tuple[GpsFix, ...],
    roads: tuple[Road, ...],
    bbox: BoundingBox,
    destination: Path,
    *,
    fps: int = DEFAULT_FPS,
    marker_image_path: Path | None = None,
    zoom_meters: float | None = None,
) -> Path | None:
    """Render a trip's merged GPS fixes into an overlay video at
    `destination`: the route driven so far, current position/heading,
    speed, and timestamp, drawn against `roads` (see osm_roads.py).

    `bbox` frames the whole trip at once by default (a static
    overview, the same every frame). `zoom_meters`, if given, switches
    to a "follow camera" instead: every frame is framed by a fresh
    bounding box of that real-world half-width, centered on the
    frame's own interpolated position (see
    osm_roads.bounding_box_around_point()) - `bbox` itself is then
    unused, since every frame gets its own. This is what makes the map
    scroll/pan as the vehicle moves rather than sitting in a fixed
    static view.

    The position marker is an arrow rotated to the GPS course over
    ground by default. `marker_image_path`, if given, is used as a
    custom marker instead (also rotated to match course) - a PNG with
    transparency is recommended, drawn pointing "up"/north in its own
    file. Raises MediaToolError if the image can't be loaded.

    Returns None (and writes nothing) if there aren't at least two
    valid, positioned fixes to draw a route from - the same "nothing
    to work with" convention export_trip()'s other outputs use.
    """

    positioned = _valid_positioned_fixes(fixes)
    if len(positioned) < 2:
        return None

    start = positioned[0].timestamp
    end = positioned[-1].timestamp
    total_seconds = (end - start).total_seconds()

    if total_seconds <= 0:
        return None

    marker_image = None
    if marker_image_path is not None:
        try:
            marker_image = Image.open(marker_image_path).convert("RGBA")
        except (FileNotFoundError, OSError) as exc:
            raise MediaToolError(
                f"could not load marker image {marker_image_path}: {exc}"
            ) from exc

    frame_count = max(2, int(total_seconds * fps) + 1)

    destination.parent.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory() as frame_dir_name:
        frame_dir = Path(frame_dir_name)
        route_so_far: list[tuple[float, float]] = []
        fix_index = 0

        for frame_number in range(frame_count):
            elapsed = min(frame_number / fps, total_seconds)
            timestamp = start + timedelta(seconds=elapsed)

            # Grow the drawn route with every real fix at or before
            # this frame's timestamp, so the line is built from real
            # fix points wherever possible, not just interpolated
            # ones.
            while (
                fix_index < len(positioned)
                and positioned[fix_index].timestamp <= timestamp
            ):
                fix = positioned[fix_index]
                route_so_far.append((fix.latitude, fix.longitude))
                fix_index += 1

            lat, lon, speed, course = interpolate_position(positioned, timestamp)
            position = (lat, lon)

            frame_bbox = (
                bounding_box_around_point(lat, lon, zoom_meters)
                if zoom_meters is not None
                else bbox
            )

            frame = render_frame(
                frame_bbox,
                roads,
                tuple(route_so_far) + (position,),
                position,
                speed_kmh=speed,
                heading=course,
                marker_image=marker_image,
                timestamp_text=timestamp.strftime("%Y-%m-%d %H:%M:%S"),
            )
            frame.save(frame_dir / f"frame_{frame_number:06d}.png")

        encode_frame_sequence(frame_dir, destination, fps)

    return destination
