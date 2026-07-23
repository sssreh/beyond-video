"""
Map-overlay frame rendering for bv-export.

Draws one frame of a trip's route on a simple basemap built from OSM
road geometry (blackvue.export.osm_roads) - no live map tiles are
fetched or drawn here, this module only draws lines/dots/text with
Pillow from data already in memory. Roads are projected from lat/lon
into pixel space with a simple equirectangular projection (longitude
scaled by cos(mean latitude)); a full Mercator projection would be
overkill at the scale a single driving trip covers and adds
complexity for no visible benefit.

The current-position marker is an arrow rotated to the GPS course
over ground by default, or a custom image (also rotated) when one is
supplied (bv-export --map-icon) - see render_frame()'s docstring.

Copyright (C) 2026 Christer R. (sssreh)

SPDX-License-Identifier: GPL-3.0-or-later
"""

from __future__ import annotations

import math
from pathlib import Path

from PIL import Image
from PIL import ImageDraw
from PIL import ImageFont

from .osm_roads import BoundingBox
from .osm_roads import Road

BACKGROUND_COLOR = (247, 244, 238)
ROAD_COLOR = (140, 134, 122)
ROUTE_COLOR = (230, 57, 70)
POSITION_DOT_COLOR = (230, 57, 70)
POSITION_DOT_OUTLINE = (255, 255, 255)
MARKER_FILL_COLOR = (230, 57, 70)
MARKER_OUTLINE_COLOR = (255, 255, 255)
TEXT_COLOR = (40, 40, 40)

DEFAULT_WIDTH = 640
DEFAULT_HEIGHT = 640
DEFAULT_MARGIN_PX = 24
DEFAULT_MARKER_LENGTH_PX = 16
DEFAULT_MARKER_HALF_WIDTH_PX = 8

_FONT_CANDIDATES = (
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "DejaVuSans-Bold.ttf",
)

# Cached after the first render_frame() call - a map.mp4 export calls
# this once per frame (every frame draws its speed/timestamp text),
# and re-opening and re-parsing the same TTF file from disk that many
# times over was a real, measured chunk of render time for no benefit
# (the font never changes mid-export).
_CACHED_FONT: ImageFont.ImageFont | None = None


def _load_font(size: int = 18) -> ImageFont.ImageFont:
    global _CACHED_FONT

    if _CACHED_FONT is None:
        for candidate in _FONT_CANDIDATES:
            try:
                _CACHED_FONT = ImageFont.truetype(candidate, size)
                break
            except OSError:
                continue
        else:
            _CACHED_FONT = ImageFont.load_default()

    return _CACHED_FONT


def _project(
    lat: float,
    lon: float,
    bbox: BoundingBox,
    width: int,
    height: int,
    margin: int,
) -> tuple[float, float]:
    mean_lat_rad = math.radians((bbox.min_lat + bbox.max_lat) / 2)
    lon_scale = math.cos(mean_lat_rad) or 1e-9

    lon_span = (bbox.max_lon - bbox.min_lon) * lon_scale or 1e-9
    lat_span = (bbox.max_lat - bbox.min_lat) or 1e-9

    usable_width = width - 2 * margin
    usable_height = height - 2 * margin

    x = margin + ((lon - bbox.min_lon) * lon_scale / lon_span) * usable_width
    # Pixel y grows downward; latitude grows upward - flip it.
    y = margin + (1 - (lat - bbox.min_lat) / lat_span) * usable_height

    return x, y


def _arrow_points(
    center: tuple[float, float],
    heading_degrees: float,
    *,
    length: float = DEFAULT_MARKER_LENGTH_PX,
    half_width: float = DEFAULT_MARKER_HALF_WIDTH_PX,
) -> tuple[tuple[float, float], tuple[float, float], tuple[float, float]]:
    """Return the 3 corners of a triangle pointing at `heading_degrees`
    (compass degrees, clockwise from north/"up") centered on `center`.
    """

    angle = math.radians(heading_degrees)
    # Screen coords: +x is east (right), +y is south (down) - up (north,
    # heading 0) is therefore -y.
    dx, dy = math.sin(angle), -math.cos(angle)
    # 90-degrees-clockwise perpendicular of (dx, dy), for the two back
    # corners either side of the nose.
    px, py = -dy, dx

    cx, cy = center
    nose = (cx + dx * length, cy + dy * length)
    back_x, back_y = cx - dx * length * 0.6, cy - dy * length * 0.6
    left = (back_x - px * half_width, back_y - py * half_width)
    right = (back_x + px * half_width, back_y + py * half_width)

    return (nose, right, left)


def _paste_marker_image(
    image: Image.Image,
    marker_image: Image.Image,
    center: tuple[float, float],
    heading_degrees: float | None,
) -> None:
    """Rotate `marker_image` (expected to point "up"/north in its own
    file, RGBA so its own alpha channel can serve as the paste mask)
    to `heading_degrees` and paste it centered on `center`.

    PIL rotates counter-clockwise for a positive angle; compass
    heading is clockwise from north, so the rotation angle is negated.
    """

    angle = -(heading_degrees or 0.0)
    rotated = marker_image.rotate(angle, expand=True, resample=Image.BICUBIC)
    x = int(center[0] - rotated.width / 2)
    y = int(center[1] - rotated.height / 2)
    image.paste(rotated, (x, y), rotated)


