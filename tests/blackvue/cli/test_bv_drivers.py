"""
Tests for cli/bv_drivers.py.

Copyright (C) 2026 Christer R. (sssreh)

SPDX-License-Identifier: GPL-3.0-or-later
"""

from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path

from blackvue.adapters.blackvue.adapter import BlackVueAdapter
from blackvue.archive.asset import Asset
from blackvue.archive.asset_file import AssetFile
from blackvue.archive.recording import Recording
from blackvue.archive.recording_id import RecordingId
from blackvue.cli import bv_drivers
from blackvue.cli.bv_drivers import parse_args
from blackvue.trip.driver_detect import DriverMatch
from blackvue.trip.driver_detect import DriverProfile
from blackvue.trip.driver_detect import DriverProfiles
from blackvue.trip.driver_detect import TripFix
from blackvue.trip.place_knowledge import load_knowledge_base
from blackvue.trip.trip import Trip


# ---------------------------------------------------------------------------
# parse_args
# ---------------------------------------------------------------------------


def test_parse_args_path_defaults_to_current_directory():
    args = parse_args([])

    assert args.path == "."


def test_parse_args_min_visits_defaults_to_two():
    args = parse_args(["/some/archive"])

    assert args.min_visits == 2
    assert args.trace is False
    assert args.debug is False


def test_parse_args_accepts_min_visits_override():
    args = parse_args(["/some/archive", "--min-visits", "3"])

    assert args.min_visits == 3


def test_parse_args_accepts_time_range_flags():
    args = parse_args(
        [
            "/some/archive",
            "--from",
            "20260101_000000",
            "--until",
            "20260201_000000",
            "--timestamp",
            "2026",
        ]
    )

    assert args.from_ == "20260101_000000"
    assert args.until == "20260201_000000"
    assert args.timestamp == "2026"


def test_parse_args_accepts_max_gap_and_gap_tolerance():
    args = parse_args(["/some/archive", "--max-gap", "10", "--gap-tolerance", "30"])

    assert args.max_gap_minutes == 10
    assert args.gap_tolerance_seconds == 30


def test_parse_args_accepts_trace_and_debug_flags():
    args = parse_args(["/some/archive", "--trace", "--debug"])

    assert args.trace is True
    assert args.debug is True


# ---------------------------------------------------------------------------
# _run() - fixtures/fakes
# ---------------------------------------------------------------------------


class _FakeArchive:
    def __init__(self, recordings):
        self.recordings = recordings


class _FakeAdapter(BlackVueAdapter):
    """Same shape as test_bv_stats.py's own _FakeAdapter - only
    open_archive() is exercised by bv_drivers._run()."""

    def __init__(self, archive):
        self._archive = archive

    def open_archive(self, path):
        return self._archive


class _FakeTripBuilder:
    """Stands in for trip_builder.TripBuilder - bv_drivers._run()
    only ever calls `TripBuilder(max_gap=..., gap_tolerance=...).build(
    recordings)`, so a fake that records its constructor kwargs and
    always returns a fixed trip list (regardless of what recordings
    it's handed) is enough to drive _run() without a real archive scan
    or GPS-aware trip detection."""

    captured_kwargs: dict = {}
    trips_to_return: list = []

    def __init__(self, *, max_gap, gap_tolerance):
        _FakeTripBuilder.captured_kwargs = {
            "max_gap": max_gap, "gap_tolerance": gap_tolerance,
        }

    def build(self, recordings):
        return _FakeTripBuilder.trips_to_return


