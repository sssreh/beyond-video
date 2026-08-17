from datetime import datetime, timedelta

from blackvue.archive.recording import Recording
from blackvue.archive.recording_id import RecordingId
from blackvue.lexicaltimeparser import LexicalTimeParser
from blackvue.lexicaltimeparser import TimeInterval
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


def test_max_parking_duration_keeps_an_oversized_recording_out_of_the_ending_trip():
    # Reproduces Christer's real archive case at a small scale: a
    # Parking-mode timelapse (90 real minutes) plays back in a few
    # minutes, but recording_duration reports its real elapsed span -
    # so the very next recording's duration-folded gap is a harmless
    # 9 seconds, well inside the ordinary max_gap threshold. Without
    # max_parking_duration, this stays one trip (the original bug).
    #
    # With a 60-minute cap: Christer was explicit that a Parking
    # recording whose own real span already exceeds the cap must never
    # be part of the trip it would otherwise close out - so the check
    # is prospective (would *including* this recording exceed the
    # cap?), not retrospective. That excludes `second` from the drive
    # entirely - it becomes its own trip instead of trip one's last
    # member. `third` (immediately following, ordinary driving) can't
    # join that trip either, since `second` alone already leaves it
    # over cap with nothing `third` can contribute to fix that - so it
    # becomes a third trip of its own. Real archives won't usually end
    # right there (see the "resumes normally afterward" test below for
    # what happens once genuine driving continues past this point).
    first = Recording(id=RecordingId("20260715_100000_N"))
    second = Recording(id=RecordingId("20260715_100500_P"))
    third = Recording(id=RecordingId("20260715_113509_N"))

    duration = _duration_lookup({str(second.id): 90 * 60})

    trips = TripBuilder(
        recording_duration=duration,
        max_parking_duration=timedelta(minutes=60),
    ).build([first, second, third])

    assert len(trips) == 3
    assert len(trips[0]) == 1
    assert trips[0].start_timestamp == ts("20260715_100000")
    assert len(trips[1]) == 1
    assert trips[1].start_timestamp == ts("20260715_100500")
    assert len(trips[2]) == 1
    assert trips[2].start_timestamp == ts("20260715_113509")


def test_max_parking_duration_resumes_normally_once_driving_continues():
    # Same oversized Parking recording as above, but this time real
    # driving continues past the immediately-following recording -
    # confirming the "nothing else can join a trip that already
    # contains an over-cap recording" effect only ever costs the one
    # recording immediately after it, not every recording from then on.
    first = Recording(id=RecordingId("20260715_100000_N"))
    second = Recording(id=RecordingId("20260715_100500_P"))
    third = Recording(id=RecordingId("20260715_113509_N"))
    fourth = Recording(id=RecordingId("20260715_113514_N"))
    fifth = Recording(id=RecordingId("20260715_113519_N"))

    duration = _duration_lookup({str(second.id): 90 * 60})

    trips = TripBuilder(
        recording_duration=duration,
        max_parking_duration=timedelta(minutes=60),
    ).build([first, second, third, fourth, fifth])

    assert len(trips) == 3
    assert len(trips[0]) == 1
    assert len(trips[1]) == 1
    assert len(trips[2]) == 3
    assert trips[2].start_timestamp == ts("20260715_113509")


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
    # between them. Including the first two (80 minutes combined)
    # would already cross the cap when the *third* one is considered,
    # so the split lands there - third is kept out of the trip that
    # already holds first/second, and starts a new one instead (with
    # fourth, itself under cap, joining it normally) - confirming the
    # cap tracks the *cumulative* run, not just each recording's own
    # length, and that the recording which would push it over stays
    # out of the trip being closed rather than ending it.
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
    assert len(trips[0]) == 2
    assert len(trips[1]) == 2
    assert trips[1].start_timestamp == ts("20260715_104505")


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
    # `second`'s own 90-minute span alone already exceeds the cap, so
    # it's `second` itself (not whatever comes after it) that gets the
    # "starts a new trip" reason - it's the one being kept out of the
    # trip it would otherwise have joined.
    first = Recording(id=RecordingId("20260715_100000_N"))
    second = Recording(id=RecordingId("20260715_100500_P"))
    third = Recording(id=RecordingId("20260715_113509_N"))

    duration = _duration_lookup({str(second.id): 90 * 60})

    reasons = {}
    TripBuilder(
        recording_duration=duration,
        max_parking_duration=timedelta(minutes=60),
    ).build([first, second, third], reasons=reasons)

    reason = reasons[second.id]
    assert "starts a new trip" in reason
    assert "Parking-mode" in reason
    assert "90.0m" in reason
    assert "60.0m" in reason


