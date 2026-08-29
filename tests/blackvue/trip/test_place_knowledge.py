"""Tests for trip/place_knowledge.py - the driver-knowledge-base
increment (common places + short/long stay rules + per-trip overrides)
built on top of driver_detect.py's route/dwell-time matcher.

Coordinates are fabricated stand-ins, same convention as
test_driver_detect.py - chosen far enough apart that
home_radius_meters/_SAME_PLACE_RADIUS_METERS cleanly separate "near"
from "far", and close enough together within place_key()'s own grid
cell that two visits to "the same place" actually land on the same
key.
"""

from __future__ import annotations

import tempfile
from datetime import datetime, timedelta
from pathlib import Path

from blackvue.trip.driver_detect import DriverProfile
from blackvue.trip.driver_detect import DriverProfiles
from blackvue.trip.driver_detect import TripFix
from blackvue.trip.place_knowledge import CommonPlace
from blackvue.trip.place_knowledge import STOP_THRESHOLD_MINUTES
from blackvue.trip.place_knowledge import TripKnowledge
from blackvue.trip.place_knowledge import _resolve_trip_driver
from blackvue.trip.place_knowledge import build_common_places
from blackvue.trip.place_knowledge import build_knowledge_base
from blackvue.trip.place_knowledge import dwell_at_destination
from blackvue.trip.place_knowledge import load_knowledge_base
from blackvue.trip.place_knowledge import local_weekday_and_time
from blackvue.trip.place_knowledge import place_key
from blackvue.trip.place_knowledge import save_knowledge_base
from blackvue.trip.place_knowledge import stop_category
from blackvue.trip.place_knowledge import undecided_places
from blackvue.trip.place_knowledge import undecided_trips
from blackvue.trip.trip import Trip

HOME = (59.3050, 18.1010)
PLACE_A = (59.3600, 18.0000)


class FakeRecordingId:
    def __init__(self, timestamp: datetime) -> None:
        self.timestamp = timestamp


class FakeRecording:
    def __init__(self, timestamp: datetime) -> None:
        self.id = FakeRecordingId(timestamp)


def make_fix(start, end, start_t, end_t) -> TripFix:
    return TripFix(start=start, end=end, start_time=start_t, end_time=end_t)


def make_trip(timestamp: datetime) -> Trip:
    return Trip(recordings=(FakeRecording(timestamp),))


def make_profiles() -> DriverProfiles:
    return DriverProfiles(
        home_name="Home",
        home_query="Home",
        home_radius_meters=300.0,
        drivers=(
            DriverProfile(label="driver1", display_name="Fru", patterns=()),
            DriverProfile(label="driver2", display_name="Christer", patterns=()),
        ),
    )


def test_local_weekday_and_time_uses_raw_timestamp_no_dst_math():
    # 2026-08-29 is a Saturday - deliberately mid-summer (real DST
    # season), to confirm this never subtracts/adds an hour: Christer
    # explicitly asked for the camera's own recorded wall-clock time
    # untouched ("a change of time requires a camera reboot").
    weekday, time_of_day = local_weekday_and_time(datetime(2026, 8, 29, 14, 5))
    assert weekday == "Saturday"
    assert time_of_day == "14:05"


def test_stop_category_threshold():
    assert stop_category(None) is None
    assert stop_category(STOP_THRESHOLD_MINUTES - 0.1) == "short"
    assert stop_category(STOP_THRESHOLD_MINUTES) == "long"
    assert stop_category(STOP_THRESHOLD_MINUTES + 30) == "long"


def test_place_key_is_deterministic_within_a_grid_cell():
    assert place_key((59.30512, 18.08912)) == place_key((59.30519, 18.08918))
    assert place_key(HOME) != place_key(PLACE_A)


def test_dwell_at_destination_excludes_home_only_trips():
    trip_local = make_fix(HOME, HOME, datetime(2026, 1, 5, 8, 0), datetime(2026, 1, 5, 8, 5))
    assert dwell_at_destination(trip_local, None, None, HOME, 300.0) is None


def test_dwell_at_destination_home_to_away_uses_next_trip_start():
    t0 = datetime(2026, 1, 5, 8, 0)
    outbound = make_fix(HOME, PLACE_A, t0, t0 + timedelta(minutes=20))
    t_return_start = t0 + timedelta(minutes=60)
    inbound = make_fix(PLACE_A, HOME, t_return_start, t_return_start + timedelta(minutes=20))

    dwell = dwell_at_destination(outbound, None, inbound, HOME, 300.0)
    assert dwell is not None and abs(dwell - 40.0) < 0.01
    assert stop_category(dwell) == "long"


def test_dwell_at_destination_returns_none_when_adjacent_trip_missing():
    t0 = datetime(2026, 1, 5, 8, 0)
    outbound = make_fix(HOME, PLACE_A, t0, t0 + timedelta(minutes=20))
    assert dwell_at_destination(outbound, None, None, HOME, 300.0) is None