def _make_trip(label_timestamp: str, minutes_span: int = 10, *, kind: str = "P") -> Trip:
    """`kind` (default "P", Parking) is stamped onto the *end*
    RecordingId only - the *start* recording is always kind "N"
    (Normal/driving), since bv_drivers.py's own "drop trips with no
    driving evidence" filter (see its own comment) now requires at
    least one non-Parking recording somewhere in the trip, same as any
    real trip: it starts on a driving recording and, per the
    Parking-mode filter tested below, is only trusted once it also
    ends on one. A bare RecordingId needs a real kind letter regardless
    (a 15-char "YYYYMMDD_HHMMSS" string with no trailing "_K" isn't a
    valid RecordingId - .kind indexes position 16, which doesn't exist
    without one).

    The end recording also gets a downloaded GPS asset attached - the
    Parking-mode filter requires not just `.id.is_parking` but also at
    least one *downloaded* asset (Christer: "bara nedladdade P assets
    raknas, inte genererade" - only downloaded P assets count, not
    generated ones), so a bare id-only Recording with no assets at all
    would be wrongly dropped by every test that doesn't care about that
    distinction either."""

    start = RecordingId(f"{label_timestamp}_N")
    end_dt = start.timestamp + timedelta(minutes=minutes_span)
    end = RecordingId(f"{end_dt:%Y%m%d_%H%M%S}_{kind}")
    end_recording = Recording(id=end)
    end_recording.assets[Asset.GPS] = AssetFile(
        asset=Asset.GPS, path=Path(f"/archive/{end}.gps"),
    )
    return Trip(
        recordings=(Recording(id=start), end_recording),
    )


HOME = (59.3050, 18.1010)
WORK = (59.3600, 18.0000)


def _profiles() -> DriverProfiles:
    return DriverProfiles(
        home_name="home",
        home_query="Hammarby Sjostad, Stockholm",
        home_radius_meters=300.0,
        drivers=(
            DriverProfile(label="christer", display_name="Christer"),
            DriverProfile(label="annika", display_name="Annika"),
        ),
    )


def _install_fakes(monkeypatch, *, trips, fixes, known_points=None, profiles=None):
    """Wires every collaborator bv_drivers._run() calls out to, except
    the pure place_knowledge.build_knowledge_base() pipeline itself -
    that's already covered end-to-end by test_place_knowledge.py, so
    these tests are about bv_drivers.py's own plumbing (archive/trip-
    builder/profile wiring, reporting, file I/O), not re-testing the
    resolution logic."""

    profiles = profiles if profiles is not None else _profiles()
    known_points = known_points if known_points is not None else {"home": HOME}

    monkeypatch.setattr(bv_drivers, "get_adapter", lambda adapter_id: _FakeAdapter(_FakeArchive([])))
    monkeypatch.setattr(bv_drivers, "TripBuilder", _FakeTripBuilder)
    _FakeTripBuilder.trips_to_return = list(trips)

    monkeypatch.setattr(bv_drivers, "write_default_driver_profiles", lambda path: profiles)
    monkeypatch.setattr(bv_drivers, "resolve_known_points", lambda profiles_, cache_dir: known_points)

    fixes_by_label = dict(zip((t.label for t in trips), fixes))

    def fake_resolve_trip_fix(adapter, trip):
        return fixes_by_label[trip.label]

    monkeypatch.setattr(bv_drivers, "resolve_trip_fix", fake_resolve_trip_fix)


# ---------------------------------------------------------------------------
# _run()
# ---------------------------------------------------------------------------


def test_run_defaults_max_gap_to_3_minutes_when_not_given(tmp_path, monkeypatch):
    # Christer: "In Drivers KB i want max gap time to be 3 min, in that
    # way i get all small visits." bv-drivers' own default is
    # deliberately shorter than trip_builder.DEFAULT_MAX_GAP (5 min,
    # still used by bv-export/bv-ls) - see bv_drivers.py's own
    # DEFAULT_MAX_GAP comment.
    from datetime import timedelta

    _install_fakes(monkeypatch, trips=[], fixes=[])

    args = parse_args([str(tmp_path), "--config-dir", str(tmp_path / "config")])
    bv_drivers._run(args, say=lambda *_: None)

    assert _FakeTripBuilder.captured_kwargs["max_gap"] == timedelta(minutes=3)


def test_run_reports_no_trips_found_when_archive_is_empty(tmp_path, monkeypatch):
    _install_fakes(monkeypatch, trips=[], fixes=[])

    said = []
    args = parse_args([str(tmp_path), "--config-dir", str(tmp_path / "config")])
    exit_code = bv_drivers._run(args, say=said.append)

    assert exit_code == bv_drivers.EXIT_OK
    assert any("no trips found" in line for line in said)
    assert not (tmp_path / "config" / "driver_knowledge.json").exists()


