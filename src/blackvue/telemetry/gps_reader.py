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

Altitude: each capture tick also emits a $GPGGA sentence sharing the
same bracket timestamp as its sibling $GPRMC (see "Each capture tick"
below) - GGA is the NMEA sentence type that actually carries MSL
altitude, which RMC has no field for at all. Christer, after asking
whether height could be calculated from the GPS data at all: rather
than building a second, separate GpsFix from GGA on its own, read_gps()
below matches each GGA's altitude to its same-tick RMC fix by that
shared bracket timestamp and carries it as GpsFix.altitude_meters -
every other field on GpsFix stays exactly as it always was, sourced
from RMC alone.

Fix validity comes from the sentence's mode indicator (the last
field), not its older status field (the one right after the time,
'A'/'V') - see GpsFix.valid's own docstring for why. Confirmed on a
real archive (Christer, 2026-07-24): a BlackVue receiver can carry
status='V' for a good while after mode already reports 'A' with a
real, physically continuous position - status lagging behind a
stricter internal accuracy/DOP confirmation the mode indicator
doesn't wait for. Using status alone silently discarded a large,
genuinely usable stretch of GPS track - up to the first ~11 minutes
of a real trip in one case - even though BlackVue's own viewer app
has the exact same blind spot (also shows no movement there), so
this isn't a deliberate design choice being second-guessed, just an
already-present signal in the sentence that wasn't being read.

