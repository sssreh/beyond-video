"""
Search a BlackVue archive's derived text assets (transcript,
translation, scene description) and GPS tracks - the library module
behind bv-search.

Copyright (C) 2026 Christer R. (sssreh)

SPDX-License-Identifier: GPL-3.0-or-later
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from pathlib import Path

from .archive.asset import Asset
from .archive.recording import Recording
from .generate.media import MediaToolError
from .telemetry.gps_reader import GpsFix
from .telemetry.gps_reader import read_gps

# Which Asset(s) each --asset category searches. Diarized transcript/
# translation and the rear scene description are grouped in with
# their plain/front counterparts, not left out - a default ("all")
# run shouldn't silently miss half of what --diarize or bv-generate/
# bv-scribe's --camera produced just because it has a different Asset
# type under the hood.
TEXT_SEARCH_ASSETS: dict[str, tuple[Asset, ...]] = {
    "transcript": (Asset.TRANSCRIPT, Asset.TRANSCRIPT_DIARIZED),
    "translation": (Asset.TRANSLATION, Asset.TRANSLATION_DIARIZED),
    "scene": (Asset.SCENE_DESCRIPTION, Asset.SCENE_DESCRIPTION_REAR),
}
TEXT_SEARCH_ASSETS["all"] = tuple(
    asset
    for group in ("transcript", "translation", "scene")
    for asset in TEXT_SEARCH_ASSETS[group]
)

# Mean Earth radius in meters - the same well-known value trip_stats.py
# and osm_roads.py each already duplicate (both module-private by
# convention) for their own haversine/bounding-box math. Duplicated
# again here rather than importing either, for the same reason
# trip_stats.py gives for not importing osm_roads.py's copy: a
# genuinely separate concern living in a separate module.
_EARTH_RADIUS_METERS = 6_371_000.0


@dataclass(frozen=True)
class TextMatch:
    """One matching line found in one of a recording's text assets."""

    asset: Asset
    path: Path
    line_number: int
    line: str


@dataclass(frozen=True)
class GeoMatch:
    """The closest GPS fix in a recording's .gps file to a search
    point, already confirmed to be within the requested radius."""

    fix: GpsFix
    distance_meters: float


def _haversine_distance_meters(
    lat1: float, lon1: float, lat2: float, lon2: float
) -> float:
    """Great-circle distance between two lat/lon points, in meters -
    same formula as trip_stats.py's own (module-private) copy."""

    lat1_rad, lon1_rad = math.radians(lat1), math.radians(lon1)
    lat2_rad, lon2_rad = math.radians(lat2), math.radians(lon2)
    dlat = lat2_rad - lat1_rad
    dlon = lon2_rad - lon1_rad

    a = (
        math.sin(dlat / 2) ** 2
        + math.cos(lat1_rad) * math.cos(lat2_rad) * math.sin(dlon / 2) ** 2
    )
    c = 2 * math.asin(min(1.0, math.sqrt(a)))

    return _EARTH_RADIUS_METERS * c


def _project_local_meters(
    lat: float, lon: float, ref_lat: float
) -> tuple[float, float]:
    """Flat-Earth (equirectangular) projection of (lat, lon) into
    local x/y meters around `ref_lat`, for the point-to-segment
    distance math below - accurate enough at the scale bv-search's
    --radius operates at (up to a few km), the same order of
    approximation _haversine_distance_meters() above already makes
    (a perfect sphere, not the real ellipsoid)."""

    x = math.radians(lon) * math.cos(math.radians(ref_lat)) * _EARTH_RADIUS_METERS
    y = math.radians(lat) * _EARTH_RADIUS_METERS
    return x, y


def _point_to_segment_distance_meters(
    lat: float,
    lon: float,
    segment_start: tuple[float, float],
    segment_end: tuple[float, float],
) -> float:
    """Shortest distance from (lat, lon) to the line segment between
    segment_start and segment_end, in meters - standard 2D point-to-
    segment projection, done in a local flat projection centered on
    the query point itself (see _project_local_meters()) rather than
    on the sphere directly, since a closed-form great-circle point-
    to-segment distance is significantly more involved for no real
    accuracy benefit at this scale."""

    ref_lat = lat
    px, py = _project_local_meters(lat, lon, ref_lat)
    ax, ay = _project_local_meters(segment_start[0], segment_start[1], ref_lat)
    bx, by = _project_local_meters(segment_end[0], segment_end[1], ref_lat)

    dx, dy = bx - ax, by - ay
    if dx == 0 and dy == 0:
        return math.hypot(px - ax, py - ay)

    t = ((px - ax) * dx + (py - ay) * dy) / (dx * dx + dy * dy)
    t = max(0.0, min(1.0, t))
    closest_x, closest_y = ax + t * dx, ay + t * dy

    return math.hypot(px - closest_x, py - closest_y)


