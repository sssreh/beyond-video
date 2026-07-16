"""
BlackVue archive.
"""

from pathlib import Path

from .archive_reader import ArchiveReader
from .recording import Recording


class Archive:
    """A BlackVue archive."""

    def __init__(self, path: str | Path):
        self._path = Path(path)
        self._recordings = ArchiveReader(self._path).read()

    def recordings(self) -> list[Recording]:
        """Return all recordings."""
        return self._recordings

    def __iter__(self):
        return iter(self._recordings)

    def __len__(self):
        return len(self._recordings)

    def __getitem__(self, index):
        return self._recordings[index]
