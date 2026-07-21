from datetime import datetime

from blackvue.archive.recording import Recording
from blackvue.archive.recording_id import RecordingId
from blackvue.trip.trip import Trip


def ts(value: str) -> datetime:
    return datetime.strptime(value, "%Y-%m-%d %H:%M:%S")


class FakeRecordingId:
    def __init__(self, timestamp: datetime):
        self.timestamp = timestamp


class FakeRecording:
    def __init__(self, timestamp: datetime):
        self.id = FakeRecordingId(timestamp)


def test_trip_start_and_end_timestamp():
    trip = Trip(
        (
            FakeRecording(ts("2026-07-15 10:00:00")),
            FakeRecording(ts("2026-07-15 10:05:00")),
        )
    )

    assert trip.start_timestamp == ts("2026-07-15 10:00:00")
    assert trip.end_timestamp == ts("2026-07-15 10:05:00")


def test_trip_first_and_last_recording():
    first = FakeRecording(ts("2026-07-15 10:00:00"))
    last = FakeRecording(ts("2026-07-15 10:05:00"))

    trip = Trip((first, last))

    assert trip.first_recording is first
    assert trip.last_recording is last


def test_trip_length():
    trip = Trip(
        (
            FakeRecording(ts("2026-07-15 10:00:00")),
            FakeRecording(ts("2026-07-15 10:05:00")),
        )
    )

    assert len(trip) == 2


def test_trip_is_iterable():
    first = FakeRecording(ts("2026-07-15 10:00:00"))
    last = FakeRecording(ts("2026-07-15 10:05:00"))

    trip = Trip((first, last))

    assert list(trip) == [first, last]


def test_single_recording_trip():
    trip = Trip((FakeRecording(ts("2026-07-15 10:00:00")),))

    assert trip.is_single_recording


def test_multiple_recording_trip():
    trip = Trip(
        (
            FakeRecording(ts("2026-07-15 10:00:00")),
            FakeRecording(ts("2026-07-15 10:05:00")),
        )
    )

    assert not trip.is_single_recording


def test_trip_label_formats_start_and_end_ids():
    trip = Trip(
        (
            FakeRecording(ts("2026-07-15 13:34:58")),
            FakeRecording(ts("2026-07-15 14:12:35")),
        )
    )

    assert trip.label == "trip_20260715_133458_20260715_141235"


def test_trip_works_against_a_real_recording_not_just_the_fake():
    # Trip/TripBuilder previously read recording.recording_id, which
    # doesn't exist on the real Recording class (it's .id) - only a
    # FakeRecording with a matching (also wrong) attribute name hid
    # this. Exercise the real class here so that kind of drift can't
    # silently reappear.
    first = Recording(id=RecordingId("20260715_133255_N"))
    last = Recording(id=RecordingId("20260715_133455_N"))

    trip = Trip((first, last))

    assert trip.start_timestamp == datetime(2026, 7, 15, 13, 32, 55)
    assert trip.end_timestamp == datetime(2026, 7, 15, 13, 34, 55)
    assert trip.label == "trip_20260715_133255_20260715_133455"
    