"""Tests for trip/place_knowledge.py - the driver-knowledge-base
increment (common places + parked/no-parking stay rules + per-trip
overrides) built on top of driver_detect.py's route/dwell-time matcher.

Coordinates are fabricated stand-ins, same convention as
test_driver_detect.py - chosen far enough apart that
home_radius_meters/_SAME_PLACE_RADIUS_METERS cleanly separate "near"
from "far", and close enough together within place_key()'s own grid
cell that two visits to "the same place" actually land on the same
key.
"""

from __future__ import annotations

import tempfile
from dataclasses import replace
from datetime import date, datetime, timedelta
from pathlib import Path

from blackvue.trip.driver_detect import DriverProfile
from blackvue.trip.driver_detect import DriverProfiles
from blackvue.trip.driver_detect import TripFix
from blackvue.trip.place_knowledge import CommonPlace
from blackvue.trip.place_knowledge import TripKnowledge
from blackvue.trip.place_knowledge import default_common_places_path
from blackvue.trip.place_knowledge import load_common_places_mirror
from blackvue.trip.place_knowledge import _assign_place_clusters
from blackvue.trip.place_knowledge import _merge_nearby_places
from blackvue.trip.place_knowledge import _resolve_trip_driver
from blackvue.trip.place_knowledge import build_common_places
from blackvue.trip.place_knowledge import build_knowledge_base
from blackvue.trip.place_knowledge import bulk_assign_undecided_trips
from blackvue.trip.place_knowledge import dwell_at_destination
from blackvue.trip.place_knowledge import group_trips_by_place
from blackvue.trip.place_knowledge import load_knowledge_base
from blackvue.trip.place_knowledge import local_weekday_and_time
from blackvue.trip.place_knowledge import mixed_driver_place_keys
from blackvue.trip.place_knowledge import place_key
from blackvue.trip.place_knowledge import save_knowledge_base
from blackvue.trip.place_knowledge import smoothness_raw_from_samples
from blackvue.trip.place_knowledge import smoothness_score
from blackvue.trip.place_knowledge import stop_category
from blackvue.trip.place_knowledge import suggest_closest_decided_trip
from blackvue.trip.place_knowledge import undecided_places
from blackvue.trip.place_knowledge import undecided_trips
from blackvue.trip.place_knowledge import _time_of_day_distance_minutes
from blackvue.trip.trip import Trip

HOME = (59.3050, 18.1010)
PLACE_A = (59.3600, 18.0000)
PLACE_B = (59.4200, 17.9200)

# Fabricated splinters near PLACE_A, standing in for Christer's real
# Sickla-area fragmentation (single-visit "places" 15-160m apart that
# used to land in different place_key() grid cells) - all at the same
# longitude as PLACE_A so the offset is pure latitude, ~111320m/degree,
# making the distances easy to reason about.
PLACE_A_NEAR = (59.3600 + 0.0004, 18.0000)  # ~44m from PLACE_A - within _CLUSTER_RADIUS_METERS (150m)
PLACE_A_TOO_FAR = (59.3600 + 0.002, 18.0000)  # ~223m from PLACE_A - outside the radius
PLACE_A_CLUSTER_2 = (59.3600 + 0.0009, 18.0000)  # ~100m from PLACE_A, ~78m from PLACE_A_NEAR
PLACE_A_NEARER_TO_CLUSTER_2 = (59.3600 + 0.0007, 18.0000)  # ~78m from PLACE_A, ~22m from PLACE_A_CLUSTER_2


class FakeRecordingId:
    def __init__(self, timestamp: datetime, *, is_parking: bool = False) -> None:
        self.timestamp = timestamp
        # _raw_trip_knowledge() reads first_recording.id.value/
        # last_recording.id.value (for the video-link fields added
        # alongside start_point/end_point) the same way the real
        # RecordingId.value is read elsewhere - a plain deterministic
        # stand-in is enough for these tests, it never has to parse
        # back into a real RecordingId.
        self.value = f"{timestamp:%Y%m%d_%H%M%S}"
        # _trip_has_downloaded_parking_footage() reads .id.is_parking -
        # a plain bool stand-in for RecordingId's own is_parking
        # property (see that function's own docstring for why this,
        # not the old wall-clock dwell threshold, is what
        # stop_category() now gates on).
        self.is_parking = is_parking


class FakeAsset:
    """Stand-in for archive.asset.Asset - _trip_has_downloaded_parking_
    footage() only ever reads .is_downloaded off whatever's in
    Recording.assets."""

    def __init__(self, is_downloaded: bool = True) -> None:
        self.is_downloaded = is_downloaded


class FakeRecording:
    def __init__(self, timestamp: datetime, *, is_parking: bool = False) -> None:
        self.id = FakeRecordingId(timestamp, is_parking=is_parking)
        # A downloaded asset only when is_parking - mirrors the real
        # "generated-only P id isn't camera evidence" distinction
        # _trip_has_downloaded_parking_footage()'s own docstring warns
        # about, even though these fakes never actually need to
        # exercise that distinction on its own.
        self.assets = (FakeAsset(is_downloaded=True),) if is_parking else ()


def make_fix(start, end, start_t, end_t) -> TripFix:
    return TripFix(start=start, end=end, start_time=start_t, end_time=end_t)


def make_trip(timestamp: datetime, *, is_parking: bool = False) -> Trip:
    return Trip(recordings=(FakeRecording(timestamp, is_parking=is_parking),))


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


def test_stop_category_reflects_parking_footage_presence():
    # Christer: "Long and short are not in the game anymore, more like
    # if you get a P file after its long" - stop_category() now gates
    # on whether the stop ended in a downloaded Parking-mode recording,
    # not a wall-clock dwell-minutes threshold.
    assert stop_category(None) is None
    assert stop_category(False) == "no-parking"
    assert stop_category(True) == "parked"


def test_place_key_is_deterministic_within_a_grid_cell():
    assert place_key((59.30512, 18.08912)) == place_key((59.30519, 18.08918))
    assert place_key(HOME) != place_key(PLACE_A)


