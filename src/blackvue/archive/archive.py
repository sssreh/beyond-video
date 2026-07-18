from __future__ import annotations

from pathlib import Path

from .archive_reader import ArchiveReader
from .configuration import Configuration
from .recording import Recording


class Archive:
    """A BlackVue archive."""

    def __init__(self, path: str | Path):
        self._path = Path(path)
        self._recordings = ArchiveReader(self._path).read()
        self._configurations = self._read_configurations()

    @property
    def recordings(self) -> list[Recording]:
        """Return all recordings."""
        return self._recordings

    @property
    def configurations(self) -> list[Configuration]:
        """Return all configuration snapshots."""
        return self._configurations

    def __iter__(self):
        return iter(self._recordings)

    def __len__(self):
        return len(self._recordings)

    def __getitem__(self, index):
        return self._recordings[index]

    def _read_configurations(self) -> list[Configuration]:
        """Read all configuration snapshots."""
        return sorted(
            (Configuration(path) for path in self._path.glob("*.config.ini")),
            key=lambda configuration: configuration.path.name,
        )
    