from __future__ import annotations

from configparser import ConfigParser
from pathlib import Path


class Configuration:
    """Camera configuration loaded from a config.ini snapshot."""

    def __init__(self, path: str | Path):
        self._path = Path(path)

        self._parser = ConfigParser()
        self._parser.read(self._path, encoding="utf-8")

    @property
    def path(self) -> Path:
        """Return the configuration file."""
        return self._path

    @property
    def record_time(self) -> int:
        """
        Return the recording duration in seconds.

        Raises
        ------
        KeyError
            If RecordTime is missing.
        ValueError
            If RecordTime is not an integer.
        """
        try:
            return int(self._parser["System"]["RecordTime"])
        except KeyError as ex:
            raise KeyError(
                f"{self._path}: missing System/RecordTime"
            ) from ex
