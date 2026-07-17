from datetime import datetime, timedelta

from blackvue.trip.trip_builder import TripBuilder


class FakeRecordingId:
    def __init__(self, timestamp):
        self.timestamp = timestamp


class FakeRecording:
    def __init__(self, timestamp):
        self.recording_id = FakeRecordingId(timestamp)


def ts(value: str) -> datetime:
    return datetime.strptime(value, "%Y%m%d_%H%M%S")


def test_no_recordings_creates_no_trips():
    trips = TripBuilder().build([])

    assert trips == []


def test_single_recording_creates_one_trip():
    recording = FakeRecording(ts("20260715_100000"))

    trips = TripBuilder().build([recording])

    assert len(trips) == 1
    assert trips[0].start_timestamp == ts("20260715_100000")
    assert trips[0].end_timestamp == ts("20260715_100000")


def test_two_close_recordings_create_one_trip():
    recordings = [
        FakeRecording(ts("20260715_100000")),
        FakeRecording(ts("20260715_100500")),
    ]

    trips = TripBuilder().build(recordings)

    assert len(trips) == 1
    assert trips[0].start_timestamp == ts("20260715_100000")
    assert trips[0].end_timestamp == ts("20260715_100500")


def test_gap_starts_new_trip():
    recordings = [
        FakeRecording(ts("20260715_100000")),
        FakeRecording(ts("20260715_100500")),
        FakeRecording(ts("20260715_103000")),
    ]

    trips = TripBuilder(max_gap=timedelta(minutes=10)).build(recordings)

    assert len(trips) == 2

    assert trips[0].start_timestamp == ts("20260715_100000")
    assert trips[0].end_timestamp == ts("20260715_100500")

    assert trips[1].start_timestamp == ts("20260715_103000")
    assert trips[1].end_timestamp == ts("20260715_103000")
    