"""
EXIF metadata reading for photo recordings (JPEG/PNG/TIFF - anything
Pillow can open and that carries an EXIF block).

Christer, following up on the GIF classification question this module
was built alongside: "Maybe we need exif now." Three capabilities,
all opportunistic (never raise, never block a photo from rendering if
the data just isn't there):

  - exif_datetime_original() - a photo's real capture time, more
    trustworthy than file mtime (which reflects when a copy/download
    happened, not when the shutter fired - see adapters/
    _recursive_scan.py's own `_resolve_timestamp()` docstring for the
    exact same reasoning already applied to ffprobe's creation_time
    vs. mtime).
  - normalize_photo_orientation() - bakes a portrait phone photo's
    EXIF Orientation tag into its actual pixel data before it's
    handed to ffmpeg, which does not auto-rotate on its own (confirmed
    directly - see that function's own docstring for the exact
    symptom this fixes).
  - exif_gps_fix() - a photo's embedded GPS coordinates, shaped as a
    telemetry.gps_reader.GpsFix so it can flow into the same
    `/archive/{camera_id}/{recording_id}/location` display machinery
    a real `.gps` sidecar already uses.

Every function here degrades to "nothing found" (None/False) rather
than raising on a file with no EXIF block, a format Pillow can't open
at all (HEIC/GPR need the optional pillow-heif/rawpy plugins, neither
a project dependency - see export/media.py's `render_image_as_video()`
docstring for the same caveat), or a corrupt file - the same "missing
telemetry is absent, not fatal" policy this project already applies
to GPS/g-sensor reads (telemetry/gps_reader.py, telemetry/movement.py).
A photo with no EXIF at all is an entirely ordinary case, not a bug.

Copyright (C) 2026 Christer R. (sssreh)

SPDX-License-Identifier: GPL-3.0-or-later
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any

from ..telemetry.gps_reader import GpsFix

# EXIF tag ids, as returned by Pillow's Image.getexif() flat mapping -
# see PIL.ExifTags for the full registry; only the handful this module
# actually reads are named here.
_TAG_ORIENTATION = 274
_TAG_DATETIME_ORIGINAL = 36867
_TAG_GPS_IFD = 34853

# GPS sub-IFD tag ids - exif.get_ifd(_TAG_GPS_IFD)'s own keys. NOT the
# same numbering as the top-level EXIF tags above; a real gotcha found
# while building this (exif.get(_TAG_GPS_IFD) alone only returns a raw
# IFD pointer, not the nested dict - get_ifd() is required).
_GPS_LATITUDE_REF = 1
_GPS_LATITUDE = 2
_GPS_LONGITUDE_REF = 3
_GPS_LONGITUDE = 4


def read_exif(path: Path) -> Any | None:
    """Return `path`'s EXIF data (Pillow's own flat Image.Exif
    mapping), or None if the file can't be opened/decoded at all, or
    carries no EXIF block whatsoever - see this module's own
    docstring for why every one of those is an ordinary, non-fatal
    outcome rather than an error worth surfacing.
    """

    try:
        from PIL import Image
    except ImportError:
        return None

    try:
        with Image.open(path) as image:
            exif = image.getexif()
    except Exception:
        return None

    if not exif:
        return None

    return exif


def exif_datetime_original(path: Path) -> datetime | None:
    """Return `path`'s EXIF DateTimeOriginal tag (id 36867) as a
    naive datetime, or None if the file has no readable EXIF data, no
    DateTimeOriginal tag, or the tag's value doesn't parse - see
    read_exif()'s own docstring for why all of those are ordinary,
    non-fatal outcomes here.

    EXIF's own DateTimeOriginal format is "YYYY:MM:DD HH:MM:SS" (note
    colons, not dashes, in the date part) - parsed directly rather
    than via datetime.fromisoformat(), which doesn't accept that
    shape.
    """

    exif = read_exif(path)
    if exif is None:
        return None

    raw = exif.get(_TAG_DATETIME_ORIGINAL)
    if not raw or not isinstance(raw, str):
        return None

    try:
        return datetime.strptime(raw, "%Y:%m:%d %H:%M:%S")
    except ValueError:
        return None


def _dms_to_decimal(dms: tuple[Any, Any, Any], ref: str) -> float:
    """Convert an EXIF GPS (degrees, minutes, seconds) tuple plus its
    reference tag ('N'/'S'/'E'/'W') into a signed decimal-degree
    float - south and west are negative, matching the plain signed
    lat/lon convention the rest of this project's GPS code (GpsFix,
    the map renderer, reverse geocoding) already uses throughout."""

    degrees, minutes, seconds = (float(component) for component in dms)
    value = degrees + minutes / 60.0 + seconds / 3600.0
    if ref in ("S", "W"):
        value = -value
    return value


def exif_gps_fix(path: Path, *, timestamp: datetime) -> GpsFix | None:
    """Return `path`'s EXIF GPS coordinates as a `GpsFix` (see
    telemetry/gps_reader.py), or None if the file has no readable
    EXIF data, no GPS sub-IFD, or an incomplete/unparseable one - the
    overwhelmingly common case for any photo not taken by a GPS-
    equipped phone/camera.

    `timestamp` is the caller's own already-resolved recording
    timestamp (RecordingId.timestamp), reused as-is rather than
    re-derived from the GPS sub-IFD's own GPSDateStamp/GPSTimeStamp
    tags - a still photo's GpsFix here is a single point read purely
    for display (see web/app.py's `/location` route), so a second,
    separately-parsed timestamp would only risk disagreeing with the
    one already driving the rest of the recording's identity, for no
    real benefit.

    `valid` is always True on a returned fix - unlike a `.gps`
    sidecar's own NMEA stream (which really can carry a "receiver
    briefly lost fix" state - see GpsFix.valid's own docstring), EXIF
    GPS tags simply aren't present at all if the device had no fix
    when the photo was taken; if the GPS sub-IFD exists with real
    latitude/longitude tags, the position is real. `speed_kmh`/
    `course` are always None - a still photo captures a single
    instant, with no motion to compute either from.
    """

    exif = read_exif(path)
    if exif is None:
        return None

    try:
        gps_ifd = exif.get_ifd(_TAG_GPS_IFD)
    except Exception:
        return None

    if not gps_ifd:
        return None

    try:
        lat_ref = gps_ifd[_GPS_LATITUDE_REF]
        lat_dms = gps_ifd[_GPS_LATITUDE]
        lon_ref = gps_ifd[_GPS_LONGITUDE_REF]
        lon_dms = gps_ifd[_GPS_LONGITUDE]
    except KeyError:
        return None

    try:
        latitude = _dms_to_decimal(lat_dms, lat_ref)
        longitude = _dms_to_decimal(lon_dms, lon_ref)
    except (TypeError, ValueError, ZeroDivisionError):
        return None

    return GpsFix(
        timestamp=timestamp,
        valid=True,
        latitude=latitude,
        longitude=longitude,
        speed_kmh=None,
        course=None,
    )


def normalize_photo_orientation(source: Path, destination: Path) -> bool:
    """Bake `source`'s own EXIF Orientation tag into its actual pixel
    data, writing the corrected image to `destination`. Returns True
    if a real rotation/flip was written to `destination`; False if
    the photo had no Orientation tag (or an already-normal one, value
    1), or couldn't even be opened by PIL - callers should render
    `source` itself unmodified in every False case, since
    `destination` isn't guaranteed to exist then.

    Exists because ffmpeg does not auto-rotate EXIF-oriented image
    input the way most photo viewers/OSes do - confirmed directly
    while building this (the exact `-loop 1`/`scale,pad` command
    `export/media.py`'s `render_image_as_video()` already uses decoded
    a portrait, Orientation=6 test JPEG as a raw, unrotated landscape
    frame). Left unfixed, a portrait phone photo dropped into a trip
    would render sideways (or upside down) in the exported clip with
    no warning at all. `PIL.ImageOps.exif_transpose()` is the actual
    fix: it rotates/flips the real pixels to match what the
    Orientation tag says, then strips the now-redundant tag so
    nothing downstream (ffmpeg included) could ever double-apply it.

    Same opportunistic, non-fatal failure handling as read_exif() -
    a HEIC/GPR source PIL can't open without the optional pillow-heif/
    rawpy plugins this project doesn't depend on, a corrupt file, or a
    format PIL simply doesn't recognize all just mean "nothing to
    normalize" here, not an error worth surfacing - the caller's
    existing ffmpeg render step still runs against the original file
    either way, exactly as it did before this module existed.
    """

    try:
        from PIL import Image
        from PIL import ImageOps
    except ImportError:
        return False

    try:
        with Image.open(source) as image:
            exif = image.getexif()
            orientation = exif.get(_TAG_ORIENTATION) if exif else None
            if orientation is None or orientation == 1:
                return False

            fixed = ImageOps.exif_transpose(image)
            if fixed is None:
                return False

            destination.parent.mkdir(parents=True, exist_ok=True)
            fixed.save(destination)
    except Exception:
        return False

    return True