def _raw_entry(place_point, trip_label="trip_x", t0=None) -> TripKnowledge:
    """A pre-clustering TripKnowledge - away_place_key=None, same as
    _raw_trip_knowledge() itself produces (see that function's own
    comment: place identity is filled in afterward, by
    _assign_place_clusters(), not per-trip)."""

    t0 = t0 or datetime(2026, 1, 5, 8, 0)
    return TripKnowledge(
        trip_label=trip_label,
        start_time=t0,
        end_time=t0,
        weekday="Monday",
        start_time_of_day="08:00",
        away_place_key=None,
        away_point=place_point,
        dwell_minutes=None,
        stop_category=None,
        candidates=(),
    )


def test_merge_nearby_places_merges_places_within_cluster_radius():
    # Christer's own real registry had two separately-keyed "Hemmet
    # för gamla" entries only 46m apart - this is that scenario in
    # miniature: same physical place, two grid-rounded keys.
    existing = {
        "key_big": CommonPlace(
            key="key_big", point=PLACE_A, label="Big place",
            visit_count=5, driver=None,
        ),
        "key_small": CommonPlace(
            key="key_small", point=PLACE_A_NEAR, label="Small place",
            visit_count=2, driver="driver1",
        ),
    }

    merged = _merge_nearby_places(existing)

    assert len(merged) == 1
    place = merged["key_big"]
    # Largest-visit_count-first: key_big is the anchor, its own label
    # wins untouched.
    assert place.label == "Big place"
    assert place.visit_count == 7
    # Anchor had no driver set; the merged-away place did - that
    # already-made decision carries over rather than vanishing.
    assert place.driver == "driver1"


def test_merge_nearby_places_anchor_driver_wins_over_merged_away_driver():
    existing = {
        "key_big": CommonPlace(
            key="key_big", point=PLACE_A, label="Big place",
            visit_count=5, driver="driver2",
        ),
        "key_small": CommonPlace(
            key="key_small", point=PLACE_A_NEAR, label="Small place",
            visit_count=2, driver="driver1",
        ),
    }

    merged = _merge_nearby_places(existing)

    assert len(merged) == 1
    assert merged["key_big"].driver == "driver2"


def test_merge_nearby_places_leaves_distant_places_separate():
    existing = {
        "key_a": CommonPlace(key="key_a", point=PLACE_A, label="A", visit_count=3),
        "key_b": CommonPlace(key="key_b", point=PLACE_B, label="B", visit_count=4),
    }

    merged = _merge_nearby_places(existing)

    assert len(merged) == 2
    assert merged["key_a"].visit_count == 3
    assert merged["key_b"].visit_count == 4


def test_assign_place_clusters_snaps_new_trip_onto_existing_place_within_radius():
    existing = {
        "key_a": CommonPlace(key="key_a", point=PLACE_A, label="A", visit_count=3),
    }
    entries = [_raw_entry(PLACE_A_NEAR)]

    updated = _assign_place_clusters(entries, existing)

    assert updated[0].away_place_key == "key_a"


def test_assign_place_clusters_mints_new_place_beyond_radius():
    existing = {
        "key_a": CommonPlace(key="key_a", point=PLACE_A, label="A", visit_count=3),
    }
    entries = [_raw_entry(PLACE_A_TOO_FAR)]

    updated = _assign_place_clusters(entries, existing)

    assert updated[0].away_place_key == place_key(PLACE_A_TOO_FAR)
    assert updated[0].away_place_key != "key_a"


def test_assign_place_clusters_snaps_onto_nearest_cluster_when_multiple_in_range():
    existing = {
        "key_a": CommonPlace(key="key_a", point=PLACE_A, label="A", visit_count=3),
        "key_c2": CommonPlace(
            key="key_c2", point=PLACE_A_CLUSTER_2, label="C2", visit_count=3,
        ),
    }
    entries = [_raw_entry(PLACE_A_NEARER_TO_CLUSTER_2)]

    updated = _assign_place_clusters(entries, existing)

    assert updated[0].away_place_key == "key_c2"


def test_assign_place_clusters_passes_through_trips_with_no_away_point():
    entry = _raw_entry(None)
    updated = _assign_place_clusters([entry], None)

    assert updated[0].away_place_key is None


def test_assign_place_clusters_groups_new_nearby_trips_with_no_existing_registry():
    # The core fragmentation fix, with no prior registry at all: two
    # trips to points 44m apart both end up under the same key, rather
    # than minting two separate single-visit "places" the way plain
    # place_key() grid rounding used to.
    entries = [
        _raw_entry(PLACE_A, trip_label="trip_1"),
        _raw_entry(PLACE_A_NEAR, trip_label="trip_2"),
    ]

    updated = _assign_place_clusters(entries, None)

    assert updated[0].away_place_key == updated[1].away_place_key


def test_dwell_at_destination_excludes_home_only_trips():
    trip_local = make_fix(HOME, HOME, datetime(2026, 1, 5, 8, 0), datetime(2026, 1, 5, 8, 5))
    assert dwell_at_destination(trip_local, None, None, HOME, 300.0) is None


def test_dwell_at_destination_home_to_away_uses_next_trip_start():
    # dwell_at_destination() is informational-only now (the "Stay: ~N
    # min" a trip displays) - stop_category() no longer derives from
    # it (see that function's own docstring), so this only checks the
    # minute count itself.
    t0 = datetime(2026, 1, 5, 8, 0)
    outbound = make_fix(HOME, PLACE_A, t0, t0 + timedelta(minutes=20))
    t_return_start = t0 + timedelta(minutes=60)
    inbound = make_fix(PLACE_A, HOME, t_return_start, t_return_start + timedelta(minutes=20))

    dwell = dwell_at_destination(outbound, None, inbound, HOME, 300.0)
    assert dwell is not None and abs(dwell - 40.0) < 0.01


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


def test_build_common_places_counts_visits():
    # Visit counting no longer cares which stop_category a trip landed
    # in - see CommonPlace's own docstring: the parked/no-parking split
    # was collapsed to one driver rule per place, because the P-ending
    # trip filter makes "no-parking" essentially always empty for real
    # data anyway.
    entries = [
        _knowledge_entry(PLACE_A, "parked", 40.0),
        _knowledge_entry(PLACE_A, "no-parking", 5.0),
        _knowledge_entry(PLACE_A, "no-parking", 3.0),
    ]
    places = build_common_places(entries)

    assert len(places) == 1
    place = next(iter(places.values()))
    assert place.visit_count == 3
    assert place.driver is None


