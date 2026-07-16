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

    def __contains__(self, asset: Asset) -> bool:
        return asset in self.assets

    def __len__(self) -> int:
        return len(self.assets)

    def __str__(self) -> str:
        return str(self.id)
