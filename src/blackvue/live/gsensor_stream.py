"""
Live scrolling g-sensor strip rendering for bv-live: draws the last
`window_seconds` of live FrontRear/LeftRight/UpperLower readings as a
three-line strip, redrawn from scratch on every frame - "gsensor line
at the bottom", per Christer.

Deliberately its own small renderer rather than reusing
export/gsensor_graph_render.py directly: that module's
render_base_frame()/render_frame() split - draw the static whole-trip
traces once, then composite just a moving playhead per frame - and its
window_start/window_end bookkeeping are both built around a trip with
a known, finite total_seconds. A live view has no fixed end at all;
retrofitting that shape onto an open-ended stream would need working
around more of it than it would actually reuse. There's no
precompute-once optimization to make here either - unlike a static
trip render, the *entire* visible window changes on every live frame
as the window slides forward, so there's nothing left that would
stay the same between frames the way the trace/axis drawing does for
gsensor_graph_render.py's own static base image.

The live JSON's own field names - FrontRear/LeftRight/UpperLower - are
used directly as the three trace labels, not relabeled to the offline
.3gf format's X/Y/Z convention - see parser/livedata.py's
_GSENSOR_PATTERN comment for why.

This is a first pass, deliberately simpler than
gsensor_graph_render.py's own current state (which went through many
rounds of tuning - independent per-axis scaling, a dedicated legend
area, lane-splitting Z out - see WORKING_CONTEXT.md for that whole
history): one shared scale across all three traces, no legend-reserve/
lane-split machinery. Expected to get the same kind of iterative
polish once Christer's actually seen it live, same as every other
visual piece of this project has.

Copyright (C) 2026 Christer R. (sssreh)

SPDX-License-Identifier: GPL-3.0-or-later
"""

from __future__ import annotations

from PIL import Image
from PIL import ImageDraw
from PIL import ImageFont

from .telemetry import GSensorSample
from .telemetry import TelemetryState

# "the gsensor line too" [bigger than usual] - Christer, since the
# live camera feed itself is small. A short, wide strip - matching
# export/gsensor_graph_render.py's own horizontal-mode shape - rather
# than a square gauge.
DEFAULT_WIDTH = 1600
DEFAULT_HEIGHT = 260

# Default rolling window - bv-live's own --gsensor-window flag
# overrides this. Long enough to read as a real trend (a corner, a
# stretch of braking), short enough to stay a "quick glance" strip
# rather than growing into a full trip history like the offline
# renderer's own whole-trip view.
DEFAULT_WINDOW_SECONDS = 60.0

BACKGROUND_COLOR = (250, 250, 250)
AXIS_COLOR = (0, 0, 0)
TEXT_COLOR = (0, 0, 0)
MARGIN_PX = 16
TRACE_LINE_WIDTH = 1

# Base identity colors, matching gsensor_graph_render.py's own choice
# of accent colors (before its own lightening step) for a little
# visual continuity between the live strip and the offline one.
FRONT_REAR_COLOR = (69, 123, 157)
LEFT_RIGHT_COLOR = (230, 57, 70)
UPPER_LOWER_COLOR = (241, 196, 15)

# A reading's own physical unit/scale isn't confirmed for this device
# (see telemetry/gsensor_reader.py's own docstring on the offline
# .3gf format) - the live JSON's FrontRear/LeftRight/UpperLower values
# aren't independently confirmed to be on the same scale as that
# either. MINIMUM_SCALE floors the auto-scale so a near-motionless
# vehicle's own tiny jitter doesn't get amplified into a wild-looking
# trace; SCALE_PADDING leaves a little headroom above the window's own
# peak so a trace never runs exactly to the very edge of its lane.
MINIMUM_SCALE = 1.0
SCALE_PADDING = 1.2

LEGEND_ROWS = (
    ("Front/Rear", FRONT_REAR_COLOR),
    ("Left/Right", LEFT_RIGHT_COLOR),
    ("Upper/Lower", UPPER_LOWER_COLOR),
)
LEGEND_SWATCH_LENGTH = 16
LEGEND_ROW_GAP = 4


def _draw_legend(draw: ImageDraw.ImageDraw, x: float, y: float) -> None:
    font = ImageFont.load_default()
    for label, color in LEGEND_ROWS:
        row_mid = y + 6
        draw.line((x, row_mid, x + LEGEND_SWATCH_LENGTH, row_mid), fill=color, width=3)
        draw.text(
            (x + LEGEND_SWATCH_LENGTH + 4, row_mid), label, font=font,
            fill=TEXT_COLOR, anchor="lm",
        )
        y += 12 + LEGEND_ROW_GAP


def render_live_gsensor_frame(
    samples: tuple[GSensorSample, ...],
    window_seconds: float,
    *,
    width: int = DEFAULT_WIDTH,
    height: int = DEFAULT_HEIGHT,
    margin: int = MARGIN_PX,
) -> Image.Image:
    """Render one live g-sensor strip frame from `samples` (already
    filtered to the current rolling window by the caller - see
    live_gsensor_frame_stream()), time running left to right across
    the full `window_seconds` span regardless of how much of it
    `samples` actually covers (a session younger than the window shows
    a partly-empty strip rather than stretching what little data
    exists to fill it, so the trace's own timescale doesn't visibly
    warp as the window fills up)."""

    image = Image.new("RGB", (width, height), BACKGROUND_COLOR)
    draw = ImageDraw.Draw(image)

    plot_left = margin
    plot_right = width - margin
    plot_top = margin
    plot_bottom = height - margin
    zero_y = (plot_top + plot_bottom) / 2

    draw.line((plot_left, zero_y, plot_right, zero_y), fill=AXIS_COLOR, width=1)

    if len(samples) >= 2:
        now = samples[-1].at
        window_start = now - window_seconds

        peak = max(
            max(abs(sample.front_rear) for sample in samples),
            max(abs(sample.left_right) for sample in samples),
            max(abs(sample.upper_lower) for sample in samples),
            MINIMUM_SCALE,
        )
        scale = peak * SCALE_PADDING

        def to_points(values: list[float]) -> list[tuple[float, float]]:
            points = []
            for sample, value in zip(samples, values):
                fraction = (sample.at - window_start) / window_seconds
                fraction = min(max(fraction, 0.0), 1.0)
                x = plot_left + fraction * (plot_right - plot_left)
                y = zero_y - (value / scale) * (zero_y - plot_top)
                points.append((x, y))
            return points

        for values, color in (
            ([sample.front_rear for sample in samples], FRONT_REAR_COLOR),
            ([sample.left_right for sample in samples], LEFT_RIGHT_COLOR),
            ([sample.upper_lower for sample in samples], UPPER_LOWER_COLOR),
        ):
            draw.line(to_points(values), fill=color, width=TRACE_LINE_WIDTH, joint="curve")

        _draw_legend(draw, plot_left + 4, plot_top + 4)

    return image


def live_gsensor_frames(
    state: TelemetryState,
    *,
    window_seconds: float = DEFAULT_WINDOW_SECONDS,
    width: int = DEFAULT_WIDTH,
    height: int = DEFAULT_HEIGHT,
):
    """Return a zero-argument callable that renders the current live
    g-sensor frame from `state` - the shape live/mjpeg.py's
    rendered_frame_stream() expects to call repeatedly forever (see
    live/app.py's /stream/gsensor route)."""

    def _render() -> Image.Image:
        samples = state.gsensor_history(window_seconds)
        return render_live_gsensor_frame(
            samples, window_seconds, width=width, height=height
        )

    return _render