def test_run_builds_and_saves_knowledge_base(tmp_path, monkeypatch):
    trip1 = _make_trip("20260709_080000")  # Thursday morning, home -> work
    trip2 = _make_trip("20260709_180000")  # Thursday evening, work -> home

    fix1 = TripFix(
        start=HOME, end=WORK,
        start_time=trip1.start_timestamp, end_time=trip1.end_timestamp,
    )
    fix2 = TripFix(
        start=WORK, end=HOME,
        start_time=trip2.start_timestamp, end_time=trip2.end_timestamp,
    )

    _install_fakes(monkeypatch, trips=[trip1, trip2], fixes=[fix1, fix2])

    config_dir = tmp_path / "config"
    said = []
    args = parse_args([str(tmp_path), "--config-dir", str(config_dir)])
    exit_code = bv_drivers._run(args, say=said.append)

    assert exit_code == bv_drivers.EXIT_OK
    assert any("2 trip(s), 1 place(s)" in line for line in said)

    knowledge_path = config_dir / "driver_knowledge.json"
    assert knowledge_path.exists()

    loaded = load_knowledge_base(knowledge_path)
    assert loaded is not None
    trips, places, trip_overrides = loaded
    assert len(trips) == 2
    assert len(places) == 1
    assert trip_overrides == {}


def test_run_summary_uses_without_a_driver_rule_wording(tmp_path, monkeypatch):
    """Christer: 'I dont like the wording "still need a driver rule",
    there is no need. Better wording is like ... without a driver
    rule'."""

    trip1 = _make_trip("20260709_080000")
    trip2 = _make_trip("20260709_180000")
    fix1 = TripFix(
        start=HOME, end=WORK,
        start_time=trip1.start_timestamp, end_time=trip1.end_timestamp,
    )
    fix2 = TripFix(
        start=WORK, end=HOME,
        start_time=trip2.start_timestamp, end_time=trip2.end_timestamp,
    )
    _install_fakes(monkeypatch, trips=[trip1, trip2], fixes=[fix1, fix2])

    said = []
    args = parse_args([str(tmp_path), "--config-dir", str(tmp_path / "config")])
    exit_code = bv_drivers._run(args, say=said.append)

    assert exit_code == bv_drivers.EXIT_OK
    assert any("without a driver rule" in line for line in said)
    assert not any("still need a driver rule" in line for line in said)
    # A single-visit place doesn't clear --min-visits, so no mixed-place
    # FYI line is expected here - that's covered on its own below.
    assert not any("mixed" in line for line in said)


def test_run_summary_reports_mixed_place_count_when_present(tmp_path, monkeypatch):
    """A place both drivers actually visit (Christer's real "Globen
    Parking") is reported as an FYI "mixed" line, not folded into
    "without a driver rule" - see mixed_driver_place_keys()'s own
    docstring. Stubbing mixed_driver_place_keys() directly keeps this
    test focused on bv_drivers.py's own wiring/wording, since the
    detection logic itself is exercised in test_place_knowledge.py."""

    trip1 = _make_trip("20260709_080000")
    trip2 = _make_trip("20260709_180000")
    fix1 = TripFix(
        start=HOME, end=WORK,
        start_time=trip1.start_timestamp, end_time=trip1.end_timestamp,
    )
    fix2 = TripFix(
        start=WORK, end=HOME,
        start_time=trip2.start_timestamp, end_time=trip2.end_timestamp,
    )
    _install_fakes(monkeypatch, trips=[trip1, trip2], fixes=[fix1, fix2])
    monkeypatch.setattr(bv_drivers, "mixed_driver_place_keys", lambda trips: {"some_place"})

    said = []
    args = parse_args([str(tmp_path), "--config-dir", str(tmp_path / "config")])
    exit_code = bv_drivers._run(args, say=said.append)

    assert exit_code == bv_drivers.EXIT_OK
    assert any(
        "1 common place(s) with drivers split across trips (mixed - already handled per-trip)"
        in line
        for line in said
    )


