"""
BlackVue recording.
"""

from dataclasses import dataclass, field

from .asset import Asset
from .asset_file import AssetFile
from .recording_id import RecordingId


@dataclass
class Recording:
    """A BlackVue recording."""

    id: RecordingId
    assets: dict[Asset, AssetFile] = field(default_factory=dict)
    size: int = 0
    # Whether `id`'s embedded timestamp reflects this recording's own
    # real capture/creation moment, as opposed to whenever some later
    # file operation (copy, download, re-encode) happened to touch it.
    #
    # Always True for a BlackVue archive - the timestamp is encoded in
    # the camera's own filename, always real device-clock data. A
    # recursive-folder scan (FolderAdapter/GoProAdapter, see
    # adapters/_recursive_scan.py's _resolve_timestamp()) sets this
    # False when it had to fall all the way back to file mtime because
    # no telemetry, EXIF, or container creation_time was available -
    # mtime reflects when a file was last *written*, not when it was
    # recorded, so for a batch-copied or re-encoded clip it can land
    # arbitrarily close to an unrelated clip's own mtime purely by
    # coincidence of when the copy happened. A confirmed real case
    # from Christer: several stock/sample test-fixture clips with no
    # embedded timestamp of any kind landed within a second of each
    # other by mtime and were about to be silently grouped into one
    # trip by TripBuilder, despite having nothing to do with each
    # other. TripBuilder (see trip/trip_builder.py's build()) checks
    # this via getattr(..., True) and force-splits around any
    # recording where it's False, on either side, never offering the
    # gap to `bridge` - there's no real time evidence for movement
    # bridging to weigh in on when the gap measurement itself might be
    # meaningless.
    timestamp_reliable: bool = True

    def has(self, asset: Asset) -> bool:
        """Return True if the recording contains the asset."""
        return asset in self.assets

    def file(self, asset: Asset) -> AssetFile | None:
        """Return the asset file or None."""
        return self.assets.get(asset)

    def ordered_assets(self):
        """Iterate over assets in display order."""
        for asset in Asset.display_order():
            if asset in self.assets:
                yield self.assets[asset]

    def add_size(self, size: int) -> None:
        """Accumulate asset size."""
        self.size += size

    def __contains__(self, asset: Asset) -> bool:
        return asset in self.assets

    def __len__(self) -> int:
        return len(self.assets)

    def __str__(self) -> str:
        return str(self.id)
    