"""
G-sensor strip-chart frame rendering for bv-export.

Draws a second, alternate g-sensor visualization alongside the
existing circular dot-gauge (gsensor_render.py/gsensor_video.py):
a strip chart with three colored line traces (X/Y/Z) drawn across the
whole trip at once, and a playhead line marking the current position -
Christer's own reference was the BlackVue SD Card Viewer app's own
g-sensor panel, a scrolling strip with time-tick labels and a moving
playhead.

Two orientations are supported (see `orientation` on render_base_frame()/
render_frame()): 'horizontal' (the default - a short, wide strip, time
running left to right, matching the reference screenshot) and
'vertical' (a tall, narrow strip, time running top to bottom) - added
for compositing into a --stitch side panel when the bottom of the
frame is already taken by --stitch-map ("select the graph like i
selects map" - Christer). Rather than just rotating the horizontal
chart wholesale (which would leave the MM:SS tick labels sideways),
vertical mode is a genuinely separate layout: the tick labels stay
upright and readable, reserved in a margin to the left of the plot
area instead of below it - Christer's own explicit choice over the
simpler rotate-the-whole-image approach.

Unlike the dot-gauge (which redraws its rings/trail/dot fresh every
frame), this is a static full-trip overview: the three traces and the
time-axis ticks are drawn once (render_base_frame()), and each output
frame is just that same base image with a thin playhead line
composited on top at the current elapsed time's position
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
# gsensor_render.py's own 480x480 dot-gauge canvas. DEFAULT_VERTICAL_*
# is just the transpose of these, for a tall, narrow side panel.
DEFAULT_WIDTH = 960
DEFAULT_HEIGHT = 220
DEFAULT_VERTICAL_WIDTH = 220
DEFAULT_VERTICAL_HEIGHT = 960
DEFAULT_MARGIN_PX = 32
# Extra room reserved for time-tick labels, beyond DEFAULT_MARGIN_PX -
# below the plot area in horizontal mode, to the left of it in vertical
# mode (kept upright there rather than rotated - see the module
# docstring). Vertical needs more room than horizontal's own bottom
# strip since "00:52"-style text takes more horizontal space than it
# does vertical space.
DEFAULT_TICK_LABEL_HEIGHT_PX = 20
DEFAULT_TICK_LABEL_WIDTH_PX = 44
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
    """Return the shared value-axis scale (the deviation-from-baseline
    magnitude that should sit at the very edge of the plot area) for a
    set of g-sensor samples: the largest deviation from `baseline` seen
    on any of the three axes, times `padding`, floored at `minimum` -
    same reasoning as gsensor_render.scale_for_samples(), extended
    across x/y/z so all three traces share one consistent value axis
    rather than each auto-scaling independently (which would make
    their relative magnitudes misleading to compare).
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
    width: int,
    height: int,
    margin: int,
    tick_label_reserve: int,
    orientation: str = "horizontal",
) -> tuple[float, float, float, float]:
    """Return (left, top, right, bottom) pixel bounds of the trace
    -drawing area, leaving `margin` on every edge plus extra room for
    time-tick labels - reserved below the bottom edge in horizontal
    mode, or to the left of the left edge in vertical mode (kept
    upright there rather than rotated - see the module docstring)."""

    if orientation == "vertical":
        left = float(margin + tick_label_reserve)
        top = float(margin)
        right = float(width - margin)
        bottom = float(height - margin)
    else:
        left = float(margin)
        top = float(margin)
        right = float(width - margin)
        bottom = float(height - margin - tick_label_reserve)

    return left, top, right, bottom


def _time_to_pos(
    elapsed_seconds: float, total_seconds: float, axis_start: float, axis_end: float
) -> float:
    """Map `elapsed_seconds` (0..total_seconds, clamped) onto the pixel
    range [axis_start, axis_end] - the time axis is `left..right` in
    horizontal mode, `top..bottom` in vertical mode (time running top
    to bottom, matching the module docstring's own convention)."""

    if total_seconds <= 0:
        return axis_start
    fraction = min(max(elapsed_seconds / total_seconds, 0.0), 1.0)
    return axis_start + fraction * (axis_end - axis_start)


def _value_to_pos(
    value: float, scale: float, axis_start: float, axis_end: float
) -> float:
    """Map a g-sensor reading (already baseline-relative) onto the
    pixel range [axis_start, axis_end] - the value axis is
    `top..bottom` in horizontal mode, `left..right` in vertical mode.
    A positive reading plots toward `axis_start` (up, in horizontal
    mode - the same convention gsensor_render._project() uses for the
    dot-gauge; left, in vertical mode) - consistent across both
    orientations even though the g-sensor's own raw units aren't
    calibrated/physically meaningful (see gsensor_reader.py's module
    docstring), so there's no independent "which way is positive"
    convention worth chasing here."""

    center = (axis_start + axis_end) / 2
    half_span = (axis_end - axis_start) / 2
    return center - (value / scale) * half_span