def test_build_common_places_carries_forward_existing_rule_and_label():
    entries = [_knowledge_entry(PLACE_A, "parked", 40.0)]
    key = place_key(PLACE_A)
    existing = {
        key: CommonPlace(
            key=key,
            point=PLACE_A,
            label="Grandma's house",
            visit_count=1,
            driver="driver2",
        )
    }

    places = build_common_places(entries, existing=existing)

    place = places[key]
    assert place.label == "Grandma's house"
    assert place.driver == "driver2"
    # visit_count itself is recomputed fresh from `entries`, not carried:
    assert place.visit_count == 1


def test_undecided_places_needs_min_visits_and_a_missing_rule():
    rare_place = CommonPlace(key="rare", point=PLACE_A, label="rare", visit_count=1)
    common_undecided = CommonPlace(
        key="other", point=(1.0, 1.0), label="common", visit_count=5,
    )
    common_decided = CommonPlace(
        key="decided", point=(2.0, 2.0), label="decided", visit_count=5,
        driver="driver1",
    )

    result = undecided_places(
        {"rare": rare_place, "other": common_undecided, "decided": common_decided},
        min_visits=2,
    )

    assert result == [common_undecided]


def _resolved_trip(place_point, driver_label, *, source="manual-trip") -> TripKnowledge:
    """A trip already resolved to a driver at `place_point`, the shape
    mixed_driver_place_keys() looks at - real ones come out of
    _resolve_trip_driver(), but the tests below only care about
    away_place_key/driver_label/source."""

    t0 = datetime(2026, 1, 5, 8, 0)
    return TripKnowledge(
        trip_label="trip_x",
        start_time=t0,
        end_time=t0,
        weekday="Monday",
        start_time_of_day="08:00",
        away_place_key=place_key(place_point),
        away_point=place_point,
        dwell_minutes=10.0,
        stop_category="parked",
        candidates=(),
        driver_label=driver_label,
        source=source,
    )


def test_mixed_driver_place_keys_flags_a_place_split_across_drivers():
    # Christer's real "Globen Parking": both drivers actually go there,
    # each trip resolved individually via a per-trip override.
    trips = [
        _resolved_trip(PLACE_A, "driver1"),
        _resolved_trip(PLACE_A, "driver2"),
        _resolved_trip(PLACE_B, "driver1"),
        _resolved_trip(PLACE_B, "driver1"),
    ]

    assert mixed_driver_place_keys(trips) == {place_key(PLACE_A)}


def test_mixed_driver_place_keys_ignores_undecided_and_placeless_entries():
    undecided = _resolved_trip(PLACE_A, "driver1", source="undecided")
    no_place = replace(_resolved_trip(PLACE_A, "driver2"), away_place_key=None)

    assert mixed_driver_place_keys([undecided, no_place]) == set()


def test_undecided_places_excludes_mixed_places_when_trips_given():
    mixed_key = place_key(PLACE_A)
    common_mixed = CommonPlace(
        key=mixed_key, point=PLACE_A, label="Globen Parking", visit_count=6,
    )
    common_undecided = CommonPlace(
        key="other", point=(1.0, 1.0), label="common", visit_count=5,
    )
    trips = [
        _resolved_trip(PLACE_A, "driver1"),
        _resolved_trip(PLACE_A, "driver2"),
    ]

    result = undecided_places(
        {mixed_key: common_mixed, "other": common_undecided},
        min_visits=2,
        trips=trips,
    )

    assert result == [common_undecided]

    # Backward-compatible: omitting trips= doesn't filter anything out,
    # so older call sites that never learned about "mixed" still work.
    assert undecided_places(
        {mixed_key: common_mixed, "other": common_undecided}, min_visits=2,
    ) == [common_mixed, common_undecided]


def test_resolve_trip_driver_prefers_manual_trip_override_over_place_rule():
    profiles = make_profiles()
    entry = _knowledge_entry(PLACE_A, "parked", 40.0)
    place = CommonPlace(
        key=entry.away_place_key, point=PLACE_A, label="Place", visit_count=1,
        driver="driver1",
    )

    resolved = _resolve_trip_driver(entry, place, profiles, "driver2")

    assert resolved.driver_label == "driver2"
    assert resolved.display_name == "Christer"
    assert resolved.source == "manual-trip"
    assert resolved.confidence == 1.0


def test_resolve_trip_driver_applies_place_rule_regardless_of_stop_category():
    # One driver per place now (Christer: "Remove it, one driver per
    # place") - the same place.driver rule applies whether this
    # particular trip ended "parked" or "no-parking".
    profiles = make_profiles()
    key = place_key(PLACE_A)
    place = CommonPlace(
        key=key, point=PLACE_A, label="Place", visit_count=2, driver="driver1",
    )

    parked_entry = _knowledge_entry(PLACE_A, "parked", 40.0)
    no_parking_entry = _knowledge_entry(PLACE_A, "no-parking", 5.0)

    resolved_parked = _resolve_trip_driver(parked_entry, place, profiles, None)
    resolved_no_parking = _resolve_trip_driver(no_parking_entry, place, profiles, None)

    assert resolved_parked.driver_label == "driver1" and resolved_parked.source == "place-rule"
    assert resolved_no_parking.driver_label == "driver1" and resolved_no_parking.source == "place-rule"


def test_resolve_trip_driver_falls_back_to_best_candidate_then_undecided():
    from blackvue.trip.driver_detect import DriverMatch

    profiles = make_profiles()
    entry_no_place = _knowledge_entry(PLACE_A, "parked", 40.0)
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


