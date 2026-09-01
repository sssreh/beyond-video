"""Tests for trip/driver_detect.py's route/dwell-time driver matcher.

Coordinates below are fabricated stand-ins (not real geocoded points -
match_driver() itself never geocodes anything, see resolve_known_points()
for the real I/O wrapper this module keeps separate on purpose) chosen
just far enough apart that DEFAULT_RADIUS_METERS/home_radius_meters
cleanly separate "near" from "far". Scenarios mirror Christer's own
verbatim route descriptions (see driver_detect.py's
christers_driver_profiles()), including the Norra Stationsgatan
same-place-opposite-parking-status disambiguation between the two
drivers (his wife's drop-off leaves the car parked; his own run is a
quick turnaround, no parking - see RoutePattern.requires_parking).
"""

import json
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

import blackvue.trip.driver_detect as driver_detect
from blackvue.telemetry.gps_reader import GpsFix
from blackvue.trip.driver_detect import (
    DriverProfile,
    DriverProfiles,
    RoutePattern,
    TripFix,
    add_driver,
    christers_driver_profiles,
    default_driver_profiles_path,
    driver_profiles_from_dict,
    driver_profiles_to_dict,
    load_driver_profiles,
    match_driver,
    rename_driver,
    resolve_via_point,
    save_driver_profiles,
    write_default_driver_profiles,
)
from blackvue.trip.trip import Trip

HOME = (59.3050, 18.1010)
SOLNA = (59.3600, 18.0000)
NORRA_STN = (59.3400, 18.0500)
NEAR_HOME = (59.3055, 18.1020)
FAR_AWAY_A = (10.0, 10.0)
FAR_AWAY_B = (20.0, 20.0)

KNOWN_POINTS = {
    "home": HOME,
    "Solna, Vintervägen 50": SOLNA,
    "Norra Stationsgatan": NORRA_STN,
}


def ts(value: str) -> datetime:
    return datetime.strptime(value, "%Y-%m-%d %H:%M:%S")


def test_christers_driver_profiles_uses_opaque_labels():
    profiles = christers_driver_profiles()

    assert profiles.drivers[0].label == "driver1"
    assert profiles.drivers[0].display_name == "Dao"
    assert profiles.drivers[1].label == "driver2"
    assert profiles.drivers[1].display_name == "Christer"


def test_christers_driver_profiles_home_is_precise_address():
    # Was home_query="Hammarby Sjöstad, Stockholm" (a neighborhood name)
    # with home_radius_meters=800.0, because the neighborhood's own
    # geocoded point sat ~750-760m from where Christer's car actually
    # arrives/departs home - dropping the radius to 300m once missed
    # every real home-adjacent GPS fix (0 away_points, 0 common places,
    # a real production run reported "164 trip(s), 0 place(s)").
    # Christer: "My parking garage is next to Heliosgatan 38, maybe
    # that help." Geocoding that address lands within 6m of the tight
    # real-arrival cluster his own trip data already implied, so
    # home_query now resolves to the actual garage, not the
    # neighborhood - which lets home_radius_meters shrink to 200.0
    # (verified: every real home-adjacent GPS fix in his 164-trip
    # archive is within 160m of the new point, with the next-nearest
    # distinct place 209m+ away). See christers_driver_profiles()'s own
    # comment for the full story.
    profiles = christers_driver_profiles()
    assert profiles.home_query == "Heliosgatan 38, Stockholm"
    assert profiles.home_radius_meters == 200.0


def test_simple_commute_match():
    profiles = christers_driver_profiles()
    trip = TripFix(
        start=HOME,
        end=SOLNA,
        start_time=ts("2026-08-29 07:00:00"),
        end_time=ts("2026-08-29 07:30:00"),
    )

    matches = match_driver(trip, None, None, profiles, KNOWN_POINTS)

    labels = {(m.driver_label, m.place) for m in matches}
    assert ("driver2", "Solna, Vintervägen 50") in labels


