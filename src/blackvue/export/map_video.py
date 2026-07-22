"""
Map-overlay video encoding for bv-export: turns a trip's merged GPS
fixes into map.mp4 - rendering one frame per interval (route driven
so far, current position, speed, timestamp) against a locally-drawn
OSM-road basemap, then handing the frame sequence to ffmpeg.

Copyright (C) 2026 Christer R. (sssreh)

SPDX-License-Identifier: GPL-3.0-or-later
"""

from __future__ import annotations

import subprocess
import tempfile
from datetime import datetime
from datetime import timedelta
from pathlib import Path

from ..generate.media import MediaToolError
from ..telemetry.gps_reader import GpsFix
from .map_render import render_frame
from .osm_roads import BoundingBox
from .osm_roads import Road

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


def interpolate_position(
    fixes: tuple[GpsFix, ...], timestamp: datetime
) -> tuple[float, float, float | None]:
    """Linearly interpolate (lat, lon, speed_kmh) at `timestamp`
    between the two fixes bracketing it.

    `fixes` must be sorted by timestamp and non-empty. A timestamp
    outside the fixes' own range clamps to the nearest end fix rather
    than extrapolating.
    """

    if timestamp <= fixes[0].timestamp:
        first = fixes[0]
        return first.latitude, first.longitude, first.speed_kmh

    if timestamp >= fixes[-1].timestamp:
        last = fixes[-1]
        return last.latitude, last.longitude, last.speed_kmh

    for previous, current in zip(fixes, fixes[1:]):
        if previous.timestamp <= timestamp <= current.timestamp:
            span = (current.timestamp - previous.timestamp).total_seconds()

            if span <= 0:
                return previous.latitude, previous.longitude, previous.speed_kmh

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

            return lat, lon, speed

    # Unreachable given the clamp checks above, but keeps the return
    # type honest if it's ever reached.
    last = fixes[-1]
    return last.latitude, last.longitude, last.speed_kmh


def render_map_video(
    fixes: tuple[GpsFix, ...],
    roads: tuple[Road, ...],
    bbox: BoundingBox,
    destination: Path,
    *,
    fps: int = DEFAULT_FPS,
) -> Path | None:
    """Render a trip's merged GPS fixes into an overlay video at
    `destination`: the route driven so far, current position, speed,
    and timestamp, drawn against `roads` (see osm_roads.py).

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

            lat, lon, speed = interpolate_position(positioned, timestamp)
            position = (lat, lon)

            frame = render_frame(
                bbox,
                roads,
                tuple(route_so_far) + (position,),
                position,
                speed_kmh=speed,
                timestamp_text=timestamp.strftime("%Y-%m-%d %H:%M:%S"),
            )
            frame.save(frame_dir / f"frame_{frame_number:06d}.png")

        try:
            subprocess.run(
                [
                    "ffmpeg",
                    "-y",
                    "-framerate", str(fps),
                    "-i", str(frame_dir / "frame_%06d.png"),
                    "-c:v", "libx264",
                    "-pix_fmt", "yuv420p",
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
                f"ffmpeg encode failed for {destination.name}: "
                f"{exc.stderr.strip()}"
            ) from exc

    return destination