def test_run_computes_via_point_for_round_trip_and_feeds_it_into_knowledge_base(
    tmp_path, monkeypatch
):
    """Christer: 'the trip starts and stops at Heliosgatan... it would
    be nice if we could get where i went, even if i returned to the
    starting place.' bv_drivers.py's fixes-building loop must call
    resolve_via_point() for a round-trip TripFix (both start/end near
    home) and thread its result into the TripFix passed into
    build_knowledge_base(), so the round trip still ends up with a
    real away_point/Common Place instead of none at all."""

    round_trip = _make_trip("20260709_080000")
    round_trip_fix = TripFix(
        start=HOME, end=HOME,
        start_time=round_trip.start_timestamp, end_time=round_trip.end_timestamp,
    )
    _install_fakes(monkeypatch, trips=[round_trip], fixes=[round_trip_fix])

    calls = []

    def fake_resolve_via_point(adapter, trip, trip_fix, home, home_radius_meters):
        calls.append((trip, trip_fix, home, home_radius_meters))
        return WORK

    monkeypatch.setattr(bv_drivers, "resolve_via_point", fake_resolve_via_point)

    config_dir = tmp_path / "config"
    args = parse_args([str(tmp_path), "--config-dir", str(config_dir)])
    exit_code = bv_drivers._run(args, say=lambda _: None)

    assert exit_code == bv_drivers.EXIT_OK
    assert len(calls) == 1
    called_trip, called_fix, called_home, called_radius = calls[0]
    assert called_trip is round_trip
    assert called_fix.start == HOME and called_fix.end == HOME
    assert called_home == HOME
    assert called_radius == 300.0

    loaded = load_knowledge_base(config_dir / "driver_knowledge.json")
    assert loaded is not None
    loaded_trips, loaded_places, _ = loaded
    assert loaded_trips[0].away_point == WORK
    assert len(loaded_places) == 1


def test_run_skips_via_point_when_resolver_finds_none(tmp_path, monkeypatch):
    """A round trip that genuinely never left home (resolve_via_point()
    returns None - see that function's own docstring) must keep the
    pre-existing no-away_point behavior, not crash or invent one."""

    round_trip = _make_trip("20260709_080000")
    round_trip_fix = TripFix(
        start=HOME, end=HOME,
        start_time=round_trip.start_timestamp, end_time=round_trip.end_timestamp,
    )
    _install_fakes(monkeypatch, trips=[round_trip], fixes=[round_trip_fix])
    monkeypatch.setattr(
        bv_drivers, "resolve_via_point",
        lambda adapter, trip, trip_fix, home, home_radius_meters: None,
    )

    config_dir = tmp_path / "config"
    args = parse_args([str(tmp_path), "--config-dir", str(config_dir)])
    exit_code = bv_drivers._run(args, say=lambda _: None)

    assert exit_code == bv_drivers.EXIT_OK
    loaded = load_knowledge_base(config_dir / "driver_knowledge.json")
    assert loaded is not None
    loaded_trips, loaded_places, _ = loaded
    assert loaded_trips[0].away_point is None
    assert loaded_places == {}