def test_max_parking_duration_split_is_never_offered_to_bridge():
    # Even with a genuine additional gap on top of the cap (so the
    # ordinary gap>threshold check would also fire) and a bridge that
    # would otherwise keep it together, a cap-forced exclusion must
    # still happen and bridge must never be consulted for it - see
    # TripBuilder's own docstring for why (a deliberate policy
    # decision, not ambiguous gap evidence to weigh). Both `second`
    # (excluded on its own oversized span) and `third` (excluded
    # because `second`'s leftover total alone already leaves no room)
    # go through the cap-exceeded path, so bridge should never fire
    # for either.
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

    assert len(trips) == 3
    assert calls == []


# --- timestamp_reliable isolation ---
#
# Christer's real report: `bv-ls GP --full` showed pairs of unrelated
# stock/sample test-fixture clips (no embedded timestamp of any kind -
# confirmed via a real `ffprobe -show_format -show_streams` dump) whose
# only "timestamp" was file mtime, landing a second apart purely by
# download-batch coincidence, and about to be silently grouped into
# one trip. Recording.timestamp_reliable (set False by
# adapters/_recursive_scan.py's _resolve_timestamp() on the mtime
# fallback) is TripBuilder's guard against exactly that - see build()'s
# own docstring for the full reasoning.


def test_two_recordings_with_unreliable_timestamps_never_group_even_within_gap():
    # Both fall back to mtime and happen to land 1 second apart - well
    # within any realistic max_gap - but must still split into two
    # singleton trips.
    first = Recording(
        id=RecordingId("20260816_144130_V"), timestamp_reliable=False
    )
    second = Recording(
        id=RecordingId("20260816_144131_V"), timestamp_reliable=False
    )

    trips = TripBuilder(max_gap=timedelta(minutes=5)).build([first, second])

    assert len(trips) == 2
    assert trips[0].recordings == (first,)
    assert trips[1].recordings == (second,)


def test_unreliable_timestamp_on_either_side_alone_still_forces_a_split():
    # Only `second` is unreliable - `first` and `third` are both real,
    # reliable timestamps - but second must still be isolated on both
    # sides.
    first = Recording(id=RecordingId("20260816_144000_V"))
    second = Recording(
        id=RecordingId("20260816_144005_V"), timestamp_reliable=False
    )
    third = Recording(id=RecordingId("20260816_144010_V"))

    trips = TripBuilder(max_gap=timedelta(minutes=5)).build(
        [first, second, third]
    )

    assert len(trips) == 3
    assert trips[0].recordings == (first,)
    assert trips[1].recordings == (second,)
    assert trips[2].recordings == (third,)


def test_reliable_recordings_around_an_unreliable_one_still_group_normally():
    # Two reliable clusters, each internally within gap, separated by
    # one unreliable singleton - the reliable clusters themselves
    # should still group normally, only the unreliable one is isolated.
    a1 = Recording(id=RecordingId("20260816_144000_V"))
    a2 = Recording(id=RecordingId("20260816_144200_V"))
    unreliable = Recording(
        id=RecordingId("20260816_144300_V"), timestamp_reliable=False
    )
    b1 = Recording(id=RecordingId("20260816_144400_V"))
    b2 = Recording(id=RecordingId("20260816_144600_V"))

    trips = TripBuilder(max_gap=timedelta(minutes=5)).build(
        [a1, a2, unreliable, b1, b2]
    )

    assert len(trips) == 3
    assert trips[0].recordings == (a1, a2)
    assert trips[1].recordings == (unreliable,)
    assert trips[2].recordings == (b1, b2)


def test_unreliable_timestamp_split_is_never_offered_to_bridge():
    first = Recording(id=RecordingId("20260816_144000_V"))
    second = Recording(
        id=RecordingId("20260816_144005_V"), timestamp_reliable=False
    )

    calls = []

    def bridge(prev, cur):
        calls.append((prev.id, cur.id))
        return True

    trips = TripBuilder(max_gap=timedelta(minutes=5), bridge=bridge).build(
        [first, second]
    )

    assert len(trips) == 2
    assert calls == []


def test_unreliable_timestamp_split_reason_names_the_culprit():
    first = Recording(id=RecordingId("20260816_144000_V"))
    second = Recording(
        id=RecordingId("20260816_144005_V"), timestamp_reliable=False
    )

    reasons = {}
    TripBuilder(max_gap=timedelta(minutes=5)).build(
        [first, second], reasons=reasons
    )

    reason = reasons[second.id]
    assert "starts a new trip" in reason
    assert "this recording's own" in reason
    assert "fell back to file mtime" in reason