def test_requires_parking_true_match_and_disambiguation():
    """Wife's Norra Stationsgatan pattern requires the arriving leg to
    end in a downloaded Parking-mode recording - a leg with
    has_parking_footage=True should match her pattern at high
    confidence and must NOT also match Christer's own
    (requires_parking=False) pattern at the same place."""

    profiles = christers_driver_profiles()
    leg1 = TripFix(
        start=HOME,
        end=NORRA_STN,
        start_time=ts("2026-08-29 08:00:00"),
        end_time=ts("2026-08-29 08:15:00"),
        has_parking_footage=True,
    )

    matches = match_driver(leg1, None, None, profiles, KNOWN_POINTS)

    wife_matches = [
        m for m in matches if m.driver_label == "driver1" and m.place == "Norra Stationsgatan"
    ]
    christer_matches = [
        m for m in matches if m.driver_label == "driver2" and m.place == "Norra Stationsgatan"
    ]
    assert wife_matches
    assert wife_matches[0].confidence >= 0.85
    assert christer_matches == []


def test_requires_parking_false_match_and_disambiguation():
    """Christer's own Norra Stationsgatan pattern is a quick turnaround
    with no parking recording - a leg with has_parking_footage=False
    should match his pattern and must NOT match his wife's
    requires_parking=True pattern at the same place."""

    profiles = christers_driver_profiles()
    leg1 = TripFix(
        start=HOME,
        end=NORRA_STN,
        start_time=ts("2026-08-29 09:00:00"),
        end_time=ts("2026-08-29 09:15:00"),
        has_parking_footage=False,
    )

    matches = match_driver(leg1, None, None, profiles, KNOWN_POINTS)

    christer_matches = [
        m for m in matches if m.driver_label == "driver2" and m.place == "Norra Stationsgatan"
    ]
    wife_matches = [
        m for m in matches if m.driver_label == "driver1" and m.place == "Norra Stationsgatan"
    ]
    assert christer_matches
    assert wife_matches == []


def test_requires_parking_checked_via_prev_fix_for_return_leg():
    """A place->home leg's own parking status is irrelevant - what
    matters is whether the *outbound* leg (prev_fix) that dropped the
    vehicle off at the place ended in a downloaded Parking-mode
    recording (see match_driver()'s own docstring for why the check
    looks at the adjacent leg rather than this one)."""

    profiles = christers_driver_profiles()
    outbound = TripFix(
        start=HOME,
        end=NORRA_STN,
        start_time=ts("2026-08-29 08:00:00"),
        end_time=ts("2026-08-29 08:15:00"),
        has_parking_footage=True,
    )
    return_leg = TripFix(
        start=NORRA_STN,
        end=HOME,
        start_time=ts("2026-08-29 08:30:00"),
        end_time=ts("2026-08-29 08:45:00"),
    )

    matches = match_driver(return_leg, outbound, None, profiles, KNOWN_POINTS)

    wife_matches = [
        m for m in matches if m.driver_label == "driver1" and m.place == "Norra Stationsgatan"
    ]
    christer_matches = [
        m for m in matches if m.driver_label == "driver2" and m.place == "Norra Stationsgatan"
    ]
    assert wife_matches
    assert wife_matches[0].confidence >= 0.85
    assert christer_matches == []


def test_any_short_trip_in_home_area_matches():
    profiles = christers_driver_profiles()
    trip = TripFix(
        start=HOME,
        end=NEAR_HOME,
        start_time=ts("2026-08-29 12:00:00"),
        end_time=ts("2026-08-29 12:10:00"),
    )

    matches = match_driver(trip, None, None, profiles, KNOWN_POINTS)

    local_matches = [m for m in matches if m.place == profiles.home_name]
    assert local_matches


def test_any_short_trip_in_home_area_respects_max_duration():
    profiles = christers_driver_profiles()
    trip = TripFix(
        start=HOME,
        end=NEAR_HOME,
        start_time=ts("2026-08-29 12:00:00"),
        end_time=ts("2026-08-29 12:30:00"),
    )

    matches = match_driver(trip, None, None, profiles, KNOWN_POINTS)

    local_matches = [m for m in matches if m.place == profiles.home_name]
    assert local_matches == []


