"""
Video-container GPS location tag reading (ISO 6709 - the `location`/
`location-eng` format tag QuickTime/MP4-family tools mux into a video
file's own container metadata), for a real video whose adapter has no
telemetry GPS source at all - FolderAdapter always (it never declares
gps support), or GoProAdapter on a clip with no GPMF track (a
downloaded/stock clip mixed into the archive, not real GoPro footage
- exactly Christer's real case below).

Christer's own report, verbatim, from a real
`ffprobe -v error -show_format -show_streams` dump he ran on one such
clip (`x:\\gopro\\archive\\13532784_1080_1920_60fps.mp4` - a stock/
sample test-fixture video with no `creation_time` tag anywhere, see
adapters/_recursive_scan.py's `Recording.timestamp_reliable` field for
that half of the same investigation):

    TAG:location-{=+05.0448-073.7965/
    TAG:location=+05.0448-073.7965/

"This looks like gps coordinates ... not found by bv-generate." He's
right - it's a real single-point GPS fix in ISO 6709's "typical"
representation (a signed latitude, a signed longitude, an optional
signed altitude, and a trailing `/`, with no separators between the
numbers), muxed in by whatever tool originally produced or re-encoded
the file - and nothing in this project read it. The `web/app.py`
`/archive/{camera_id}/{recording_id}/location` route already has an
exact precedent for this shape of fallback: `archive/exif.py`'s
`exif_gps_fix()`, wired in for a photo with no `.gps` sidecar but real
EXIF GPS data (task #957). This module is that same idea applied to a
real video's own container tag instead of a photo's EXIF sub-IFD -
same `GpsFix` shape, same "a single point read purely for display"
framing, same non-fatal degrade-to-None on anything missing or
unparseable (the overwhelmingly common case: most videos carry no
location tag at all).

Copyright (C) 2026 Christer R. (sssreh)

SPDX-License-Identifier: GPL-3.0-or-later
"""

from __future__ import annotations

import json
import re
import subprocess
from datetime import datetime
from pathlib import Path

from ..telemetry.gps_reader import GpsFix

# Matches ISO 6709's "typical" representation as ffmpeg/QuickTime
# write it: a signed latitude, a signed longitude, and an optional
# signed altitude, packed with no separators and a trailing '/' - e.g.
# "+05.0448-073.7965/" (no altitude) or "+27.5916+086.5640+8850/"
# (with one). Digit-width-agnostic (finds every signed-number
# substring in the tag) rather than a strict fixed-width regex, since
# ISO 6709 doesn't mandate one fixed precision and this project would
# rather parse a slightly-unusual-but-valid tag than reject it over
# formatting.
_SIGNED_NUMBER = re.compile(r"[+-]\d+(?:\.\d+)?")


def _probe_container_location(path: Path) -> tuple[float, float] | None:
    """Return `path`'s embedded (latitude, longitude) from its
    container-level `location` format tag (ffprobe), or None if
    ffprobe is missing, fails, the tag isn't present, or its value
    doesn't parse into at least a lat/lon pair - all ordinary,
    non-fatal outcomes; most videos simply have no location tag at
    all."""

    try:
        result = subprocess.run(
            [
                "ffprobe", "-v", "error",
                "-show_entries", "format_tags=location",
                "-of", "json",
                str(path),
            ],
            capture_output=True,
            text=True,
            check=True,
        )
    except (FileNotFoundError, subprocess.CalledProcessError):
        return None

    try:
        tags = json.loads(result.stdout)["format"]["tags"]
        raw = tags["location"]
    except (KeyError, ValueError, json.JSONDecodeError):
        return None

    if not isinstance(raw, str):
        return None

    numbers = _SIGNED_NUMBER.findall(raw)
    if len(numbers) < 2:
        return None

    try:
        latitude = float(numbers[0])
        longitude = float(numbers[1])
    except ValueError:
        return None

    return latitude, longitude


def container_location_fix(path: Path, *, timestamp: datetime) -> GpsFix | None:
    """Return `path`'s embedded container-level GPS location as a
    `GpsFix` (see telemetry/gps_reader.py), or None if none is
    present/parseable - see `_probe_container_location()`'s own
    docstring.

    `timestamp` is the caller's own already-resolved recording
    timestamp, reused as-is rather than derived from anything in the
    tag itself (ISO 6709 carries no separate time component) - the
    same "single point read purely for display" framing
    `archive/exif.py`'s `exif_gps_fix()` already uses, which this
    function otherwise mirrors closely. `valid` is always True on a
    returned fix, and `speed_kmh`/`course` are always None, for the
    same reason: a container location tag is a single static point,
    not a track."""

    coordinates = _probe_container_location(path)
    if coordinates is None:
        return None

    latitude, longitude = coordinates
    return GpsFix(
        timestamp=timestamp,
        valid=True,
        latitude=latitude,
        longitude=longitude,
        speed_kmh=None,
        course=None,
    )
