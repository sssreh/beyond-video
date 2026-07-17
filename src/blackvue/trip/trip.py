from __future__ import annotations

from dataclasses import dataclass

from blackvue.archive.recording import Recording


@dataclass(frozen=True)
class Trip:
    recordings: tuple[Recording, ...]

    @property
    def start_timestamp(self):
        return self.recordings[0].recording_id.timestamp

    @property
    def end_timestamp(self):
        return self.recordings[-1].recording_id.timestamp
