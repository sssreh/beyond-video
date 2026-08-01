from __future__ import annotations

from configparser import ConfigParser
from pathlib import Path

from .recording_id import RecordingId

# Snapshot files live at the archive root as
# "<recording_id>.record_time.txt", holding nothing but the derived
# RecordTime in seconds - never a copy of the raw config.ini. The
# camera's config.ini also carries Wi-Fi/cloud credentials (SSIDs,
# "encrypted" passwords of unverified strength) - see
# WORKING_CONTEXT.md's RecordTime entry for the real file Christer
# shared and why persisting it verbatim into the archive was rejected.
# parse_record_time_seconds() below is the only thing ever allowed to
# read the raw file/response text, and it returns just this one int.
RECORD_TIME_SUFFIX = ".record_time.txt"


class ConfigurationError(Exception):
    """Raised when a config.ini's RecordTime can't be parsed."""


def parse_record_time_seconds(text: str) -> int:
    """Parse the RecordTime field (minutes, in the [Tab1] section) out
    of a raw config.ini's text and return it in seconds.

    This is the only function in this module allowed to see the full
    raw config.ini text - it reads out RecordTime and nothing else,
    and the text itself is never written to disk anywhere (see
    write_record_time_snapshot()). interpolation=None disables
    ConfigParser's "%" interpolation entirely - config.ini has no use
    for it, and it removes a class of parse errors an unrelated field
    (a Wi-Fi password, userString, etc.) could otherwise trigger.
    """

    parser = ConfigParser(interpolation=None)
    parser.read_string(text)

    try:
        return int(parser["Tab1"]["RecordTime"]) * 60
    except (KeyError, ValueError) as exc:
        raise ConfigurationError(
            f"couldn't find a valid RecordTime in [Tab1]: {exc}"
        ) from exc


def write_record_time_snapshot(
    destination: Path,
    recording_id: str,
    record_time_seconds: int,
) -> Path:
    """Write a RecordTime snapshot for `recording_id` into `destination`
    (an archive root) - just the derived integer in seconds, matching
    the plain "<value>\\n" format .duration.txt already uses. Never
    the raw config.ini it came from - see this module's own docstring/
    RECORD_TIME_SUFFIX comment for why.
    """

    path = destination / f"{recording_id}{RECORD_TIME_SUFFIX}"
    path.write_text(f"{record_time_seconds}\n", encoding="utf-8")
    return path


def read_record_time_snapshot(path: Path) -> int | None:
    """Read a previously-written RecordTime snapshot file. Returns
    None if it can't be read or parsed, the same "just don't crash"
    handling read_duration_seconds() gives a bad .duration.txt."""

    try:
        return int(path.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return None


class Configuration:
    """One RecordTime snapshot: the camera's configured recording
    segment length as of a specific recording, derived from a
    config.ini response - never the raw config.ini itself (see this
    module's own docstring)."""

    TOLERANCE = 10

    def __init__(
        self,
        path: str | Path,
        *,
        record_time: int | None = None,
    ):
        self._path = Path(path)

        if record_time is None:
            self._record_time = parse_record_time_seconds(
                self._path.read_text(encoding="utf-8")
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
        stem = self._path.name.removesuffix(RECORD_TIME_SUFFIX)
        return RecordingId(stem)

    @property
    def record_time(self) -> int:
        """Return the nominal recording duration in seconds."""
        return self._record_time

    @property
    def maximum_gap(self) -> int:
        """Return the maximum allowed gap to the next recording."""
        return self.record_time + self.TOLERANCE
    