def test_run_scoped_rebuild_preserves_trips_and_places_outside_scanned_window(
    tmp_path, monkeypatch
):
    """Christer: 'i rub Driver KB just for today, and voila common
    places name gone.' A build scoped to --from/--until only rescans
    that window's own trips - build_common_places() used to only ever
    count places from *this run's* trip list, so a place/trip outside
    the scanned window was silently dropped from driver_knowledge.json
    on save, not just hidden from view. A previously-saved trip/place
    from well outside this run's own window must survive untouched."""

    from blackvue.trip.place_knowledge import CommonPlace
    from blackvue.trip.place_knowledge import TripKnowledge
    from blackvue.trip.place_knowledge import place_key
    from blackvue.trip.place_knowledge import save_knowledge_base

    config_dir = tmp_path / "config"
    knowledge_path = config_dir / "driver_knowledge.json"

    old_place_point = (59.5000, 18.2000)
    old_entry = TripKnowledge(
        trip_label="trip_20260101_080000_20260101_083000",
        start_time=datetime(2026, 1, 1, 8, 0),
        end_time=datetime(2026, 1, 1, 8, 30),
        weekday="Thursday",
        start_time_of_day="08:00",
        away_place_key=place_key(old_place_point),
        away_point=old_place_point,
        dwell_minutes=None,
        stop_category=None,
        candidates=(),
        first_recording_id="20260101_080000_N",
        last_recording_id="20260101_083000_P",
    )
    old_place = CommonPlace(
        key=place_key(old_place_point), point=old_place_point,
        label="Christer's beautiful old place", visit_count=1, driver="christer",
    )
    save_knowledge_base(
        knowledge_path, trips=[old_entry], places={old_place.key: old_place},
        trip_overrides={},
    )

    # This run is scoped to just 2026-07-09 ("today").
    trip_today = _make_trip("20260709_080000")
    fix_today = TripFix(
        start=HOME, end=WORK,
        start_time=trip_today.start_timestamp, end_time=trip_today.end_timestamp,
    )
    _install_fakes(monkeypatch, trips=[trip_today], fixes=[fix_today])

    args = parse_args([
        str(tmp_path), "--config-dir", str(config_dir),
        "--from", "20260709_000000", "--until", "20260709_235959",
    ])
    exit_code = bv_drivers._run(args, say=lambda _: None)

    assert exit_code == bv_drivers.EXIT_OK
    loaded = load_knowledge_base(knowledge_path)
    assert loaded is not None
    trips, places, _ = loaded
    labels = {entry.trip_label for entry in trips}
    assert old_entry.trip_label in labels
    assert trip_today.label in labels
    assert old_place.key in places
    assert places[old_place.key].label == "Christer's beautiful old place"


def test_run_scoped_rebuild_drops_stale_trip_inside_scanned_window(
    tmp_path, monkeypatch
):
    """A previously-saved trip whose own first_recording_id falls
    INSIDE this run's scanned window - but that this run's own rescan
    no longer produces (e.g. a changed --max-gap re-split it) - must
    not be carried forward as a stale duplicate. Only trips truly
    outside the scanned window survive untouched (see the companion
    test above)."""

    from blackvue.trip.place_knowledge import CommonPlace
    from blackvue.trip.place_knowledge import TripKnowledge
    from blackvue.trip.place_knowledge import place_key
    from blackvue.trip.place_knowledge import save_knowledge_base

    config_dir = tmp_path / "config"
    knowledge_path = config_dir / "driver_knowledge.json"

    stale_point = (59.5000, 18.2000)
    stale_entry = TripKnowledge(
        trip_label="trip_20260709_070000_20260709_073000",
        start_time=datetime(2026, 7, 9, 7, 0),
        end_time=datetime(2026, 7, 9, 7, 30),
        weekday="Thursday",
        start_time_of_day="07:00",
        away_place_key=place_key(stale_point),
        away_point=stale_point,
        dwell_minutes=None,
        stop_category=None,
        candidates=(),
        first_recording_id="20260709_070000_N",
        last_recording_id="20260709_073000_P",
    )
    stale_place = CommonPlace(
        key=place_key(stale_point), point=stale_point,
        label="Stale place", visit_count=1, driver=None,
    )
    save_knowledge_base(
        knowledge_path, trips=[stale_entry], places={stale_place.key: stale_place},
        trip_overrides={},
    )

    trip_today = _make_trip("20260709_080000")
    fix_today = TripFix(
        start=HOME, end=WORK,
        start_time=trip_today.start_timestamp, end_time=trip_today.end_timestamp,
    )
    _install_fakes(monkeypatch, trips=[trip_today], fixes=[fix_today])

    args = parse_args([
        str(tmp_path), "--config-dir", str(config_dir),
        "--from", "20260709_000000", "--until", "20260709_235959",
    ])
    exit_code = bv_drivers._run(args, say=lambda _: None)

    assert exit_code == bv_drivers.EXIT_OK
    loaded = load_knowledge_base(knowledge_path)
    assert loaded is not None
    trips, places, _ = loaded
    labels = {entry.trip_label for entry in trips}
    assert stale_entry.trip_label not in labels
    assert trip_today.label in labels


