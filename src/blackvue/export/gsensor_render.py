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

The background is a flat chroma-key green rather than the cream tone
map_render.py uses - gsensor.mp4 is meant to be composited over the
front/rear footage later (the future --stitch item), not watched on
its own, so the background needs to key out cleanly (ffmpeg's
colorkey/chromakey filters) rather than blend in.

Copyright (C) 2026 Christer R. (sssreh)

SPDX-License-Identifier: GPL-3.0-or-later
"""

from __future__ import annotations

from PIL import Image
from PIL import ImageDraw

# Pure green: the simplest possible target for a chroma-key filter to
# match exactly (a single RGB value, no gradient/anti-aliasing blend
# to account for) since PIL's ImageDraw fills solid shapes with no
# anti-aliasing of its own.
BACKGROUND_COLOR = (0, 255, 0)
RING_COLOR = (255, 255, 255)
AXIS_COLOR = (255, 255, 255)
TRAIL_COLOR = (230, 57, 70)
DOT_COLOR = (230, 57, 70)
DOT_OUTLINE = (255, 255, 255)

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
    width: int = DEFAULT_SIZE,
    height: int = DEFAULT_SIZE,
    margin: int = DEFAULT_MARGIN_PX,
) -> Image.Image:
    """Render one dot-gauge frame on a flat chroma-key green
    background: reference rings/axes, a fading trail of recent (x, y)
    samples, and a dot at the current sample."""

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

    return image