def test_resolve_trip_driver_pattern_match_uses_current_display_name_not_stale_candidate():
    from blackvue.trip.driver_detect import DriverMatch

    # Christer: renamed "Fru" to "Dao" via /drivers' inline rename form,
    # then couldn't find her trips filtering the Specific trips list by
    # "Dao". driver_label survived the rename fine (rename never touches
    # labels), but this entry's own `candidates` are a snapshot from
    # whatever build/rescan first produced them - taken *before* the
    # rename, still saying "Fru" - and _resolve_trip_driver()'s
    # pattern-match branch used to read display_name straight off that
    # stale candidate instead of looking it up fresh in the *current*
    # profiles, the same way the override/place-rule branches above it
    # already do. profiles here already has driver1 renamed to "Dao";
    # the candidate is deliberately built with the pre-rename "Fru" to
    # prove the resolved display_name comes from `profiles`, not the
    # candidate.
    profiles = DriverProfiles(
        home_name="Home", home_query="Home", home_radius_meters=300.0,
        drivers=(
            DriverProfile(label="driver1", display_name="Dao", patterns=()),
            DriverProfile(label="driver2", display_name="Christer", patterns=()),
        ),
    )
    entry = _knowledge_entry(PLACE_A, "parked", 40.0)
    entry = entry.__class__(
        **{
            **entry.__dict__,
            "away_place_key": None,
            "away_point": None,
            "candidates": (DriverMatch("driver1", "Fru", "Somewhere", 0.6, "stale"),),
        }
    )

    resolved = _resolve_trip_driver(entry, None, profiles, None)

    assert resolved.driver_label == "driver1"
    assert resolved.source == "pattern-match"
    assert resolved.display_name == "Dao"


def test_undecided_trips_filters_by_source():
    resolved = _knowledge_entry(PLACE_A, "parked", 40.0)  # source defaults "undecided"
    decided = resolved.__class__(
        **{**resolved.__dict__, "driver_label": "driver1", "source": "place-rule"}
    )
    assert undecided_trips([resolved, decided]) == [resolved]


def _entry_on(day, *, trip_label="trip_x", source="undecided"):
    entry = _knowledge_entry(PLACE_A, "parked", 40.0)
    start = datetime(2026, 8, day, 8, 0)
    return entry.__class__(
        **{
            **entry.__dict__,
            "trip_label": trip_label,
            "start_time": start,
            "end_time": start,
            "source": source,
        }
    )


def test_bulk_assign_undecided_trips_only_touches_undecided_in_range():
    in_range_undecided = _entry_on(10, trip_label="trip_in_range")
    in_range_decided = _entry_on(
        11, trip_label="trip_already_resolved", source="place-rule"
    )
    out_of_range_undecided = _entry_on(20, trip_label="trip_out_of_range")

    updated = bulk_assign_undecided_trips(
        [in_range_undecided, in_range_decided, out_of_range_undecided],
        {},
        from_date=date(2026, 8, 9),
        until_date=date(2026, 8, 12),
        driver_label="christer",
    )

    assert updated == {"trip_in_range": "christer"}


def test_bulk_assign_undecided_trips_is_inclusive_of_both_endpoints():
    first_day = _entry_on(9, trip_label="trip_first_day")
    last_day = _entry_on(12, trip_label="trip_last_day")

    updated = bulk_assign_undecided_trips(
        [first_day, last_day],
        {},
        from_date=date(2026, 8, 9),
        until_date=date(2026, 8, 12),
        driver_label="christer",
    )

    assert updated == {"trip_first_day": "christer", "trip_last_day": "christer"}


def test_bulk_assign_undecided_trips_preserves_unrelated_existing_overrides():
    in_range = _entry_on(10, trip_label="trip_in_range")

    updated = bulk_assign_undecided_trips(
        [in_range],
        {"trip_elsewhere": "fru"},
        from_date=date(2026, 8, 9),
        until_date=date(2026, 8, 12),
        driver_label="christer",
    )

    assert updated == {"trip_elsewhere": "fru", "trip_in_range": "christer"}


def _entry_at(place_point, day, *, trip_label, source="undecided"):
    """Same shape as _entry_on() above but lets the caller pick which
    place the trip resolves to - needed for group_trips_by_place()'s
    own tests, which (unlike the bulk-assign tests) care about more
    than one distinct place."""

    entry = _knowledge_entry(place_point, "parked", 40.0)
    start = datetime(2026, 8, day, 8, 0)
    return entry.__class__(
        **{
            **entry.__dict__,
            "trip_label": trip_label,
            "start_time": start,
            "end_time": start,
            "source": source,
        }
    )


def test_group_trips_by_place_groups_by_away_place_key():
    trip_a1 = _entry_at(PLACE_A, 5, trip_label="trip_a1")
    trip_a2 = _entry_at(PLACE_A, 10, trip_label="trip_a2")
    trip_b1 = _entry_at(PLACE_B, 7, trip_label="trip_b1")

    grouped = group_trips_by_place([trip_a1, trip_a2, trip_b1])

    assert set(grouped.keys()) == {place_key(PLACE_A), place_key(PLACE_B)}
    assert {e.trip_label for e in grouped[place_key(PLACE_A)]} == {"trip_a1", "trip_a2"}
    assert [e.trip_label for e in grouped[place_key(PLACE_B)]] == ["trip_b1"]


def test_group_trips_by_place_sorts_most_recent_first():
    earliest = _entry_at(PLACE_A, 1, trip_label="trip_earliest")
    latest = _entry_at(PLACE_A, 20, trip_label="trip_latest")
    middle = _entry_at(PLACE_A, 10, trip_label="trip_middle")

    grouped = group_trips_by_place([earliest, latest, middle])

    assert [e.trip_label for e in grouped[place_key(PLACE_A)]] == [
        "trip_latest",
        "trip_middle",
        "trip_earliest",
    ]


def test_group_trips_by_place_skips_trips_with_no_away_place_key():
    entry = _entry_at(PLACE_A, 5, trip_label="trip_a")
    no_away = entry.__class__(**{**entry.__dict__, "away_place_key": None})

    grouped = group_trips_by_place([entry, no_away])

    assert sum(len(v) for v in grouped.values()) == 1
    assert grouped[place_key(PLACE_A)][0].trip_label == "trip_a"