def test_no_match_for_unrelated_endpoints():
    profiles = christers_driver_profiles()
    trip = TripFix(
        start=FAR_AWAY_A,
        end=FAR_AWAY_B,
        start_time=ts("2026-08-29 12:00:00"),
        end_time=ts("2026-08-29 12:10:00"),
    )

    matches = match_driver(trip, None, None, profiles, KNOWN_POINTS)

    assert matches == ()


def test_unverifiable_parking_status_still_matches_at_reduced_confidence():
    """A place->home leg with no prev_fix - there's no way to check
    whether the outbound leg that dropped the vehicle off ended in a
    Parking recording, so both drivers' Norra Stationsgatan patterns
    should still surface (a false 'no match' would be worse than a
    low-confidence maybe), but capped at reduced confidence and
    flagged as unverified. (A home->place leg never hits this case -
    its own has_parking_footage is always known, see TripFix's own
    docstring - so only the place->home direction can be unverified.)"""

    profiles = christers_driver_profiles()
    trip = TripFix(
        start=NORRA_STN,
        end=HOME,
        start_time=ts("2026-08-29 08:30:00"),
        end_time=ts("2026-08-29 08:45:00"),
    )

    matches = match_driver(trip, None, None, profiles, KNOWN_POINTS)

    unverified = [m for m in matches if m.place == "Norra Stationsgatan"]
    assert unverified
    assert all(m.confidence <= 0.5 for m in unverified)
    assert all("unverified" in m.reason for m in unverified)


def test_place_resolution_caching_via_known_points_dict():
    """resolve_known_points() itself does real network I/O (forward
    geocoding), so it isn't exercised here - but match_driver() must
    never re-derive a place's point itself and must simply skip any
    pattern whose place is missing from known_points (e.g. a name
    load_or_forward_geocode() failed to resolve), not raise."""

    profiles = DriverProfiles(
        home_name="Home",
        home_query="Home",
        home_radius_meters=300.0,
        drivers=(
            DriverProfile(
                label="driver1",
                display_name="Someone",
                patterns=(RoutePattern(place="Unresolvable Place"),),
            ),
        ),
    )
    trip = TripFix(
        start=HOME,
        end=SOLNA,
        start_time=ts("2026-08-29 07:00:00"),
        end_time=ts("2026-08-29 07:30:00"),
    )

    matches = match_driver(trip, None, None, profiles, {"home": HOME})

    assert matches == ()


def test_driver_profiles_json_round_trip():
    profiles = christers_driver_profiles()

    data = driver_profiles_to_dict(profiles)
    json_text = json.dumps(data)
    restored = driver_profiles_from_dict(json.loads(json_text))

    assert restored.home_query == profiles.home_query
    assert len(restored.drivers) == len(profiles.drivers)
    assert restored.drivers[1].patterns[-1].requires_parking is False


def test_christers_driver_profiles_leaves_default_from_unset():
    """christers_driver_profiles() seeds the shared repo's own default
    driver_profiles.json - default_from (see DriverProfiles' own
    docstring) is a purely personal, opt-in value Christer sets by
    hand-editing his own file, never something this seed function
    should write. Christer, on why the once-considered "cutover date"
    doesn't belong in tracked source: "Notera att allt som rör 6 juli,
    bara gäller mig, ingenting som skall in i github."""

    assert christers_driver_profiles().default_from is None


def test_default_from_round_trips_through_json():
    profiles = DriverProfiles(
        home_name="Home", home_query="Home", home_radius_meters=300.0,
        drivers=(), default_from="20260706",
    )

    data = driver_profiles_to_dict(profiles)
    restored = driver_profiles_from_dict(json.loads(json.dumps(data)))

    assert data["default_from"] == "20260706"
    assert restored.default_from == "20260706"