Status is not thrown away, though - it's exposed separately as
GpsFix.confirmed (see that field's own docstring). Two real bugs
(2026-08-24, on trip_stats.py's max_speed_kmh and
elevation_change_meters - see that module's own comments) traced back
to exactly the mode='A'/status='V' disagreement window this docstring
already describes: a reading counts as `valid` (a real position was
computed) while still not being `confirmed` (the receiver's own
stricter internal check hasn't caught up yet), and both times the
readings inside that window turned out to be garbage. Christer, after
being shown this: "could our stats problem be related to using
non-confirmed positions?" - yes, exactly. `valid` stays mode-based
everywhere it already was (map rendering, live GPS, trip building,
gap detection, ...), since discarding that ~11 minutes of real track
was the whole reason it moved off status in the first place. But
trip_stats.py's own aggregate numbers (distance, speed, altitude) now
additionally require `confirmed`, on top of `valid` - see that
module's own comments for the "only use confirmed positions for
stats" policy this motivated.

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

_RMC_PATTERN = re.compile(r"\[(\d+)\](\$GPRMC,[^\[]*)", re.DOTALL)
_GGA_PATTERN = re.compile(r"\[(\d+)\](\$GPGGA,[^\[]*)", re.DOTALL)


@dataclass(frozen=True)
class GpsFix:
    """One $GPRMC fix from a .gps file, with altitude enriched in from
    that same tick's sibling $GPGGA sentence.

    `valid` reflects the sentence's mode indicator (its last field) -
    False only when the receiver has no position at all ('N', no
    fix); any other mode ('A'utonomous, 'D'ifferential, 'E'stimated/
    dead-reckoning, ...) counts as valid, since all of them mean a
    real position was computed. Deliberately NOT the older status
    field ('A'/'V', right after the time) - see gps_reader.py's own
    module docstring for why that field alone turned out to
    underreport real, usable GPS data on a real archive.

    `confirmed` reflects that older status field instead ('A' ->
    True, anything else, usually 'V' -> False) - kept separate from
    `valid` rather than replacing it, since the two answer different
    questions: `valid` is "did the receiver compute a position at
    all," `confirmed` is "has the receiver's own stricter internal
    accuracy check caught up to that position yet." A fix can be
    valid but not confirmed (mode='A', status still 'V') - the exact
    disagreement window behind two real trip_stats.py bugs (a 305.9
    km/h speed spike, a -7397m elevation plunge - see that module's
    own comments) that only showed up because both readings were
    trusted for being `valid` without also being `confirmed`.
    Defaults to True so every pre-existing GpsFix(...) call site
    across the codebase (tests, container_gps.py, exif.py, gpmf.py,
    ...) that never had a real NMEA status field to read from keeps
    behaving exactly as before - only gps_reader.py's own
    _parse_rmc(), which has a real status field to read, ever sets
    this False.
    """

    timestamp: datetime
    valid: bool
    latitude: float | None
    longitude: float | None
    speed_kmh: float | None
    course: float | None
    # See `confirmed`'s own docstring above for why this defaults to
    # True (same "pre-existing call sites keep working unchanged"
    # reasoning altitude_meters' own default below already uses).
    confirmed: bool = True
    # None whenever no $GPGGA sentence shared this fix's exact bracket
    # timestamp (see module docstring's "Altitude" paragraph) - has a
    # default so every pre-existing GpsFix(...) call site across the
    # codebase (tests, container_gps.py, exif.py, gpmf.py, ...) keeps
    # working unchanged, same "optional field, default None" pattern
    # trip_stats.TripStats's own moving_seconds/idle_seconds already
    # use.
    altitude_meters: float | None = None


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


def _parse_gga_altitude(sentence: str) -> float | None:
    """Extract the MSL altitude (meters) from one [ts]$GPGGA,...
    match, or None if the sentence has the wrong field count or an
    empty/non-numeric altitude field.

    $GPGGA is the sentence type that actually carries altitude -
    $GPRMC (this reader's primary sentence) has no altitude field at
    all, see this module's own "Altitude" docstring paragraph above.
    Deliberately not folded into a full GgaFix-style dataclass the way
    _parse_rmc() builds a whole GpsFix: nothing else on GpsFix is
    sourced from GGA, so the only thing worth pulling out of it here
    is this one number - read_gps() below matches it back to its
    sibling RMC fix by their shared bracket timestamp.
    """

    body = sentence.split("*", 1)[0].strip()
    fields = body.split(",")

    # $GPGGA + 14 fields: time, lat, N/S, lon, E/W, fix quality,
    # numSats, HDOP, altitude, altitude-unit, geoid-sep,
    # geoid-sep-unit, dgps-age, dgps-station-id.
    if len(fields) != 15:
        return None

    altitude = fields[9]

    try:
        return float(altitude) if altitude else None
    except ValueError:
        return None


def _parse_rmc(
    timestamp_ms: str, sentence: str, altitude_meters: float | None
) -> GpsFix | None:
    """Parse one [ts]$GPRMC,... match into a GpsFix, or None if the
    sentence is too malformed to use - either the wrong number of
    fields, or a field that doesn't parse as the number it's
    supposed to be (a coordinate with no decimal point, a non-numeric
    speed/course, ...). The latter was a real gap until this docstring's
    own claim was actually true: a single corrupted sentence -
    plausible on a recording where the camera lost power mid-write,
    the same class of real-world corruption this project has already
    hit elsewhere (see WORKING_CONTEXT.md) - used to raise a raw
    ValueError straight out of this function instead of being treated
    like any other malformed line. Every caller (bv-export's own
    _merge_gps(), and bv-web's archive_recording_location route) only
    ever guards against MediaToolError, not ValueError, so an
    unhandled one here would have propagated all the way up as an
    uncaught exception - a crashed export or a 500 page - rather than
    just skipping the one bad sentence the way a non-matching line
    already does.

    `altitude_meters` is read_gps()'s own lookup of this same tick's
    sibling $GPGGA sentence (see that function and GpsFix's own
    docstring) - just passed through onto the resulting GpsFix, not
    parsed here.
    """

    body = sentence.split("*", 1)[0].strip()
    fields = body.split(",")

    # $GPRMC + 12 fields: time, status, lat, N/S, lon, E/W,
    # speed(knots), course, date, magvar, magvar E/W, mode.
    if len(fields) != 13:
        return None

    _, _time, status, lat, ns, lon, ew, speed_knots, course, _date, _mv, _mvd, mode = fields

    try:
        timestamp = datetime.utcfromtimestamp(int(timestamp_ms) / 1000)
        # See GpsFix.valid's own docstring - deliberately the mode
        # indicator, not the older `status` field.
        valid = mode != "N"
        # See GpsFix.confirmed's own docstring - the older status
        # field, now exposed rather than discarded.
        confirmed = status == "A"

        latitude = (
            _nmea_coordinate_to_decimal(lat, ns) if lat and ns else None
        )
        longitude = (
            _nmea_coordinate_to_decimal(lon, ew) if lon and ew else None
        )
        speed_kmh = float(speed_knots) * 1.852 if speed_knots else None
        course_value = float(course) if course else None
    except ValueError:
        return None

    return GpsFix(
        timestamp=timestamp,
        valid=valid,
        latitude=latitude,
        longitude=longitude,
        speed_kmh=speed_kmh,
        course=course_value,
        confirmed=confirmed,
        altitude_meters=altitude_meters,
    )


def read_gps(path: Path) -> tuple[GpsFix, ...]:
    """Read every $GPRMC fix from a raw BlackVue .gps file, with
    altitude enriched in from each fix's sibling $GPGGA sentence (see
    GpsFix's own docstring)."""

    try:
        text = path.read_text(encoding="ascii", errors="replace")
    except OSError as exc:
        raise MediaToolError(f"could not read {path.name}: {exc}") from exc

    # Built up front, in one pass over every $GPGGA sentence in the
    # file, rather than re-scanning per-RMC - a .gps file's sentences
    # aren't in any guaranteed order (a GGA can appear before or after
    # its same-tick RMC), so this has to be a lookup by shared bracket
    # timestamp rather than "the most recently seen GGA".
    altitudes_by_timestamp_ms: dict[str, float] = {}
    for timestamp_ms, sentence in _GGA_PATTERN.findall(text):
        altitude = _parse_gga_altitude(sentence)
        if altitude is not None:
            altitudes_by_timestamp_ms[timestamp_ms] = altitude

    fixes = []

    for timestamp_ms, sentence in _RMC_PATTERN.findall(text):
        fix = _parse_rmc(
            timestamp_ms,
            sentence,
            altitudes_by_timestamp_ms.get(timestamp_ms),
        )
        if fix is not None:
            fixes.append(fix)

    return tuple(fixes)