def test_build_knowledge_base_end_to_end_with_real_trip_objects():
    profiles = make_profiles()
    t0 = datetime(2026, 1, 5, 8, 0)  # a Monday
    # trip_a's own tail is the stop at PLACE_A (home->away leg) - give
    # it a downloaded Parking-mode recording so both legs of this
    # round trip resolve to stop_category "parked" (see
    # _raw_trip_knowledge()'s own docstring for why the away->home leg
    # borrows trip_a's parking status rather than having its own).
    trip_a = make_trip(t0, is_parking=True)
    t1 = t0 + timedelta(minutes=60)
    trip_b = make_trip(t1)

    outbound_fix = make_fix(HOME, PLACE_A, t0, t0)
    inbound_fix = make_fix(PLACE_A, HOME, t1, t1)

    resolved, places = build_knowledge_base(
        [trip_a, trip_b], [outbound_fix, inbound_fix], profiles, {"home": HOME},
        camera_id="kirby",
    )

    assert len(resolved) == 2
    assert resolved[0].weekday == "Monday"
    assert resolved[0].away_place_key == place_key(PLACE_A)
    # Both legs of the same round trip share the one 60-minute dwell -
    # the gap between this trip's own end and the other trip's start
    # (dwell_minutes is informational only now, see dwell_at_
    # destination()'s own docstring - it no longer drives the category).
    assert resolved[0].dwell_minutes is not None and abs(resolved[0].dwell_minutes - 60.0) < 0.01
    assert resolved[0].stop_category == "parked"
    assert resolved[1].stop_category == "parked"
    assert len(places) == 1
    assert next(iter(places.values())).visit_count == 2

    # start_point/end_point/first_recording_id/last_recording_id/
    # camera_id (task: link to first/last video + address of start
    # and stop) - trip_a is single-recording (make_trip() builds a
    # one-recording Trip), so its own start/end fix and first/last
    # recording id are the same outbound_fix/single recording.
    assert resolved[0].start_point == HOME
    assert resolved[0].end_point == PLACE_A
    assert resolved[0].first_recording_id == f"{t0:%Y%m%d_%H%M%S}"
    assert resolved[0].last_recording_id == f"{t0:%Y%m%d_%H%M%S}"
    assert resolved[0].camera_id == "kirby"
    assert resolved[1].start_point == PLACE_A
    assert resolved[1].end_point == HOME
    assert resolved[1].camera_id == "kirby"


def test_build_knowledge_base_round_trip_uses_via_point_as_away_point():
    """Christer: 'When i drive my wife to work in the morning, the
    trip starts and stops at Heliosgatan... it would be nice if we
    could get where i went, even if i returned to the starting
    place.' A single trip whose start/end are both near home but whose
    trip_fix.via_point reached PLACE_A (see driver_detect.
    resolve_via_point()) should cluster into a Common Place there -
    exactly like a real one-way trip's away_point already does - not
    be left with no destination at all."""

    profiles = make_profiles()
    t0 = datetime(2026, 1, 5, 8, 0)
    round_trip = make_trip(t0)
    round_trip_fix = TripFix(
        start=HOME, end=HOME, start_time=t0, end_time=t0, via_point=PLACE_A,
    )

    resolved, places = build_knowledge_base(
        [round_trip], [round_trip_fix], profiles, {"home": HOME},
    )

    assert len(resolved) == 1
    assert resolved[0].away_point == PLACE_A
    assert resolved[0].away_place_key == place_key(PLACE_A)
    assert len(places) == 1
    # No real "stop" happened here (see this test's own docstring) -
    # there's no adjacent-trip dwell to measure and no parking
    # footage evidence, so both stay unset, unlike a real one-way
    # trip's stop.
    assert resolved[0].dwell_minutes is None
    assert resolved[0].stop_category is None


def test_build_knowledge_base_round_trip_without_via_point_has_no_destination():
    """The pre-existing behavior this feature must not disturb: a
    round trip whose via_point was never resolved (e.g. the vehicle
    genuinely never left home - a garage motion blip) still gets no
    away_point at all, same as before this feature existed."""

    profiles = make_profiles()
    t0 = datetime(2026, 1, 5, 8, 0)
    round_trip = make_trip(t0)
    round_trip_fix = TripFix(start=HOME, end=HOME, start_time=t0, end_time=t0)

    resolved, places = build_knowledge_base(
        [round_trip], [round_trip_fix], profiles, {"home": HOME},
    )

    assert len(resolved) == 1
    assert resolved[0].away_point is None
    assert resolved[0].away_place_key is None
    assert places == {}


def test_build_knowledge_base_existing_trips_survive_a_scoped_rebuild():
    """Christer: 'i rub Driver KB just for today, and voila common
    places name gone.' bv_drivers.py's own caller only ever hands in
    `existing_trips` entries that fall outside whatever window this
    run actually rescanned (see that module's own carried_forward_
    trips comment) - build_knowledge_base() itself just needs to make
    sure those entries survive into `resolved` and still count toward
    `places`, not just whatever this call's own `trips`/`fixes` cover."""

    profiles = make_profiles()
    t0 = datetime(2026, 1, 5, 8, 0)

    # A place from a much earlier build - this run's own trips/fixes
    # never go near it at all.
    old_entry = _knowledge_entry(PLACE_B, "parked", 40.0)
    old_place = CommonPlace(
        key=place_key(PLACE_B), point=PLACE_B, label="Old place",
        visit_count=1, driver="driver1",
    )

    # This run only rescanned a trip to PLACE_A.
    trip_a = make_trip(t0)
    outbound_fix = make_fix(HOME, PLACE_A, t0, t0)

    resolved, places = build_knowledge_base(
        [trip_a], [outbound_fix], profiles, {"home": HOME},
        existing_places={old_place.key: old_place},
        existing_trips=[old_entry],
    )

    labels = {entry.trip_label for entry in resolved}
    assert labels == {"trip_x", trip_a.label}
    # The old place must still be in the returned registry - not
    # silently dropped just because this run's own trip list never
    # touched it.
    assert old_place.key in places
    assert places[old_place.key].label == "Old place"
    assert places[old_place.key].visit_count == 1


