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
"""

from __future__ import annotations

from ..archive.asset import Asset
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
