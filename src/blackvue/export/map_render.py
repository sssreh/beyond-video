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
ROAD_COLOR = (200, 196, 188)
ROUTE_COLOR = (230, 57, 70)
POSITION_DOT_COLOR = (230, 57, 70)
POSITION_DOT_OUTLINE = (255, 255, 255)
TEXT_COLOR = (40, 40, 40)

DEFAULT_WIDTH = 640
DEFAULT_HEIGHT = 640
DEFAULT_MARGIN_PX = 24

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


def render_frame(
    bbox: BoundingBox,
    roads: tuple[Road, ...],
    route_points: tuple[tuple[float, float], ...],
    position: tuple[float, float] | None,
    *,
    speed_kmh: float | None = None,
    timestamp_text: str | None = None,
    width: int = DEFAULT_WIDTH,
    height: int = DEFAULT_HEIGHT,
    margin: int = DEFAULT_MARGIN_PX,
) -> Image.Image:
    """Render one map-overlay frame: background roads, the route
    driven so far, a position dot, and an optional speed/timestamp
    text overlay in the corner."""

    image = Image.new("RGB", (width, height), BACKGROUND_COLOR)
    draw = ImageDraw.Draw(image)

    def proj(lat: float, lon: float) -> tuple[float, float]:
        return _project(lat, lon, bbox, width, height, margin)

    for road in roads:
        pixels = [proj(lat, lon) for lat, lon in road.points]
        if len(pixels) >= 2:
            draw.line(pixels, fill=ROAD_COLOR, width=2)

    if len(route_points) >= 2:
        pixels = [proj(lat, lon) for lat, lon in route_points]
        draw.line(pixels, fill=ROUTE_COLOR, width=4, joint="curve")

    if position is not None:
        x, y = proj(*position)
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