def _min_distance_to_lines_meters(
    lat: float,
    lon: float,
    lines: tuple[tuple[tuple[float, float], ...], ...],
) -> float | None:
    """Shortest distance from (lat, lon) to any of `lines` (each a
    polyline of (lat, lon) vertices) - the nearest point *on* a road/
    area boundary, not just to one of its vertices. Returns None if
    `lines` is empty. A degenerate single-vertex "line" (Nominatim
    can return one for a very short way) falls back to plain point
    distance."""

    best: float | None = None

    for line in lines:
        if len(line) == 1:
            distance = _haversine_distance_meters(lat, lon, line[0][0], line[0][1])
            if best is None or distance < best:
                best = distance
            continue

        for segment_start, segment_end in zip(line, line[1:]):
            distance = _point_to_segment_distance_meters(
                lat, lon, segment_start, segment_end
            )
            if best is None or distance < best:
                best = distance

    return best


def search_text(
    recording: Recording,
    pattern: str,
    *,
    assets: tuple[Asset, ...] = TEXT_SEARCH_ASSETS["all"],
    case_sensitive: bool = False,
    regex: bool = False,
) -> list[TextMatch]:
    """Search `pattern` across whichever of `assets` `recording`
    actually has, returning every matching line in file order. An
    asset the recording doesn't have is silently skipped - the same
    "not every recording has every asset" shape bv-ls already treats
    as normal, not an error.

    Plain (non-regex) matching is a case-insensitive substring test by
    default, matching grep's own default and avoiding surprise misses
    from a transcript's capitalization not matching the search term's.
    """

    if regex:
        flags = 0 if case_sensitive else re.IGNORECASE
        try:
            compiled = re.compile(pattern, flags)
        except re.error as exc:
            raise MediaToolError(f"invalid --text regex: {exc}") from exc

        def test(line: str) -> bool:
            return compiled.search(line) is not None
    else:
        needle = pattern if case_sensitive else pattern.lower()

        def test(line: str) -> bool:
            haystack = line if case_sensitive else line.lower()
            return needle in haystack

    matches: list[TextMatch] = []

    for asset in assets:
        asset_file = recording.file(asset)
        if asset_file is None:
            continue

        try:
            text = asset_file.path.read_text(encoding="utf-8")
        except OSError as exc:
            raise MediaToolError(
                f"could not read {asset_file.path.name}: {exc}"
            ) from exc

        for line_number, line in enumerate(text.splitlines(), start=1):
            if test(line):
                matches.append(TextMatch(asset, asset_file.path, line_number, line))

    return matches


def search_near(
    recording: Recording,
    lat: float,
    lon: float,
    radius_meters: float,
    *,
    lines: tuple[tuple[tuple[float, float], ...], ...] = (),
) -> GeoMatch | None:
    """Return the closest valid GPS fix in `recording`'s .gps file to
    (lat, lon), if any fall within `radius_meters` - or None if the
    recording has no GPS data at all, or none of its fixes come that
    close.

    `lines` - one or more polylines (each a sequence of (lat, lon)
    vertices) - overrides the plain point/`lat`/`lon` distance check
    with distance-to-nearest-point-on-any-line when given. bv-search's
    --place passes a road/area's own line geometry here whenever
    Nominatim resolves the name to one, since a long road's single
    representative point (what `lat`/`lon` would otherwise be) badly
    undershoots how much of the road actually counts as "near" it -
    see export/geocoding.py's GeocodeResult docstring."""

    gps_file = recording.file(Asset.GPS)
    if gps_file is None:
        return None

    best: GeoMatch | None = None

    for fix in read_gps(gps_file.path):
        if not fix.valid or fix.latitude is None or fix.longitude is None:
            continue

        if lines:
            distance = _min_distance_to_lines_meters(
                fix.latitude, fix.longitude, lines
            )
            if distance is None:
                continue
        else:
            distance = _haversine_distance_meters(
                lat, lon, fix.latitude, fix.longitude
            )
        if distance > radius_meters:
            continue

        if best is None or distance < best.distance_meters:
            best = GeoMatch(fix, distance)

    return best