def test_run_full_rebuild_unaffected_by_carry_forward(tmp_path, monkeypatch):
    """No --from/--until/--timestamp at all -> the scanned interval
    covers everything, so no previously-saved trip is ever "outside"
    it - an ordinary full rebuild must behave exactly as before this
    fix (build_knowledge_base()'s own existing_trips param), fully
    replacing driver_knowledge.json's trip/place list from this run's
    own fresh scan."""

    from blackvue.trip.place_knowledge import CommonPlace
    from blackvue.trip.place_knowledge import TripKnowledge
    from blackvue.trip.place_knowledge import place_key
    from blackvue.trip.place_knowledge import save_knowledge_base

    config_dir = tmp_path / "config"
    knowledge_path = config_dir / "driver_knowledge.json"

    old_point = (59.5000, 18.2000)
    old_entry = TripKnowledge(
        trip_label="trip_20260101_080000_20260101_083000",
        start_time=datetime(2026, 1, 1, 8, 0),
        end_time=datetime(2026, 1, 1, 8, 30),
        weekday="Thursday",
        start_time_of_day="08:00",
        away_place_key=place_key(old_point),
        away_point=old_point,
        dwell_minutes=None,
        stop_category=None,
        candidates=(),
        first_recording_id="20260101_080000_N",
        last_recording_id="20260101_083000_P",
    )
    old_place = CommonPlace(
        key=place_key(old_point), point=old_point,
        label="Old place", visit_count=1, driver=None,
    )
    save_knowledge_base(
        knowledge_path, trips=[old_entry], places={old_place.key: old_place},
        trip_overrides={},
    )

    trip_today = _make_trip("20260709_080000")
    fix_today = TripFix(
        start=HOME, end=WORK,
        start_time=trip_today.start_timestamp, end_time=trip_today.end_timestamp,
    )
    _install_fakes(monkeypatch, trips=[trip_today], fixes=[fix_today])

    args = parse_args([str(tmp_path), "--config-dir", str(config_dir)])
    exit_code = bv_drivers._run(args, say=lambda _: None)

    assert exit_code == bv_drivers.EXIT_OK
    loaded = load_knowledge_base(knowledge_path)
    assert loaded is not None
    trips, places, _ = loaded
    labels = {entry.trip_label for entry in trips}
    assert labels == {trip_today.label}
    assert old_place.key not in places


def test_run_drops_trips_not_ending_in_parking_mode(tmp_path, monkeypatch):
    # Christer, having reasoned it through himself: "En trip borjar och
    # slutar ju i hammarby sjostad aven om hon jobbar pa norra
    # stationsgatan" - every real trip eventually comes back to a real
    # stop, so a trip is only trusted if it ends with a Parking-mode
    # (P) recording. No date involved at all - this is a simple,
    # dateless, universal rule (see bv_drivers.py's own comment for
    # the real-archive verification numbers behind it).
    dropped = _make_trip("20260701_080000", kind="N")
    kept = _make_trip("20260701_180000", kind="P")

    trips = [dropped, kept]
    fixes = [
        TripFix(
            start=HOME, end=WORK,
            start_time=trip.start_timestamp, end_time=trip.end_timestamp,
        )
        for trip in trips
    ]
    _install_fakes(monkeypatch, trips=trips, fixes=fixes)

    config_dir = tmp_path / "config"
    args = parse_args([str(tmp_path), "--config-dir", str(config_dir)])
    exit_code = bv_drivers._run(args, say=lambda _: None)

    assert exit_code == bv_drivers.EXIT_OK
    loaded = load_knowledge_base(config_dir / "driver_knowledge.json")
    assert loaded is not None
    saved_trips, _, _ = loaded
    saved_labels = {entry.trip_label for entry in saved_trips}
    assert saved_labels == {kept.label}


