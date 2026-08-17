"""
Adapter-aware GPS/g-sensor reading for a single Recording.

Bridges the declarative manifest fields (gps_source_asset/
gsensor_source_asset, see manifest.py) to a CameraAdapter's
read_gps()/read_gsensor() methods, so pipeline code (trip_export.py,
search.py, telemetry/movement.py, web/archive_browser.py) can read a
recording's telemetry without knowing whether it lives in a separate
sidecar file (BlackVue's .gps/.3gf) or is embedded in the video itself
(a future adapter's GPMF-style stream) - both are just "the file at
gps_source_asset", read through whichever adapter is active.

STATUS: introduced as part of rewiring the GPS/g-sensor pipeline
through CameraAdapter (see docs/CAMERA_ADAPTERS.md) - previously every
one of this module's callers read BlackVue's .gps/.3gf sidecars
directly via telemetry.gps_reader/gsensor_reader, bypassing the
adapter abstraction entirely, even for BlackVue itself.

resolve_recording_gps_span() (added alongside cli/bv_ls.py's GPS
column and telemetry/movement.py's --gps-split force-split check) goes
one step further than the read_recording_gps()/read_recording_gsensor()
functions above: it composes a real adapter read with the EXIF (photo)/
container-tag (video) fallback archive/exif.py and archive/container_gps.py
provide for a recording with no real telemetry source at all - see its
own docstring for the exact fallback order and the real report that
motivated it.
"""

from __future__ import annotations

from ..archive.asset import Asset
from ..archive.container_gps import container_location_fix
from ..archive.exif import exif_gps_fix
from ..archive.photo import recording_is_photo
from ..archive.recording import Recording
from ..generate.media import MediaToolError
from ..telemetry.gps_reader import GpsFix
from ..telemetry.gsensor_reader import GSensorSample
from .base import CameraAdapter


def recording_has_gps(adapter: CameraAdapter, recording: Recording) -> bool:
    """True if `recording` has a file adapter.read_gps() could be
    asked to parse - a cheap existence check (no file read/parse) for
    callers that just want to know "is there a GPS log at all" before
    committing to a real read, e.g. bv-web's archive detail page
    deciding whether to show a "no GPS log" message versus attempting
    the location lookup at all."""

    if not adapter.manifest.supports("gps"):
        return False

    asset_name = adapter.manifest.gps_source_asset
    if asset_name is None:
        return False

    return recording.file(Asset[asset_name]) is not None


def recording_has_gsensor(adapter: CameraAdapter, recording: Recording) -> bool:
    """Same as recording_has_gps(), for g-sensor data."""

    if not adapter.manifest.supports("gsensor"):
        return False

    asset_name = adapter.manifest.gsensor_source_asset
    if asset_name is None:
        return False

    return recording.file(Asset[asset_name]) is not None


def read_recording_gps(adapter: CameraAdapter, recording: Recording) -> tuple[GpsFix, ...]:
    """Return `recording`'s GPS fixes via `adapter`, or `()` if this
    adapter has no GPS capability, its manifest doesn't declare a
    gps_source_asset, this recording has no file for that asset, or
    the file exists but fails to parse (MediaToolError) - the same
    "missing/bad telemetry is not fatal, just absent" contract every
    direct read_gps() call site already had before this rewire.
    """

    if not adapter.manifest.supports("gps"):
        return ()

    asset_name = adapter.manifest.gps_source_asset
    if asset_name is None:
        return ()

    asset_file = recording.file(Asset[asset_name])
    if asset_file is None:
        return ()

    try:
        return adapter.read_gps(asset_file.path)
    except MediaToolError:
        return ()


def read_recording_gsensor(adapter: CameraAdapter, recording: Recording) -> tuple[GSensorSample, ...]:
    """Same as read_recording_gps(), for g-sensor samples via
    adapter.read_gsensor() / manifest.gsensor_source_asset."""

    if not adapter.manifest.supports("gsensor"):
        return ()

    asset_name = adapter.manifest.gsensor_source_asset
    if asset_name is None:
        return ()

    asset_file = recording.file(Asset[asset_name])
    if asset_file is None:
        return ()

    try:
        return adapter.read_gsensor(asset_file.path)
    except MediaToolError:
        return ()


def resolve_recording_gps_span(
    adapter: CameraAdapter, recording: Recording
) -> tuple[GpsFix | None, GpsFix | None]:
    """Return (start_fix, end_fix) - the best available GPS position(s)
    for `recording`, real telemetry preferred, falling back to a still
    photo's EXIF GPS tag or a video's own ISO 6709 container `location`
    tag when there's no real fix at all.

    Real telemetry: the first and last of read_recording_gps()'s valid,
    positioned fixes - a genuine start/end pair, same "valid" and
    "positioned" definition web/archive_browser.py's
    first_valid_gps_fix()/last_valid_gps_fix() already use for the
    archive detail page's "Show start and stop location" link.

    Fallback: exif_gps_fix() (photos) or container_location_fix()
    (videos) on the recording's FRONT file - both single-point reads
    (a photo captures one instant; a container location tag is one
    static point, not a track), so the same fix is returned as both
    `start_fix` and `end_fix` - matching web/app.py's own
    archive_recording_location() route ("the same fix serves as both
    start and stop"). Reached whenever real telemetry comes up empty,
    not just when recording_has_gps() is False outright - see
    cli/bv_ls.py's own _recording_gps_available() docstring for why a
    GoPro-adapter recording that declares gps support but has no real
    GPMF track (a stock/downloaded clip mixed into the archive) needs
    this same fallback, not just a "no telemetry source at all" one.

    (None, None) if nothing at all is available (no FRONT file, no
    EXIF/container-tag data, no real telemetry). A caller that only
    wants a single best-available fix can just take `start_fix`
    (`end_fix` is the same value for the fallback case, and a genuine
    second data point only for real telemetry).

    This is a real per-recording probe when it falls through to the
    EXIF/container-tag path (a Pillow read and/or an ffprobe
    subprocess), not a free check - every caller of this function
    should already know it's opting into that cost (see cli/bv_ls.py's
    GPS column and telemetry/movement.py's --gps-split for the two
    current callers, both of which document the tradeoff at their own
    call sites).
    """

    if recording_has_gps(adapter, recording):
        fixes = [
            fix
            for fix in read_recording_gps(adapter, recording)
            if fix.valid and fix.latitude is not None and fix.longitude is not None
        ]
        if fixes:
            return fixes[0], fixes[-1]

    front = recording.file(Asset.FRONT)
    if front is None:
        return None, None

    fallback_fix = None
    if recording_is_photo(recording):
        fallback_fix = exif_gps_fix(front.path, timestamp=recording.id.timestamp)

    if fallback_fix is None:
        fallback_fix = container_location_fix(
            front.path, timestamp=recording.id.timestamp
        )

    return fallback_fix, fallback_fix
