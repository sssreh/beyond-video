"""
G-sensor dot-gauge frame rendering for bv-export.

Draws one frame of a racing-telemetry-style "dot gauge": a circular
dial with a dot at the current sample's (x, y) position (relative to
a per-trip baseline - see baseline_for_samples()) and a short fading
trail behind it. The g-sensor's raw units aren't calibrated (see
telemetry.gsensor_reader's module docstring - could be milli-g, raw
ADC counts, or something else), so this scales purely to the trip's
own observed range rather than claiming any absolute g-force value,
and axes are labeled X/Y rather than "lateral"/"braking" - which
physical direction each axis corresponds to isn't confirmed either.

Copyright (C) 2026 Christer R. (sssreh)

SPDX-License-Identifier: GPL-3.0-or-later
"""

from __future__ import annotations

from PIL import Image
from PIL import ImageDraw
from PIL import ImageFont

# Same fallback chain as map_render._load_font - duplicated rather
# than imported since that's a module-private helper there too.
_FONT_CANDIDATES = (
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "DejaVuSans-Bold.ttf",
)


def _load_font(size: int = 18) -> ImageFont.ImageFont:
    for candidate in _FONT_CANDIDATES:
        try:
            return ImageFont.truetype(candidate, size)
        except OSError:
            continue
    return ImageFont.load_default()


BACKGROUND_COLOR = (247, 244, 238)
RING_COLOR = (222, 218, 210)
AXIS_COLOR = (210, 206, 198)
TRAIL_COLOR = (230, 57, 70)
DOT_COLOR = (230, 57, 70)
DOT_OUTLINE = (255, 255, 255)
TEXT_COLOR = (40, 40, 40)

DEFAULT_SIZE = 480
DEFAULT_MARGIN_PX = 40
DEFAULT_MINIMUM_SCALE = 1.0
DEFAULT_SCALE_PADDING = 1.2


def _median(values: list[float]) -> float:
    ordered = sorted(values)
    count = len(ordered)
    middle = count // 2

    if count % 2:
        return ordered[middle]
    return (ordered[middle - 1] + ordered[middle]) / 2.0


def baseline_for_samples(samples) -> tuple[float, float]:
    """Return the (x, y) reading gsensor.mp4's gauge should treat as
    its center, for a set of g-sensor samples: the trip's own median
    x and median y.

    A dashcam mounted at even a slight angle - or a plain sensor bias
    - means "level, driving straight" rarely reads exactly raw (0, 0),
    so drawing around literal (0, 0) leaves the dot sitting off-center
    even during ordinary driving. The median (rather than the mean) is
    robust to the trip's own turns/bumps pulling the average off to
    one side. Returns (0.0, 0.0) for no samples.
    """

    if not samples:
        return 0.0, 0.0

    return (
        _median([float(sample.x) for sample in samples]),
        _median([float(sample.y) for sample in samples]),
    )


def scale_for_samples(
    samples,
    *,
    baseline: tuple[float, float] = (0.0, 0.0),
    padding: float = DEFAULT_SCALE_PADDING,
    minimum: float = DEFAULT_MINIMUM_SCALE,
) -> float:
    """Return the gauge scale (the (x, y) magnitude that should sit at
    the gauge's outer ring) for a set of g-sensor samples: the largest
    deviation from `baseline` seen in either axis across all of them,
    times `padding` so the busiest moment doesn't sit exactly on the
    rim.

    Floors at `minimum` so a trip with a near-flat sensor reading
    (parked, or a very gentle drive) still gets a sane, non-degenerate
    scale instead of dividing by ~0.
    """

    baseline_x, baseline_y = baseline
    peak = 0.0
    for sample in samples:
        peak = max(peak, abs(sample.x - baseline_x), abs(sample.y - baseline_y))

    return max(peak * padding, minimum)


def _project(
    x: float, y: float, scale: float, radius: float, center: tuple[float, float]
) -> tuple[float, float]:
    cx, cy = center
    # Pixel y grows downward; screen "up" should read as the sample's
    # positive y - flip it.
    return cx + (x / scale) * radius, cy - (y / scale) * radius


def render_frame(
    scale: float,
    trail_points: tuple[tuple[float, float], ...],
    position: tuple[float, float] | None,
    *,
    timestamp_text: str | None = None,
    width: int = DEFAULT_SIZE,
    height: int = DEFAULT_SIZE,
    margin: int = DEFAULT_MARGIN_PX,
) -> Image.Image:
    """Render one dot-gauge frame: reference rings/axes, a fading
    trail of recent (x, y) samples, a dot at the current sample, and
    an optional caption in the corner."""

    image = Image.new("RGB", (width, height), BACKGROUND_COLOR)
    draw = ImageDraw.Draw(image)

    center = (width / 2, height / 2)
    radius = max(min(width, height) / 2 - margin, 1.0)

    for fraction in (1.0, 2 / 3, 1 / 3):
        ring_radius = radius * fraction
        draw.ellipse(
            (
                center[0] - ring_radius, center[1] - ring_radius,
                center[0] + ring_radius, center[1] + ring_radius,
            ),
            outline=RING_COLOR,
            width=1,
        )

    draw.line(
        (center[0] - radius, center[1], center[0] + radius, center[1]),
        fill=AXIS_COLOR,
        width=1,
    )
    draw.line(
        (center[0], center[1] - radius, center[0], center[1] + radius),
        fill=AXIS_COLOR,
        width=1,
    )

    if len(trail_points) >= 2:
        pixels = [
            _project(x, y, scale, radius, center) for x, y in trail_points
        ]
        draw.line(pixels, fill=TRAIL_COLOR, width=2, joint="curve")

    if position is not None:
        x, y = _project(*position, scale, radius, center)
        dot_radius = 8
        draw.ellipse(
            (x - dot_radius, y - dot_radius, x + dot_radius, y + dot_radius),
            fill=DOT_COLOR,
            outline=DOT_OUTLINE,
            width=2,
        )

    if timestamp_text:
        font = _load_font()
        draw.text(
            (margin, height - margin - 4),
            timestamp_text,
            fill=TEXT_COLOR,
            font=font,
            anchor="ls",
        )

    return image