def test_run_drops_parking_trip_whose_only_asset_is_generated(tmp_path, monkeypatch):
    # Christer: "Notera att bara nedladdade P assets raknas, inte
    # genererade." A P-kind ending recording whose only asset is
    # something bv-generate/bv-scribe derived after the fact (here,
    # RECORDING_STATS from bv-generate --stats) isn't camera evidence
    # of a real stop - it's evidence some generation step ran, which
    # could happen even if the source it was derived from has since
    # been pruned. Only a *downloaded* asset (FRONT/REAR/INTERIOR
    # video, GPS, GSENSOR, or a *_THUMBNAIL) should count. `dropped` is
    # deliberately not the last trip in the list - the chronologically
    # last trip is exempt from this whole check (see the dedicated
    # last-trip-exemption test below), so testing the generated-vs-
    # downloaded distinction needs a non-last trip.
    dropped_end = Recording(id=RecordingId("20260701_181000_P"))
    dropped_end.assets[Asset.RECORDING_STATS] = AssetFile(
        asset=Asset.RECORDING_STATS,
        path=Path("/archive/20260701_181000_P.stats.json"),
    )
    dropped = Trip(
        recordings=(
            # Kind "N" (not "P") so this trip has real driving
            # evidence and reaches the P-ending/generated-asset check
            # below rather than being caught earlier by the
            # no-driving-evidence filter for an unrelated reason.
            Recording(id=RecordingId("20260701_180000_N")),
            dropped_end,
        ),
    )
    kept = _make_trip("20260709_080000", kind="P")

    trips = [dropped, kept]
    fixes = [
        TripFix(
            start=HOME, end=WORK,
            start_time=trip.start_timestamp, end_time=trip.end_timestamp,
        )
        for trip in trips
    ]
    _install_fakes(monkeypatch, trips=trips, fixes=fixes)

    config_dir = tmp_path / "config"
    args = parse_args([str(tmp_path), "--config-dir", str(config_dir)])
    exit_code = bv_drivers._run(args, say=lambda _: None)

    assert exit_code == bv_drivers.EXIT_OK
    loaded = load_knowledge_base(config_dir / "driver_knowledge.json")
    assert loaded is not None
    saved_trips, _, _ = loaded
    saved_labels = {entry.trip_label for entry in saved_trips}
    assert saved_labels == {kept.label}


def test_run_keeps_last_trip_even_if_not_ending_in_parking(tmp_path, monkeypatch):
    # Christer: "All trips end with a P except for the last one, it
    # might get it the next download." The car may simply still be
    # parked with its Parking-mode sidecars not downloaded yet - that's
    # not the same as an unverified trip, so the chronologically last
    # trip is exempt from the P-ending/downloaded-asset check
    # regardless of what it ends in. A *middle* trip not ending in P is
    # still dropped normally - only the very last one gets the pass.
    earliest = _make_trip("20260701_080000", kind="P")
    middle_dropped = _make_trip("20260705_080000", kind="N")
    last_kept = _make_trip("20260709_080000", kind="N")

    trips = [earliest, middle_dropped, last_kept]
    fixes = [
        TripFix(
            start=HOME, end=WORK,
            start_time=trip.start_timestamp, end_time=trip.end_timestamp,
        )
        for trip in trips
    ]
    _install_fakes(monkeypatch, trips=trips, fixes=fixes)

    config_dir = tmp_path / "config"
    args = parse_args([str(tmp_path), "--config-dir", str(config_dir)])
    exit_code = bv_drivers._run(args, say=lambda _: None)

    assert exit_code == bv_drivers.EXIT_OK
    loaded = load_knowledge_base(config_dir / "driver_knowledge.json")
    assert loaded is not None
    saved_trips, _, _ = loaded
    saved_labels = {entry.trip_label for entry in saved_trips}
    assert saved_labels == {earliest.label, last_kept.label}


