"""
BlackVue archive asset file.
"""

from dataclasses import dataclass
from pathlib import Path

from .asset import Asset


@dataclass(frozen=True)
class AssetFile:
    """A file containing an asset."""

    asset: Asset
    path: Path

    @property
    def name(self) -> str:
        """Return the filename."""

        return self.path.name

    def __str__(self) -> str:
        return self.name