def test_unreliable_timestamp_split_reason_names_the_previous_recording():
    first = Recording(
        id=RecordingId("20260816_144000_V"), timestamp_reliable=False
    )
    second = Recording(id=RecordingId("20260816_144005_V"))

    reasons = {}
    TripBuilder(max_gap=timedelta(minutes=5)).build(
        [first, second], reasons=reasons
    )

    reason = reasons[second.id]
    assert "starts a new trip" in reason
    assert str(first.id) in reason


def test_fake_recordings_without_timestamp_reliable_attribute_are_unaffected():
    # FakeRecording (used throughout this file) never sets
    # timestamp_reliable at all - getattr(..., True) must treat that
    # exactly like a real, reliable Recording, not raise or silently
    # force splits everywhere.
    recordings = [
        FakeRecording(ts("20260715_100000")),
        FakeRecording(ts("20260715_100500")),
    ]

    trips = TripBuilder(max_gap=timedelta(minutes=10)).build(recordings)

    assert len(trips) == 1


# --- build_for_interval() ---
#
# Christer: bv-export's own archive-wide trip detection felt slow even
# to export a single day - build_for_interval() bounds detection to
# just the trip(s) touching a requested interval instead, by seeding
# on the recordings actually inside it and growing outward only as far
# as needed to prove a real gap on each side (see its own docstring).
# These tests build a synthetic multi-trip archive and check the
# bounded result always matches a plain build() over the *entire*
# archive, for a range of request shapes - the whole point of the
# feature is "same answer, less work," so every test here compares
# against that same real ground truth rather than asserting a fixed
# expected shape by hand.


def _multi_trip_recordings(
    trip_count: int,
    *,
    recordings_per_trip: int = 10,
    start: str = "20260101_080000",
    within_trip_gap_minutes: int = 5,
    between_trip_gap_minutes: int = 30,
) -> list[Recording]:
    """`trip_count` trips of `recordings_per_trip` recordings each,
    `within_trip_gap_minutes` apart inside a trip and
    `between_trip_gap_minutes` apart between trips - `TripBuilder(
    max_gap=timedelta(minutes=10))` (used by every test below) then
    splits this into exactly `trip_count` distinct trips, giving a
    predictable, larger-than-any-single-test-needs archive to seed
    requests into."""

    recordings = []
    t = datetime.strptime(start, "%Y%m%d_%H%M%S")
    for trip_index in range(trip_count):
        for recording_index in range(recordings_per_trip):
            recordings.append(
                Recording(id=RecordingId(t.strftime("%Y%m%d_%H%M%S") + "_N"))
            )
            t += timedelta(minutes=within_trip_gap_minutes)
        t += timedelta(minutes=between_trip_gap_minutes)
    return recordings


def _exact_interval(recording: Recording) -> TimeInterval:
    """A `--timestamp`-style interval matching only `recording`'s own
    exact timestamp - the tightest possible seed, one recording deep
    inside whatever trip it belongs to, so build_for_interval() must
    grow outward on its own to recover the rest of that trip."""

    return LexicalTimeParser(
        timestamp=recording.id.timestamp.strftime("%Y%m%d_%H%M%S")
    ).parse()


