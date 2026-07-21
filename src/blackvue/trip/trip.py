from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from datetime import timedelta

from blackvue.archive.recording import Recording


@dataclass(frozen=True)
class Trip:
    recordings: tuple[Recording, ...]

    def __iter__(self) -> Iterator[Recording]:
        return iter(self.recordings)

    def __len__(self) -> int:
        return len(self.recordings)

    @property
    def is_single_recording(self) -> bool:
        return len(self) == 1

    @property
    def first_recording(self) -> Recording:
        return self.recordings[0]

    @property
    def last_recording(self) -> Recording:
        return self.recordings[-1]

    @property
    def start_timestamp(self):
        return self.first_recording.id.timestamp

    @property
    def end_timestamp(self):
        return self.last_recording.id.timestamp

    @property
    def duration(self) -> timedelta:
        return self.end_timestamp - self.start_timestamp

    @property
    def label(self) -> str:
        """Return the trip_<start>_<end> label used both in bv-ls's
        --trips listing and (as a suffix, optionally prefixed) in
        bv-export's generated folder names."""

        return (
            "trip_"
            f"{self.start_timestamp:%Y%m%d_%H%M%S}_"
            f"{self.end_timestamp:%Y%m%d_%H%M%S}"
        )
    