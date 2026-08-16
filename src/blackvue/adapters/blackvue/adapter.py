"""
BlackVueAdapter - the "blackvue" CameraAdapter.

Deliberately a pure delegation wrapper: every method forwards straight to
the existing core/parser/telemetry/archive code this project has always
used, with zero behavior change. That's the point, not a limitation - per
docs/CAMERA_ADAPTERS.md's "Suggested next steps": "if BlackVueAdapter can
be built as a pure delegation layer with zero behavior change, the
interface is right." This class is that validation, not a rewrite of
anything.

STATUS: implemented and registered (see registry.py), but not yet called
from any real bv-* command or bv-web route - see base.py's own docstring
for what's still queued (wiring bv-ls and the archive browser through
this, per docs/CAMERA_ADAPTERS.md).
"""

from __future__ import annotations

from pathlib import Path

from ...archive.archive import Archive
from ...archive.archive_reader import ArchiveReader
from ...archive.configuration import parse_record_time_seconds
from ...archive.recording import Recording
from ...archive.recording_id import RecordingId
from ...core.connection import connect as _connect
from ...core.endpoint import Endpoint
from ...telemetry.gps_reader import GpsFix
from ...telemetry.gps_reader import read_gps as _read_gps_file
from ...telemetry.gsensor_reader import GSensorSample
from ...telemetry.gsensor_reader import read_gsensor as _read_gsensor_file
from ..base import AdapterCapabilityError
from ..manifest import load_manifest

_MANIFEST_PATH = Path(__file__).parent / "manifest.json"
_MANIFEST = load_manifest(_MANIFEST_PATH)


class BlackVueAdapter:
    """The "blackvue" CameraAdapter - see module docstring."""

    manifest = _MANIFEST

    def open_archive(self, path: Path) -> Archive:
        """Delegates to archive.Archive(path) unchanged - same flat
        scandir()-based scan (ArchiveReader) and same RecordTime-snapshot
        lookup (Configuration) every bv-* command already uses."""

        return Archive(path)

    def find_recording(self, path: Path, recording_id: RecordingId) -> Recording | None:
        """Delegates to ArchiveReader.read_recording() unchanged - the
        same fixed-stat-count targeted lookup bv-web's archive browser
        already relies on for thumbnail/video-serving performance (see
        base.py's own docstring)."""

        return ArchiveReader(path).read_recording(recording_id)

    def read_gps(self, path: Path) -> tuple[GpsFix, ...]:
        """Delegates to telemetry.gps_reader.read_gps() unchanged."""

        return _read_gps_file(path)

    def read_gsensor(self, path: Path) -> tuple[GSensorSample, ...]:
        """Delegates to telemetry.gsensor_reader.read_gsensor()
        unchanged."""

        return _read_gsensor_file(path)

    def connect(
        self, endpoints: list[Endpoint], timeout: int = 5
    ) -> tuple[Endpoint, object]:
        """Delegates to core.connection.connect() unchanged."""

        if not self.manifest.supports("network_connect"):
            raise AdapterCapabilityError(
                f"{self.manifest.adapter_id} adapter's manifest does not "
                "declare network_connect support"
            )

        return _connect(endpoints, timeout=timeout)

    def config_snapshot_seconds(self, config_text: str) -> int:
        """Delegates to archive.configuration.parse_record_time_seconds()
        unchanged."""

        if not self.manifest.supports("config_snapshot"):
            raise AdapterCapabilityError(
                f"{self.manifest.adapter_id} adapter's manifest does not "
                "declare config_snapshot support"
            )

        return parse_record_time_seconds(config_text)
