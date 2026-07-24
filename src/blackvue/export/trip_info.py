"""
trip_info.txt writer for bv-export - a short, human-readable summary
of one trip's real-world facts: duration, distance, speed, and (when
reverse geocoding succeeded) a place name/address for where it started
and ended.

Copyright (C) 2026 Christer R. (sssreh)

SPDX-License-Identifier: GPL-3.0-or-later
"""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path

from .trip_stats import TripStats


def write_trip_info(
    path: Path,
    *,
    duration: timedelta,
    stats: TripStats | None = None,
    start_address: str | None = None,
    end_address: str | None = None,
) -> None:
    """Write a short plain-text trip summary to `path`.

    `duration` (see trip.Trip.end_timestamp) is always written - it's
    always known, even if it comes out as 0:00:00 for a trip whose
    real video length couldn't be determined (see that property's own
    docstring). `stats` (trip_stats.compute_trip_stats()'s result) may
    be None if there wasn't enough GPS data to compute distance/speed
    from - those lines are simply omitted rather than shown as zero,
    since "unknown" and "genuinely zero" are different facts worth not
    conflating. `start_address`/`end_address` are each independently
    optional - reverse geocoding degrades one point at a time (see
    trip_export.py), so a trip can end up with either, both, or
    neither.
    """

    lines = [f"Duration: {duration}"]

    if stats is not None:
        lines.append(f"Distance: {stats.distance_km:.2f} km")
        if stats.average_speed_kmh is not None:
            lines.append(f"Average speed: {stats.average_speed_kmh:.1f} km/h")
        if stats.max_speed_kmh is not None:
            lines.append(f"Max speed: {stats.max_speed_kmh:.1f} km/h")

    if start_address:
        lines.append(f"Start location: {start_address}")
    if end_address:
        lines.append(f"End location: {end_address}")

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
