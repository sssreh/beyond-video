"""
G-sensor strip-chart video encoding for bv-export: turns a trip's
merged g-sensor samples into gsensor_graph.mp4 - a second, alternate
g-sensor visualization alongside the existing circular dot-gauge
(gsensor_video.py/gsensor_render.py). See gsensor_graph_render.py's
own module docstring for the visual design (a static whole-trip strip
chart of X/Y/Z traces with a moving playhead, modeled on the BlackVue
SD Card Viewer app's own g-sensor panel) and why the flat chroma-key
green background is used.

Unlike gsensor_video.py's per-frame dot/trail redraw, the strip chart
itself is fixed for the whole trip - only the playhead moves - so this
module renders the base chart once and reuses it for every output
frame, rather than needing gsensor_video.py's own O(samples + frames)
interpolation machinery at all: there's no per-frame sample lookup
here, just a playhead x position computed directly from elapsed time.

Copyright (C) 2026 Christer R. (sssreh)

SPDX-License-Identifier: GPL-3.0-or-later
"""

from __future__ import annotations

import tempfile
from pathlib import Path

from ..telemetry.gsensor_reader import GSensorSample
from .gsensor_graph_render import baseline_for_samples
from .gsensor_graph_render import render_base_frame
from .gsensor_graph_render import render_frame
from .gsensor_graph_render import scale_for_samples
from .media import encode_frame_sequence

# A slow-moving playhead over an otherwise static chart doesn't need
# gsensor.mp4's own 10fps (chosen there to match the raw ~100ms
# g-sensor sample interval) - 5fps matches map.mp4's own "camera sweep
# over a static overview" convention (see map_video.py's DEFAULT_FPS),
# which this is the same shape of render as.
DEFAULT_FPS = 5


def render_gsensor_graph_video(
    samples: tuple[GSensorSample, ...],
    destination: Path,
    *,
    fps: int = DEFAULT_FPS,
    duration_seconds: float | None = None,
    orientation: str = "horizontal",
    width: int | None = None,
    height: int | None = None,
) -> Path | None:
    """Render a trip's merged g-sensor samples into a strip-chart
    overlay video at `destination`: three colored X/Y/Z line traces
    drawn once across the whole trip, with a playhead moving as the
    video plays, on the same flat chroma-key green background
    gsensor.mp4 uses.

    `samples` are already trip-relative (see trip_export.py's
    _merge_gsensor()) - same assumption gsensor_video.py's
    render_gsensor_video() documents for its own `samples` parameter.

    `duration_seconds`, if given (typically the trip's real
    concatenated front/rear video duration), sets how long the
    playhead takes to cross the chart, independent of where the last
    g-sensor sample itself falls - same reasoning and same fallback
    (the last sample's own offset) as render_gsensor_video()'s own
    `duration_seconds` parameter; see that function's docstring for
    why a recording with no trailing g-sensor data needs this to stay
    in sync with the real video length.

    `orientation` ('horizontal', the default, or 'vertical') and
    `width`/`height` are forwarded straight to
    gsensor_graph_render.render_base_frame()/render_frame() - see
    those functions' own docstrings, and the module docstring there,
    for what each orientation looks like and why. `width`/`height`
    matter in particular for --stitch's own --stitch-graph panel (see
    stitch.py's _render_graph_panel()), which needs the video encoded
    at an exact pixel size to hstack/vstack cleanly alongside the
    camera composite - left as None here (the render module's own
    per-orientation defaults) for the standalone --gsensor-graph-video
    case, which has no such constraint.

    Returns None (and writes nothing) if there aren't at least two
    samples, or they span zero time - the same convention
    render_gsensor_video() and export_trip()'s other outputs use.
    """

    if len(samples) < 2:
        return None

    total_seconds = (
        duration_seconds
        if duration_seconds is not None
        else samples[-1].offset.total_seconds()
    )
    if total_seconds <= 0:
        return None

    baseline = baseline_for_samples(samples)
    scale = scale_for_samples(samples, baseline=baseline)
    base_image = render_base_frame(
        samples, baseline, scale, total_seconds,
        orientation=orientation, width=width, height=height,
    )

    frame_count = max(2, int(total_seconds * fps) + 1)

    destination.parent.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory() as frame_dir_name:
        frame_dir = Path(frame_dir_name)

        for frame_number in range(frame_count):
            elapsed_seconds = min(frame_number / fps, total_seconds)
            frame = render_frame(
                base_image, elapsed_seconds, total_seconds,
                orientation=orientation,
            )
            frame.save(frame_dir / f"frame_{frame_number:06d}.png")

        encode_frame_sequence(frame_dir, destination, fps)

    return destination
