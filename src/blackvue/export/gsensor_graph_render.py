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

Background is a plain, light near-white - NOT gsensor_render.py's own
chroma-key green. Unlike the dot-gauge, this panel is never
chroma-keyed/composited over footage; stitch.py just hstacks/vstacks
it straight onto the --stitch composite, the same way it handles the
map panel. The green was inherited from gsensor_render.py's own
BACKGROUND_COLOR without checking whether that reasoning actually
applied here, and it read as a plain solid green box in real rendered
output (Christer's own feedback: "awful green background"; verified
via stitch.py - no colorkey/chromakey filter is ever applied to this
panel). BACKGROUND_COLOR is defined locally in this module now,
decoupled from gsensor_render.py's own chroma-key green, which still
needs to stay green for the dot-gauge's own real chroma-key use.

Each axis's own trace is labeled by a small color-key legend (Christer:
"nothing explaining the colors"), spelling out what each axis
physically means - "X — Left/right", "Y — Forward/back",
"Z — Up/down" - the standard accelerometer axis convention, offered as
a best-effort explanation rather than a device-verified calibration
(see gsensor_reader.py's own module docstring: the physical unit/
orientation of these readings isn't independently confirmed for this
device). Christer asked for this exact wording in both orientations.

The legend gets its own dedicated space rather than sitting on top of
the traces: `_legend_reserve_px(orientation)` adds extra room to the
plot area's own left edge in horizontal mode, or its own top edge in
vertical mode (on top of the ordinary DEFAULT_MARGIN_PX), so the chart
itself never renders underneath the legend text - Christer's own
explicit request ("legend should not be on top of lines ... a little
space to the left on horizontal and at the top for vertical"), after
an earlier version drew the legend directly inside the plot area's own
top-left corner where it could visually compete with whatever traces
happened to pass through that exact spot. In vertical mode the legend
is still centered horizontally within the full panel width (see
_draw_legend()), now within its own reserved top strip rather than the
narrower plot area.

Real accelerometer data is jittery enough, sample to sample, that the
three raw traces blurred together at this panel's size (Christer: "vey
cluttered output", diagnosed as the three traces overlapping/blurring
rather than tick-mark or general detail clutter). Lightened trace
colors (see _lighten()) plus light smoothing (a short centered moving
average, see _smoothed()) reduce that blur, but Christer's own
follow-up call - after seeing X/Y/Z still crossing each other
regardless - was to stop sharing one plot entirely for Z specifically:
"let the cluttered Z have its own line". X and Y still share one lane
(`main_lane`), but Z now gets its own separate `z_lane` (see
_split_lanes()), so Z's own trace never crosses X's or Y's. Christer
picked a ~1/3 share of the panel for Z's own lane (Z is one trace
against X/Y's two) over an equal 50/50 split, and the very same
`scale` value for both lanes (not an independent auto-scale for Z)
so a given magnitude still looks the same size in either lane - only
which pixels a lane owns changed, not how values map to pixels within
one. TRACE_LINE_WIDTH itself has gone back and forth several times
(1px, 2px, 1px, 2px again) as the actual sources of clutter got fixed
one at a time - the legend's former overlap with the traces (see
_legend_reserve_px()) and now the lane split above; once those two
were addressed, 2px read fine again.

Copyright (C) 2026 Christer R. (sssreh)

