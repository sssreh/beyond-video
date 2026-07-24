from __future__ import annotations

from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from datetime import timedelta

from blackvue.archive.recording import Recording

# Defined here (not imported from trip_builder.py, which imports Trip
# from this module) to avoid a circular import - structurally the same
# callable shape as trip_builder.RecordingDuration, typically
# blackvue.generate.media.read_duration_seconds in practice.
RecordingDuration = Callable[[Recording], "int | None"]


@dataclass(frozen=True)
class Trip:
    recordings: tuple[Recording, ...]
    # Optional - see end_timestamp()'s own docstring for why this
    # exists and what it changes. Excluded from equality/hash
    # (compare=False) so two Trips built from the same recordings
    # still compare equal regardless of whether a duration lookup was
    # threaded through - comparing two Trips has never been about
    # which callable happened to be used to build them.
    recording_duration: RecordingDuration | None = field(
        default=None, compare=False
    )

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
        """The trip's real end: the last recording's own start
        timestamp, plus its real video span when known.

        `recording_duration` (typically
        blackvue.generate.media.read_duration_seconds, threaded
        through by TripBuilder.build() using the exact same callback
        it already takes for its own internal gap calculation - see
        TripBuilder._end_timestamp()) supplies that span. Falls back
        to just the last recording's own start timestamp - the
        original behavior - when `recording_duration` is None
        (a Trip built without one, e.g. directly in a test) or
        returns None for this trip's last recording specifically
        (no .duration.txt for it yet).

        Without this, end_timestamp (and therefore `duration` and
        `label`) never reflected a trip's real length at all - just
        the gap between its first and last recording's own start
        times. Most visible for a single-recording trip, whose
        end_timestamp was therefore always identical to its
        start_timestamp and `duration` always exactly zero, no matter
        how long the recording's real video actually was - confirmed
        confusing in bv-ls --trips output (Duration column reading
        0:00:00 against a real, many-minutes/500MB+ video) once trip
        detection started producing more single-recording trips (see
        trip_builder.recordings_with_front_video()). Also silently
        undercounted multi-recording trips by the same amount, just
        less visibly, since those already had a real gap between
        start and end.
        """

        if self.recording_duration is not None:
            duration_seconds = self.recording_duration(self.last_recording)
            if duration_seconds is not None:
                return self.last_recording.id.timestamp + timedelta(
                    seconds=duration_seconds
                )

        return self.last_recording.id.timestamp

    @property
    def duration(self) -> timedelta:
        return self.end_timestamp - self.start_timestamp

    @property
    def total_size(self) -> int:
        """Combined byte size of every asset (video/audio/GPS/g-sensor/
        etc.) across every recording in the trip - `Recording.size` is
        already accumulated per-asset by ArchiveReader as it reads the
        archive, so this is just a sum, not a fresh filesystem scan."""

        return sum(recording.size for recording in self.recordings)

    @property
    def has_parking_footage(self) -> bool:
        """Whether any recording in the trip is Parking-mode
        (RecordingId.kind == "P") - a normal-driving trip is the
        common case, so this is meant to flag the exceptional one
        rather than be shown unconditionally either way (see
        trip_info.py's own use of it)."""

        return any(recording.id.is_parking for recording in self.recordings)

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
    