def test_dwell_at_destination_returns_none_when_next_trip_starts_elsewhere():
    t0 = datetime(2026, 1, 5, 8, 0)
    outbound = make_fix(HOME, PLACE_A, t0, t0 + timedelta(minutes=20))
    somewhere_else = (10.0, 10.0)
    t_return_start = t0 + timedelta(minutes=60)
    inbound = make_fix(somewhere_else, HOME, t_return_start, t_return_start + timedelta(minutes=20))

    assert dwell_at_destination(outbound, None, inbound, HOME, 300.0) is None


def _knowledge_entry(place_point, category, dwell) -> TripKnowledge:
    t0 = datetime(2026, 1, 5, 8, 0)
    return TripKnowledge(
        trip_label="trip_x",
        start_time=t0,
        end_time=t0,
        weekday="Monday",
        start_time_of_day="08:00",
        away_place_key=place_key(place_point),
        away_point=place_point,
        dwell_minutes=dwell,
        stop_category=category,
        candidates=(),
    )


def test_build_common_places_counts_visits_by_stop_category():
    entries = [
        _knowledge_entry(PLACE_A, "long", 40.0),
        _knowledge_entry(PLACE_A, "short", 5.0),
        _knowledge_entry(PLACE_A, "short", 3.0),
    ]
    places = build_common_places(entries)

    assert len(places) == 1
    place = next(iter(places.values()))
    assert place.visit_count == 3
    assert place.long_stay_count == 1
    assert place.short_stay_count == 2
    assert place.short_stay_driver is None
    assert place.long_stay_driver is None


def test_build_common_places_carries_forward_existing_rules_and_label():
    entries = [_knowledge_entry(PLACE_A, "long", 40.0)]
    key = place_key(PLACE_A)
    existing = {
        key: CommonPlace(
            key=key,
            point=PLACE_A,
            label="Grandma's house",
            visit_count=1,
            short_stay_count=0,
            long_stay_count=1,
            short_stay_driver="driver1",
            long_stay_driver="driver2",
        )
    }

    places = build_common_places(entries, existing=existing)

    place = places[key]
    assert place.label == "Grandma's house"
    assert place.short_stay_driver == "driver1"
    assert place.long_stay_driver == "driver2"
    # visit_count itself is recomputed fresh from `entries`, not carried:
    assert place.visit_count == 1


def test_undecided_places_needs_min_visits_and_a_missing_rule():
    key = place_key(PLACE_A)
    rare_place = CommonPlace(
        key=key, point=PLACE_A, label="rare", visit_count=1,
        short_stay_count=1, long_stay_count=0,
    )
    common_undecided = CommonPlace(
        key="other", point=(1.0, 1.0), label="common", visit_count=5,
        short_stay_count=3, long_stay_count=2,
    )
    common_decided = CommonPlace(
        key="decided", point=(2.0, 2.0), label="decided", visit_count=5,
        short_stay_count=3, long_stay_count=2,
        short_stay_driver="driver1", long_stay_driver="driver2",
    )

    result = undecided_places(
        {"rare": rare_place, "other": common_undecided, "decided": common_decided},
        min_visits=2,
    )

    assert result == [common_undecided]


def test_resolve_trip_driver_prefers_manual_trip_override_over_place_rule():
    profiles = make_profiles()
    entry = _knowledge_entry(PLACE_A, "long", 40.0)
    place = CommonPlace(
        key=entry.away_place_key, point=PLACE_A, label="Place", visit_count=1,
        short_stay_count=0, long_stay_count=1, long_stay_driver="driver1",
    )

    resolved = _resolve_trip_driver(entry, place, profiles, "driver2")

    assert resolved.driver_label == "driver2"
    assert resolved.display_name == "Christer"
    assert resolved.source == "manual-trip"
    assert resolved.confidence == 1.0


def test_resolve_trip_driver_disambiguates_short_vs_long_stay_rule():
    profiles = make_profiles()
    key = place_key(PLACE_A)
    place = CommonPlace(
        key=key, point=PLACE_A, label="Place", visit_count=2,
        short_stay_count=1, long_stay_count=1,
        short_stay_driver="driver2", long_stay_driver="driver1",
    )

    long_entry = _knowledge_entry(PLACE_A, "long", 40.0)
    short_entry = _knowledge_entry(PLACE_A, "short", 5.0)

    resolved_long = _resolve_trip_driver(long_entry, place, profiles, None)
    resolved_short = _resolve_trip_driver(short_entry, place, profiles, None)

    assert resolved_long.driver_label == "driver1" and resolved_long.source == "place-rule"
    assert resolved_short.driver_label == "driver2" and resolved_short.source == "place-rule"


