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
itself is static for however long it spans - only the playhead moves -
so this module renders each such base chart once and reuses it for
every output frame that falls within its span, rather than needing
gsensor_video.py's own O(samples + frames) interpolation machinery at
all: there's no per-frame sample lookup here, just a playhead position
computed directly from elapsed time.

By default there is exactly one base chart, spanning the whole trip -
render_gsensor_graph_video()'s own `window_seconds=None` default,
used by the standalone --gsensor-graph-video output. Passing a real
`window_seconds` (used only by --stitch-graph's own panel; see
render_gsensor_graph_video()'s docstring and stitch.py's
_render_graph_panel()) instead paginates a long trip into several
fixed-length base charts, one per window, and picks the right one per
output frame - Christer: "maybe a rolling windows of 10 minutes".

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
    show_z: bool = False,
    window_seconds: float | None = None,
) -> Path | None:
    """Render a trip's merged g-sensor samples into a strip-chart
    overlay video at `destination`: colored X/Y (and Z, when `show_z`)
    line traces drawn once across the whole trip, with a playhead
    moving as the video plays, on the same flat chroma-key green
    background gsensor.mp4 uses.

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

    `show_z` (default False, also forwarded straight to
    render_base_frame()/render_frame()) - Z is hidden by default; see
    gsensor_graph_render.py's own module docstring for Christer's
    reasoning ("Z is just not useful, unless you hit a giant pothole,
    but then the video probably got that and the reaction of the
    driver"). Pass True for a specific look at a bump/vibration event.

    `window_seconds` (default None) switches from this function's own
    original "one static chart spanning the whole trip" rendering to a
    paginated one: the trip is split into fixed `window_seconds`-long
    chunks (0..window_seconds, window_seconds..2*window_seconds, ...,
    the last one however much of the trip remains), each rendered as
    its own base image via render_base_frame()'s `window_start`/
    `window_end` (see that function's own docstring) - X/Y/Z traces
    only from that chunk's own samples, tick labels showing each
    chunk's own real absolute trip time rather than resetting to 00:00
    per chunk, but all chunks sharing the one baseline/scale computed
    from the *whole* trip's samples, so a given magnitude still reads
    the same size on every page. Per output frame, whichever chunk
    `elapsed_seconds` currently falls in supplies the base image, and
    render_frame() gets that chunk's own chunk-relative elapsed/total
    seconds - so the playhead still sweeps left-to-right (or top-to-
    bottom) exactly as it always has, just across one page at a time,
    jumping to the next page's own base image once the elapsed time
    crosses a `window_seconds` boundary. If the trip's own
    `total_seconds` doesn't exceed `window_seconds`, this falls back
    to the plain single-whole-trip-chart rendering, since there's
    nothing meaningful to paginate. None (the default) always uses the
    original single-chart rendering regardless of trip length - this
    is what the standalone --gsensor-graph-video output uses; only
    --stitch-graph's own panel (see stitch.py's _render_graph_panel())
    passes a real value here (Christer: "maybe a rolling windows of 10
    minutes", scoped to the --stitch-graph panel specifically - see
    WORKING_CONTEXT.md for the full discussion).

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

    paginated = (
        window_seconds is not None
        and window_seconds > 0
        and total_seconds > window_seconds
    )

    if paginated:
        # Fixed window_seconds-wide chunks covering the whole trip -
        # the last one is whatever's left over, not padded back out to
        # a full window_seconds (see the docstring above; the playhead
        # sweeps that shorter final page proportionally faster than
        # the others as a result, a deliberate simplicity trade-off
        # rather than padding blank trailing space onto it).
        chunk_boundaries: list[tuple[float, float]] = []
        chunk_start = 0.0
        while chunk_start < total_seconds:
            chunk_end = min(chunk_start + window_seconds, total_seconds)
            chunk_boundaries.append((chunk_start, chunk_end))
            chunk_start = chunk_end

        chunk_images = [
            render_base_frame(
                [
                    sample for sample in samples
                    if chunk_window_start
                    <= sample.offset.total_seconds()
                    <= chunk_window_end
                ],
                baseline, scale, total_seconds,
                window_start=chunk_window_start, window_end=chunk_window_end,
                orientation=orientation, width=width, height=height,
                show_z=show_z,
            )
            for chunk_window_start, chunk_window_end in chunk_boundaries
        ]
    else:
        base_image = render_base_frame(
            samples, baseline, scale, total_seconds,
            orientation=orientation, width=width, height=height, show_z=show_z,
        )

    frame_count = max(2, int(total_seconds * fps) + 1)

    destination.parent.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory() as frame_dir_name:
        frame_dir = Path(frame_dir_name)

        for frame_number in range(frame_count):
            elapsed_seconds = min(frame_number / fps, total_seconds)

            if paginated:
                chunk_index = min(
                    int(elapsed_seconds // window_seconds),
                    len(chunk_boundaries) - 1,
                )
                chunk_window_start, chunk_window_end = chunk_boundaries[chunk_index]
                frame = render_frame(
                    chunk_images[chunk_index],
                    elapsed_seconds - chunk_window_start,
                    chunk_window_end - chunk_window_start,
                    orientation=orientation, show_z=show_z,
                )
            else:
                frame = render_frame(
                    base_image, elapsed_seconds, total_seconds,
                    orientation=orientation, show_z=show_z,
                )
            frame.save(frame_dir / f"frame_{frame_number:06d}.png")

        encode_frame_sequence(frame_dir, destination, fps)

    return destination