def render_base_map(
    bbox: BoundingBox,
    roads: tuple[Road, ...],
    *,
    width: int = DEFAULT_WIDTH,
    height: int = DEFAULT_HEIGHT,
    margin: int = DEFAULT_MARGIN_PX,
) -> Image.Image:
    """Render just the background + road network for `bbox` - the
    part of render_frame()'s output that's identical on every frame of
    a *static*-bbox render (map.mp4's default whole-trip overview
    mode, as opposed to --map-zoom's follow-camera mode, where bbox/
    roads are freshly recomputed every frame and there's no single
    base image to reuse).

    render_map_video() calls this once for a static-bbox render and
    passes the result back into render_frame() as `base_image`, so
    each frame draws only its own route/position/text on a copy of
    this instead of every frame re-projecting and re-drawing the same
    `roads` from scratch. Confirmed via profiling (a synthetic
    5,402-fix/3,000-road trip) to be the dominant cost of a static
    -mode map.mp4 render - well past interpolation, which
    render_map_video()'s own O(fixes x frames) fix already addressed -
    ~27 million road-point projections for a mere 600-frame slice, all
    recomputing an answer that never changes since `bbox` and `roads`
    are the same object on every call.
    """

    image = Image.new("RGB", (width, height), BACKGROUND_COLOR)
    draw = ImageDraw.Draw(image)

    def proj(lat: float, lon: float) -> tuple[float, float]:
        return _project(lat, lon, bbox, width, height, margin)

    for road in roads:
        pixels = [proj(lat, lon) for lat, lon in road.points]
        if len(pixels) >= 2:
            draw.line(pixels, fill=ROAD_COLOR, width=2)

    return image


def render_frame(
    bbox: BoundingBox,
    roads: tuple[Road, ...],
    route_points: tuple[tuple[float, float], ...],
    position: tuple[float, float] | None,
    *,
    speed_kmh: float | None = None,
    heading: float | None = None,
    marker_image: Image.Image | None = None,
    timestamp_text: str | None = None,
    width: int = DEFAULT_WIDTH,
    height: int = DEFAULT_HEIGHT,
    margin: int = DEFAULT_MARGIN_PX,
    base_image: Image.Image | None = None,
) -> Image.Image:
    """Render one map-overlay frame: background roads, the route
    driven so far, a position marker, and an optional speed/timestamp
    text overlay in the corner.

    The position marker is an arrow rotated to `heading` (compass
    degrees, clockwise from north) when `heading` is given, `marker_image`
    (a custom RGBA image, also rotated to `heading`) when that's given
    instead, or a plain dot when neither is available (e.g. a
    single-fix/stationary trip with no course data to point an arrow
    in).

    `base_image`, if given, is used as the starting canvas (copied, not
    mutated) instead of a fresh background with `roads` drawn onto it
    - see render_base_map(). `roads` is then only used by callers that
    still need it for something else; this function itself won't
    re-draw it. Passing `base_image` only makes sense when `bbox`
    matches whatever bbox `base_image` was rendered with - it's the
    caller's responsibility to keep those in sync (render_map_video()
    only does this in its static, non-`--map-zoom` mode, where `bbox`
    is the same object on every call).
    """

    if base_image is not None:
        image = base_image.copy()
    else:
        image = Image.new("RGB", (width, height), BACKGROUND_COLOR)
    draw = ImageDraw.Draw(image)

    def proj(lat: float, lon: float) -> tuple[float, float]:
        return _project(lat, lon, bbox, width, height, margin)

    if base_image is None:
        for road in roads:
            pixels = [proj(lat, lon) for lat, lon in road.points]
            if len(pixels) >= 2:
                draw.line(pixels, fill=ROAD_COLOR, width=2)

    if len(route_points) >= 2:
        pixels = [proj(lat, lon) for lat, lon in route_points]
        draw.line(pixels, fill=ROUTE_COLOR, width=4, joint="curve")

    if position is not None:
        point = proj(*position)

        if marker_image is not None:
            _paste_marker_image(image, marker_image, point, heading)
        elif heading is not None:
            draw.polygon(
                _arrow_points(point, heading),
                fill=MARKER_FILL_COLOR,
                outline=MARKER_OUTLINE_COLOR,
                width=2,
            )
        else:
            x, y = point
            radius = 7
            draw.ellipse(
                (x - radius, y - radius, x + radius, y + radius),
                fill=POSITION_DOT_COLOR,
                outline=POSITION_DOT_OUTLINE,
                width=2,
            )

    lines = [line for line in (timestamp_text, _speed_text(speed_kmh)) if line]
    if lines:
        text = "\n".join(lines)
        font = _load_font()
        draw.multiline_text(
            (margin, height - margin - 24 * len(lines)),
            text,
            fill=TEXT_COLOR,
            font=font,
            spacing=6,
        )

    return image


def _speed_text(speed_kmh: float | None) -> str | None:
    if speed_kmh is None:
        return None
    return f"{speed_kmh:.0f} km/h"
