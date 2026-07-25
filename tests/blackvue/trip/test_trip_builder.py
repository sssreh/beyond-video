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


def _duration_lookup(overrides: dict[str, int]):
    """A recording_duration callback backed by a plain {id_value:
    seconds} dict - returns None for anything not listed, matching
    read_duration_seconds()'s own "unknown" convention."""

    return lambda recording: overrides.get(str(recording.id))


def test_max_parking_duration_forces_a_split_when_a_single_recording_exceeds_it():
    # Reproduces Christer's real archive case at a small scale: a
    # Parking-mode timelapse (90 real minutes) plays back in a few
    # minutes, but recording_duration reports its real elapsed span -
    # so the very next recording's duration-folded gap is a harmless
    # 9 seconds, well inside the ordinary max_gap threshold. Without
    # max_parking_duration, this stays one trip (the original bug).
    # With a 60-minute cap, the Parking recording's own 90-minute real
    # span already exceeds it, forcing a split before the next
    # recording is even considered.
    first = Recording(id=RecordingId("20260715_100000_N"))
    second = Recording(id=RecordingId("20260715_100500_P"))
    third = Recording(id=RecordingId("20260715_113509_N"))

    duration = _duration_lookup({str(second.id): 90 * 60})

    trips = TripBuilder(
        recording_duration=duration,
        max_parking_duration=timedelta(minutes=60),
    ).build([first, second, third])

    assert len(trips) == 2
    assert len(trips[0]) == 2
    assert trips[0].start_timestamp == ts("20260715_100000")
    assert len(trips[1]) == 1
    assert trips[1].start_timestamp == ts("20260715_113509")


def test_max_parking_duration_lets_a_shorter_stop_through():
    # Same shape as above, but the Parking recording's real span (30
    # minutes) stays under the 60-minute cap - stays one trip.
    first = Recording(id=RecordingId("20260715_100000_N"))
    second = Recording(id=RecordingId("20260715_100500_P"))
    third = Recording(id=RecordingId("20260715_103505_N"))

    duration = _duration_lookup({str(second.id): 30 * 60})

    trips = TripBuilder(
        recording_duration=duration,
        max_parking_duration=timedelta(minutes=60),
    ).build([first, second, third])

    assert len(trips) == 1
    assert len(trips[0]) == 3


def test_max_parking_duration_splits_between_two_chained_parking_recordings():
    # Three consecutive Parking recordings, each individually under
    # the 60-minute cap (40 minutes apiece), chained with ~0 real gap
    # between them. The first two combined (80 minutes) already cross
    # the cap, so the split lands between the second and third Parking
    # recording - a "parking-only" trip of just the third recording -
    # confirming the cap tracks the *cumulative* run, not just each
    # recording's own length.
    first = Recording(id=RecordingId("20260715_100000_N"))
    second = Recording(id=RecordingId("20260715_100500_P"))
    third = Recording(id=RecordingId("20260715_104505_P"))
    fourth = Recording(id=RecordingId("20260715_112510_P"))

    duration = _duration_lookup({
        str(second.id): 40 * 60,
        str(third.id): 40 * 60,
        str(fourth.id): 10 * 60,
    })

    trips = TripBuilder(
        recording_duration=duration,
        max_parking_duration=timedelta(minutes=60),
    ).build([first, second, third, fourth])

    assert len(trips) == 2
    assert len(trips[0]) == 3
    assert len(trips[1]) == 1
    assert trips[1].start_timestamp == ts("20260715_112510")


def test_max_parking_duration_resets_after_a_non_parking_recording():
    # Two Parking recordings (40 minutes each - 80 minutes combined,
    # over the 60-minute cap) with a normal driving recording between
    # them. The drive in the middle breaks the run, so the two Parking
    # spans are never summed together - stays one trip.
    first = Recording(id=RecordingId("20260715_100000_N"))
    second = Recording(id=RecordingId("20260715_100500_P"))
    third = Recording(id=RecordingId("20260715_104505_N"))
    fourth = Recording(id=RecordingId("20260715_105010_P"))
    fifth = Recording(id=RecordingId("20260715_113015_N"))

    duration = _duration_lookup({
        str(second.id): 40 * 60,
        str(third.id): 5 * 60,
        str(fourth.id): 40 * 60,
    })

    trips = TripBuilder(
        recording_duration=duration,
        max_parking_duration=timedelta(minutes=60),
    ).build([first, second, third, fourth, fifth])

    assert len(trips) == 1
    assert len(trips[0]) == 5


def test_max_parking_duration_is_a_no_op_without_recording_duration():
    # max_parking_duration alone, with no recording_duration callback
    # to supply real spans, can never have anything to compare against
    # - _parking_contribution() short-circuits to 0 in that case, so
    # this behaves exactly like max_parking_duration being unset.
    first = Recording(id=RecordingId("20260715_100000_N"))
    second = Recording(id=RecordingId("20260715_100500_P"))
    third = Recording(id=RecordingId("20260715_101000_N"))

    trips = TripBuilder(max_parking_duration=timedelta(minutes=60)).build(
        [first, second, third]
    )

    assert len(trips) == 1


def test_max_parking_duration_unset_never_touches_is_parking():
    # A regression guard for the plain FakeRecording/FakeRecordingId
    # stand-ins used throughout the rest of this file, which predate
    # this feature and don't define is_parking at all -
    # _parking_contribution() must short-circuit on
    # max_parking_duration being unset *before* ever reading
    # recording.id.is_parking, or every other test in this file using
    # recording_duration would start raising AttributeError.
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


def test_max_parking_duration_split_reason_names_the_limit_and_accumulated_time():
    first = Recording(id=RecordingId("20260715_100000_N"))
    second = Recording(id=RecordingId("20260715_100500_P"))
    third = Recording(id=RecordingId("20260715_113509_N"))

    duration = _duration_lookup({str(second.id): 90 * 60})

    reasons = {}
    TripBuilder(
        recording_duration=duration,
        max_parking_duration=timedelta(minutes=60),
    ).build([first, second, third], reasons=reasons)

    reason = reasons[third.id]
    assert "starts a new trip" in reason
    assert "Parking-mode" in reason
    assert "90.0m" in reason
    assert "60.0m" in reason
    assert str(second.id) in reason


def test_max_parking_duration_split_is_never_offered_to_bridge():
    # Even with a genuine additional gap on top of the cap (so the
    # ordinary gap>threshold check would also fire) and a bridge that
    # would otherwise keep it together, a cap-forced split must still
    # happen and bridge must never be consulted for it - see
    # TripBuilder's own docstring for why (a deliberate policy
    # decision, not ambiguous gap evidence to weigh).
    first = Recording(id=RecordingId("20260715_100000_N"))
    second = Recording(id=RecordingId("20260715_100500_P"))
    third = Recording(id=RecordingId("20260715_120000_N"))

    duration = _duration_lookup({str(second.id): 90 * 60})

    calls = []

    def bridge(prev, cur):
        calls.append((prev.id, cur.id))
        return True

    trips = TripBuilder(
        recording_duration=duration,
        max_parking_duration=timedelta(minutes=60),
        bridge=bridge,
    ).build([first, second, third])

    assert len(trips) == 2
    assert calls == []
    