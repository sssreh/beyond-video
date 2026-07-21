from __future__ import annotations

from collections.abc import Callable, Iterable
from datetime import datetime, timedelta

from blackvue.archive.recording import Recording
from blackvue.trip.trip import Trip


DEFAULT_MAX_GAP = timedelta(minutes=10)

# Small fixed safety margin added on top of max_gap before a gap counts
# as a split. Exists to absorb measurement noise that has nothing to
# do with whether the vehicle actually stopped: .duration.txt is
# rounded to the nearest second (see compute_span), recording
# timestamps come from filenames with only 1-second resolution, and
# real dashcams take a moment to close one file and open the next
# even during genuinely continuous recording. None of that should be
# mistaken for a real gap. It's not a trip-detection knob the way
# max_gap is - just noise-absorption - so it defaults on rather than
# being opt-in like `bridge`/`recording_duration`.
DEFAULT_GAP_TOLERANCE = timedelta(seconds=10)

Bridge = Callable[[Recording, Recording], bool]
RecordingDuration = Callable[[Recording], "int | None"]


class TripBuilder:
    """Groups recordings into trips.

    The primary rule is a time gap: consecutive recordings more than
    `max_gap` (plus `gap_tolerance`, a small fixed noise margin - see
    DEFAULT_GAP_TOLERANCE) apart start a new trip. The gap is measured
    from the *end* of the earlier recording where possible, not just
    its start - see `recording_duration` below.

    An optional `bridge` callback can override the gap rule for a
    specific gap: if `bridge(previous, current)` returns True for a
    gap that would otherwise split the trip, the two recordings are
    kept in the same trip anyway. `bridge` is only ever consulted
    when the (duration-adjusted) gap rule would split - it never
    forces a split on its own.

    An optional `recording_duration` callback returns a recording's
    real-world length in seconds (typically backed by its
    `.duration.txt` file - see
    `blackvue.generate.media.read_duration_seconds`), or None if
    unknown. When known, the gap to the *next* recording is measured
    from `previous.timestamp + duration` instead of bare
    `previous.timestamp` - i.e. the duration is folded in before the
    result is ever compared against `max_gap`. This matters most for
    long recordings (Parking-mode timelapses in particular, where the
    played-back file length is nothing like the real elapsed time):
    without it, a recording that's itself longer than `max_gap` can
    look like a gap to the *next* recording even when there was no
    real gap at all. A recording with no known duration falls back to
    its raw start timestamp, so this is backward compatible one
    recording at a time, not just when unset entirely.

    Passing neither `bridge` nor `recording_duration`, and leaving
    `gap_tolerance` at its default, reproduces the original pure
    start-to-start-gap behaviour for any max_gap realistically used
    (minutes, not single-digit seconds) - pass `gap_tolerance=
    timedelta(0)` for the literal old behaviour at any max_gap.
    """

    def __init__(
        self,
        max_gap: timedelta = DEFAULT_MAX_GAP,
        *,
        bridge: Bridge | None = None,
        recording_duration: RecordingDuration | None = None,
        gap_tolerance: timedelta = DEFAULT_GAP_TOLERANCE,
    ):
        self.max_gap = max_gap
        self.bridge = bridge
        self.recording_duration = recording_duration
        self.gap_tolerance = gap_tolerance

    def _end_timestamp(self, recording: Recording) -> datetime:
        if self.recording_duration is not None:
            duration_seconds = self.recording_duration(recording)
            if duration_seconds is not None:
                return recording.id.timestamp + timedelta(
                    seconds=duration_seconds
                )

        return recording.id.timestamp

    def build(self, recordings: Iterable[Recording]) -> list[Trip]:
        recordings = tuple(recordings)

        if not recordings:
            return []

        trips: list[Trip] = []

        current_trip: list[Recording] = [recordings[0]]

        for recording in recordings[1:]:
            previous = current_trip[-1]

            gap = recording.id.timestamp - self._end_timestamp(previous)

            if gap > self.max_gap + self.gap_tolerance and not (
                self.bridge and self.bridge(previous, recording)
            ):
                trips.append(Trip(tuple(current_trip)))
                current_trip = [recording]
            else:
                current_trip.append(recording)

        trips.append(Trip(tuple(current_trip)))

        return trips
    