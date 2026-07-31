"""
BlackVue live data parser.

blackvue_livedata.cgi streams a never-ending multipart/x-mixed-replace
sequence of small JSON objects - GPS and g-sensor readings,
interleaved, with no way to ask for just one kind (see
BlackVueClient.live_gps()'s docstring for how a caller reads a bounded
slice of that stream rather than trying to read it to completion).

Copyright (C) 2026 Christer R. (sssreh)

SPDX-License-Identifier: GPL-3.0-or-later
"""

from __future__ import annotations

import re

# Matches just the {"GPS": {"LATITUDE": .., "LONGITUDE": ..}} object's
# two numbers, deliberately not the surrounding multipart framing
# (boundary line, Content-Type, Content-Length headers) - those are
# noise here, and a plain substring search is far more robust against
# a GPS object landing anywhere within (or split across) whatever
# chunk boundaries the underlying socket read happens to produce than
# trying to fully parse the multipart structure would be.
_GPS_PATTERN = re.compile(
    r'"GPS"\s*:\s*\{\s*"LATITUDE"\s*:\s*(-?[0-9.]+)\s*,\s*'
    r'"LONGITUDE"\s*:\s*(-?[0-9.]+)\s*\}'
)

# The g-sensor ("3G") side of the same interleaved stream - confirmed
# field names/casing from the camera's own firmware strings (see
# WORKING_CONTEXT.md's original bv-gps entry). Deliberately kept as
# these plain-English field names throughout this project's live-data
# handling (FrontRear/LeftRight/UpperLower) rather than relabeled to
# the offline .3gf format's X/Y/Z convention - there's no independently
# confirmed mapping between the two (see telemetry/gsensor_reader.py's
# own docstring on the .3gf axis mapping being unconfirmed), so
# pretending they're the same axes would be a guess dressed up as fact.
_GSENSOR_PATTERN = re.compile(
    r'"3G"\s*:\s*\{\s*"FrontRear"\s*:\s*(-?[0-9.]+)\s*,\s*'
    r'"LeftRight"\s*:\s*(-?[0-9.]+)\s*,\s*"UpperLower"\s*:\s*(-?[0-9.]+)\s*\}'
)


def parse_gps_fix(text: str) -> tuple[float, float] | None:
    """Find the first GPS reading in a chunk of blackvue_livedata.cgi
    response text, returning it as (latitude, longitude).

    Returns None if no complete GPS object is present yet - either
    because this chunk only contains g-sensor ("3G") readings, or
    because a GPS object is present but was cut off mid-way (the
    multipart stream arrives as a sequence of independent socket
    reads, not one read per JSON object). Callers read progressively
    larger buffers and re-parse until this stops returning None - see
    BlackVueClient.live_gps().
    """

    match = _GPS_PATTERN.search(text)

    if match is None:
        return None

    return float(match.group(1)), float(match.group(2))


def parse_gsensor_reading(text: str) -> tuple[float, float, float] | None:
    """Find the first g-sensor reading in a chunk of
    blackvue_livedata.cgi response text, returning it as
    (front_rear, left_right, upper_lower) - see this module's own
    _GSENSOR_PATTERN comment for why these field names, not X/Y/Z.

    Same "None means not found (yet)" contract as parse_gps_fix().
    """

    match = _GSENSOR_PATTERN.search(text)

    if match is None:
        return None

    return float(match.group(1)), float(match.group(2)), float(match.group(3))


def find_next_gps(text: str) -> tuple[tuple[float, float], int, int] | None:
    """Like parse_gps_fix(), but also returns where the match starts
    and ends in `text` (its regex .start()/.end() indices).

    parse_gps_fix()'s only existing caller (BlackVueClient.live_gps())
    reads one bounded slice and returns after the first match, with
    nothing left to keep scanning - it has no need for this. A
    continuous scanner reading blackvue_livedata.cgi forever instead
    (see live/telemetry.py's LiveTelemetryPump) does: it needs to know
    how much of its own growing buffer to discard after consuming a
    match, so the same object is never matched twice and the buffer
    doesn't grow without bound. `start` matters too whenever a scanner
    is choosing between this and find_next_gsensor_reading()'s own
    result on the same buffer - whichever of the two starts earliest
    has to be the one actually consumed, or an object of the *other*
    kind sitting ahead of it in the buffer would be silently discarded
    along with everything before the later match (see
    live/telemetry.py's _drain_livedata_buffer()).
    """

    match = _GPS_PATTERN.search(text)

    if match is None:
        return None

    return (float(match.group(1)), float(match.group(2))), match.start(), match.end()


def find_next_gsensor_reading(
    text: str,
) -> tuple[tuple[float, float, float], int, int] | None:
    """Same as find_next_gps(), for a g-sensor reading instead - see
    its own docstring for why this exists alongside
    parse_gsensor_reading(), and for why it returns a start index
    too."""

    match = _GSENSOR_PATTERN.search(text)

    if match is None:
        return None

    return (
        (float(match.group(1)), float(match.group(2)), float(match.group(3))),
        match.start(),
        match.end(),
    )
