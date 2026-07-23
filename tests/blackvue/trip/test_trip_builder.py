from datetime import datetime, timedelta

from blackvue.archive.recording import Recording
from blackvue.archive.recording_id import RecordingId
from blackvue.trip.trip_builder import DEFAULT_GAP_TOLERANCE
from blackvue.trip.trip_builder import TripBuilder


class FakeRecordingId:
    def __init__(self, timestamp):
        self.timestamp = timestamp


class FakeRecording:
    def __init__(self, timestamp):
        self.id = FakeRecordingId(timestamp)


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


def test_bridge_keeps_a_gap_together_when_it_returns_true():
    recordings = [
        FakeRecording(ts("20260715_100000")),
        FakeRecording(ts("20260715_100500")),
        FakeRecording(ts("20260715_103000")),
    ]

    trips = TripBuilder(
        max_gap=timedelta(minutes=10), bridge=lambda prev, cur: True
    ).build(recordings)

    assert len(trips) == 1
    assert trips[0].start_timestamp == ts("20260715_100000")
    assert trips[0].end_timestamp == ts("20260715_103000")


def test_bridge_returning_false_still_splits_the_trip():
    recordings = [
        FakeRecording(ts("20260715_100000")),
        FakeRecording(ts("20260715_103000")),
    ]

    trips = TripBuilder(
        max_gap=timedelta(minutes=10), bridge=lambda prev, cur: False
    ).build(recordings)

    assert len(trips) == 2


def test_bridge_is_not_consulted_when_gap_already_fits():
    calls = []

    def bridge(prev, cur):
        calls.append((prev, cur))
        return False

    recordings = [
        FakeRecording(ts("20260715_100000")),
        FakeRecording(ts("20260715_100500")),
    ]

    trips = TripBuilder(max_gap=timedelta(minutes=10), bridge=bridge).build(
        recordings
    )

    assert len(trips) == 1
    assert calls == []


def test_bridge_receives_the_bracketing_recordings():
    seen = []

    def bridge(prev, cur):
        seen.append((prev.id.timestamp, cur.id.timestamp))
        return True

    recordings = [
        FakeRecording(ts("20260715_100000")),
        FakeRecording(ts("20260715_103000")),
        FakeRecording(ts("20260715_110000")),
    ]

    TripBuilder(max_gap=timedelta(minutes=10), bridge=bridge).build(
        recordings
    )

    assert seen == [
        (ts("20260715_100000"), ts("20260715_103000")),
        (ts("20260715_103000"), ts("20260715_110000")),
    ]


def test_recording_duration_extends_a_recording_past_its_start():
    # Recording starts at 10:00:00 and, per recording_duration, really
    # runs for 12 real minutes - so it doesn't actually end until
    # 10:12:00. The next recording starts at 10:11:00, only 1 minute
    # after that real end, well inside a 10-minute max_gap - even
    # though the raw start-to-start gap (11 minutes) would exceed it.
    recordings = [
        FakeRecording(ts("20260715_100000")),
        FakeRecording(ts("20260715_101100")),
    ]

    def duration(recording):
        return 12 * 60 if recording.id.timestamp == ts("20260715_100000") else None

    trips = TripBuilder(
        max_gap=timedelta(minutes=10), recording_duration=duration
    ).build(recordings)

    assert len(trips) == 1


def test_recording_duration_still_splits_a_genuine_gap():
    # Same real duration as above, but the next recording starts well
    # after the real end this time (10:25:00 vs a real end of
    # 10:12:00) - a genuine 13-minute gap, so it should still split.
    recordings = [
        FakeRecording(ts("20260715_100000")),
        FakeRecording(ts("20260715_102500")),
    ]

    def duration(recording):
        return 12 * 60 if recording.id.timestamp == ts("20260715_100000") else None

    trips = TripBuilder(
        max_gap=timedelta(minutes=10), recording_duration=duration
    ).build(recordings)

    assert len(trips) == 2


def test_recording_duration_returning_none_falls_back_to_start_timestamp():
    recordings = [
        FakeRecording(ts("20260715_100000")),
        FakeRecording(ts("20260715_103000")),
    ]

    trips = TripBuilder(
        max_gap=timedelta(minutes=10), recording_duration=lambda r: None
    ).build(recordings)

    assert len(trips) == 2


def test_unset_recording_duration_matches_old_pure_start_to_start_behaviour():
    recordings = [
        FakeRecording(ts("20260715_100000")),
        FakeRecording(ts("20260715_100500")),
        FakeRecording(ts("20260715_103000")),
    ]

    trips = TripBuilder(max_gap=timedelta(minutes=10)).build(recordings)

    assert len(trips) == 2


