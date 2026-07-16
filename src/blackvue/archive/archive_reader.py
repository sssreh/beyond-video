"""
BlackVue archive reader.
"""

from pathlib import Path

from .asset import Asset
from .asset_file import AssetFile
from .recording import Recording
from .recording_id import RecordingId


class ArchiveReader:
    """Read a BlackVue archive."""

    ASSETS = (
        ("F.mp4", Asset.FRONT),
        ("R.mp4", Asset.REAR),
        (".gps", Asset.GPS),
        (".3gf", Asset.GSENSOR),
        ("F.thm", Asset.FRONT_THUMBNAIL),
        ("R.thm", Asset.REAR_THUMBNAIL),
        (".aac", Asset.AUDIO),
        (".gpx", Asset.GPX),
    )

    def __init__(self, path: Path):
        self._path = path

    def read(self) -> list[Recording]:
        """Read the archive."""

        recordings: dict[RecordingId, Recording] = {}

        for path in sorted(self._path.iterdir()):

            if not path.is_file():
                continue

            recording_id = RecordingId.parse(path.name)
            if recording_id is None:
                continue

            asset = self._detect_asset(path.name)
            if asset is None:
                continue

            recording = recordings.setdefault(
                recording_id,
                Recording(recording_id),
            )

            recording.assets[asset] = AssetFile(
                asset=asset,
                path=path,
            )

        return sorted(recordings.values(), key=lambda r: r.id)

    @classmethod
    def _detect_asset(cls, filename: str) -> Asset | None:
        """Return the asset represented by the filename."""

        for suffix, asset in cls.ASSETS:
            if filename.endswith(suffix):
                return asset

        return None