def test_build_knowledge_base_existing_trips_inside_scanned_window_are_not_duplicated():
    """A carried-forward entry whose trip_label matches one this run
    just (re)built must be replaced, not kept alongside it - the
    caller is expected to only pass in entries outside the scanned
    window, but build_knowledge_base() itself stays safe even if one
    slips through (e.g. a --max-gap change re-splitting a trip this
    run did rescan)."""

    profiles = make_profiles()
    t0 = datetime(2026, 1, 5, 8, 0)

    trip_a = make_trip(t0)
    outbound_fix = make_fix(HOME, PLACE_A, t0, t0)

    stale_entry = replace(
        _knowledge_entry(PLACE_B, "parked", 40.0), trip_label=trip_a.label,
    )

    resolved, _ = build_knowledge_base(
        [trip_a], [outbound_fix], profiles, {"home": HOME},
        existing_trips=[stale_entry],
    )

    assert len(resolved) == 1
    assert resolved[0].away_point == PLACE_A


def test_build_knowledge_base_existing_trips_reresolve_against_current_place_rule():
    """A carried-forward entry isn't just copied verbatim - it's reset
    to undecided and re-resolved against the *current* places/
    overrides, same reasoning reresolve_trip_drivers() already
    documents for its own reset. Otherwise a place rule Christer since
    removed would leave a scoped rebuild's carried-forward trips stuck
    showing the old driver forever."""

    profiles = make_profiles()
    stale_entry = replace(
        _knowledge_entry(PLACE_B, "parked", 40.0),
        driver_label="driver1", display_name="Fru",
        confidence=0.95, source="place-rule",
    )
    # No driver rule on this place anymore, and no trip in this run's
    # own scan touches PLACE_B at all.
    place_no_rule = CommonPlace(
        key=place_key(PLACE_B), point=PLACE_B, label="Old place",
        visit_count=1, driver=None,
    )

    t0 = datetime(2026, 1, 5, 8, 0)
    trip_a = make_trip(t0)
    outbound_fix = make_fix(HOME, PLACE_A, t0, t0)

    resolved, _ = build_knowledge_base(
        [trip_a], [outbound_fix], profiles, {"home": HOME},
        existing_places={place_no_rule.key: place_no_rule},
        existing_trips=[stale_entry],
    )

    carried = next(entry for entry in resolved if entry.trip_label == "trip_x")
    assert carried.source == "undecided"
    assert carried.driver_label is None


def test_build_knowledge_base_full_rebuild_unaffected_by_existing_trips_param():
    """The ordinary unscoped rebuild (no --from/--until/--timestamp,
    so bv_drivers.py's own caller passes existing_trips=[] - see
    interval covering everything) must behave exactly as it did before
    this parameter existed."""

    profiles = make_profiles()
    t0 = datetime(2026, 1, 5, 8, 0)
    trip_a = make_trip(t0)
    outbound_fix = make_fix(HOME, PLACE_A, t0, t0)

    resolved, places = build_knowledge_base(
        [trip_a], [outbound_fix], profiles, {"home": HOME},
        existing_trips=[],
    )

    assert len(resolved) == 1
    assert resolved[0].trip_label == trip_a.label
    assert len(places) == 1


def test_build_knowledge_base_defaults_camera_id_to_none():
    profiles = make_profiles()
    t0 = datetime(2026, 1, 5, 8, 0)
    trip_a = make_trip(t0)
    outbound_fix = make_fix(HOME, PLACE_A, t0, t0)

    resolved, _ = build_knowledge_base(
        [trip_a], [outbound_fix], profiles, {"home": HOME},
    )

    assert resolved[0].camera_id is None


