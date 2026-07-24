"""
trip_info.txt writer for bv-export - a short, human-readable summary
of one trip's real-world facts: when it happened, duration, distance,
speed, moving/idle time, a place name/address for where it started and
ended (when reverse geocoding succeeded), whether it includes any
Parking-mode footage, and its total on-disk size.

Copyright (C) 2026 Christer R. (sssreh)

SPDX-License-Identifier: GPL-3.0-or-later
"""

from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path

from .trip_stats import TripStats

# Matches bv-ls --trips's own timestamp column formatting
# (cli/bv_ls.py) - a trip_info.txt reader who's also seen that table
# shouldn't have to mentally translate a different date/time format
# for the same underlying value.
_TIMESTAMP_FORMAT = "%Y-%m-%d %H:%M:%S"


def _format_size(size_bytes: int) -> str:
    """A human-readable byte count, e.g. "512.34 MB".

    Deliberately a small local reimplementation of cli/bv_ls.py's own
    `format_size()` rather than an import - that one lives in the CLI
    layer (which already depends on this export layer, not the other
    way around) and is tuned for a compact table column ("512.34M");
    this one spells out the unit ("512.34 MB") to read better in a
    prose-style summary file.
    """

    units = ("B", "KB", "MB", "GB", "TB")
    value = float(size_bytes)
    for unit in units:
        if value < 1024 or unit == units[-1]:
            if unit == "B":
                return f"{int(value)} {unit}"
            return f"{value:.2f} {unit}"
        value /= 1024
    raise AssertionError  # pragma: no cover - unreachable, see loop above


def write_trip_info(
    path: Path,
    *,
    duration: timedelta,
    start_timestamp: datetime | None = None,
    end_timestamp: datetime | None = None,
    stats: TripStats | None = None,
    start_address: str | None = None,
    end_address: str | None = None,
    has_parking_footage: bool = False,
    total_size_bytes: int | None = None,
) -> None:
    """Write a short plain-text trip summary to `path`.

    `duration` (see trip.Trip.end_timestamp) is always written - it's
    always known, even if it comes out as 0:00:00 for a trip whose
    real video length couldn't be determined (see that property's own
    docstring). `start_timestamp`/`end_timestamp` (trip.Trip's own
    properties) are each independently optional only so this function
    stays usable without a full Trip object in hand (e.g. in a unit
    test); trip_export.py always has both and always passes them.

    `stats` (trip_stats.compute_trip_stats()'s result) may be None if
    there wasn't enough GPS data to compute distance/speed from -
    those lines (including moving/idle time) are simply omitted rather
    than shown as zero, since "unknown" and "genuinely zero" are
    different facts worth not conflating - same reasoning
    average_speed_kmh/max_speed_kmh already use, extended to
    moving_seconds/idle_seconds.

    `start_address`/`end_address` are each independently optional -
    reverse geocoding degrades one point at a time (see
    trip_export.py), so a trip can end up with either, both, or
    neither.

    `has_parking_footage` is a plain bool, always known - but only
    written when True (see trip.Trip.has_parking_footage's own
    docstring): a normal-driving trip is the common case, so this
    flags the exceptional one rather than restating "no" in every
    file. `total_size_bytes` is omitted (not shown as "0 B") when
    None, the same "unknown vs. genuinely zero/absent" convention
    every other optional field here uses.
    """

    lines = []
    if start_timestamp is not None:
        lines.append(f"Started: {start_timestamp:{_TIMESTAMP_FORMAT}}")
    if end_timestamp is not None:
        lines.append(f"Ended: {end_timestamp:{_TIMESTAMP_FORMAT}}")
    lines.append(f"Duration: {duration}")

    if stats is not None:
        lines.append(f"Distance: {stats.distance_km:.2f} km")
        if stats.average_speed_kmh is not None:
            lines.append(f"Average speed: {stats.average_speed_kmh:.1f} km/h")
        if stats.max_speed_kmh is not None:
            lines.append(f"Max speed: {stats.max_speed_kmh:.1f} km/h")
        if stats.moving_seconds is not None:
            lines.append(f"Moving time: {timedelta(seconds=round(stats.moving_seconds))}")
        if stats.idle_seconds is not None:
            lines.append(f"Idle time: {timedelta(seconds=round(stats.idle_seconds))}")

    if start_address:
        lines.append(f"Start location: {start_address}")
    if end_address:
        lines.append(f"End location: {end_address}")

    if has_parking_footage:
        lines.append("Includes Parking-mode footage")
    if total_size_bytes is not None:
        lines.append(f"Total size: {_format_size(total_size_bytes)}")

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