def test_default_from_absent_from_json_loads_as_none():
    """A driver_profiles.json written before this field existed has no
    "default_from" key at all - should load as None, not KeyError."""

    data = {
        "home": {"name": "Home", "query": "Home", "radius_meters": 300.0},
        "drivers": {},
    }

    assert driver_profiles_from_dict(data).default_from is None


def test_pattern_from_dict_migrates_old_min_max_stay_minutes():
    """A driver_profiles.json written before the P-file redesign might
    still have min_stay_minutes/max_stay_minutes - both should migrate
    to the equivalent requires_parking value (see _pattern_from_dict()'s
    own docstring: min_stay meant "stayed a while" -> requires_parking
    True, max_stay meant "quick turnaround" -> requires_parking False).
    A pattern with neither key stays requires_parking=None (no dwell
    condition at all)."""

    data = {
        "home": {"name": "Home", "query": "Home", "radius_meters": 300.0},
        "drivers": {
            "driver1": {
                "display_name": "Dao",
                "patterns": [
                    {"place": "A", "min_stay_minutes": 10},
                    {"place": "B", "max_stay_minutes": 10},
                    {"place": "C"},
                ],
            },
        },
    }

    restored = driver_profiles_from_dict(data)

    patterns = restored.drivers[0].patterns
    assert patterns[0].requires_parking is True
    assert patterns[1].requires_parking is False
    assert patterns[2].requires_parking is None


def test_write_default_driver_profiles_seeds_and_is_idempotent(tmp_path=None):
    with tempfile.TemporaryDirectory() as tmp:
        path = default_driver_profiles_path(Path(tmp))
        assert not path.exists()

        written = write_default_driver_profiles(path)
        assert path.exists()
        assert written.drivers[1].display_name == "Christer"

        loaded = load_driver_profiles(path)
        assert loaded is not None
        assert loaded.home_query == written.home_query

        again = write_default_driver_profiles(path)
        assert again.home_query == loaded.home_query


def test_load_driver_profiles_returns_none_when_missing():
    with tempfile.TemporaryDirectory() as tmp:
        path = default_driver_profiles_path(Path(tmp))
        assert load_driver_profiles(path) is None


def test_add_driver_appends_next_opaque_label():
    profiles = christers_driver_profiles()  # driver1 (Dao), driver2 (Christer)

    updated = add_driver(profiles, "Sofia")

    assert len(updated.drivers) == 3
    new_driver = updated.drivers[-1]
    assert new_driver.label == "driver3"
    assert new_driver.display_name == "Sofia"
    assert new_driver.patterns == ()
    # Original untouched (pure function).
    assert len(profiles.drivers) == 2


def test_add_driver_skips_gaps_never_reuses_a_number():
    two_drivers = DriverProfiles(
        home_name="Home", home_query="Home", home_radius_meters=300.0,
        drivers=(
            DriverProfile(label="driver1", display_name="A"),
            DriverProfile(label="driver5", display_name="B"),
        ),
    )

    updated = add_driver(two_drivers, "C")

    assert updated.drivers[-1].label == "driver6"


def test_add_driver_on_empty_profiles_starts_at_driver1():
    empty = DriverProfiles(
        home_name="Home", home_query="Home", home_radius_meters=300.0, drivers=()
    )

    updated = add_driver(empty, "First")

    assert updated.drivers[0].label == "driver1"


def test_rename_driver_updates_display_name_by_label():
    # Christer: "Jag vill aven byta namn pa 'Fru' till 'Dao'." - the
    # web /drivers form this backs looks up by opaque label (driver1),
    # not the current/old display_name.
    profiles = christers_driver_profiles()  # driver1 (Dao), driver2 (Christer)

    updated = rename_driver(profiles, "driver1", "Sofia")

    assert updated.drivers[0].label == "driver1"
    assert updated.drivers[0].display_name == "Sofia"
    assert updated.drivers[1].display_name == "Christer"
    # Original untouched (pure function).
    assert profiles.drivers[0].display_name == "Dao"


