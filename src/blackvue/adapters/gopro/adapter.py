"""
GoProAdapter - the "gopro" CameraAdapter.

A recursive folder of GoPro clips, scanned exactly the same way
FolderAdapter scans a plain video folder - see adapters/_recursive_scan.py's
own docstring for why that logic is shared rather than duplicated (per
Christer's steer on the open design question from this adapter's own
design pass: "Share them as long as it is possible, in worst case you
make a branch later"). The one real difference from FolderAdapter:
GoPro embeds its own GPS/g-sensor telemetry directly in each video's
GPMF ('gpmd') track (adapters/gopro/gpmf.py), rather than having no
telemetry at all - so read_gps()/read_gsensor() here actually do
something, gated by that video's own embedded stream rather than a
manifest capability declared False.

Per-recording degradation, not all-or-nothing: a real GoPro SD card or
export folder is realistically a mix of everything - clean GPMF-shaped
clips, clips with GPS lock lost for a stretch or no GPMF track at all
(re-encoded, trimmed by another tool, or just an older firmware),
photos and screenshots (already excluded by video_extensions matching,
same as FolderAdapter). A video file that scans fine but has no
parseable GPMF stream must not break the whole archive scan - it
should behave exactly like a FolderAdapter recording for that one
file: present, playable, just with no GPS/g-sensor track. This falls
out for free from the existing contract rather than needing special
handling here: read_gps()/read_gsensor() raise MediaToolError for that
one file (gpmf.py's own contract - see its module docstring), and
telemetry_bridge.py's read_recording_gps()/read_recording_gsensor()
already catch MediaToolError and return () rather than propagating it
(see that module's own docstring: "missing/bad telemetry is not
fatal, just absent" - the exact same contract every other adapter's
telemetry already gets). open_archive() itself never touches GPMF at
all - only reading a specific recording's telemetry does - so a
mixed-content folder scans in full regardless of how many of its
clips turn out to have no usable GPMF stream.
"""

from __future__ import annotations

from pathlib import Path

from ...archive.archive import Archive
from ...archive.recording import Recording
from ...archive.recording_id import RecordingId
from ...core.endpoint import Endpoint
from ...telemetry.gps_reader import GpsFix
from ...telemetry.gsensor_reader import GSensorSample
from .._recursive_scan import find_recording_in_recursive_archive
from .._recursive_scan import scan_recursive_archive
from ..base import AdapterCapabilityError
from ..manifest import load_manifest
from . import gpmf

_MANIFEST_PATH = Path(__file__).parent / "manifest.json"
_MANIFEST = load_manifest(_MANIFEST_PATH)

# The single kind/direction code manifest.json's kind_vocabulary and
# direction_vocabulary both declare - see FolderAdapter's own module
# docstring for the same convention.
_KIND_CODE = "V"


class GoProAdapter:
    """The "gopro" CameraAdapter - see module docstring."""

    manifest = _MANIFEST

    def open_archive(self, path: Path) -> Archive:
        return scan_recursive_archive(path, self.manifest, _KIND_CODE)  # type: ignore[return-value]

    def find_recording(self, path: Path, recording_id: RecordingId) -> Recording | None:
        """Resolve a single recording by id - see
        _recursive_scan.find_recording_in_recursive_archive()'s own
        docstring for why this is a full rescan filtered by id rather
        than a targeted lookup."""

        return find_recording_in_recursive_archive(
            path, recording_id, self.manifest, _KIND_CODE
        )

    def read_gps(self, path: Path) -> tuple[GpsFix, ...]:
        """Parse the GPS5 stream embedded in this video's own GPMF
        track - `path` is the video file itself (gps_source_asset is
        "FRONT" in manifest.json, not a separate sidecar). Raises
        MediaToolError for a video with no GPMF track, no GPS5 stream,
        or an unparseable one - see gpmf.py's own module docstring;
        telemetry_bridge.py absorbs that into "no telemetry for this
        recording" for every caller (see this module's own docstring).
        """

        return gpmf.read_gps(path)

    def read_gsensor(self, path: Path) -> tuple[GSensorSample, ...]:
        """Parse the ACCL stream embedded in this video's own GPMF
        track - same per-file contract as read_gps() above."""

        return gpmf.read_gsensor(path)

    def connect(
        self, endpoints: list[Endpoint], timeout: int = 5
    ) -> tuple[Endpoint, object]:
        raise AdapterCapabilityError(
            f"{self.manifest.adapter_id} adapter's manifest does not "
            "declare network_connect support"
        )

    def config_snapshot_seconds(self, config_text: str) -> int:
        raise AdapterCapabilityError(
            f"{self.manifest.adapter_id} adapter's manifest does not "
            "declare config_snapshot support"
        )