def test_build_for_interval_matches_a_middle_trip_seeded_by_one_recording():
    recordings = _multi_trip_recordings(10)
    builder = TripBuilder(max_gap=timedelta(minutes=10))
    full = builder.build(recordings)
    target = full[5]

    # Seed on the target trip's own middle recording, not its first or
    # last - build_for_interval() must grow both backward and forward
    # within the same trip to recover it in full.
    seed_recording = target.recordings[len(target) // 2]

    bounded = builder.build_for_interval(recordings, _exact_interval(seed_recording))
    relevant = [
        trip
        for trip in bounded
        if any(r.id.value in _exact_interval(seed_recording) for r in trip)
    ]

    assert len(relevant) == 1
    assert relevant[0].recordings == target.recordings


def test_build_for_interval_matches_the_first_trip_in_the_archive():
    recordings = _multi_trip_recordings(10)
    builder = TripBuilder(max_gap=timedelta(minutes=10))
    full = builder.build(recordings)
    target = full[0]

    interval = _exact_interval(target.first_recording)
    bounded = builder.build_for_interval(recordings, interval)
    relevant = [
        trip for trip in bounded if any(r.id.value in interval for r in trip)
    ]

    assert len(relevant) == 1
    assert relevant[0].recordings == target.recordings


def test_build_for_interval_matches_the_last_trip_in_the_archive():
    recordings = _multi_trip_recordings(10)
    builder = TripBuilder(max_gap=timedelta(minutes=10))
    full = builder.build(recordings)
    target = full[-1]

    interval = _exact_interval(target.last_recording)
    bounded = builder.build_for_interval(recordings, interval)
    relevant = [
        trip for trip in bounded if any(r.id.value in interval for r in trip)
    ]

    assert len(relevant) == 1
    assert relevant[0].recordings == target.recordings


def test_build_for_interval_returns_empty_for_no_matching_recordings():
    recordings = _multi_trip_recordings(5)
    builder = TripBuilder(max_gap=timedelta(minutes=10))

    interval = LexicalTimeParser(timestamp="20300101_000000").parse()

    assert builder.build_for_interval(recordings, interval) == []


def test_build_for_interval_covers_a_range_spanning_several_trips():
    recordings = _multi_trip_recordings(10)
    builder = TripBuilder(max_gap=timedelta(minutes=10))
    full = builder.build(recordings)

    interval = LexicalTimeParser(
        from_=full[3].first_recording.id.timestamp.strftime("%Y%m%d_%H%M%S"),
        until=full[6].last_recording.id.timestamp.strftime("%Y%m%d_%H%M%S"),
    ).parse()

    bounded = builder.build_for_interval(recordings, interval)
    relevant = [
        trip for trip in bounded if any(r.id.value in interval for r in trip)
    ]

    assert [trip.recordings for trip in relevant] == [
        trip.recordings for trip in full[3:7]
    ]


def test_build_for_interval_matches_build_for_the_whole_archive_sentinel():
    recordings = _multi_trip_recordings(6)
    builder = TripBuilder(max_gap=timedelta(minutes=10))

    sentinel = LexicalTimeParser().parse()
    full = builder.build(recordings)
    bounded = builder.build_for_interval(recordings, sentinel)

    assert [trip.recordings for trip in bounded] == [
        trip.recordings for trip in full
    ]


def test_build_for_interval_populates_reasons_for_the_returned_trips():
    recordings = _multi_trip_recordings(6)
    builder = TripBuilder(max_gap=timedelta(minutes=10))
    full = builder.build(recordings)
    target = full[3]

    reasons: dict = {}
    builder.build_for_interval(
        recordings, _exact_interval(target.first_recording), reasons=reasons
    )

    for recording in target:
        assert recording.id in reasons


def test_build_for_interval_heals_every_recording_in_the_final_window():
    # A recording_duration callback with a real side effect (like
    # load_or_compute_duration - bv-export's own bounded
    # --duration-heal-archive) must be called on every recording the
    # search actually settles on, including whichever one lands right
    # on the final window's own edge - build()'s own gap-walking loop
    # alone never calls it for a window's last recording (nothing
    # follows it to trigger _end_timestamp()), which is exactly the
    # gap this behaviour closes - see build_for_interval()'s own
    # docstring.
    recordings = _multi_trip_recordings(6)
    builder_for_ground_truth = TripBuilder(max_gap=timedelta(minutes=10))
    full = builder_for_ground_truth.build(recordings)
    target = full[3]

    healed: list = []

    def heal(recording):
        healed.append(recording.id)
        return None

    builder = TripBuilder(max_gap=timedelta(minutes=10), recording_duration=heal)
    bounded = builder.build_for_interval(
        recordings, _exact_interval(target.first_recording)
    )

    window_recordings = [r for trip in bounded for r in trip]
    assert set(healed) == {r.id for r in window_recordings}


def test_build_for_interval_reads_far_fewer_recordings_than_a_full_scan():
    # The whole point: recovering one trip out of a much larger archive
    # shouldn't need duration data for recordings nowhere near it.
    recordings = _multi_trip_recordings(50)
    target_index = 25

    calls: list = []

    def counting_duration(recording):
        calls.append(recording.id)
        return None

    builder = TripBuilder(
        max_gap=timedelta(minutes=10), recording_duration=counting_duration
    )
    full = TripBuilder(max_gap=timedelta(minutes=10)).build(recordings)
    target = full[target_index]

    calls.clear()
    builder.build_for_interval(recordings, _exact_interval(target.first_recording))
    bounded_call_count = len(calls)

    calls.clear()
    builder.build(recordings)
    full_call_count = len(calls)

    assert bounded_call_count < full_call_count / 4
    