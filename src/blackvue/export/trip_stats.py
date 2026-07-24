"""
Trip-level distance/speed statistics for bv-export's trip_info.txt.

Copyright (C) 2026 Christer R. (sssreh)

SPDX-License-Identifier: GPL-3.0-or-later
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from ..telemetry.gps_reader import GpsFix
from ..telemetry.movement import DEFAULT_SPEED_THRESHOLD_KMH

# Mean Earth radius in meters - the same well-known value
# osm_roads.py's own (module-private) constant uses for its bounding
# -box math. Duplicated here rather than imported, since that one is
# module-private by convention (leading underscore) and this module's
# use of it (a haversine distance, not a bounding box) is a genuinely
# separate concern.
_EARTH_RADIUS_METERS = 6_371_000.0


@dataclass(frozen=True)
class TripStats:
    """Summary statistics for a trip's merged GPS fixes."""

    distance_km: float
    average_speed_kmh: float | None
    max_speed_kmh: float | None
    # Optional (default None, not 0.0) so any existing caller
    # constructing a TripStats without these still works unchanged -
    # see compute_trip_stats()'s own docstring for what "no speed data
    # at all" (None) means vs. a genuine zero.
    moving_seconds: float | None = None
    idle_seconds: float | None = None


def _haversine_distance_meters(
    lat1: float, lon1: float, lat2: float, lon2: float
) -> float:
    """Great-circle distance between two lat/lon points, in meters."""

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


def compute_trip_stats(fixes: tuple[GpsFix, ...]) -> TripStats | None:
    """Compute distance/average/max speed from a trip's merged GPS
    fixes.

    Distance is the sum of the great-circle distance between each pair
    of consecutive valid, positioned fixes - a straight-line
    approximation between fixes, not the road-following distance a
    routing engine would give, but fixes are frequent enough (roughly
    1Hz - see telemetry/gps_reader.py) that the difference is
    negligible for any normal driving speed.

    `average_speed_kmh` is the mean of each fix's own instantaneous
    `speed_kmh` reading (not distance/duration) - deliberately, so a
    long stationary stretch (traffic, a red light, parked with the
    engine running) correctly pulls the average down via its own
    near-zero readings, the same way a GPS trip computer usually
    reports "average speed". `max_speed_kmh` is the highest single
    reading. Both are None if no fix in the trip has a `speed_kmh`
    reading at all (some GPS sentence types don't carry one).

    `moving_seconds`/`idle_seconds` split the time between consecutive
    positioned fixes into "the vehicle was moving" vs. "it wasn't",
    using the same DEFAULT_SPEED_THRESHOLD_KMH (5.0) cutoff
    telemetry/movement.py already uses to decide whether GPS evidence
    shows movement at a trip-gap edge - reused here rather than
    picking a new, unrelated number. Each gap between two consecutive
    fixes is classified by the mean of each fix's own speed reading if
    it has one, or otherwise its *carried-forward* speed - the most
    recent earlier fix in the trip that did have a reading (see the
    forward-fill loop below). Confirmed against a real archive
    (Christer, 2026-07-24): a fix having a valid position but no speed
    reading of its own turns out to be common enough in practice - a
    long, otherwise perfectly GPS-tracked ~28-minute city drive showed
    barely 40% of its span reflected in moving_seconds+idle_seconds
    before this fix, because a gap between two speed-less fixes was
    previously skipped outright (counted toward neither bucket)
    instead of falling back to nearby data - silently discarding most
    of a real drive's duration from the breakdown without any
    indication in trip_info.txt that anything was missing. Only a
    fix with genuinely no earlier speed reading anywhere before it in
    the trip (i.e. no real reading has been seen yet at all) still
    contributes no classifiable segment - unavoidable, since there's
    truly nothing to carry forward from yet. Both moving_seconds/
    idle_seconds are None under the same condition average_speed_kmh/
    max_speed_kmh are None - no speed data at all anywhere in the
    trip. (average_speed_kmh/max_speed_kmh themselves are NOT
    carried-forward - they're deliberately each fix's own raw,
    unfilled reading only, same as before this fix.)

    Returns None if there are fewer than two valid, positioned fixes -
    not enough to measure any distance from, the same "nothing to
    work with" convention render_map_video() and write_gpx() already
    use.
    """

    positioned = tuple(
        fix
        for fix in fixes
        if fix.valid and fix.latitude is not None and fix.longitude is not None
    )

    if len(positioned) < 2:
        return None

    # Forward-fill: each fix's own speed_kmh reading if it has one,
    # otherwise the most recent earlier reading in the trip (None
    # until the very first real reading appears in `positioned`) -
    # see the moving_seconds/idle_seconds docstring above for why.
    effective_speeds: list[float | None] = []
    last_known_speed_kmh: float | None = None
    for fix in positioned:
        if fix.speed_kmh is not None:
            last_known_speed_kmh = fix.speed_kmh
        effective_speeds.append(last_known_speed_kmh)

    total_meters = 0.0
    moving_seconds = 0.0
    idle_seconds = 0.0
    any_speed_data = False

    for index, (previous, current) in enumerate(zip(positioned, positioned[1:])):
        total_meters += _haversine_distance_meters(
            previous.latitude, previous.longitude,
            current.latitude, current.longitude,
        )

        segment_speeds = [
            speed
            for speed in (effective_speeds[index], effective_speeds[index + 1])
            if speed is not None
        ]
        if not segment_speeds:
            continue
        any_speed_data = True

        elapsed_seconds = (current.timestamp - previous.timestamp).total_seconds()
        segment_speed_kmh = sum(segment_speeds) / len(segment_speeds)
        if segment_speed_kmh < DEFAULT_SPEED_THRESHOLD_KMH:
            idle_seconds += elapsed_seconds
        else:
            moving_seconds += elapsed_seconds

    speeds = [fix.speed_kmh for fix in positioned if fix.speed_kmh is not None]
    average_speed_kmh = sum(speeds) / len(speeds) if speeds else None
    max_speed_kmh = max(speeds) if speeds else None

    return TripStats(
        distance_km=total_meters / 1000,
        average_speed_kmh=average_speed_kmh,
        max_speed_kmh=max_speed_kmh,
        moving_seconds=moving_seconds if any_speed_data else None,
        idle_seconds=idle_seconds if any_speed_data else None,
    )