def test_rename_driver_unknown_label_is_a_no_op():
    profiles = christers_driver_profiles()

    updated = rename_driver(profiles, "driver99", "Nobody")

    assert updated == profiles


def test_save_driver_profiles_round_trips():
    with tempfile.TemporaryDirectory() as tmp:
        path = default_driver_profiles_path(Path(tmp))
        profiles = christers_driver_profiles()
        updated = add_driver(profiles, "Sofia")

        save_driver_profiles(path, updated)

        assert path.exists()
        loaded = load_driver_profiles(path)
        assert loaded is not None
        assert len(loaded.drivers) == 3
        assert loaded.drivers[-1].display_name == "Sofia"
        assert loaded.drivers[-1].label == "driver3"


# --------------------------------------------------------------------
# Round-trip via_point (Christer: "When i drive my wife to work in the
# morning, the trip starts and stops at Heliosgatan... it would be nice
# if we could get where i went, even if i returned to the starting
# place.")
# --------------------------------------------------------------------


def _gps_fix(offset_seconds, point, *, valid=True, confirmed=True):
    lat, lon = point
    return GpsFix(
        timestamp=datetime(2026, 8, 29, 7, 0, 0) + timedelta(seconds=offset_seconds),
        valid=valid,
        latitude=lat,
        longitude=lon,
        speed_kmh=30.0,
        course=0.0,
        confirmed=confirmed,
    )


def test_via_round_trip_matches_commute_pattern():
    """A round trip whose via_point actually reached `place` should
    match the same commute pattern a one-way trip would - the vehicle
    really did go there before turning back."""

    profiles = christers_driver_profiles()
    trip = TripFix(
        start=HOME,
        end=HOME,
        start_time=ts("2026-08-29 07:00:00"),
        end_time=ts("2026-08-29 07:20:00"),
        via_point=SOLNA,
    )

    matches = match_driver(trip, None, None, profiles, KNOWN_POINTS)

    solna_matches = [m for m in matches if m.place == "Solna, Vintervägen 50"]
    assert solna_matches
    assert any(m.driver_label == "driver2" for m in solna_matches)
    assert all("round trip" in m.reason for m in solna_matches)


def test_via_point_none_does_not_affect_ordinary_one_way_match():
    """The overwhelming common case - no via_point at all - must match
    exactly as before; the via_round_trip branch is purely additive."""

    profiles = christers_driver_profiles()
    trip = TripFix(
        start=HOME,
        end=SOLNA,
        start_time=ts("2026-08-29 07:00:00"),
        end_time=ts("2026-08-29 07:30:00"),
    )

    matches = match_driver(trip, None, None, profiles, KNOWN_POINTS)

    labels = {(m.driver_label, m.place) for m in matches}
    assert ("driver2", "Solna, Vintervägen 50") in labels
    assert all("round trip" not in m.reason for m in matches)


def test_via_round_trip_parking_requirement_stays_unverified():
    """A round trip via Norra Stationsgatan has no adjacent-trip
    evidence of a real stop there (it's the same single trip there and
    back, no parking involved) - both drivers' requires_parking
    patterns should surface at reduced, unverified confidence rather
    than one silently winning, same treatment as an unresolvable
    place_to_home leg."""

    profiles = christers_driver_profiles()
    trip = TripFix(
        start=HOME,
        end=HOME,
        start_time=ts("2026-08-29 08:00:00"),
        end_time=ts("2026-08-29 08:20:00"),
        via_point=NORRA_STN,
    )

    matches = match_driver(trip, None, None, profiles, KNOWN_POINTS)

    norra_matches = [m for m in matches if m.place == "Norra Stationsgatan"]
    assert norra_matches
    assert all(m.confidence <= 0.5 for m in norra_matches)
    assert all("unverified" in m.reason for m in norra_matches)