def test_resolve_trip_driver_falls_back_to_best_candidate_then_undecided():
    from blackvue.trip.driver_detect import DriverMatch

    profiles = make_profiles()
    entry_no_place = _knowledge_entry(PLACE_A, "long", 40.0)
    entry_no_place = entry_no_place.__class__(
        **{**entry_no_place.__dict__, "away_place_key": None, "away_point": None}
    )

    # No place, no override, but a candidate from driver_detect's own
    # pattern matcher (as if a named RoutePattern already matched this
    # trip in increment 1) - the best-confidence one wins.
    entry_with_candidates = entry_no_place.__class__(
        **{
            **entry_no_place.__dict__,
            "candidates": (
                DriverMatch("driver1", "Fru", "Somewhere", 0.4, "unverified"),
                DriverMatch("driver2", "Christer", "Somewhere", 0.9, "verified"),
            ),
        }
    )
    resolved = _resolve_trip_driver(entry_with_candidates, None, profiles, None)
    assert resolved.driver_label == "driver2" and resolved.source == "pattern-match"
    assert resolved.confidence == 0.9

    resolved_undecided = _resolve_trip_driver(entry_no_place, None, profiles, None)
    assert resolved_undecided.driver_label is None
    assert resolved_undecided.source == "undecided"


def test_undecided_trips_filters_by_source():
    resolved = _knowledge_entry(PLACE_A, "long", 40.0)  # source defaults "undecided"
    decided = resolved.__class__(
        **{**resolved.__dict__, "driver_label": "driver1", "source": "place-rule"}
    )
    assert undecided_trips([resolved, decided]) == [resolved]


def test_build_knowledge_base_end_to_end_with_real_trip_objects():
    profiles = make_profiles()
    t0 = datetime(2026, 1, 5, 8, 0)  # a Monday
    trip_a = make_trip(t0)
    t1 = t0 + timedelta(minutes=60)
    trip_b = make_trip(t1)

    outbound_fix = make_fix(HOME, PLACE_A, t0, t0)
    inbound_fix = make_fix(PLACE_A, HOME, t1, t1)

    resolved, places = build_knowledge_base(
        [trip_a, trip_b], [outbound_fix, inbound_fix], profiles, {"home": HOME},
    )

    assert len(resolved) == 2
    assert resolved[0].weekday == "Monday"
    assert resolved[0].away_place_key == place_key(PLACE_A)
    # Both legs of the same round trip share the one 60-minute dwell -
    # the gap between this trip's own end and the other trip's start.
    assert resolved[0].dwell_minutes is not None and abs(resolved[0].dwell_minutes - 60.0) < 0.01
    assert resolved[0].stop_category == "long"
    assert len(places) == 1
    assert next(iter(places.values())).visit_count == 2


def test_save_and_load_knowledge_base_round_trip_preserves_overrides():
    profiles = make_profiles()
    entry = _knowledge_entry(PLACE_A, "long", 40.0)
    key = entry.away_place_key
    place = CommonPlace(
        key=key, point=PLACE_A, label="My place", visit_count=1,
        short_stay_count=0, long_stay_count=1, long_stay_driver="driver1",
    )
    resolved = _resolve_trip_driver(entry, place, profiles, None)

    tmp_path = Path(tempfile.mkdtemp()) / "driver_knowledge.json"
    save_knowledge_base(
        tmp_path,
        trips=[resolved],
        places={key: place},
        trip_overrides={"trip_x": "driver2"},
    )

    loaded = load_knowledge_base(tmp_path)
    assert loaded is not None
    loaded_trips, loaded_places, loaded_overrides = loaded

    assert len(loaded_trips) == 1
    assert loaded_trips[0].driver_label == "driver1"
    assert loaded_trips[0].away_point == PLACE_A
    assert loaded_places[key].label == "My place"
    assert loaded_places[key].long_stay_driver == "driver1"
    assert loaded_overrides == {"trip_x": "driver2"}


def test_load_knowledge_base_returns_none_when_missing():
    missing = Path(tempfile.mkdtemp()) / "does_not_exist.json"
    assert load_knowledge_base(missing) is None


def test_rebuild_preserves_manual_place_rules_via_existing_places_arg():
    profiles = make_profiles()
    t0 = datetime(2026, 1, 5, 8, 0)
    trip_a = make_trip(t0)
    t1 = t0 + timedelta(minutes=60)
    trip_b = make_trip(t1)
    outbound_fix = make_fix(HOME, PLACE_A, t0, t0)
    inbound_fix = make_fix(PLACE_A, HOME, t1, t1)

    # First build: no rule set yet, trip stays undecided.
    first_resolved, first_places = build_knowledge_base(
        [trip_a, trip_b], [outbound_fix, inbound_fix], profiles, {"home": HOME},
    )
    assert first_resolved[0].source == "undecided"

    # Christer sets a rule by hand (simulating the web form POST).
    key = next(iter(first_places))
    edited_places = dict(first_places)
    edited_places[key] = edited_places[key].__class__(
        **{**edited_places[key].__dict__, "long_stay_driver": "driver1"}
    )

    # Rebuild (as `bv-drivers build` would do again later) - the rule
    # survives because `existing_places` carries it forward.
    second_resolved, second_places = build_knowledge_base(
        [trip_a, trip_b], [outbound_fix, inbound_fix], profiles, {"home": HOME},
        existing_places=edited_places,
    )
    assert second_places[key].long_stay_driver == "driver1"
    assert second_resolved[0].driver_label == "driver1"
    assert second_resolved[0].source == "place-rule"
