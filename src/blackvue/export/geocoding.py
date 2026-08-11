"""
Reverse geocoding (lat/lon -> place name/address) for bv-export's
trip_info.txt, via OpenStreetMap's Nominatim service.

Only ever called for two points per trip (the first and last valid GPS
fix - see trip_export.py) - light, occasional lookups, exactly the use
Nominatim's public usage policy is meant for
(https://operations.osmfoundation.org/policies/nominatim/): max 1
request/second, a real contactable User-Agent (shares osm_roads.py's
own USER_AGENT - same project, same contact), no bulk/systematic
querying. Results are cached to disk the same one-fetch-then-fully
-offline way osm_roads.py already caches road/area data, so a repeat
export of the same trip (or a different trip through the same spot)
never re-queries.

Copyright (C) 2026 Christer R. (sssreh)

SPDX-License-Identifier: GPL-3.0-or-later
"""

from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass
from pathlib import Path
from urllib.error import URLError
from urllib.parse import urlencode
from urllib.request import Request
from urllib.request import urlopen

from ..generate.media import MediaToolError
from .osm_roads import USER_AGENT

NOMINATIM_URL = "https://nominatim.openstreetmap.org/reverse"
NOMINATIM_SEARCH_URL = "https://nominatim.openstreetmap.org/search"

# Shorter than osm_roads.py's own 60s Overpass timeout (road/area data
# is essential to a requested map render; an address is a purely
# cosmetic trip_info.txt line - see reverse_geocode()'s own docstring)
# so a slow/unreachable network doesn't stall every export by up to a
# full minute just for two optional lookups.
DEFAULT_TIMEOUT_SECONDS = 10.0

# Nominatim's public usage policy caps requests at 1/second. This
# module only geocodes two points per trip, but a batch bv-export run
# across many trips (each fetching its own two points) could still
# fire requests faster than that without an explicit throttle - a
# single process-wide "don't call again too soon" gate, enforced here
# rather than left to callers to remember.
_MIN_REQUEST_INTERVAL_SECONDS = 1.0
_last_request_time: float | None = None


def _throttle() -> None:
    global _last_request_time

    now = time.monotonic()
    if _last_request_time is not None:
        elapsed = now - _last_request_time
        if elapsed < _MIN_REQUEST_INTERVAL_SECONDS:
            time.sleep(_MIN_REQUEST_INTERVAL_SECONDS - elapsed)

    _last_request_time = time.monotonic()


def _cache_key(lat: float, lon: float) -> str:
    """Deterministic cache filename for a coordinate, rounded to 4
    decimal places (~11m - the same rounding osm_roads.py's own
    _cache_key() uses for bounding boxes) so near-identical positions
    share a cache hit instead of each minting their own file.

    `geocode_`-prefixed, matching osm_roads.py's own `areas_` prefix
    convention - this module shares the same on-disk cache directory
    as road/area data (trip_export.py passes the same `.osm_cache`
    folder to both), so the prefix keeps a geocoding cache file
    visually distinct from a road/area one even though the different
    field counts (2 coordinates vs. 4 bbox edges) already make an
    actual filename collision impossible.
    """

    return f"geocode_{lat:.4f}_{lon:.4f}.json"


def reverse_geocode(
    lat: float, lon: float, *, timeout: float = DEFAULT_TIMEOUT_SECONDS
) -> str | None:
    """Look up a human-readable place name/address for (lat, lon) via
    Nominatim's reverse-geocoding endpoint.

    Returns None if Nominatim has no address for this exact coordinate
    (open water, well outside any mapped area) - a genuine, cacheable
    "no result", not a failure. Raises MediaToolError if the request
    itself fails (network error, malformed response) - the same "let
    the caller decide whether to degrade" convention
    osm_roads.fetch_roads()/fetch_areas() already use, rather than
    silently swallowing a real problem here.
    """

    _throttle()

    query = urlencode(
        {
            "format": "jsonv2",
            "lat": repr(lat),
            "lon": repr(lon),
            "zoom": "18",
            "addressdetails": "0",
        }
    )
    request = Request(
        f"{NOMINATIM_URL}?{query}",
        headers={"User-Agent": USER_AGENT},
    )

    try:
        with urlopen(request, timeout=timeout) as response:
            raw = response.read()
    except URLError as exc:
        raise MediaToolError(
            f"could not reach Nominatim for reverse geocoding: {exc}"
        ) from exc

    try:
        payload = json.loads(raw)
    except ValueError as exc:
        raise MediaToolError(
            f"could not parse Nominatim's reverse geocoding response: {exc}"
        ) from exc

    return payload.get("display_name")


