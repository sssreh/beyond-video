"""
Trip-level distance/speed statistics for bv-export's trip_info.txt.

Copyright (C) 2026 Christer R. (sssreh)

SPDX-License-Identifier: GPL-3.0-or-later
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from ..telemetry.gps_reader import GpsFix

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

    total_meters = 0.0
    for previous, current in zip(positioned, positioned[1:]):
        total_meters += _haversine_distance_meters(
            previous.latitude, previous.longitude,
            current.latitude, current.longitude,
        )

    speeds = [fix.speed_kmh for fix in positioned if fix.speed_kmh is not None]
    average_speed_kmh = sum(speeds) / len(speeds) if speeds else None
    max_speed_kmh = max(speeds) if speeds else None

    return TripStats(
        distance_km=total_meters / 1000,
        average_speed_kmh=average_speed_kmh,
        max_speed_kmh=max_speed_kmh,
    )
