from datetime import datetime, timedelta

from blackvue.export.trip_stats import compute_trip_stats
from blackvue.telemetry.gps_reader import GpsFix
from blackvue.telemetry.movement import DEFAULT_SPEED_THRESHOLD_KMH


def _fix(offset_seconds, lat, lon, speed_kmh=None, *, valid=True):
    return GpsFix(
        timestamp=datetime(2026, 7, 15, 13, 0, 0) + timedelta(seconds=offset_seconds),
        valid=valid,
        latitude=lat,
        longitude=lon,
        speed_kmh=speed_kmh,
        course=45.0,
    )


def test_compute_trip_stats_returns_none_for_fewer_than_two_positioned_fixes():
    assert compute_trip_stats(()) is None
    assert compute_trip_stats((_fix(0, 59.30, 18.000),)) is None


def test_compute_trip_stats_skips_invalid_and_unpositioned_fixes():
    fixes = (
        _fix(0, 59.30, 18.000, 40.0),
        _fix(1, None, None, 40.0),  # unpositioned - not a real fix
        _fix(2, 59.31, 18.001, 40.0, valid=False),  # invalid - GPS lost
        _fix(3, 59.32, 18.002, 40.0),
    )

    stats = compute_trip_stats(fixes)

    assert stats is not None
    # Only the two valid, positioned fixes (offsets 0 and 3) count.
    assert stats.distance_km > 0


def test_compute_trip_stats_distance_matches_known_geography():
    # Stockholm Central to Uppsala Central is roughly 68km as the crow
    # flies - a coarse sanity check on the haversine math, not an
    # exact fixture.
    fixes = (
        _fix(0, 59.3300, 18.0592, 0.0),
        _fix(3600, 59.8586, 17.6389, 0.0),
    )

    stats = compute_trip_stats(fixes)

    assert 60 < stats.distance_km < 75


def test_compute_trip_stats_average_and_max_speed():
    fixes = (
        _fix(0, 59.30, 18.000, 20.0),
        _fix(1, 59.31, 18.001, 40.0),
        _fix(2, 59.32, 18.002, 60.0),
    )

    stats = compute_trip_stats(fixes)

    assert stats.average_speed_kmh == 40.0
    assert stats.max_speed_kmh == 60.0


def test_compute_trip_stats_speed_fields_are_none_without_any_speed_data():
    fixes = (
        _fix(0, 59.30, 18.000, None),
        _fix(1, 59.31, 18.001, None),
    )

    stats = compute_trip_stats(fixes)

    assert stats.average_speed_kmh is None
    assert stats.max_speed_kmh is None
    assert stats.moving_seconds is None
    assert stats.idle_seconds is None


def test_compute_trip_stats_splits_moving_and_idle_time_by_speed_threshold():
    below = DEFAULT_SPEED_THRESHOLD_KMH - 1.0
    above = DEFAULT_SPEED_THRESHOLD_KMH + 20.0

    # Each segment is classified by the *mean* of its two endpoint
    # speeds (see compute_trip_stats()'s own docstring) - not by
    # either fix's instantaneous value alone. So: segment 1 (both ends
    # below threshold, 30s) is idle; segment 2 (below -> above, 45s)
    # and segment 3 (above -> below, 15s) each have a mean pulled
    # above threshold by the one fast endpoint they share, so both
    # count as moving.
    fixes = (
        _fix(0, 59.300, 18.0000, below),
        _fix(30, 59.301, 18.0005, below),
        _fix(75, 59.310, 18.0100, above),
        _fix(90, 59.311, 18.0105, below),
    )

    stats = compute_trip_stats(fixes)

    assert stats.idle_seconds == 30.0
    assert stats.moving_seconds == 45.0 + 15.0


def test_compute_trip_stats_classifies_a_segment_by_the_mean_of_both_endpoints():
    # One fix well above threshold, one well below - the segment's
    # mean speed lands above threshold, so the whole 10s segment
    # counts as moving, not idle.
    fixes = (
        _fix(0, 59.300, 18.0000, DEFAULT_SPEED_THRESHOLD_KMH + 20.0),
        _fix(10, 59.301, 18.0002, DEFAULT_SPEED_THRESHOLD_KMH - 4.0),
    )

    stats = compute_trip_stats(fixes)

    assert stats.moving_seconds == 10.0
    assert stats.idle_seconds == 0.0


def test_compute_trip_stats_skips_segments_with_no_speed_data_on_either_end():
    # First segment has no speed reading on either fix - not counted
    # toward moving or idle. Second segment has a real reading, so the
    # trip overall still reports a (partial) moving/idle split rather
    # than None.
    fixes = (
        _fix(0, 59.300, 18.0000, None),
        _fix(10, 59.301, 18.0002, None),
        _fix(20, 59.302, 18.0004, DEFAULT_SPEED_THRESHOLD_KMH + 20.0),
    )

    stats = compute_trip_stats(fixes)

    assert stats.moving_seconds == 10.0
    assert stats.idle_seconds == 0.0
