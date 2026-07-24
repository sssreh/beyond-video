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


def test_compute_trip_stats_skips_the_leading_gap_before_any_speed_data_exists():
    # Only the very first segment (before *any* real speed reading has
    # appeared yet anywhere in the trip) is genuinely unclassifiable -
    # there's nothing earlier to carry forward from. Once the first
    # real reading (offset 20) appears, it classifies the segment
    # leading into it too (see effective_speeds' forward-fill).
    fixes = (
        _fix(0, 59.300, 18.0000, None),
        _fix(10, 59.301, 18.0002, None),
        _fix(20, 59.302, 18.0004, DEFAULT_SPEED_THRESHOLD_KMH + 20.0),
    )

    stats = compute_trip_stats(fixes)

    assert stats.moving_seconds == 10.0
    assert stats.idle_seconds == 0.0


def test_compute_trip_stats_carries_the_last_known_speed_across_a_gap():
    # Regression test for a real bug Christer found on his own
    # archive: a long, continuously GPS-tracked drive (1708 fixes at
    # ~1Hz over ~28 real minutes) reported barely 40% of that span
    # across moving_seconds+idle_seconds combined, because any segment
    # between two fixes that *both* happened to lack their own speed
    # reading (common in practice, apparently, even with a good
    # position fix) was silently dropped - counted toward neither
    # bucket, with nothing in trip_info.txt to show time was missing.
    #
    # Here: a real 40 km/h reading at offset 0, then two fixes in a
    # row with no speed reading of their own (offsets 10 and 20 -
    # exactly the "neither endpoint has one" case that used to be
    # dropped entirely), then a real reading again at offset 30. The
    # whole 0->30 span must now be classified using the carried
    # -forward 40 km/h (above threshold - moving), not silently
    # dropped.
    above = DEFAULT_SPEED_THRESHOLD_KMH + 20.0
    fixes = (
        _fix(0, 59.3000, 18.0000, above),
        _fix(10, 59.3005, 18.0002, None),
        _fix(20, 59.3010, 18.0004, None),
        _fix(30, 59.3015, 18.0006, above),
    )

    stats = compute_trip_stats(fixes)

    assert stats.moving_seconds == 30.0
    assert stats.idle_seconds == 0.0


def test_compute_trip_stats_carry_forward_still_respects_a_later_speed_change():
    # The carried-forward value must actually update once a new real
    # reading appears, not just latch onto the very first one seen -
    # here the vehicle is fast (above threshold), then a gap with no
    # readings, then a real slow (idle) reading, then another gap.
    # That second gap must carry the *slow* reading forward, not the
    # original fast one.
    above = DEFAULT_SPEED_THRESHOLD_KMH + 20.0
    below = DEFAULT_SPEED_THRESHOLD_KMH - 1.0
    fixes = (
        _fix(0, 59.3000, 18.0000, above),
        _fix(10, 59.3005, 18.0002, None),
        _fix(20, 59.3010, 18.0004, below),
        _fix(30, 59.3015, 18.0006, None),
        _fix(40, 59.3020, 18.0008, None),
    )

    stats = compute_trip_stats(fixes)

    # 0->10 (carries `above` into the gap) and 10->20 (real `below`
    # reading arrives, but the segment is still classified by the mean
    # of the carried `above` and the new `below` - see the "classifies
    # by the mean" test above for that same rule) both land above
    # threshold; 20->30 and 30->40 both carry the newer `below`
    # reading forward and land below threshold.
    assert stats.moving_seconds == 10.0 + 10.0
    assert stats.idle_seconds == 10.0 + 10.0