def render_base_frame(
    samples,
    baseline: tuple[float, float, float],
    scale: float,
    total_seconds: float,
    *,
    width: int | None = None,
    height: int | None = None,
    margin: int = DEFAULT_MARGIN_PX,
    tick_label_reserve: int | None = None,
    orientation: str = "horizontal",
) -> Image.Image:
    """Render the static, whole-trip part of the strip chart once: the
    three X/Y/Z traces plotted across the full length, a zero-line, and
    time-tick labels - everything that doesn't change frame to frame.
    render_frame() composites the moving playhead on top of a copy of
    this image per output frame, rather than this function being
    called again for every frame.

    `orientation` is 'horizontal' (time runs left to right, tick labels
    below the plot area - the default, matching Christer's reference
    screenshot) or 'vertical' (time runs top to bottom, tick labels
    upright in a reserved margin to the left - for a --stitch side
    panel; see the module docstring). `width`/`height`/
    `tick_label_reserve` default to whichever orientation's own
    DEFAULT_*/DEFAULT_VERTICAL_* constants match `orientation` when not
    given explicitly.
    """

    if width is None:
        width = DEFAULT_VERTICAL_WIDTH if orientation == "vertical" else DEFAULT_WIDTH
    if height is None:
        height = (
            DEFAULT_VERTICAL_HEIGHT if orientation == "vertical" else DEFAULT_HEIGHT
        )
    if tick_label_reserve is None:
        tick_label_reserve = (
            DEFAULT_TICK_LABEL_WIDTH_PX
            if orientation == "vertical"
            else DEFAULT_TICK_LABEL_HEIGHT_PX
        )

    image = Image.new("RGB", (width, height), BACKGROUND_COLOR)
    draw = ImageDraw.Draw(image)

    left, top, right, bottom = _plot_area(
        width, height, margin, tick_label_reserve, orientation
    )

    if orientation == "vertical":
        time_start, time_end = top, bottom
        value_start, value_end = left, right
    else:
        time_start, time_end = left, right
        value_start, value_end = top, bottom

    zero_pos = _value_to_pos(0.0, scale, value_start, value_end)
    if orientation == "vertical":
        draw.line((zero_pos, top, zero_pos, bottom), fill=AXIS_COLOR, width=1)
    else:
        draw.line((left, zero_pos, right, zero_pos), fill=AXIS_COLOR, width=1)

    baseline_x, baseline_y, baseline_z = baseline
    for axis_index, (color, base) in enumerate((
        (X_COLOR, baseline_x), (Y_COLOR, baseline_y), (Z_COLOR, baseline_z),
    )):
        if len(samples) < 2:
            continue
        points = []
        for sample in samples:
            t = _time_to_pos(
                sample.offset.total_seconds(), total_seconds, time_start, time_end
            )
            v = _value_to_pos(
                (sample.x, sample.y, sample.z)[axis_index] - base,
                scale, value_start, value_end,
            )
            points.append((v, t) if orientation == "vertical" else (t, v))
        draw.line(points, fill=color, width=TRACE_LINE_WIDTH, joint="curve")

    if total_seconds > 0:
        font = _load_font()
        tick_interval = _nice_tick_interval(total_seconds)
        tick_seconds = 0.0
        while tick_seconds <= total_seconds:
            tick_pos = _time_to_pos(tick_seconds, total_seconds, time_start, time_end)
            label = _format_tick(tick_seconds)
            if orientation == "vertical":
                draw.line(
                    (left - 4, tick_pos, left, tick_pos), fill=TICK_COLOR, width=1
                )
                draw.text(
                    (left - 6, tick_pos), label, font=font, fill=TICK_COLOR,
                    anchor="rm",
                )
            else:
                draw.line(
                    (tick_pos, bottom, tick_pos, bottom + 4), fill=TICK_COLOR, width=1
                )
                draw.text(
                    (tick_pos, bottom + 6), label, font=font, fill=TICK_COLOR,
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
    tick_label_reserve: int | None = None,
    orientation: str = "horizontal",
) -> Image.Image:
    """Return a copy of `base_image` (see render_base_frame()) with a
    playhead line composited at the position corresponding to
    `elapsed_seconds` out of `total_seconds` - a vertical line in
    horizontal mode, a horizontal line in vertical mode. `orientation`
    must match whatever render_base_frame() was called with for the
    same `base_image`."""

    if tick_label_reserve is None:
        tick_label_reserve = (
            DEFAULT_TICK_LABEL_WIDTH_PX
            if orientation == "vertical"
            else DEFAULT_TICK_LABEL_HEIGHT_PX
        )

    width, height = base_image.size
    left, top, right, bottom = _plot_area(
        width, height, margin, tick_label_reserve, orientation
    )

    frame = base_image.copy()
    draw = ImageDraw.Draw(frame)

    if orientation == "vertical":
        playhead_pos = _time_to_pos(elapsed_seconds, total_seconds, top, bottom)
        draw.line(
            (left, playhead_pos, right, playhead_pos),
            fill=PLAYHEAD_COLOR,
            width=PLAYHEAD_LINE_WIDTH,
        )
    else:
        playhead_pos = _time_to_pos(elapsed_seconds, total_seconds, left, right)
        draw.line(
            (playhead_pos, top, playhead_pos, bottom),
            fill=PLAYHEAD_COLOR,
            width=PLAYHEAD_LINE_WIDTH,
        )

    return frame
