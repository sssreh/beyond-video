"""
G-sensor dot-gauge video encoding for bv-export: turns a trip's merged
g-sensor samples into gsensor.mp4 - rendering one frame per interval
(a dot moving around a gauge, with a short fading trail) and handing
the frame sequence to ffmpeg.

Copyright (C) 2026 Christer R. (sssreh)

SPDX-License-Identifier: GPL-3.0-or-later
"""

from __future__ import annotations

import tempfile
from datetime import datetime
from datetime import timedelta
from pathlib import Path

from ..telemetry.gsensor_reader import GSensorSample
from .gsensor_render import render_frame
from .gsensor_render import scale_for_samples
from .media import encode_frame_sequence

# G-sensor samples land roughly every 100ms (see gsensor_reader.py),
# so 10fps draws straight from the native sample rate without inventing
# detail that isn't there. Independent of front/rear video's own frame
# rate - see map_video.py's DEFAULT_FPS for why that's fine.
DEFAULT_FPS = 10

# How many recent (interpolated) samples make up the fading trail
# behind the current dot - long enough to show a turn/braking event's
# shape, short enough that the trail doesn't just fill the gauge.
DEFAULT_TRAIL_LENGTH = 8


def interpolate_sample(
    samples: tuple[GSensorSample, ...], elapsed: timedelta
) -> tuple[float, float, float]:
    """Linearly interpolate (x, y, z) at `elapsed` between the two
    samples bracketing it.

    `samples` must be sorted by offset and non-empty. An `elapsed`
    outside the samples' own range clamps to the nearest end sample
    rather than extrapolating.
    """

    if elapsed <= samples[0].offset:
        first = samples[0]
        return float(first.x), float(first.y), float(first.z)

    if elapsed >= samples[-1].offset:
        last = samples[-1]
        return float(last.x), float(last.y), float(last.z)

    for previous, current in zip(samples, samples[1:]):
        if previous.offset <= elapsed <= current.offset:
            span = (current.offset - previous.offset).total_seconds()

            if span <= 0:
                return float(previous.x), float(previous.y), float(previous.z)

            t = (elapsed - previous.offset).total_seconds() / span
            x = previous.x + (current.x - previous.x) * t
            y = previous.y + (current.y - previous.y) * t
            z = previous.z + (current.z - previous.z) * t

            return x, y, z

    # Unreachable given the clamp checks above, but keeps the return
    # type honest if it's ever reached.
    last = samples[-1]
    return float(last.x), float(last.y), float(last.z)


def render_gsensor_video(
    samples: tuple[GSensorSample, ...],
    destination: Path,
    *,
    fps: int = DEFAULT_FPS,
    start_timestamp: datetime | None = None,
) -> Path | None:
    """Render a trip's merged g-sensor samples into an overlay video
    at `destination`: a dot moving around a gauge (see
    gsensor_render.py), with a fading trail and an optional
    wall-clock caption when `start_timestamp` (the trip's own start)
    is given.

    Returns None (and writes nothing) if there aren't at least two
    samples, or they span zero time - the same "nothing to work with"
    convention export_trip()'s other outputs use.
    """

    if len(samples) < 2:
        return None

    total_seconds = samples[-1].offset.total_seconds()
    if total_seconds <= 0:
        return None

    scale = scale_for_samples(samples)
    frame_count = max(2, int(total_seconds * fps) + 1)

    destination.parent.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory() as frame_dir_name:
        frame_dir = Path(frame_dir_name)
        trail: list[tuple[float, float]] = []

        for frame_number in range(frame_count):
            elapsed_seconds = min(frame_number / fps, total_seconds)
            elapsed = timedelta(seconds=elapsed_seconds)

            x, y, _z = interpolate_sample(samples, elapsed)
            position = (x, y)

            trail.append(position)
            if len(trail) > DEFAULT_TRAIL_LENGTH:
                trail.pop(0)

            timestamp_text = None
            if start_timestamp is not None:
                timestamp_text = (
                    start_timestamp + elapsed
                ).strftime("%Y-%m-%d %H:%M:%S")

            frame = render_frame(
                scale,
                tuple(trail),
                position,
                timestamp_text=timestamp_text,
            )
            frame.save(frame_dir / f"frame_{frame_number:06d}.png")

        encode_frame_sequence(frame_dir, destination, fps)

    return destination
