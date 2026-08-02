"""
G-sensor dot-gauge frame rendering for bv-export.

Draws one frame of a racing-telemetry-style "dot gauge": a circular
dial with a dot at the current sample's (lateral, longitudinal)
position (relative to a per-trip baseline - see
baseline_for_samples()) and a short fading trail behind it. The
gauge's horizontal axis is always lateral movement and its vertical
axis is always longitudinal (acceleration/braking) movement - that
screen-space convention hasn't changed - but which *raw* g-sensor
field feeds each has flipped twice now; see below for the current
answer.

A recording with known, timestamped maneuvers (two right turns and a
left U-turn, plus a braking event caught incidentally just before the
U-turn) found raw Y tracking turning (sustained positive during both
right turns, sustained negative during the U-turn) and raw Z tracking
braking (a sustained dip right before the U-turn) - with raw X showing
no sustained response to any of the three events. Told this directly -
that treating X as lateral runs against that same recording - Christer
chose to override it anyway ("Actually swap which raw channel means
what"), swapping the module to plot raw X as lateral and raw Y as
longitudinal/braking, with a note that a planned follow-up recording
with hard acceleration, braking, and turns should settle the question
either way once it landed.

That follow-up recording arrived (`20260802_103545_M.3gf`, two labeled
real-world events: heavy acceleration at 16s, a 540-degree left turn
in a roundabout at 127s) and reconfirmed the original finding, not the
override: raw Z moved 26.6x its own baseline stdev during the
acceleration event (X and Y far smaller), and raw Y moved 41.6x its
own baseline stdev during the turn (X and Z far smaller). Raw X showed
no sustained response to either event, on either recording. Christer
accepted this result, so the module is back to plotting raw Y as
lateral and raw Z as longitudinal/braking, with raw X unused (assumed
vertical/mounting axis) - see WORKING_CONTEXT.md for the full numbers.
See gsensor_reader.py's own module docstring for the standing caveat
that the physical *unit* of these readings (milli-g, raw ADC counts,
or something else) remains unconfirmed regardless of which axis is
which.

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
# Black rather than white: reads as a well-defined "target" (rings +
# crosshair) against the green background - Christer's call, picked
# from three rendered mockups (plain recolor vs. a full bullseye vs.
# a black marker instead).
RING_COLOR = (0, 0, 0)
AXIS_COLOR = (0, 0, 0)
TRAIL_COLOR = (230, 57, 70)
DOT_COLOR = (230, 57, 70)
DOT_OUTLINE = (255, 255, 255)

DEFAULT_SIZE = 480
DEFAULT_MARGIN_PX = 40
DEFAULT_MINIMUM_SCALE = 1.0
# 1.0, not >1.0: Christer wants the trip's single busiest moment to
# actually reach the gauge's outermost ring, not sit padded inside it
# - "the max size of gsensor overlay output should reach the third
# outer ring." A trip's peak dot now lands exactly on the rim; nothing
# in a normal trip should overshoot it, since it's defined as that
# trip's own observed peak.
DEFAULT_SCALE_PADDING = 1.0

# A single-pixel outline reads fine on this module's own 480x480
# canvas, viewed on its own - but by the time gsensor.mp4 actually
# reaches the screen, it's been through --stitch's own downscale (the
# overlay is composited at a fraction of the camera composite's own
# width - see stitch.py's gsensor_size) and a real H.264 encode, both
# of which blur/discard thin single-pixel detail. Confirmed on a real
# export: at a realistic overlay size, a 1px ring survived at ~0% of
# its own outline (a handful of stray pixels, indistinguishable from
# encoder noise) while the much bolder 8px-radius dot came through
# fine - Christer saw the dot but not the rings around it. 2px roughly
# triples that survival rate in the same test; the rings/crosshair
# both use it, not just the outer ring.
RING_LINE_WIDTH = 2


def _median(values: list[float]) -> float:
    ordered = sorted(values)
    count = len(ordered)
    middle = count // 2

    if count % 2:
        return ordered[middle]
    return (ordered[middle - 1] + ordered[middle]) / 2.0


def baseline_for_samples(samples) -> tuple[float, float]:
    """Return the (lateral, longitudinal) reading gsensor.mp4's gauge
    should treat as its center, for a set of g-sensor samples: the
    trip's own median Y (lateral) and median Z (longitudinal) - see
    this module's own docstring for the two real test recordings that
    settled this.

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
        _median([float(sample.y) for sample in samples]),
        _median([float(sample.z) for sample in samples]),
    )


def scale_for_samples(
    samples,
    *,
    baseline: tuple[float, float] = (0.0, 0.0),
    padding: float = DEFAULT_SCALE_PADDING,
    minimum: float = DEFAULT_MINIMUM_SCALE,
) -> float:
    """Return the gauge scale (the (lateral, longitudinal) magnitude
    that should sit at the gauge's outer ring) for a set of g-sensor
    samples: the largest deviation from `baseline` seen in either axis
    across all of them, times `padding`.

    `padding` defaults to 1.0 (see DEFAULT_SCALE_PADDING) - the trip's
    single busiest moment lands exactly on the outer ring rather than
    inside it, at Christer's own request. A caller passing a `padding`
    > 1.0 would instead leave headroom above the trip's own observed
    peak, e.g. for a scale meant to be reused across multiple trips.

    Floors at `minimum` so a trip with a near-flat sensor reading
    (parked, or a very gentle drive) still gets a sane, non-degenerate
    scale instead of dividing by ~0.
    """

    baseline_lateral, baseline_longitudinal = baseline
    peak = 0.0
    for sample in samples:
        peak = max(
            peak,
            abs(sample.y - baseline_lateral),
            abs(sample.z - baseline_longitudinal),
        )

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
            width=RING_LINE_WIDTH,
        )

    draw.line(
        (center[0] - radius, center[1], center[0] + radius, center[1]),
        fill=AXIS_COLOR,
        width=RING_LINE_WIDTH,
    )
    draw.line(
        (center[0], center[1] - radius, center[0], center[1] + radius),
        fill=AXIS_COLOR,
        width=RING_LINE_WIDTH,
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
