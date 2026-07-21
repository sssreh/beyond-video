"""
Raw BlackVue .gps file reader.

File format (reverse-engineered from a real recording, not officially
documented): a stream of standard NMEA-0183 sentences, each one
prefixed with the Unix epoch time in milliseconds the sentence was
captured at, in square brackets - e.g.:

    [1784555901923]$GPRMC,125819.00,A,5917.94615,N,01805.17070,E,\
8.704,162.13,200726,,,A*6D

Each capture "tick" (roughly once a second) repeats the same bracket
timestamp across several sentence types (GGA, GSA, GSV x3-4, GLL,
RMC, VTG). $GPRMC alone carries everything this reader needs in one
sentence - fix validity, position, speed, and course - so only $GPRMC
lines are parsed; the rest are ignored.

Camera clock note: the bracket timestamp (a real Unix epoch, so UTC)
was found to match the recording's filename timestamp
(RecordingId.timestamp, which is naive/local) to the second in a real
sample file. That means the camera's system clock isn't set to local
time - it's effectively UTC-equivalent, or at least close enough that
naively comparing the two as if they were on the same timescale is
correct in practice. read_gps() therefore returns naive datetimes
computed the same way RecordingId.timestamp is, so the two remain
directly comparable without any timezone conversion.

Copyright (C) 2026 Christer R. (sssreh)

SPDX-License-Identifier: GPL-3.0-or-later
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from ..generate.media import MediaToolError

_SENTENCE_PATTERN = re.compile(r"\[(\d+)\](\$GPRMC,[^\[]*)", re.DOTALL)


@dataclass(frozen=True)
class GpsFix:
    """One $GPRMC fix from a .gps file."""

    timestamp: datetime
    valid: bool
    latitude: float | None
    longitude: float | None
    speed_kmh: float | None
    course: float | None


def _nmea_coordinate_to_decimal(value: str, hemisphere: str) -> float:
    """Convert an NMEA ddmm.mmmm / dddmm.mmmm coordinate to decimal
    degrees.

    NMEA always encodes the minutes as exactly the two digits
    immediately before the decimal point (plus whatever follows it),
    with the degrees being whatever precedes that - this holds
    regardless of whether degrees is 2 digits (latitude) or 3
    (longitude), so no separate lat/lon-specific parsing is needed.
    """

    dot = value.index(".")
    minutes_start = dot - 2
    degrees = int(value[:minutes_start])
    minutes = float(value[minutes_start:])
    decimal = degrees + minutes / 60

    if hemisphere in ("S", "W"):
        decimal = -decimal

    return decimal


def _parse_rmc(timestamp_ms: str, sentence: str) -> GpsFix | None:
    """Parse one [ts]$GPRMC,... match into a GpsFix, or None if the
    sentence is too malformed to use."""

    body = sentence.split("*", 1)[0].strip()
    fields = body.split(",")

    # $GPRMC + 12 fields: time, status, lat, N/S, lon, E/W,
    # speed(knots), course, date, magvar, magvar E/W, mode.
    if len(fields) != 13:
        return None

    _, _time, status, lat, ns, lon, ew, speed_knots, course, _date, _mv, _mvd, _mode = fields

    timestamp = datetime.utcfromtimestamp(int(timestamp_ms) / 1000)
    valid = status == "A"

    latitude = (
        _nmea_coordinate_to_decimal(lat, ns) if lat and ns else None
    )
    longitude = (
        _nmea_coordinate_to_decimal(lon, ew) if lon and ew else None
    )
    speed_kmh = float(speed_knots) * 1.852 if speed_knots else None
    course_value = float(course) if course else None

    return GpsFix(
        timestamp=timestamp,
        valid=valid,
        latitude=latitude,
        longitude=longitude,
        speed_kmh=speed_kmh,
        course=course_value,
    )


def read_gps(path: Path) -> tuple[GpsFix, ...]:
    """Read every $GPRMC fix from a raw BlackVue .gps file."""

    try:
        text = path.read_text(encoding="ascii", errors="replace")
    except OSError as exc:
        raise MediaToolError(f"could not read {path.name}: {exc}") from exc

    fixes = []

    for timestamp_ms, sentence in _SENTENCE_PATTERN.findall(text):
        fix = _parse_rmc(timestamp_ms, sentence)
        if fix is not None:
            fixes.append(fix)

    return tuple(fixes)
