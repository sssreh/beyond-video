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


def forward_geocode(
    name: str, *, timeout: float = DEFAULT_TIMEOUT_SECONDS
) -> tuple[float, float] | None:
    """Look up a (lat, lon) coordinate for a free-text place name via
    Nominatim's forward-geocoding (search) endpoint - the inverse of
    reverse_geocode() above, for bv-search's --place option.

    Returns None if Nominatim has no match for this query (a genuine,
    cacheable "no result"). Raises MediaToolError if the request
    itself fails (network error, malformed response), same convention
    as reverse_geocode(). Only the single best-ranked match is used -
    bv-search wants one search point, not a disambiguation list.
    """

    _throttle()

    query = urlencode({"format": "jsonv2", "q": name, "limit": "1"})
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
        return float(payload[0]["lat"]), float(payload[0]["lon"])
    except (KeyError, ValueError, TypeError) as exc:
        raise MediaToolError(
            f"Nominatim's place-name lookup response was malformed: {exc}"
        ) from exc


def load_or_forward_geocode(
    name: str,
    cache_dir: Path,
    *,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
) -> tuple[float, float] | None:
    """Reuse a cached forward-geocode lookup for this place name if
    one exists on disk, otherwise geocode fresh and persist the
    result - same load-or-fetch-and-cache pattern as
    load_or_reverse_geocode() above.
    """

    cache_dir.mkdir(parents=True, exist_ok=True)
    cache_path = cache_dir / _forward_cache_key(name)

    if cache_path.exists():
        payload = json.loads(cache_path.read_text(encoding="utf-8"))
        coordinates = payload.get("coordinates")
        return tuple(coordinates) if coordinates is not None else None

    coordinates = forward_geocode(name, timeout=timeout)
    cache_path.write_text(
        json.dumps({"coordinates": coordinates}), encoding="utf-8"
    )
    return coordinates
