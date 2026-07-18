from __future__ import annotations

from configparser import ConfigParser
from pathlib import Path

from .recording_id import RecordingId


class Configuration:
    """Camera configuration loaded from a config.ini snapshot."""

    TOLERANCE = 10

    def __init__(
        self,
        path: str | Path,
        *,
        record_time: int | None = None,
    ):
        self._path = Path(path)

        if record_time is None:
            parser = ConfigParser()
            parser.read(self._path, encoding="utf-8")

            self._record_time = (
                int(parser["Tab1"]["RecordTime"]) * 60
            )
        else:
            self._record_time = record_time

    @classmethod
    def fallback(cls) -> "Configuration":
        """Return a fallback configuration."""

        return cls(
            "<fallback>",
            record_time=300,
        )

    @property
    def path(self) -> Path:
        """Return the configuration file."""
        return self._path

    @property
    def recording_id(self) -> RecordingId:
        """Return the recording at which this configuration became active."""
        stem = self._path.name.removesuffix(".config.ini")
        return RecordingId(stem)

    @property
    def record_time(self) -> int:
        """Return the nominal recording duration in seconds."""
        return self._record_time

    @property
    def maximum_gap(self) -> int:
        """Return the maximum allowed gap to the next recording."""
        return self.record_time + self.TOLERANCE
    