def test_run_rebuild_preserves_existing_place_label(tmp_path, monkeypatch):
    # Two separate bv-drivers runs over the same trips: the first
    # names the common place, the second (a fresh scan) must not
    # clobber that name - build_common_places(existing=...)'s own
    # carry-forward contract, exercised here through the CLI's own
    # load-existing/save-merged path rather than calling
    # place_knowledge functions directly.
    trip1 = _make_trip("20260709_080000")
    trip2 = _make_trip("20260709_180000")
    fix1 = TripFix(
        start=HOME, end=WORK,
        start_time=trip1.start_timestamp, end_time=trip1.end_timestamp,
    )
    fix2 = TripFix(
        start=WORK, end=HOME,
        start_time=trip2.start_timestamp, end_time=trip2.end_timestamp,
    )
    _install_fakes(monkeypatch, trips=[trip1, trip2], fixes=[fix1, fix2])

    config_dir = tmp_path / "config"
    args = parse_args([str(tmp_path), "--config-dir", str(config_dir)])
    bv_drivers._run(args, say=lambda _: None)

    knowledge_path = config_dir / "driver_knowledge.json"
    trips, places, trip_overrides = load_knowledge_base(knowledge_path)
    place_key = next(iter(places))
    from dataclasses import replace
    places[place_key] = replace(
        places[place_key], label="Work", driver="christer",
    )
    from blackvue.trip.place_knowledge import save_knowledge_base
    save_knowledge_base(
        knowledge_path, trips=trips, places=places, trip_overrides=trip_overrides,
    )

    # Rebuild - fresh fakes (new Trip instances, same underlying data).
    trip1b = _make_trip("20260709_080000")
    trip2b = _make_trip("20260709_180000")
    fix1b = TripFix(
        start=HOME, end=WORK,
        start_time=trip1b.start_timestamp, end_time=trip1b.end_timestamp,
    )
    fix2b = TripFix(
        start=WORK, end=HOME,
        start_time=trip2b.start_timestamp, end_time=trip2b.end_timestamp,
    )
    _install_fakes(monkeypatch, trips=[trip1b, trip2b], fixes=[fix1b, fix2b])

    args2 = parse_args([str(tmp_path), "--config-dir", str(config_dir)])
    bv_drivers._run(args2, say=lambda _: None)

    _, places_after, _ = load_knowledge_base(knowledge_path)
    place_after = places_after[place_key]
    assert place_after.label == "Work"
    assert place_after.driver == "christer"


class _FakeGSensorSample:
    def __init__(self, x, y, z):
        self.x, self.y, self.z = x, y, z


def test_run_wires_smoothness_raw_into_knowledge_base(tmp_path, monkeypatch):
    # Christer's own follow-up ask ("anything else you can do to make
    # it easier for me to decide driver") - the driving-smoothness
    # idea. bv_drivers._run() pools every recording in a trip's own
    # g-sensor samples via read_recording_gsensor() and stamps the
    # mean lateral+accel/brake magnitude onto each TripKnowledge.
    trip1 = _make_trip("20260709_080000")
    trip2 = _make_trip("20260709_180000")
    fix1 = TripFix(
        start=HOME, end=WORK,
        start_time=trip1.start_timestamp, end_time=trip1.end_timestamp,
    )
    fix2 = TripFix(
        start=WORK, end=HOME,
        start_time=trip2.start_timestamp, end_time=trip2.end_timestamp,
    )
    _install_fakes(monkeypatch, trips=[trip1, trip2], fixes=[fix1, fix2])

    # trip1's recordings each report one sample (x=3, y=4 -> magnitude
    # 5); trip2's recordings report none at all (empty tuple, same as
    # a real trip with no g-sensor data).
    def fake_read_recording_gsensor(adapter, recording):
        if recording in trip1.recordings:
            return (_FakeGSensorSample(x=3, y=4, z=0),)
        return ()

    monkeypatch.setattr(
        bv_drivers, "read_recording_gsensor", fake_read_recording_gsensor
    )

    config_dir = tmp_path / "config"
    args = parse_args([str(tmp_path), "--config-dir", str(config_dir)])
    exit_code = bv_drivers._run(args, say=lambda _: None)

    assert exit_code == bv_drivers.EXIT_OK
    knowledge_path = config_dir / "driver_knowledge.json"
    trips, _, _ = load_knowledge_base(knowledge_path)
    by_label = {t.trip_label: t for t in trips}
    assert by_label[trip1.label].smoothness_raw == 5.0
    assert by_label[trip2.label].smoothness_raw is None


def test_run_debug_prints_phase_timings(tmp_path, monkeypatch):
    _install_fakes(monkeypatch, trips=[], fixes=[])

    said = []
    args = parse_args(
        [str(tmp_path), "--config-dir", str(tmp_path / "config"), "--debug"]
    )
    bv_drivers._run(args, say=said.append)

    assert any("debug: scanned archive" in line for line in said)
    assert any("debug: detected 0 trip(s)" in line for line in said)
