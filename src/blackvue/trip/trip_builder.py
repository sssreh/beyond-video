from __future__ import annotations

from datetime import timedelta

from blackvue.archive.recording import Recording
from blackvue.trip.trip import Trip


class TripBuilder:
    def __init__(self, max_gap: timedelta = timedelta(minutes=10)):
        self.max_gap = max_gap

    def build(self, recordings: list[Recording]) -> list[Trip]:
        if not recordings:
            return []

        trips: list[Trip] = []

        current_trip: list[Recording] = [recordings[0]]

        for recording in recordings[1:]:
            previous = current_trip[-1]

            gap = recording.recording_id.timestamp - previous.recording_id.timestamp

            if gap > self.max_gap:
                trips.append(Trip(tuple(current_trip)))
                current_trip = [recording]
            else:
                current_trip.append(recording)

        trips.append(Trip(tuple(current_trip)))

        return trips
    