def test_resolve_via_point_returns_furthest_confirmed_point(monkeypatch):
    """A round trip (both ends near home) whose GPS track actually
    reached somewhere far away should return that furthest point, not
    an intermediate one."""

    trip_fix = TripFix(
        start=HOME,
        end=HOME,
        start_time=ts("2026-08-29 07:00:00"),
        end_time=ts("2026-08-29 07:20:00"),
    )
    trip = Trip(recordings=("rec1", "rec2"))
    fixes_by_recording = {
        "rec1": [_gps_fix(0, HOME), _gps_fix(300, NORRA_STN)],
        "rec2": [_gps_fix(600, SOLNA), _gps_fix(900, HOME)],
    }
    monkeypatch.setattr(
        driver_detect,
        "read_recording_gps",
        lambda adapter, recording: fixes_by_recording[recording],
    )

    via_point = resolve_via_point(None, trip, trip_fix, HOME, 300.0)

    assert via_point == SOLNA


def test_resolve_via_point_ignores_unconfirmed_glitches(monkeypatch):
    """A lone unconfirmed GPS glitch far from home must not become the
    via_point - only a confirmed fix counts (mirrors trip_stats.py's
    own 'Require confirmed positions' redesign)."""

    trip_fix = TripFix(
        start=HOME,
        end=HOME,
        start_time=ts("2026-08-29 07:00:00"),
        end_time=ts("2026-08-29 07:20:00"),
    )
    trip = Trip(recordings=("rec1",))
    fixes_by_recording = {
        "rec1": [
            _gps_fix(0, HOME),
            _gps_fix(60, FAR_AWAY_A, confirmed=False),
            _gps_fix(120, SOLNA, confirmed=True),
            _gps_fix(300, HOME),
        ],
    }
    monkeypatch.setattr(
        driver_detect,
        "read_recording_gps",
        lambda adapter, recording: fixes_by_recording[recording],
    )

    via_point = resolve_via_point(None, trip, trip_fix, HOME, 300.0)

    assert via_point == SOLNA


def test_resolve_via_point_returns_none_when_trip_never_left_home(monkeypatch):
    """A real garage-blip round trip (every fix within home_radius_meters)
    must return None, not some noise point still technically "furthest" -
    the exact case away_point/dwell_at_destination() were designed to
    exclude."""

    trip_fix = TripFix(
        start=HOME,
        end=HOME,
        start_time=ts("2026-08-29 07:00:00"),
        end_time=ts("2026-08-29 07:05:00"),
    )
    trip = Trip(recordings=("rec1",))
    fixes_by_recording = {"rec1": [_gps_fix(0, HOME), _gps_fix(60, NEAR_HOME)]}
    monkeypatch.setattr(
        driver_detect,
        "read_recording_gps",
        lambda adapter, recording: fixes_by_recording[recording],
    )

    via_point = resolve_via_point(None, trip, trip_fix, HOME, 300.0)

    assert via_point is None


def test_resolve_via_point_skips_one_way_trips_without_reading_gps(monkeypatch):
    """resolve_via_point() must do nothing for an ordinary one-way trip
    (start/end not both near home) - it should never even call
    read_recording_gps(), since that full-track read is the whole cost
    this function is meant to avoid paying for the common case."""

    trip_fix = TripFix(
        start=HOME,
        end=SOLNA,
        start_time=ts("2026-08-29 07:00:00"),
        end_time=ts("2026-08-29 07:20:00"),
    )
    trip = Trip(recordings=("rec1",))

    def _boom(adapter, recording):
        raise AssertionError("read_recording_gps() should not be called")

    monkeypatch.setattr(driver_detect, "read_recording_gps", _boom)

    via_point = resolve_via_point(None, trip, trip_fix, HOME, 300.0)

    assert via_point is None


def test_resolve_via_point_returns_none_without_home():
    trip_fix = TripFix(
        start=HOME,
        end=HOME,
        start_time=ts("2026-08-29 07:00:00"),
        end_time=ts("2026-08-29 07:20:00"),
    )
    trip = Trip(recordings=("rec1",))

    via_point = resolve_via_point(None, trip, trip_fix, None, 300.0)

    assert via_point is None