def load_or_reverse_geocode(
    lat: float,
    lon: float,
    cache_dir: Path,
    *,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
) -> str | None:
    """Reuse a cached Nominatim lookup for this coordinate if one
    exists on disk, otherwise geocode fresh and persist the result -
    same one-fetch-then-fully-offline pattern
    osm_roads.load_or_fetch_roads() uses.

    Only a successful lookup (whether it found an address or
    genuinely found none) is cached - if reverse_geocode() raises,
    that propagates straight to the caller and nothing is written
    here, so a transient failure (network blip) gets retried on the
    next export instead of being permanently remembered as "no
    result".
    """

    cache_dir.mkdir(parents=True, exist_ok=True)
    cache_path = cache_dir / _cache_key(lat, lon)

    if cache_path.exists():
        payload = json.loads(cache_path.read_text(encoding="utf-8"))
        return payload.get("display_name")

    display_name = reverse_geocode(lat, lon, timeout=timeout)
    cache_path.write_text(
        json.dumps({"display_name": display_name}), encoding="utf-8"
    )
    return display_name


def _forward_cache_key(name: str) -> str:
    """Deterministic cache filename for a place-name query.

    Unlike _cache_key() above (a coordinate, already a bounded,
    filesystem-safe pair of numbers), a free-text query can contain
    arbitrary characters and length - normalized (casefolded,
    whitespace-collapsed, so trivially different spellings of the
    same query share a cache hit) and then hashed, rather than used
    directly as a filename.
    """

    normalized = " ".join(name.casefold().split())
    digest = hashlib.sha1(normalized.encode("utf-8")).hexdigest()
    return f"geocode_place_{digest}.json"


@dataclass(frozen=True)
class GeocodeResult:
    """A forward-geocoded place for bv-search's --place option.

    `point` is always populated - Nominatim's own best-match lat/lon,
    usable on its own exactly like a plain --near coordinate. `lines`
    is additionally populated when the match is a road or an area
    boundary rather than a point-like address/POI: one or more
    polylines (each a tuple of (lat, lon) vertices) tracing the
    match's actual OSM geometry.

    This exists because a single point is a poor stand-in for a long
    road - Nominatim's own lat/lon for a road match is just one point
    somewhere along it (often near its middle, or one endpoint of
    whichever OSM way segment matched best), so a normal search
    --radius around that one point would miss GPS fixes near the rest
    of the road entirely. search_near() in search.py uses `lines`
    (distance to the nearest point *on* the road) instead of `point`
    whenever it's non-empty, so --place "Highway 1" behaves correctly
    along the road's whole length, not just near wherever Nominatim
    happened to drop its pin.
    """

    point: tuple[float, float]
    lines: tuple[tuple[tuple[float, float], ...], ...] = ()


def _geojson_ring_to_line(
    ring: list[list[float]],
) -> tuple[tuple[float, float], ...]:
    """A GeoJSON ring/line is a list of [lon, lat] pairs (GeoJSON's
    own coordinate order) - flipped here to the (lat, lon) order used
    everywhere else in this codebase (GpsFix, --near, ...)."""

    return tuple((lat, lon) for lon, lat in ring)


