from __future__ import annotations

from pathlib import Path

from .archive_reader import ArchiveReader
from .configuration import RECORD_TIME_SUFFIX
from .configuration import Configuration
from .configuration import read_record_time_snapshot
from .recording import Recording


class Archive:
    """A BlackVue archive."""

    def __init__(self, path: str | Path):
        self._path = Path(path)
        self._recordings = ArchiveReader(self._path).read()
        self._configurations = self._read_configurations()
        self._warned_missing_configuration = False

    @property
    def recordings(self) -> list[Recording]:
        """Return all recordings."""
        return self._recordings

    @property
    def configurations(self) -> list[Configuration]:
        """Return all configuration snapshots."""
        return self._configurations

    def configuration(self, recording: Recording) -> Configuration:
        """Return the configuration active for a recording."""

        configuration = None

        for candidate in self._configurations:
            if candidate.recording_id <= recording.id:
                configuration = candidate
            else:
                break

        if configuration is None:
            if not self._warned_missing_configuration:
                print(
                    "Warning: archive contains no configuration snapshot. "
                    "Using fallback RecordTime of 300 seconds."
                )
                self._warned_missing_configuration = True

            return Configuration.fallback()

        return configuration

    def __iter__(self):
        return iter(self._recordings)

    def __len__(self):
        return len(self._recordings)

    def __getitem__(self, index):
        return self._recordings[index]

    def _read_configurations(self) -> list[Configuration]:
        """Read all RecordTime snapshots (see configuration.py's own
        module docstring - "<recording_id>.record_time.txt" files,
        written by bv-download, never a raw config.ini copy).

        A snapshot that fails to parse (read_record_time_snapshot()
        returning None - corrupt or hand-edited) is skipped rather
        than raising, so one bad file can't break configuration()
        lookups for the whole archive.
        """

        configurations = []

        for path in self._path.glob(f"*{RECORD_TIME_SUFFIX}"):
            record_time = read_record_time_snapshot(path)
            if record_time is None:
                continue

            configurations.append(Configuration(path, record_time=record_time))

        return sorted(
            configurations,
            key=lambda configuration: configuration.recording_id,
        )
    