SPDX-License-Identifier: GPL-3.0-or-later
"""

from __future__ import annotations

from PIL import Image
from PIL import ImageDraw
from PIL import ImageFont

# Plain light near-white - not chroma-keyed (see module docstring for
# why this panel is decoupled from gsensor_render.py's own green).
BACKGROUND_COLOR = (250, 250, 250)


def _lighten(color: tuple[int, int, int], amount: float) -> tuple[int, int, int]:
    """Blend `color` toward BACKGROUND_COLOR by `amount` (0..1) - used
    to soften the trace colors below so overlapping traces read as
    less of a solid blur (Christer: "vey cluttered output", the three
    traces overlapping) without losing which color is which."""

    r, g, b = color
    bg_r, bg_g, bg_b = BACKGROUND_COLOR
    return (
        round(r + (bg_r - r) * amount),
        round(g + (bg_g - g) * amount),
        round(b + (bg_b - b) * amount),
    )


# Base identity colors before lightening - red picks up
# gsensor_render.py's own dot/trail accent color for a little visual
# continuity between the two g-sensor views; blue and amber are chosen
# for contrast against both red and each other, and against the black
# axis/tick text.
_BASE_X_COLOR = (230, 57, 70)
_BASE_Y_COLOR = (69, 123, 157)
_BASE_Z_COLOR = (241, 196, 15)

# The actual trace (and legend swatch) colors - lightened from the
# base identity colors above so the three overlapping traces read as
# softer, less cluttered lines rather than a solid blur. The legend
# draws these exact colors, not the (darker) base ones, so what's
# labeled always matches what's actually on the chart.
X_COLOR = _lighten(_BASE_X_COLOR, 0.25)
Y_COLOR = _lighten(_BASE_Y_COLOR, 0.25)
Z_COLOR = _lighten(_BASE_Z_COLOR, 0.25)

AXIS_COLOR = (0, 0, 0)
TICK_COLOR = (0, 0, 0)
# A saturated purple - white (the old choice) would be invisible
# against the new light background. Purple stays clearly distinct
# from the red/blue/amber trace family, the black axis/tick text, and
# the light background alike, including where it crosses a trace or
# the zero-line.
PLAYHEAD_COLOR = (156, 39, 176)

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

# 2px again - the 1px/2px back-and-forth (see the two preceding
# entries in WORKING_CONTEXT.md) was really about the traces crossing
# each other and the legend overlapping them, not line width alone;
# now that Z has its own lane (see Z_LANE_FRACTION below) there's much
# less crossing left for 2px to make worse, so Christer picked the
# bolder width back.
TRACE_LINE_WIDTH = 2
PLAYHEAD_LINE_WIDTH = 3
TICK_FONT_SIZE = 14

# Z gets its own dedicated lane, separate from the shared X/Y plot -
# Christer's own request ("let the cluttered Z have its own line"),
# after X/Y/Z all sharing one plot kept crossing each other regardless
# of line width or smoothing. Z is one trace against X/Y's two, so it
# gets proportionally less of the panel's own value axis (~1/3, the
# rest split none further - X and Y still share the remaining ~2/3
# lane exactly as before). Both lanes are plotted using the very same
# `scale` value (see render_base_frame()) - Christer explicitly chose
# a shared scale over an independent one for Z, so a given magnitude
# still looks the same size in either lane; only the crossing between
# Z and X/Y is what's being removed, not comparability.
Z_LANE_FRACTION = 1 / 3
# Small visual gap between the two lanes, plus a thin divider line
# drawn in it - without some separation the two lanes would look like
# one plot with a kink in it rather than two deliberately distinct
# regions.
LANE_GAP_PX = 8
LANE_DIVIDER_COLOR = (210, 210, 210)

# G-sensor samples arrive roughly every 100ms (see gsensor_reader.py's
# own module docstring), so a 5-sample centered window is roughly half
# a second of smoothing - enough to blur out sample-to-sample jitter on
# real accelerometer data (Christer's own complaint: "vey cluttered
# output", diagnosed as the three traces overlapping/blurring) without
# erasing genuine driving events (braking, cornering), which play out
# over a second or more.
SMOOTHING_WINDOW_SAMPLES = 5

LEGEND_FONT_SIZE = 12
LEGEND_SWATCH_LENGTH = 14
LEGEND_ROW_HEIGHT = 14
LEGEND_PADDING = 6
# Full axis-meaning wording, not just "X"/"Y"/"Z" - Christer's own
# request, kept identical in both orientations even though it runs
# past the narrow vertical side-panel's own right edge (his explicit
# call over abbreviating there - see the module docstring). The exact
# physical axis mapping isn't independently confirmed for this device
# (see gsensor_reader.py's own module docstring on the unconfirmed
# unit/orientation) - this is the standard accelerometer convention,
# offered as a best-effort explanation of the colors, not a verified
# calibration.
LEGEND_LABELS = (
    ("X", "Left/right"),
    ("Y", "Forward/back"),
    ("Z", "Up/down"),
)

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


def _smoothed(
    values: list[float], window: int = SMOOTHING_WINDOW_SAMPLES
) -> list[float]:
    """Return `values` replaced by a centered moving average over
    `window` samples, shrinking the window near both ends rather than
    padding with a fixed value there (which would drag the smoothed
    line toward an artificial reading right where the trip starts/ends).

    Applied only to the drawn trace in render_base_frame() - NOT to the
    raw samples baseline_for_samples()/scale_for_samples() use, since a
    smoothed peak would under-report the trip's real scale.
    """

    if window <= 1 or len(values) < 2:
        return list(values)

    half = window // 2
    count = len(values)
    smoothed = []
    for i in range(count):
        lo = max(0, i - half)
        hi = min(count, i + half + 1)
        window_slice = values[lo:hi]
        smoothed.append(sum(window_slice) / len(window_slice))
    return smoothed


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
    legend_reserve: float = 0.0,
) -> tuple[float, float, float, float]:
    """Return (left, top, right, bottom) pixel bounds of the trace
    -drawing area, leaving `margin` on every edge plus extra room for
    time-tick labels - reserved below the bottom edge in horizontal
    mode, or to the left of the left edge in vertical mode (kept
    upright there rather than rotated - see the module docstring).

    `legend_reserve` reserves further room, on top of `margin`, for the
    legend to have its own dedicated space rather than sitting on top
    of the traces (Christer: "legend should not be on top of lines") -
    added to the left edge in horizontal mode (the legend's own column
    sits to the left of the chart), or to the top edge in vertical mode
    (the legend's own row sits above the chart). See
    _legend_reserve_px() for how this is actually computed.
    """

    if orientation == "vertical":
        left = float(margin + tick_label_reserve)
        top = float(margin + legend_reserve)
        right = float(width - margin)
        bottom = float(height - margin)
    else:
        left = float(margin + legend_reserve)
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


def _split_lanes(value_start: float, value_end: float) -> tuple[
    tuple[float, float], tuple[float, float]
]:
    """Split the overall value axis [value_start, value_end] into
    (main_lane, z_lane) sub-ranges, separated by LANE_GAP_PX - X/Y
    share `main_lane` (the larger of the two), Z gets its own
    `z_lane` (Z_LANE_FRACTION of the space), so Z's trace no longer
    crosses X/Y's (Christer: "let the cluttered Z have its own line").
    Both sub-ranges get passed the very same `scale` value by the
    caller when actually plotting into them (see render_base_frame()),
    so a given magnitude looks the same size in either lane - only
    which pixels each lane owns changes here, not how values map to
    pixels within a lane."""

    available = (value_end - value_start) - LANE_GAP_PX
    z_span = available * Z_LANE_FRACTION
    main_span = available - z_span

    main_start = value_start
    main_end = value_start + main_span
    z_start = main_end + LANE_GAP_PX
    z_end = value_end

    return (main_start, main_end), (z_start, z_end)


def _legend_text_width(draw: ImageDraw.ImageDraw, font: ImageFont.ImageFont) -> float:
    """Return the widest of the three legend rows' own rendered text
    width ("X — Left/right", "Y — Forward/back", "Z — Up/down") -
    shared by _legend_reserve_px() (how much space to set aside) and
    _draw_legend() (where exactly to center the block within it), so
    the two can never disagree with each other about the same width.
    """

    return max(
        draw.textbbox((0, 0), f"{axis} — {meaning}", font=font)[2]
        for axis, meaning in LEGEND_LABELS
    )


def _legend_reserve_px(orientation: str) -> float:
    """Return how much extra room, beyond DEFAULT_MARGIN_PX, the plot
    area should give up so the legend gets its own dedicated space
    instead of sitting on top of the traces (Christer: "legend should
    not be on top of lines ... a little space to the left on horizontal
    and at the top for vertical"). Horizontal mode reserves a column
    wide enough for the widest legend row (swatch + gap + text, plus
    padding on both sides); vertical mode reserves a row tall enough
    for all three stacked legend rows (plus padding top and bottom).
    See _plot_area()'s own `legend_reserve` parameter for where this
    actually gets applied.
    """

    if orientation == "vertical":
        return 3 * LEGEND_ROW_HEIGHT + LEGEND_PADDING * 2

    font = _load_font(LEGEND_FONT_SIZE)
    measuring_draw = ImageDraw.Draw(Image.new("RGB", (1, 1)))
    text_width = _legend_text_width(measuring_draw, font)
    return LEGEND_SWATCH_LENGTH + 4 + text_width + LEGEND_PADDING * 2


def _draw_legend(
    draw: ImageDraw.ImageDraw,
    left: float,
    top: float,
    *,
    canvas_width: float | None = None,
    orientation: str = "horizontal",
) -> None:
    """Draw a small X/Y/Z color-key, one row per axis - the chart
    otherwise has no indication of which trace is which (Christer:
    "nothing explaining the colors"). Draws into the legend's own
    dedicated space (see _legend_reserve_px()/_plot_area()'s own
    `legend_reserve`), not on top of the plot area, so it never
    competes visually with whatever traces happen to pass through the
    same spot.

    Horizontal mode (the default): anchored at (`left`, `top`) -
    callers pass the panel's own margin corner here, since the legend's
    reserved column sits between the margin and the (now further
    right) plot area's own left edge.

    Vertical mode: horizontally centered as a block within
    `canvas_width` (the panel's own full image width) rather than
    left-anchored - Christer's own request, and also what keeps the
    longest row ("Y — Forward/back", ~123px) comfortably inside the
    panel rather than running past an edge. All three rows share one
    swatch x position (the widest row's own left edge) so the block
    reads as one clean centered unit rather than each row independently
    re-centering around its own, differently-long text. `top` still
    anchors the block's own y position - callers pass the panel's own
    margin corner here too, since the legend's reserved row sits above
    the (now further down) plot area's own top edge.
    """

    font = _load_font(LEGEND_FONT_SIZE)
    colors = (X_COLOR, Y_COLOR, Z_COLOR)
    rows = [
        (axis, meaning, color, f"{axis} — {meaning}")
        for (axis, meaning), color in zip(LEGEND_LABELS, colors)
    ]

    if orientation == "vertical" and canvas_width is not None:
        block_width = LEGEND_SWATCH_LENGTH + 4 + _legend_text_width(draw, font)
        x = (canvas_width - block_width) / 2
    else:
        x = left + LEGEND_PADDING

    y = top + LEGEND_PADDING
    for _, _, color, text in rows:
        row_mid = y + LEGEND_ROW_HEIGHT / 2
        draw.line(
            (x, row_mid, x + LEGEND_SWATCH_LENGTH, row_mid),
            fill=color, width=TRACE_LINE_WIDTH + 2,
        )
        draw.text(
            (x + LEGEND_SWATCH_LENGTH + 4, row_mid), text,
            font=font, fill=AXIS_COLOR, anchor="lm",
        )
        y += LEGEND_ROW_HEIGHT


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

    legend_reserve = _legend_reserve_px(orientation)
    left, top, right, bottom = _plot_area(
        width, height, margin, tick_label_reserve, orientation,
        legend_reserve=legend_reserve,
    )

    if orientation == "vertical":
        time_start, time_end = top, bottom
        value_start, value_end = left, right
    else:
        time_start, time_end = left, right
        value_start, value_end = top, bottom

    # X/Y share main_lane, Z gets its own z_lane - see _split_lanes()'s
    # own docstring for why (Christer: "let the cluttered Z have its
    # own line"). Both are sub-ranges of the same overall value axis,
    # separated by a small gap plus a light divider line.
    main_lane, z_lane = _split_lanes(value_start, value_end)
    main_start, main_end = main_lane
    z_start, z_end = z_lane

    main_zero_pos = _value_to_pos(0.0, scale, main_start, main_end)
    z_zero_pos = _value_to_pos(0.0, scale, z_start, z_end)
    divider_pos = (main_end + z_start) / 2
    if orientation == "vertical":
        draw.line((main_zero_pos, top, main_zero_pos, bottom), fill=AXIS_COLOR, width=1)
        draw.line((z_zero_pos, top, z_zero_pos, bottom), fill=AXIS_COLOR, width=1)
        draw.line(
            (divider_pos, top, divider_pos, bottom), fill=LANE_DIVIDER_COLOR, width=1
        )
    else:
        draw.line((left, main_zero_pos, right, main_zero_pos), fill=AXIS_COLOR, width=1)
        draw.line((left, z_zero_pos, right, z_zero_pos), fill=AXIS_COLOR, width=1)
        draw.line(
            (left, divider_pos, right, divider_pos), fill=LANE_DIVIDER_COLOR, width=1
        )

    baseline_x, baseline_y, baseline_z = baseline
    if len(samples) >= 2:
        times = [
            _time_to_pos(
                sample.offset.total_seconds(), total_seconds, time_start, time_end
            )
            for sample in samples
        ]
        # Lightly smoothed before plotting (see _smoothed()'s own
        # docstring) - the raw per-sample values are still what
        # baseline/scale were computed from, just not what gets drawn.
        smoothed_axes = (
            _smoothed([sample.x for sample in samples]),
            _smoothed([sample.y for sample in samples]),
            _smoothed([sample.z for sample in samples]),
        )
        for axis_index, (color, base) in enumerate((
            (X_COLOR, baseline_x), (Y_COLOR, baseline_y), (Z_COLOR, baseline_z),
        )):
            lane_start, lane_end = z_lane if axis_index == 2 else main_lane
            points = []
            for t, value in zip(times, smoothed_axes[axis_index]):
                v = _value_to_pos(value - base, scale, lane_start, lane_end)
                points.append((v, t) if orientation == "vertical" else (t, v))
            draw.line(points, fill=color, width=TRACE_LINE_WIDTH, joint="curve")

        _draw_legend(draw, margin, margin, canvas_width=width, orientation=orientation)

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
    legend_reserve = _legend_reserve_px(orientation)
    left, top, right, bottom = _plot_area(
        width, height, margin, tick_label_reserve, orientation,
        legend_reserve=legend_reserve,
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
