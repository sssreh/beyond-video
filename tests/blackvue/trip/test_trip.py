from blackvue.trip.trip import Trip


class FakeRecordingId:
    def __init__(self, timestamp):
        self.timestamp = timestamp


class FakeRecording:
    def __init__(self, timestamp):
        self.recording_id = FakeRecordingId(timestamp)


def test_trip_start_and_end_timestamp():
    r1 = FakeRecording("20260715_100000")
    r2 = FakeRecording("20260715_103000")

    trip = Trip((r1, r2))

    assert trip.start_timestamp == "20260715_100000"
    assert trip.end_timestamp == "20260715_103000"
