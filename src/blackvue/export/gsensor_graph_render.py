"""
G-sensor strip-chart frame rendering for bv-export.

Draws a second, alternate g-sensor visualization alongside the
existing circular dot-gauge (gsensor_render.py/gsensor_video.py):
a horizontal strip chart with three colored line traces (X/Y/Z) drawn
across the whole trip at once, and a vertical playhead line marking
the current position - Christer's own reference was the BlackVue SD
Card Viewer app's own g-sensor panel, a scrolling strip with time-tick
labels along the bottom and a moving playhead.

Unlike the dot-gauge (which redraws its rings/trail/dot fresh every
frame), this is a static full-trip overview: the three traces and the
time-axis ticks are drawn once (render_base_frame()), and each output
frame is just that same base image with a thin vertical playhead line
composited on top at the current elapsed time's x position
(render_frame()) - see gsensor_graph_video.py's own frame loop, which
takes advantage of this split to only pay per-frame drawing cost for
the playhead itself, not the whole chart.

Same chroma-key green background as gsensor_render.py (see that
module's own docstring for why) - this is meant to be composited over
front/rear footage later too, so BACKGROUND_COLOR is imported from
there rather than redefined, guaranteeing both g-sensor overlays key
out identically.

Copyright (C) 2026 Christer R. (sssreh)

SPDX-License-Identifier: GPL-3.0-or-later
"""

from __future__ import annotations

from PIL import Image
from PIL import ImageDraw
from PIL import ImageFont

from .gsensor_render import BACKGROUND_COLOR

# Three distinct, non-green trace colors (green is reserved for the
# chroma-key background - a green trace would key out along with it
# once composited). Red picks up gsensor_render.py's own dot/trail
# accent color for a little visual continuity between the two g-sensor
# views; blue and amber are chosen for contrast against both red and
# each other, and against the black axis/tick text.
X_COLOR = (230, 57, 70)
Y_COLOR = (69, 123, 157)
Z_COLOR = (241, 196, 15)
AXIS_COLOR = (0, 0, 0)
TICK_COLOR = (0, 0, 0)
# White reads clearly against all three trace colors and the black
# zero-line/ticks alike - no single trace color would stay visible
# against a trace of its own color crossing it.
PLAYHEAD_COLOR = (255, 255, 255)

# A short, wide strip - "the bottom panel", not a square gauge -
# matching the shape of Christer's reference screenshot rather than
# gsensor_render.py's own 480x480 dot-gauge canvas.
DEFAULT_WIDTH = 960
DEFAULT_HEIGHT = 220
DEFAULT_MARGIN_PX = 32
# Extra room below the plot area itself for the time-tick labels -
# added to DEFAULT_MARGIN_PX on the bottom edge only.
DEFAULT_TICK_LABEL_HEIGHT_PX = 20
DEFAULT_MINIMUM_SCALE = 1.0
DEFAULT_SCALE_PADDING = 1.2

TRACE_LINE_WIDTH = 2
PLAYHEAD_LINE_WIDTH = 3
TICK_FONT_SIZE = 14

_FONT_CANDIDATES = (
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "DejaVuSans-Bold.ttf",
)

# Cached by size, same pattern (and same reasoning - avoid re-parsing
# the same TTF file from disk once per frame) as map_render.py's own
# _load_font()/parking_transition.py's _load_font().
_CACHED_FONT_BY_SIZE: dict[int, ImageFont.ImageFont] = {}


def _load_font(size: int = TICK_FONT_SIZE) -> ImageFont.ImageFont:
    if size not in _CACHED_FONT_BY_SIZE:
        for candidate in _FONT_CANDIDATES:
            try:
                _CACHED_FONT_BY_SIZE[size] = ImageFont.truetype(candidate, size)
                break
            except OSError:
                continue
        else:
            _CACHED_FONT_BY_SIZE[size] = ImageFont.load_default()

    return _CACHED_FONT_BY_SIZE[size]


def _median(values: list[float]) -> float:
    ordered = sorted(values)
    count = len(ordered)
    middle = count // 2

    if count % 2:
        return ordered[middle]
    return (ordered[middle - 1] + ordered[middle]) / 2.0


def baseline_for_samples(samples) -> tuple[float, float, float]:
    """Return the (x, y, z) reading the strip chart's zero-line should
    represent, for a set of g-sensor samples: the trip's own median x,
    y, and z - the same per-axis-median approach
    gsensor_render.baseline_for_samples() uses for the dot-gauge (see
    its own docstring for why median rather than raw zero), extended
    to a third axis since the strip chart, unlike the dot-gauge, plots
    z as well.

    Returns (0.0, 0.0, 0.0) for no samples.
    """

    if not samples:
        return 0.0, 0.0, 0.0

    return (
        _median([float(sample.x) for sample in samples]),
        _median([float(sample.y) for sample in samples]),
        _median([float(sample.z) for sample in samples]),
    )


def scale_for_samples(
    samples,
    *,
    baseline: tuple[float, float, float] = (0.0, 0.0, 0.0),
    padding: float = DEFAULT_SCALE_PADDING,
    minimum: float = DEFAULT_MINIMUM_SCALE,
) -> float:
    """Return the shared vertical scale (the deviation-from-baseline
    magnitude that should sit at the very top/bottom edge of the plot
    area) for a set of g-sensor samples: the largest deviation from
    `baseline` seen on any of the three axes, times `padding`, floored
    at `minimum` - same reasoning as
    gsensor_render.scale_for_samples(), extended across x/y/z so all
    three traces share one consistent vertical axis rather than each
    auto-scaling independently (which would make their relative
    magnitudes misleading to compare).
    """

    baseline_x, baseline_y, baseline_z = baseline
    peak = 0.0
    for sample in samples:
        peak = max(
            peak,
            abs(sample.x - baseline_x),
            abs(sample.y - baseline_y),
            abs(sample.z - baseline_z),
        )

    return max(peak * padding, minimum)


