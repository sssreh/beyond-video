"""
Tests for cli/bv_drivers.py.

Copyright (C) 2026 Christer R. (sssreh)

SPDX-License-Identifier: GPL-3.0-or-later
"""

from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path

from blackvue.adapters.blackvue.adapter import BlackVueAdapter
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


def _make_trip(label_timestamp: str, minutes_span: int = 10) -> Trip:
    start = RecordingId(label_timestamp)
    end_dt = start.timestamp + timedelta(minutes=minutes_span)
    end = RecordingId(f"{end_dt:%Y%m%d_%H%M%S}")
    return Trip(
        recordings=(Recording(id=start), Recording(id=end)),
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


def test_run_reports_no_trips_found_when_archive_is_empty(tmp_path, monkeypatch):
    _install_fakes(monkeypatch, trips=[], fixes=[])

    said = []
    args = parse_args([str(tmp_path), "--config-dir", str(tmp_path / "config")])
    exit_code = bv_drivers._run(args, say=said.append)

    assert exit_code == bv_drivers.EXIT_OK
    assert any("no trips found" in line for line in said)
    assert not (tmp_path / "config" / "driver_knowledge.json").exists()


def test_run_builds_and_saves_knowledge_base(tmp_path, monkeypatch):
    trip1 = _make_trip("20260702_080000")  # Thursday morning, home -> work
    trip2 = _make_trip("20260702_180000")  # Thursday evening, work -> home

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


def test_run_rebuild_preserves_existing_place_label(tmp_path, monkeypatch):
    # Two separate bv-drivers runs over the same trips: the first
    # names the common place, the second (a fresh scan) must not
    # clobber that name - build_common_places(existing=...)'s own
    # carry-forward contract, exercised here through the CLI's own
    # load-existing/save-merged path rather than calling
    # place_knowledge functions directly.
    trip1 = _make_trip("20260702_080000")
    trip2 = _make_trip("20260702_180000")
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
        places[place_key], label="Work", long_stay_driver="christer",
    )
    from blackvue.trip.place_knowledge import save_knowledge_base
    save_knowledge_base(
        knowledge_path, trips=trips, places=places, trip_overrides=trip_overrides,
    )

    # Rebuild - fresh fakes (new Trip instances, same underlying data).
    trip1b = _make_trip("20260702_080000")
    trip2b = _make_trip("20260702_180000")
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
    assert place_after.long_stay_driver == "christer"


def test_run_debug_prints_phase_timings(tmp_path, monkeypatch):
    _install_fakes(monkeypatch, trips=[], fixes=[])

    said = []
    args = parse_args(
        [str(tmp_path), "--config-dir", str(tmp_path / "config"), "--debug"]
    )
    bv_drivers._run(args, say=said.append)

    assert any("debug: scanned archive" in line for line in said)
    assert any("debug: detected 0 trip(s)" in line for line in said)