def _geojson_to_lines(
    geojson: dict | None,
) -> tuple[tuple[tuple[float, float], ...], ...]:
    """Normalize a Nominatim `geojson` field (requested via
    `polygon_geojson=1`) into 0+ polylines for GeocodeResult.lines.

    A plain "Point" match (the common case - most addresses/POIs)
    yields no lines at all; forward_geocode()'s own `point` field is
    already the right representation for those, so returning `()`
    here just means "use `point` alone" downstream. "LineString"/
    "MultiLineString" (roads) map directly. "Polygon"/"MultiPolygon"
    (areas - parks, water, administrative boundaries) use only each
    ring's *exterior* boundary, not its holes - bv-search only needs
    a proximity-to-boundary check here, not full point-in-polygon
    containment, so a hole (e.g. a lake with an island) doesn't need
    separate handling. Anything else (missing/unrecognized type) also
    falls back to `()` - the same "just use `point`" fallback, rather
    than raising, since not having line geometry for a query is a
    normal outcome, not a failure.
    """

    if not geojson:
        return ()

    geom_type = geojson.get("type")
    coordinates = geojson.get("coordinates")

    if geom_type == "LineString" and coordinates:
        return (_geojson_ring_to_line(coordinates),)

    if geom_type == "MultiLineString" and coordinates:
        return tuple(_geojson_ring_to_line(line) for line in coordinates)

    if geom_type == "Polygon" and coordinates:
        return (_geojson_ring_to_line(coordinates[0]),)

    if geom_type == "MultiPolygon" and coordinates:
        return tuple(
            _geojson_ring_to_line(polygon[0]) for polygon in coordinates if polygon
        )

    return ()


def forward_geocode(
    name: str, *, timeout: float = DEFAULT_TIMEOUT_SECONDS
) -> GeocodeResult | None:
    """Look up a place name via Nominatim's forward-geocoding (search)
    endpoint - the inverse of reverse_geocode() above, for bv-search's
    --place option.

    Requests `polygon_geojson=1` alongside the usual lat/lon fields,
    so a road or area match comes back with its actual line geometry
    (see GeocodeResult's own docstring for why that matters) as well
    as the plain point every match already has.

    Returns None if Nominatim has no match for this query (a genuine,
    cacheable "no result"). Raises MediaToolError if the request
    itself fails (network error, malformed response), same convention
    as reverse_geocode(). Only the single best-ranked match is used -
    bv-search wants one search target, not a disambiguation list.
    """

    _throttle()

    query = urlencode(
        {"format": "jsonv2", "q": name, "limit": "1", "polygon_geojson": "1"}
    )
    request = Request(
        f"{NOMINATIM_SEARCH_URL}?{query}",
        headers={"User-Agent": USER_AGENT},
    )

    try:
        with urlopen(request, timeout=timeout) as response:
            raw = response.read()
    except URLError as exc:
        raise MediaToolError(
            f"could not reach Nominatim for place-name lookup: {exc}"
        ) from exc

    try:
        payload = json.loads(raw)
    except ValueError as exc:
        raise MediaToolError(
            f"could not parse Nominatim's place-name lookup response: {exc}"
        ) from exc

    if not payload:
        return None

    try:
        point = (float(payload[0]["lat"]), float(payload[0]["lon"]))
    except (KeyError, ValueError, TypeError) as exc:
        raise MediaToolError(
            f"Nominatim's place-name lookup response was malformed: {exc}"
        ) from exc

    lines = _geojson_to_lines(payload[0].get("geojson"))
    return GeocodeResult(point=point, lines=lines)


def _geocode_result_to_json(result: GeocodeResult | None) -> dict:
    if result is None:
        return {"point": None, "lines": []}
    return {
        "point": list(result.point),
        "lines": [[list(vertex) for vertex in line] for line in result.lines],
    }


def _geocode_result_from_json(payload: dict) -> GeocodeResult | None:
    point = payload.get("point")
    if point is None:
        return None
    lines = tuple(
        tuple((vertex[0], vertex[1]) for vertex in line)
        for line in payload.get("lines", [])
    )
    return GeocodeResult(point=(point[0], point[1]), lines=lines)


def load_or_forward_geocode(
    name: str,
    cache_dir: Path,
    *,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
) -> GeocodeResult | None:
    """Reuse a cached forward-geocode lookup for this place name if
    one exists on disk, otherwise geocode fresh and persist the
    result - same load-or-fetch-and-cache pattern as
    load_or_reverse_geocode() above.
    """

    cache_dir.mkdir(parents=True, exist_ok=True)
    cache_path = cache_dir / _forward_cache_key(name)

    if cache_path.exists():
        payload = json.loads(cache_path.read_text(encoding="utf-8"))
        return _geocode_result_from_json(payload)

    result = forward_geocode(name, timeout=timeout)
    cache_path.write_text(
        json.dumps(_geocode_result_to_json(result)), encoding="utf-8"
    )
    return result