def test_save_and_load_knowledge_base_round_trip_preserves_overrides():
    profiles = make_profiles()
    entry = _knowledge_entry(PLACE_A, "parked", 40.0)
    key = entry.away_place_key
    place = CommonPlace(
        key=key, point=PLACE_A, label="My place", visit_count=1, driver="driver1",
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
    assert loaded_places[key].driver == "driver1"
    assert loaded_overrides == {"trip_x": "driver2"}


def test_load_knowledge_base_returns_none_when_missing():
    missing = Path(tempfile.mkdtemp()) / "does_not_exist.json"
    assert load_knowledge_base(missing) is None


def test_save_knowledge_base_backs_up_existing_file_first():
    # Christer: "All common places has lost the beautiful names i gave
    # them" - traced to a `bv-drivers build` running against a
    # driver_knowledge.json that was itself already broken (0 places),
    # so build_common_places()'s label-carry-forward had nothing to
    # carry forward from and every place got the generic "Place near
    # lat, lon" fallback name instead. save_knowledge_base() now rolls
    # whatever was on disk into a `.bak` sibling before every overwrite
    # - this test is the "his labels would have survived" regression
    # check: an old, hand-labeled file gets clobbered by a fresh build,
    # but the pre-clobber content is recoverable from the backup.
    tmp_path = Path(tempfile.mkdtemp()) / "driver_knowledge.json"
    profiles = make_profiles()

    old_entry = _knowledge_entry(PLACE_A, "parked", 40.0)
    old_key = old_entry.away_place_key
    old_place = CommonPlace(
        key=old_key, point=PLACE_A, label="Christer's beautiful name",
        visit_count=3, driver="driver1",
    )
    old_resolved = _resolve_trip_driver(old_entry, old_place, profiles, None)
    save_knowledge_base(
        tmp_path, trips=[old_resolved], places={old_key: old_place},
        trip_overrides={},
    )

    # Simulate a broken rebuild overwriting it with a fresh, unlabeled
    # place (build_common_places() would do this for real if `existing`
    # had no matching key).
    new_entry = _knowledge_entry(PLACE_A, "parked", 45.0)
    new_key = new_entry.away_place_key
    new_place = CommonPlace(
        key=new_key, point=PLACE_A,
        label=f"Place near {PLACE_A[0]:.3f}, {PLACE_A[1]:.3f}",
        visit_count=1, driver=None,
    )
    save_knowledge_base(
        tmp_path, trips=[new_entry], places={new_key: new_place},
        trip_overrides={},
    )

    backup_path = tmp_path.with_suffix(tmp_path.suffix + ".bak")
    assert backup_path.is_file()

    backed_up = load_knowledge_base(backup_path)
    assert backed_up is not None
    _, backed_up_places, _ = backed_up
    assert backed_up_places[old_key].label == "Christer's beautiful name"
    assert backed_up_places[old_key].driver == "driver1"

    # The live file itself now holds the new (unlabeled) content -
    # the backup is a separate recovery copy, not a silent revert.
    current = load_knowledge_base(tmp_path)
    assert current is not None
    _, current_places, _ = current
    assert current_places[new_key].label.startswith("Place near")


def test_save_knowledge_base_first_save_creates_no_backup():
    # Nothing to back up yet - shouldn't create a stray .bak file out
    # of nowhere on a brand-new driver_knowledge.json.
    tmp_path = Path(tempfile.mkdtemp()) / "driver_knowledge.json"
    save_knowledge_base(tmp_path, trips=[], places={}, trip_overrides={})

    backup_path = tmp_path.with_suffix(tmp_path.suffix + ".bak")
    assert not backup_path.exists()


def test_save_knowledge_base_writes_common_places_mirror(tmp_path):
    # Christer's "crazy idea": a separate common_places.json, never
    # overwritten wholesale, only appended to / kept in sync -
    # save_knowledge_base() writes it alongside driver_knowledge.json
    # on every save.
    knowledge_path = tmp_path / "driver_knowledge.json"
    place = CommonPlace(
        key="place_a", point=PLACE_A, label="Grandma's house",
        visit_count=3, driver="driver1",
    )
    save_knowledge_base(
        knowledge_path, trips=[], places={"place_a": place}, trip_overrides={},
    )

    mirror_path = default_common_places_path(tmp_path)
    assert mirror_path.is_file()

    mirror = load_common_places_mirror(mirror_path)
    assert mirror is not None
    assert mirror["place_a"].label == "Grandma's house"
    assert mirror["place_a"].driver == "driver1"


def test_common_places_mirror_keeps_a_place_dropped_from_a_later_save(tmp_path):
    # The whole point of "append-only": a place that was known once
    # (e.g. before a scoped `--from`/`--until` rebuild, or filtered out
    # by min-visits somewhere upstream) must survive even if a later
    # save_knowledge_base() call doesn't include it in `places` at all.
    knowledge_path = tmp_path / "driver_knowledge.json"
    place_a = CommonPlace(
        key="place_a", point=PLACE_A, label="Grandma's house",
        visit_count=3, driver="driver1",
    )
    save_knowledge_base(
        knowledge_path, trips=[], places={"place_a": place_a}, trip_overrides={},
    )

    # A later save only knows about a different place - place_a is
    # absent from `places` this time, simulating a scoped rebuild.
    place_b = CommonPlace(
        key="place_b", point=PLACE_B, label="Work", visit_count=5, driver="driver2",
    )
    save_knowledge_base(
        knowledge_path, trips=[], places={"place_b": place_b}, trip_overrides={},
    )

    mirror = load_common_places_mirror(default_common_places_path(tmp_path))
    assert mirror is not None
    assert mirror["place_a"].label == "Grandma's house"
    assert mirror["place_b"].label == "Work"


def test_common_places_mirror_refreshes_an_existing_place(tmp_path):
    # Confirmed via AskUserQuestion: a "living mirror", not a frozen
    # audit log - relabeling a place or changing its driver on
    # /drivers (a later save_knowledge_base() call with the same key
    # but different label/driver) must be reflected in the mirror too.
    knowledge_path = tmp_path / "driver_knowledge.json"
    place = CommonPlace(
        key="place_a", point=PLACE_A, label="Old label",
        visit_count=1, driver=None,
    )
    save_knowledge_base(
        knowledge_path, trips=[], places={"place_a": place}, trip_overrides={},
    )

    renamed = CommonPlace(
        key="place_a", point=PLACE_A, label="Renamed place",
        visit_count=2, driver="driver1",
    )
    save_knowledge_base(
        knowledge_path, trips=[], places={"place_a": renamed}, trip_overrides={},
    )

    mirror = load_common_places_mirror(default_common_places_path(tmp_path))
    assert mirror is not None
    assert mirror["place_a"].label == "Renamed place"
    assert mirror["place_a"].driver == "driver1"
    assert mirror["place_a"].visit_count == 2


def test_load_common_places_mirror_returns_none_when_missing(tmp_path):
    assert load_common_places_mirror(default_common_places_path(tmp_path)) is None


def test_place_from_dict_prefers_new_driver_key():
    from blackvue.trip.place_knowledge import _place_from_dict

    data = {
        "point": [PLACE_A[0], PLACE_A[1]],
        "label": "New place",
        "visit_count": 5,
        "driver": "driver1",
        # Stale leftovers from an older generation's file must not
        # override the new `driver` key once it's present.
        "parked_driver": "driver2",
    }

    place = _place_from_dict("some_key", data)

    assert place.driver == "driver1"


def test_place_from_dict_migrates_parked_driver_over_no_parking_driver():
    """A driver_knowledge.json written before the parked/no-parking
    split was collapsed back into one `driver` field (Christer: "If no
    parking sidecars, then there is no trip.") still has
    parked_driver/no_parking_driver keys. Checked against Christer's
    real registry: 6 places had parked_driver set, 0 had
    no_parking_driver - so parked_driver must win when both (or only
    parked_driver) are present."""

    from blackvue.trip.place_knowledge import _place_from_dict

    old_data = {
        "point": [PLACE_A[0], PLACE_A[1]],
        "label": "Old place",
        "visit_count": 5,
        "parked_driver": "driver1",
        "no_parking_driver": "driver2",
    }

    place = _place_from_dict("some_key", old_data)

    assert place.driver == "driver1"


def test_place_from_dict_falls_back_to_no_parking_driver_then_long_stay_driver():
    from blackvue.trip.place_knowledge import _place_from_dict

    only_no_parking = {
        "point": [PLACE_A[0], PLACE_A[1]],
        "label": "Old place",
        "visit_count": 5,
        "no_parking_driver": "driver2",
    }
    assert _place_from_dict("k1", only_no_parking).driver == "driver2"

    # Oldest generation still, from before *that* redesign (see
    # stop_category()'s own docstring): long_stay_driver/
    # short_stay_driver instead of parked_driver/no_parking_driver.
    only_long_stay = {
        "point": [PLACE_A[0], PLACE_A[1]],
        "label": "Old place",
        "visit_count": 5,
        "long_stay_driver": "driver1",
    }
    assert _place_from_dict("k2", only_long_stay).driver == "driver1"


def test_trip_from_dict_migrates_old_short_long_stop_category():
    """A driver_knowledge.json trip entry written before the P-file
    redesign might still have stop_category "short"/"long" - both
    should migrate to the equivalent new category so place-rule
    resolution keeps working immediately after upgrade, without
    requiring a fresh `bv-drivers build` first."""

    from blackvue.trip.place_knowledge import _trip_to_dict, _trip_from_dict

    entry = _knowledge_entry(PLACE_A, "parked", 40.0)
    data = _trip_to_dict(entry)
    data["stop_category"] = "long"
    restored = _trip_from_dict(data)
    assert restored.stop_category == "parked"

    data["stop_category"] = "short"
    restored = _trip_from_dict(data)
    assert restored.stop_category == "no-parking"


def test_rebuild_preserves_manual_place_rules_via_existing_places_arg():
    profiles = make_profiles()
    t0 = datetime(2026, 1, 5, 8, 0)
    # trip_a's own tail is the stop at PLACE_A - needs a downloaded
    # Parking recording for the round trip to resolve "parked" (see
    # test_build_knowledge_base_end_to_end_with_real_trip_objects's own
    # comment for why).
    trip_a = make_trip(t0, is_parking=True)
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
        **{**edited_places[key].__dict__, "driver": "driver1"}
    )

    # Rebuild (as `bv-drivers build` would do again later) - the rule
    # survives because `existing_places` carries it forward.
    second_resolved, second_places = build_knowledge_base(
        [trip_a, trip_b], [outbound_fix, inbound_fix], profiles, {"home": HOME},
        existing_places=edited_places,
    )
    assert second_places[key].driver == "driver1"
    assert second_resolved[0].driver_label == "driver1"
    assert second_resolved[0].source == "place-rule"