def _nice_tick_interval(total_seconds: float, *, target_tick_count: int = 6) -> float:
    """Pick a "round" time-tick interval (in seconds) for a strip
    spanning `total_seconds`, aiming for roughly `target_tick_count`
    ticks - the same kind of small-step-table approach most charting
    libraries use rather than dividing evenly and landing on an
    unreadable interval like "37 seconds".
    """

    candidates = (
        1, 2, 5, 10, 15, 30, 60, 120, 300, 600, 900, 1800, 3600,
    )

    if total_seconds <= 0:
        return candidates[0]

    raw_interval = total_seconds / target_tick_count
    for candidate in candidates:
        if candidate >= raw_interval:
            return candidate

    return candidates[-1]


def _format_tick(seconds: float) -> str:
    """Format a tick's own time as MM:SS, matching Christer's reference
    screenshot's own "00:52"-style labels."""

    total = round(seconds)
    minutes, secs = divmod(total, 60)
    return f"{minutes:02d}:{secs:02d}"


def _plot_area(
    width: int, height: int, margin: int, tick_label_height: int
) -> tuple[float, float, float, float]:
    """Return (left, top, right, bottom) pixel bounds of the trace
    -drawing area, leaving `margin` on every edge plus extra room for
    time-tick labels beneath the bottom edge."""

    left = float(margin)
    top = float(margin)
    right = float(width - margin)
    bottom = float(height - margin - tick_label_height)
    return left, top, right, bottom


def _time_to_x(elapsed_seconds: float, total_seconds: float, left: float, right: float) -> float:
    if total_seconds <= 0:
        return left
    fraction = min(max(elapsed_seconds / total_seconds, 0.0), 1.0)
    return left + fraction * (right - left)


def _value_to_y(value: float, scale: float, top: float, bottom: float) -> float:
    center = (top + bottom) / 2
    half_span = (bottom - top) / 2
    # Pixel y grows downward; a positive reading should plot above
    # the zero-line - flip it, same convention gsensor_render._project()
    # uses for the dot-gauge.
    return center - (value / scale) * half_span


def render_base_frame(
    samples,
    baseline: tuple[float, float, float],
    scale: float,
    total_seconds: float,
    *,
    width: int = DEFAULT_WIDTH,
    height: int = DEFAULT_HEIGHT,
    margin: int = DEFAULT_MARGIN_PX,
    tick_label_height: int = DEFAULT_TICK_LABEL_HEIGHT_PX,
) -> Image.Image:
    """Render the static, whole-trip part of the strip chart once: the
    three X/Y/Z traces plotted across the full width, a zero-line, and
    time-tick labels along the bottom - everything that doesn't change
    frame to frame. render_frame() composites the moving playhead on
    top of a copy of this image per output frame, rather than this
    function being called again for every frame.
    """

    image = Image.new("RGB", (width, height), BACKGROUND_COLOR)
    draw = ImageDraw.Draw(image)

    left, top, right, bottom = _plot_area(width, height, margin, tick_label_height)

    zero_y = _value_to_y(0.0, scale, top, bottom)
    draw.line((left, zero_y, right, zero_y), fill=AXIS_COLOR, width=1)

    baseline_x, baseline_y, baseline_z = baseline
    for axis_index, (color, base) in enumerate((
        (X_COLOR, baseline_x), (Y_COLOR, baseline_y), (Z_COLOR, baseline_z),
    )):
        if len(samples) < 2:
            continue
        points = [
            (
                _time_to_x(sample.offset.total_seconds(), total_seconds, left, right),
                _value_to_y(
                    (sample.x, sample.y, sample.z)[axis_index] - base,
                    scale, top, bottom,
                ),
            )
            for sample in samples
        ]
        draw.line(points, fill=color, width=TRACE_LINE_WIDTH, joint="curve")

    if total_seconds > 0:
        font = _load_font()
        tick_interval = _nice_tick_interval(total_seconds)
        tick_seconds = 0.0
        while tick_seconds <= total_seconds:
            tick_x = _time_to_x(tick_seconds, total_seconds, left, right)
            draw.line((tick_x, bottom, tick_x, bottom + 4), fill=TICK_COLOR, width=1)
            draw.text(
                (tick_x, bottom + 6),
                _format_tick(tick_seconds),
                font=font,
                fill=TICK_COLOR,
                anchor="ma",
            )
            tick_seconds += tick_interval

    return image


def render_frame(
    base_image: Image.Image,
    elapsed_seconds: float,
    total_seconds: float,
    *,
    margin: int = DEFAULT_MARGIN_PX,
    tick_label_height: int = DEFAULT_TICK_LABEL_HEIGHT_PX,
) -> Image.Image:
    """Return a copy of `base_image` (see render_base_frame()) with a
    vertical playhead line composited at the x position corresponding
    to `elapsed_seconds` out of `total_seconds`."""

    width, height = base_image.size
    left, top, right, bottom = _plot_area(width, height, margin, tick_label_height)

    frame = base_image.copy()
    draw = ImageDraw.Draw(frame)
    playhead_x = _time_to_x(elapsed_seconds, total_seconds, left, right)
    draw.line(
        (playhead_x, top, playhead_x, bottom),
        fill=PLAYHEAD_COLOR,
        width=PLAYHEAD_LINE_WIDTH,
    )

    return frame
