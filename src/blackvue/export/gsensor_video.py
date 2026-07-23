"""
G-sensor dot-gauge video encoding for bv-export: turns a trip's merged
g-sensor samples into gsensor.mp4 - rendering one frame per interval
(a dot moving around a gauge, centered on the trip's own median
reading rather than raw (0, 0), with a short fading trail) on a flat
chroma-key green background, then handing the frame sequence to
ffmpeg. See gsensor_render.py for why the background is green rather
than transparent - h264/mp4 has no alpha channel, so a chroma-key
background is the way to make this compositable later (the future
--stitch item), not a real transparent video file.

Copyright (C) 2026 Christer R. (sssreh)

SPDX-License-Identifier: GPL-3.0-or-later
"""

from __future__ import annotations

import tempfile
from datetime import timedelta
from pathlib import Path

from ..telemetry.gsensor_reader import GSensorSample
from .gsensor_render import baseline_for_samples
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

    Scans `samples` from the start every call - fine for a one-off
    lookup (which is all this is used for now; kept public mainly for
    its own tests below). render_gsensor_video()'s own per-frame loop
    does NOT call this anymore - see _advance_search_index()/
    _interpolate_from_index() for the O(samples + frames) path it uses
    instead, and why the O(samples x frames) this function would give
    turned into an apparent hang on a real multi-hour trip.
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


def _advance_search_index(
    samples: tuple[GSensorSample, ...], elapsed: timedelta, index: int
) -> int:
    """Move `index` forward (never backward) to the largest index i
    such that samples[i].offset <= elapsed - or len(samples) - 1 if
    `elapsed` is past every sample.

    Only correct when called with a non-decreasing sequence of
    `elapsed` values across successive calls, each time passing back
    in the index the previous call returned - exactly the shape of
    render_gsensor_video()'s own per-frame loop below, where `elapsed`
    is frame_number/fps and only ever increases. Under that condition,
    each call only has to scan forward past samples the previous calls
    haven't already passed, rather than rescanning from the start -
    see _interpolate_from_index()'s own docstring for why that
    distinction matters in practice.
    """

    last = len(samples) - 1
    while index < last and samples[index + 1].offset <= elapsed:
        index += 1
    return index


def _interpolate_from_index(
    samples: tuple[GSensorSample, ...], elapsed: timedelta, index: int
) -> tuple[float, float, float]:
    """Same interpolation result as interpolate_sample(samples, elapsed)
    - identical clamp-before-first/clamp-after-last/linear-interpolate
    behavior - but taking an already-known bracketing `index` (see
    _advance_search_index()) instead of scanning `samples` for one.

    This exists because interpolate_sample() rescans `samples` from
    its own start on every call - fine for an occasional one-off
    lookup, but render_gsensor_video()'s frame loop below calls it once
    per output frame, and both the sample count and the frame count
    scale with trip duration. That made the frame loop O(samples x
    frames) - quadratic in trip length - which on a real multi-hour
    trip (g-sensor samples roughly every 100ms, see gsensor_reader.py)
    looked from the outside exactly like a hang: Christer reported
    bv-export appearing stuck at "starting gsensor.mp4 render" with no
    completion for a long time. `_advance_search_index()`'s forward
    -only scan plus this function together bring the same loop down to
    O(samples + frames).
    """

    current = samples[index]

    if index == 0 and elapsed <= current.offset:
        return float(current.x), float(current.y), float(current.z)

    if index == len(samples) - 1:
        return float(current.x), float(current.y), float(current.z)

    nxt = samples[index + 1]
    span = (nxt.offset - current.offset).total_seconds()

    if span <= 0:
        return float(current.x), float(current.y), float(current.z)

    t = (elapsed - current.offset).total_seconds() / span
    x = current.x + (nxt.x - current.x) * t
    y = current.y + (nxt.y - current.y) * t
    z = current.z + (nxt.z - current.z) * t

    return x, y, z


def render_gsensor_video(
    samples: tuple[GSensorSample, ...],
    destination: Path,
    *,
    fps: int = DEFAULT_FPS,
) -> Path | None:
    """Render a trip's merged g-sensor samples into an overlay video
    at `destination`: a dot moving around a gauge (see
    gsensor_render.py), centered on the trip's own median (x, y)
    reading rather than raw (0, 0) (see baseline_for_samples()), with
    a fading trail, on a flat chroma-key green background meant to be
    keyed out when composited over the front/rear footage later.

    Returns None (and writes nothing) if there aren't at least two
    samples, or they span zero time - the same "nothing to work with"
    convention export_trip()'s other outputs use.
    """

    if len(samples) < 2:
        return None

    total_seconds = samples[-1].offset.total_seconds()
    if total_seconds <= 0:
        return None

    # Center the gauge on the trip's own median reading, not raw
    # (0, 0) - a dashcam mounted at even a slight angle (or the
    # sensor's own bias) means "level, driving straight" rarely reads
    # exactly zero, so drawing around literal (0, 0) leaves the dot
    # sitting off-center the whole trip. See baseline_for_samples().
    baseline_x, baseline_y = baseline_for_samples(samples)
    scale = scale_for_samples(samples, baseline=(baseline_x, baseline_y))
    frame_count = max(2, int(total_seconds * fps) + 1)

    destination.parent.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory() as frame_dir_name:
        frame_dir = Path(frame_dir_name)
        trail: list[tuple[float, float]] = []
        # See _interpolate_from_index()'s docstring - carried across
        # iterations (never reset) so each frame's lookup resumes
        # where the last one left off, instead of interpolate_sample()'s
        # own full rescan from samples[0] every time.
        search_index = 0

        for frame_number in range(frame_count):
            elapsed_seconds = min(frame_number / fps, total_seconds)
            elapsed = timedelta(seconds=elapsed_seconds)

            search_index = _advance_search_index(samples, elapsed, search_index)
            x, y, _z = _interpolate_from_index(samples, elapsed, search_index)
            position = (x - baseline_x, y - baseline_y)

            trail.append(position)
            if len(trail) > DEFAULT_TRAIL_LENGTH:
                trail.pop(0)

            frame = render_frame(scale, tuple(trail), position)
            frame.save(frame_dir / f"frame_{frame_number:06d}.png")

        encode_frame_sequence(frame_dir, destination, fps)

    return destination
