"""
BlackVue archive package.

Deliberately NOT re-exported here: container_gps.container_location_fix
and exif.exif_gps_fix (callers import them directly from their own
submodules). Both ultimately import telemetry.gps_reader, which itself
imports generate.media - re-exporting either one from this __init__
would make importing generate.media (which imports archive.asset,
triggering this file) circle straight back into this
still-mid-execution module and fail with a "partially initialized
module" ImportError. Confirmed the hard way: registering
container_location_fix here once broke `import blackvue.generate.media`
and `import blackvue.telemetry.gps_reader` as anyone's first import in
a fresh interpreter - exif_gps_fix was never added here in the first
place, which is why it never surfaced this until container_gps.py did.
"""

from .archive import Archive
from .archive_reader import ArchiveReader
from .asset import Asset
from .asset_file import AssetFile
from .configuration import Configuration
from .photo import DEFAULT_PHOTO_DURATION_SECONDS
from .photo import GIF_EXTENSIONS
from .photo import PHOTO_EXTENSIONS
from .photo import count_gif_frames
from .photo import is_gif_path
from .photo import is_photo_path
from .photo import recording_is_photo
from .recording import Recording
from .recording_id import RecordingId

__all__ = [
    "Archive",
    "ArchiveReader",
    "Asset",
    "AssetFile",
    "Configuration",
    "DEFAULT_PHOTO_DURATION_SECONDS",
    "GIF_EXTENSIONS",
    "PHOTO_EXTENSIONS",
    "Recording",
    "RecordingId",
    "count_gif_frames",
    "is_gif_path",
    "is_photo_path",
    "recording_is_photo",
]
