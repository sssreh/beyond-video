"""
OpenStreetMap road-geometry fetching for map-overlay video rendering.

Deliberately does NOT use tile.openstreetmap.org, or any commercial
tile-display API: rendering a trip's route needs a whole bounding
box's worth of map data fetched once, ahead of time, rather than
however a human happens to pan/zoom - exactly the "pre-seeding"/
offline pattern tile.openstreetmap.org's usage policy prohibits
(see WORKING_CONTEXT.md for the research behind this). Commercial
tile APIs generally have the same problem the other way round: their
terms license *live map display*, not baking tiles into a video file
the user keeps forever.

Instead, this queries the Overpass API (OSM's own read-only data API,
explicitly recommended by the OSM Foundation for small-area,
non-editing queries like this one - see
https://operations.osmfoundation.org/policies/api/) for raw road
*geometry*. That's ODbL-licensed OSM *data*, not a rendered image -
explicitly fine to cache, store, and redistribute offline with
attribution, unlike a pre-rendered tile. map_render.py then draws
this geometry itself, so no live map service is involved once a
region's data has been fetched once and cached.

Copyright (C) 2026 Christer R. (sssreh)

SPDX-License-Identifier: GPL-3.0-or-later
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from pathlib import Path
from urllib.error import URLError
from urllib.request import Request
from urllib.request import urlopen

from ..generate.media import MediaToolError
from ..telemetry.gps_reader import GpsFix

OVERPASS_URL = "https://overpass-api.de/api/interpreter"

# A clear, contactable User-Agent, per OSM's API usage policy
# (https://operations.osmfoundation.org/policies/api/) - generic
# library defaults ("python-urllib/x.y") get requests blocked.
USER_AGENT = "beyond-video/0.1 (+https://github.com/sssreh/beyond-video)"

# ~0.01 degrees is roughly 1km at typical driving latitudes - a
# margin so the rendered map has some road context beyond the exact
# route, without ballooning the query area.
DEFAULT_MARGIN_DEGREES = 0.01
DEFAULT_TIMEOUT_SECONDS = 60.0

# Mean Earth radius in meters, used to convert a real-world distance
# into degrees of latitude/longitude for bounding_box_around_point().
_EARTH_RADIUS_METERS = 6_371_000.0
_METERS_PER_DEGREE_LATITUDE = _EARTH_RADIUS_METERS * math.pi / 180

# A street-level "follow camera" default for bv-export --map-zoom:
# half-width of the view, so the full frame covers roughly this
# distance x2 - close enough to read individual streets/turns, not so
# close the route runs off-frame between GPS fixes.
DEFAULT_ZOOM_RADIUS_METERS = 120.0

# Floors bounding_box_around_point()'s radius so a caller-supplied
# --map-zoom of 0 (or a negative/tiny value) can't produce a
# degenerate, near-zero-area view.
MIN_ZOOM_RADIUS_METERS = 5.0


@dataclass(frozen=True)
class BoundingBox:
    """A lat/lon bounding box, south/west/north/east."""

    min_lat: float
    min_lon: float
    max_lat: float
    max_lon: float


@dataclass(frozen=True)
class Road:
    """One OSM way tagged `highway=*`, as a sequence of (lat, lon)
    points in the order Overpass returned them."""

    points: tuple[tuple[float, float], ...]


def bounding_box_for_fixes(
    fixes: tuple[GpsFix, ...],
    *,
    margin_degrees: float = DEFAULT_MARGIN_DEGREES,
) -> BoundingBox | None:
    """Compute a padded bounding box covering every valid, positioned
    fix. Returns None if there's nothing to bound (no fixes, or none
    of them have a valid fix with a position)."""

    positions = [
        (fix.latitude, fix.longitude)
        for fix in fixes
        if fix.valid and fix.latitude is not None and fix.longitude is not None
    ]

    if not positions:
        return None

    lats = [lat for lat, _ in positions]
    lons = [lon for _, lon in positions]

    return BoundingBox(
        min_lat=min(lats) - margin_degrees,
        min_lon=min(lons) - margin_degrees,
        max_lat=max(lats) + margin_degrees,
        max_lon=max(lons) + margin_degrees,
    )


def bounding_box_around_point(
    lat: float, lon: float, radius_meters: float
) -> BoundingBox:
    """Compute a square-ish bounding box of real-world half-width
    `radius_meters`, centered on (lat, lon).

    Used for map.mp4's optional "follow camera" mode (bv-export
    --map-zoom): a fresh bounding box like this, centered on the
    current interpolated position, computed fresh for every frame,
    makes the rendered map scroll/pan as the vehicle moves - unlike
    the single whole-trip bounding box (bounding_box_for_fixes())
    used for the default static-overview map.

    Longitude degrees cover less real-world distance than latitude
    degrees away from the equator (they converge at the poles), so
    the longitude delta is widened by 1/cos(latitude) to keep the box
    the same real-world width/height in both directions - the same
    correction map_render.py's own projection applies. `radius_meters`
    is floored at MIN_ZOOM_RADIUS_METERS to avoid a degenerate
    near-zero-area box.
    """

    radius_meters = max(radius_meters, MIN_ZOOM_RADIUS_METERS)

    delta_lat = radius_meters / _METERS_PER_DEGREE_LATITUDE

    lon_scale = math.cos(math.radians(lat)) or 1e-9
    delta_lon = radius_meters / (_METERS_PER_DEGREE_LATITUDE * lon_scale)

    return BoundingBox(
        min_lat=lat - delta_lat,
        min_lon=lon - delta_lon,
        max_lat=lat + delta_lat,
        max_lon=lon + delta_lon,
    )


def _overpass_query(bbox: BoundingBox) -> str:
    return (
        "[out:json][timeout:60];"
        "way[highway]"
        f"({bbox.min_lat},{bbox.min_lon},{bbox.max_lat},{bbox.max_lon});"
        "out geom;"
    )


def _parse_overpass_payload(payload: dict) -> tuple[Road, ...]:
    roads = []

    for element in payload.get("elements", ()):
        if element.get("type") != "way":
            continue

        geometry = element.get("geometry")
        if not geometry:
            continue

        points = tuple(
            (point["lat"], point["lon"])
            for point in geometry
            if "lat" in point and "lon" in point
        )
        if points:
            roads.append(Road(points=points))

    return tuple(roads)


def _fetch_overpass_payload(bbox: BoundingBox, timeout: float) -> dict:
    query = _overpass_query(bbox)
    request = Request(
        OVERPASS_URL,
        data=f"data={query}".encode("utf-8"),
        headers={"User-Agent": USER_AGENT},
        method="POST",
    )

    try:
        with urlopen(request, timeout=timeout) as response:
            raw = response.read()
    except URLError as exc:
        raise MediaToolError(
            f"could not reach the Overpass API for map data: {exc}"
        ) from exc

    try:
        return json.loads(raw)
    except ValueError as exc:
        raise MediaToolError(
            f"could not parse the Overpass API's response: {exc}"
        ) from exc


def fetch_roads(
    bbox: BoundingBox, *, timeout: float = DEFAULT_TIMEOUT_SECONDS
) -> tuple[Road, ...]:
    """Query the Overpass API for road geometry within bbox.

    Raises MediaToolError on any network/parse failure, following the
    same convention as the rest of beyond-video's external-tool calls.
    """

    return _parse_overpass_payload(_fetch_overpass_payload(bbox, timeout))


def _cache_key(bbox: BoundingBox) -> str:
    """Deterministic cache filename for a bounding box, rounded to 4
    decimal places (~11m) so near-identical trips through the same
    area share a cache hit instead of each minting their own file."""

    return (
        f"{bbox.min_lat:.4f}_{bbox.min_lon:.4f}_"
        f"{bbox.max_lat:.4f}_{bbox.max_lon:.4f}.json"
    )


def load_or_fetch_roads(
    bbox: BoundingBox,
    cache_dir: Path,
    *,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
) -> tuple[Road, ...]:
    """Reuse a cached Overpass response for this bbox if one exists on
    disk, otherwise fetch fresh from Overpass and persist the raw
    response - so repeated exports of the same trip, or different
    trips through the same region, only ever hit Overpass once. This
    is the same one-fetch-then-fully-offline pattern `bv-lang install`
    already uses for translation packages.
    """

    cache_dir.mkdir(parents=True, exist_ok=True)
    cache_path = cache_dir / _cache_key(bbox)

    if cache_path.exists():
        payload = json.loads(cache_path.read_text(encoding="utf-8"))
    else:
        payload = _fetch_overpass_payload(bbox, timeout)
        cache_path.write_text(json.dumps(payload), encoding="utf-8")

    return _parse_overpass_payload(payload)
