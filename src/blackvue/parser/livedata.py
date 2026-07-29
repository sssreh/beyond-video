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