def test_default_gap_tolerance_absorbs_a_few_seconds_of_overage():
    # 10 minutes and 5 seconds apart - over max_gap by less than the
    # default 10-second tolerance, so it should still be one trip.
    recordings = [
        FakeRecording(ts("20260715_100000")),
        FakeRecording(ts("20260715_101005")),
    ]

    trips = TripBuilder(max_gap=timedelta(minutes=10)).build(recordings)

    assert len(trips) == 1


def test_default_gap_tolerance_does_not_absorb_a_real_overage():
    # 10 minutes and 11 seconds apart - just past the default
    # 10-second tolerance, so it should split.
    recordings = [
        FakeRecording(ts("20260715_100000")),
        FakeRecording(ts("20260715_101011")),
    ]

    trips = TripBuilder(max_gap=timedelta(minutes=10)).build(recordings)

    assert len(trips) == 2


def test_gap_tolerance_boundary_is_inclusive():
    # Exactly max_gap + the default tolerance - the split condition is
    # a strict ">", so exactly on the boundary should NOT split.
    recordings = [
        FakeRecording(ts("20260715_100000")),
        FakeRecording(ts("20260715_100000") + timedelta(minutes=10) + DEFAULT_GAP_TOLERANCE),
    ]

    trips = TripBuilder(max_gap=timedelta(minutes=10)).build(recordings)

    assert len(trips) == 1


def test_gap_tolerance_zero_reproduces_the_strict_legacy_boundary():
    # With gap_tolerance explicitly zeroed out, even a 1-second
    # overage should split - the literal old pure-gap behaviour.
    recordings = [
        FakeRecording(ts("20260715_100000")),
        FakeRecording(ts("20260715_100000") + timedelta(minutes=10, seconds=1)),
    ]

    trips = TripBuilder(
        max_gap=timedelta(minutes=10), gap_tolerance=timedelta(0)
    ).build(recordings)

    assert len(trips) == 2


def test_trip_builder_works_against_real_recordings():
    # Same drift risk as test_trip.py's equivalent - the fakes above
    # use .id (matching Recording), but assert against the real class
    # too so a future rename can't silently break this again.
    recordings = [
        Recording(id=RecordingId("20260715_100000_N")),
        Recording(id=RecordingId("20260715_100500_N")),
        Recording(id=RecordingId("20260715_103000_N")),
    ]

    trips = TripBuilder(max_gap=timedelta(minutes=10)).build(recordings)

    assert len(trips) == 2
    assert trips[0].label == "trip_20260715_100000_20260715_100500"
    assert trips[1].label == "trip_20260715_103000_20260715_103000"


def test_reasons_records_the_first_recording_in_the_archive():
    recording = Recording(id=RecordingId("20260715_100000_N"))

    reasons = {}
    TripBuilder().build([recording], reasons=reasons)

    assert reasons[recording.id] == "first recording in the archive"


def test_reasons_records_a_within_threshold_continuation():
    first = Recording(id=RecordingId("20260715_100000_N"))
    second = Recording(id=RecordingId("20260715_100500_N"))

    reasons = {}
    TripBuilder(max_gap=timedelta(minutes=10)).build(
        [first, second], reasons=reasons
    )

    reason = reasons[second.id]
    assert "continues the trip" in reason
    assert "within" in reason
    assert str(first.id) in reason


def test_reasons_records_a_gap_that_starts_a_new_trip():
    first = Recording(id=RecordingId("20260715_100000_N"))
    second = Recording(id=RecordingId("20260715_103000_N"))

    reasons = {}
    TripBuilder(max_gap=timedelta(minutes=10)).build(
        [first, second], reasons=reasons
    )

    reason = reasons[second.id]
    assert "starts a new trip" in reason
    assert "no movement evidence bridged it" in reason


def test_reasons_records_the_bridges_own_reason_text():
    first = Recording(id=RecordingId("20260715_100000_N"))
    second = Recording(id=RecordingId("20260715_103000_N"))

    reasons = {}
    TripBuilder(
        max_gap=timedelta(minutes=10),
        bridge=lambda prev, cur: "GPS speed at 42 km/h",
    ).build([first, second], reasons=reasons)

    reason = reasons[second.id]
    assert "continues the trip" in reason
    assert "bridged by: GPS speed at 42 km/h" in reason


def test_describe_gap_flags_a_negative_gap_explicitly():
    description = TripBuilder._describe_gap(timedelta(seconds=-5))

    assert "BEFORE" in description
    assert "5.0s" in description


def test_describe_gap_renders_a_positive_gap_plainly():
    description = TripBuilder._describe_gap(timedelta(seconds=45))

    assert description == "45.0s"
    assert "BEFORE" not in description
    