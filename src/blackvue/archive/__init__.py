"""
BlackVue archive package.
"""

from .archive import Archive
from .archive_reader import ArchiveReader
from .asset import Asset
from .asset_file import AssetFile
from .configuration import Configuration
from .photo import DEFAULT_PHOTO_DURATION_SECONDS
from .photo import PHOTO_EXTENSIONS
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
    "PHOTO_EXTENSIONS",
    "Recording",
    "RecordingId",
    "is_photo_path",
    "recording_is_photo",
]