class FakeGSensorSample:
    def __init__(self, x: int, y: int, z: int) -> None:
        self.x, self.y, self.z = x, y, z


def test_smoothness_raw_from_samples_uses_lateral_and_accel_brake_only():
    # x=3, y=4 -> magnitude 5 (3-4-5 triangle); z is deliberately huge
    # (a road bump) and must not affect the result at all.
    samples = [FakeGSensorSample(x=3, y=4, z=999), FakeGSensorSample(x=3, y=4, z=-999)]
    assert smoothness_raw_from_samples(samples) == 5.0


def test_smoothness_raw_from_samples_none_when_empty():
    assert smoothness_raw_from_samples([]) is None


def test_smoothness_score_spreads_evenly_across_a_population():
    # Hand-verified 10-element population: min maps to bucket 0, max to
    # bucket 9, spread roughly evenly in between.
    population = [float(n) for n in range(1, 11)]
    assert smoothness_score(1.0, population) == 0
    assert smoothness_score(5.0, population) == 4
    assert smoothness_score(10.0, population) == 9


def test_smoothness_score_none_when_raw_or_population_missing():
    assert smoothness_score(None, [1.0, 2.0, 3.0]) is None
    assert smoothness_score(5.0, []) is None


def test_time_of_day_distance_minutes_handles_midnight_wraparound():
    assert _time_of_day_distance_minutes("08:00", "08:00") == 0
    assert _time_of_day_distance_minutes("08:00", "08:30") == 30
    assert _time_of_day_distance_minutes("23:50", "00:10") == 20


def test_suggest_closest_decided_trip_prefers_same_place_over_same_weekday():
    entry = _entry_at(PLACE_A, 10, trip_label="trip_undecided")  # a Monday
    same_place_other_weekday = entry.__class__(
        **{
            **_entry_at(PLACE_A, 4, trip_label="trip_same_place").__dict__,
            "weekday": "Tuesday",
            "source": "place-rule",
        }
    )
    same_weekday_other_place = entry.__class__(
        **{
            **_entry_at(PLACE_B, 3, trip_label="trip_same_weekday").__dict__,
            "weekday": entry.weekday,
            "source": "place-rule",
        }
    )

    closest = suggest_closest_decided_trip(
        entry, [same_place_other_weekday, same_weekday_other_place]
    )

    assert closest is not None
    assert closest.trip_label == "trip_same_place"


def test_suggest_closest_decided_trip_excludes_undecided_and_self():
    entry = _entry_at(PLACE_A, 10, trip_label="trip_undecided")
    still_undecided = _entry_at(PLACE_A, 4, trip_label="trip_still_undecided")

    assert suggest_closest_decided_trip(entry, [entry, still_undecided]) is None


def test_suggest_closest_decided_trip_returns_none_with_no_candidates():
    entry = _entry_at(PLACE_A, 10, trip_label="trip_undecided")
    assert suggest_closest_decided_trip(entry, []) is None


def test_build_knowledge_base_threads_smoothness_values_by_index():
    profiles = make_profiles()
    t0 = datetime(2026, 1, 5, 8, 0)
    trip_a = make_trip(t0)
    t1 = t0 + timedelta(minutes=60)
    trip_b = make_trip(t1)
    outbound_fix = make_fix(HOME, PLACE_A, t0, t0)
    inbound_fix = make_fix(PLACE_A, HOME, t1, t1)

    resolved, _ = build_knowledge_base(
        [trip_a, trip_b], [outbound_fix, inbound_fix], profiles, {"home": HOME},
        smoothness_values=[1.5, None],
    )

    assert resolved[0].smoothness_raw == 1.5
    assert resolved[1].smoothness_raw is None


def test_trip_knowledge_smoothness_raw_round_trips_through_save_load():
    profiles = make_profiles()
    entry = _knowledge_entry(PLACE_A, "parked", 40.0)
    entry = entry.__class__(**{**entry.__dict__, "smoothness_raw": 2.75})
    key = entry.away_place_key
    place = CommonPlace(
        key=key, point=PLACE_A, label="My place", visit_count=1, driver="driver1",
    )
    resolved = _resolve_trip_driver(entry, place, profiles, None)

    tmp_path = Path(tempfile.mkdtemp()) / "driver_knowledge.json"
    save_knowledge_base(
        tmp_path, trips=[resolved], places={key: place}, trip_overrides={},
    )

    loaded = load_knowledge_base(tmp_path)
    assert loaded is not None
    loaded_trips, _, _ = loaded
    assert loaded_trips[0].smoothness_raw == 2.75
