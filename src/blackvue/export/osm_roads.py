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
    aspect_ratio: float | None = None,
) -> BoundingBox | None:
    """Compute a padded bounding box covering every valid, positioned
    fix. Returns None if there's nothing to bound (no fixes, or none
    of them have a valid fix with a position).

    `aspect_ratio` (width/height), if given, grows the box's *shorter*
    real-world dimension outward from its own center so the box's real
    -world shape matches that ratio - e.g. a north-south trip (tall)
    gets wider, an east-west trip (wide) gets taller. Without this, a
    caller rendering the box onto a non-square canvas (map_render.py's
    render_frame(), which scales longitude and latitude span to the
    canvas width/height independently) would get an unevenly stretched
    map, since a route's own real-world bounding box is essentially
    never already shaped like the requested panel. Longitude degrees
    are narrower than latitude degrees away from the equator, so the
    comparison is done in real-world units via the same cos(latitude)
    correction render_frame()/bounding_box_around_point() already use,
    not raw degrees. `aspect_ratio` must be positive if given; omitted
    (None) keeps the previous margin-only behavior unchanged.
    """

    positions = [
        (fix.latitude, fix.longitude)
        for fix in fixes
        if fix.valid and fix.latitude is not None and fix.longitude is not None
    ]

    if not positions:
        return None

    lats = [lat for lat, _ in positions]
    lons = [lon for _, lon in positions]

    min_lat = min(lats) - margin_degrees
    max_lat = max(lats) + margin_degrees
    min_lon = min(lons) - margin_degrees
    max_lon = max(lons) + margin_degrees

    if aspect_ratio is not None:
        min_lat, max_lat, min_lon, max_lon = _grow_to_aspect_ratio(
            min_lat, max_lat, min_lon, max_lon, aspect_ratio
        )

    return BoundingBox(
        min_lat=min_lat, min_lon=min_lon, max_lat=max_lat, max_lon=max_lon
    )


def _grow_to_aspect_ratio(
    min_lat: float,
    max_lat: float,
    min_lon: float,
    max_lon: float,
    aspect_ratio: float,
) -> tuple[float, float, float, float]:
    """Symmetrically expand whichever of (min_lat, max_lat, min_lon,
    max_lon) is the shorter real-world dimension, so the box's
    real-world width/height ratio becomes `aspect_ratio`. The longer
    dimension, and the box's center, are left unchanged - this only
    ever adds margin, never crops anything already inside the box.
    """

    mean_lat_rad = math.radians((min_lat + max_lat) / 2)
    lon_scale = math.cos(mean_lat_rad) or 1e-9

    # Both spans expressed in the same "lat-degree-equivalent" real
    # -world units, so they're directly comparable.
    height_units = max_lat - min_lat
    width_units = (max_lon - min_lon) * lon_scale

    desired_width_units = height_units * aspect_ratio

    if width_units < desired_width_units:
        delta_lon = (desired_width_units - width_units) / lon_scale / 2
        min_lon -= delta_lon
        max_lon += delta_lon
    else:
        desired_height_units = width_units / aspect_ratio
        delta_lat = (desired_height_units - height_units) / 2
        min_lat -= delta_lat
        max_lat += delta_lat

    return min_lat, max_lat, min_lon, max_lon


def bounding_box_around_point(
    lat: float, lon: float, radius_meters: float, *, aspect_ratio: float | None = None
) -> BoundingBox:
    """Compute a bounding box of real-world vertical half-height
    `radius_meters`, centered on (lat, lon).

    Used for map.mp4's optional "follow camera" mode (bv-export
    --map-zoom): a fresh bounding box like this, centered on the
    current interpolated position, computed fresh for every frame,
    makes the rendered map scroll/pan as the vehicle moves - unlike
    the single whole-trip bounding box (bounding_box_for_fixes())
    used for the default static-overview map.

    By default (`aspect_ratio=None`) the box is square-ish: the same
    real-world half-width as half-height. If `aspect_ratio` (width/
    height) is given instead, the horizontal half-width becomes
    `radius_meters * aspect_ratio` - unlike bounding_box_for_fixes(),
    which has to *grow* a fixed real-world extent to avoid cropping
    anything, a follow-camera view has no pre-existing "real" extent to
    preserve (it's freely chosen every frame), so it can just be built
    already shaped to the target panel directly.

    Longitude degrees cover less real-world distance than latitude
    degrees away from the equator (they converge at the poles), so
    the longitude delta is widened by 1/cos(latitude) to keep the
    intended real-world width - the same correction map_render.py's
    own projection applies. `radius_meters` is floored at
    MIN_ZOOM_RADIUS_METERS to avoid a degenerate near-zero-area box.
    """

    radius_meters = max(radius_meters, MIN_ZOOM_RADIUS_METERS)

    delta_lat = radius_meters / _METERS_PER_DEGREE_LATITUDE

    width_radius_meters = (
        radius_meters * aspect_ratio if aspect_ratio is not None else radius_meters
    )
    lon_scale = math.cos(math.radians(lat)) or 1e-9
    delta_lon = width_radius_meters / (_METERS_PER_DEGREE_LATITUDE * lon_scale)

    return BoundingBox(
        min_lat=lat - delta_lat,
        min_lon=lon - delta_lon,
        max_lat=lat + delta_lat,
        max_lon=lon + delta_lon,
    )


def index_roads(roads: tuple[Road, ...]) -> tuple[tuple[Road, BoundingBox], ...]:
    """Precompute each road's own (min/max lat/lon) bounding box once.

    Used by roads_within_bbox() to filter down to only the roads
    visible in a given frame - map.mp4's "follow camera" mode
    (--map-zoom) calls that once per frame, so doing this rescan of
    every road's points up front (rather than inside the per-frame
    filter) is what keeps that filtering itself cheap.
    """

    indexed = []
    for road in roads:
        lats = [lat for lat, _lon in road.points]
        lons = [lon for _lat, lon in road.points]
        indexed.append((
            road,
            BoundingBox(min(lats), min(lons), max(lats), max(lons)),
        ))
    return tuple(indexed)


def roads_within_bbox(
    indexed_roads: tuple[tuple[Road, BoundingBox], ...], bbox: BoundingBox
) -> tuple[Road, ...]:
    """Return the subset of `indexed_roads` (see index_roads()) whose
    own bounding box overlaps `bbox` at all.

    A cheap rectangle-overlap test per road, not a real geometric
    intersection - a road that merely passes near a corner of `bbox`
    without actually crossing it can pass this check too, but that
    just means a road frame_bbox happens to be drawn one frame that
    turns out to be just off-screen once its points are projected,
    which is harmless (map_render.py still just draws its (possibly
    off-canvas) line) and far cheaper than checking properly.
    """

    return tuple(
        road
        for road, road_bbox in indexed_roads
        if road_bbox.min_lat <= bbox.max_lat
        and road_bbox.max_lat >= bbox.min_lat
        and road_bbox.min_lon <= bbox.max_lon
        and road_bbox.max_lon >= bbox.min_lon
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
