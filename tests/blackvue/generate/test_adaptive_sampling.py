from datetime import datetime, timedelta

from blackvue.generate.adaptive_sampling import compute_adaptive_timestamps
from blackvue.telemetry.gps_reader import GpsFix
from blackvue.telemetry.gsensor_reader import GSensorSample

_START = datetime(2026, 8, 23, 10, 0, 0)


def _fix(offset_seconds, *, speed_kmh=None, course=None, valid=True, confirmed=True):
    return GpsFix(
        timestamp=_START + timedelta(seconds=offset_seconds),
        valid=valid,
        confirmed=confirmed,
        latitude=59.0,
        longitude=18.0,
        speed_kmh=speed_kmh,
        course=course,
    )


def _sample(offset_seconds, x, y, z):
    return GSensorSample(offset=timedelta(seconds=offset_seconds), x=x, y=y, z=z)


# ---------------------------------------------------------------------------
# Degenerate inputs - [] rather than erroring, per the module's own
# docstring ("nothing sensible to return").
# ---------------------------------------------------------------------------


def test_compute_adaptive_timestamps_zero_duration_returns_empty():
    assert compute_adaptive_timestamps(0.0, [], [], None, count=10) == []


def test_compute_adaptive_timestamps_negative_duration_returns_empty():
    assert compute_adaptive_timestamps(-5.0, [], [], None, count=10) == []


def test_compute_adaptive_timestamps_zero_count_returns_empty():
    assert compute_adaptive_timestamps(60.0, [], [], None, count=0) == []


def test_compute_adaptive_timestamps_negative_count_returns_empty():
    assert compute_adaptive_timestamps(60.0, [], [], None, count=-1) == []


# ---------------------------------------------------------------------------
# Graceful fallback: no telemetry at all (or no recording_start to anchor
# GPS fixes against) collapses to plain even spacing - see module
# docstring's "graceful fallback, by construction" paragraph.
# ---------------------------------------------------------------------------


def test_compute_adaptive_timestamps_no_telemetry_is_evenly_spaced():
    # Even spacing here means each pick lands at the center of its own
    # 1-second weight bin (see _systematic_sample()'s own bin-center
    # math) - roughly every 10s across a 60s/count=6 clip, offset by
    # half a bin (4.5 rather than 5.0) because bin centers sit at
    # (bin_idx + 0.5), not at the bin's leading edge.
    timestamps = compute_adaptive_timestamps(60.0, [], [], None, count=6)

    assert timestamps == [4.5, 14.5, 24.5, 34.5, 44.5, 54.5]


def test_compute_adaptive_timestamps_gps_without_recording_start_is_ignored():
    # GPS fixes are present but recording_start is None - _speed_and_turn_points()
    # can't convert absolute fix timestamps into recording-relative offsets, so
    # this should behave identically to having no GPS at all (even spacing).
    fixes = [_fix(0, speed_kmh=0), _fix(30, speed_kmh=120)]

    with_start = compute_adaptive_timestamps(60.0, [], [], None, count=6)
    without_gps_anchor = compute_adaptive_timestamps(60.0, fixes, [], None, count=6)

    assert without_gps_anchor == with_start


def test_compute_adaptive_timestamps_results_are_ascending_and_deduplicated():
    fixes = [_fix(i, speed_kmh=float(i)) for i in range(0, 61, 5)]
    timestamps = compute_adaptive_timestamps(60.0, fixes, [], _START, count=8)

    assert timestamps == sorted(set(timestamps))


def test_compute_adaptive_timestamps_is_deterministic():
    # Same telemetry -> same timestamps every time (systematic resampling,
    # no randomness) - important both for testability and so a
    # --dry-run-style preview means anything.
    fixes = [_fix(i, speed_kmh=float(i % 7) * 10) for i in range(0, 61, 3)]
    samples = [_sample(i, i % 5, 0, 0) for i in range(0, 61, 2)]

    first = compute_adaptive_timestamps(60.0, fixes, samples, _START, count=8)
    second = compute_adaptive_timestamps(60.0, fixes, samples, _START, count=8)

    assert first == second


# ---------------------------------------------------------------------------
# Weighting: a sharp turn/speed-change/g-force event should pull more of
# the frame budget toward its own span than an equally-long quiet stretch.
# ---------------------------------------------------------------------------


def test_compute_adaptive_timestamps_biases_toward_hard_turn():
    # A long, steady straight run except for one sharp turn right in the
    # middle (t=50-51s) - the turn bin and its immediate neighbors should
    # attract a disproportionate share of the count=6 budget.
    fixes = []
    for t in range(0, 101, 2):
        course = 90.0 if t < 50 else (270.0 if t >= 52 else 180.0)
        fixes.append(_fix(t, speed_kmh=50.0, course=course))

    timestamps = compute_adaptive_timestamps(100.0, fixes, [], _START, count=6)

    # At least one picked timestamp should land near the turn event,
    # rather than all 6 being purely evenly spaced across [0, 100].
    near_turn = [t for t in timestamps if 45.0 <= t <= 60.0]
    assert near_turn, f"expected a sample near the turn event, got {timestamps}"


def test_compute_adaptive_timestamps_biases_toward_gforce_spike():
    duration = 60.0
    # Flat g-sensor baseline throughout, one hard-brake spike at t=30s.
    samples = [_sample(t, 0, 0, 0) for t in range(0, 61, 1) if t != 30]
    samples.append(_sample(30, 200, 0, 0))

    timestamps = compute_adaptive_timestamps(duration, [], samples, _START, count=6)

    near_spike = [t for t in timestamps if 25.0 <= t <= 35.0]
    assert near_spike, f"expected a sample near the g-force spike, got {timestamps}"


def test_compute_adaptive_timestamps_damps_low_speed_stretch():
    # A recording that's fast (interesting) for the first half and
    # stopped at a red light (uninteresting, < _LOW_SPEED_KMH) for the
    # second half - the stopped half should get proportionally fewer
    # samples than the moving half.
    fixes = []
    for t in range(0, 121, 2):
        speed = 60.0 if t < 60 else 0.0
        fixes.append(_fix(t, speed_kmh=speed))

    timestamps = compute_adaptive_timestamps(120.0, fixes, [], _START, count=10)

    moving_half = [t for t in timestamps if t < 60.0]
    stopped_half = [t for t in timestamps if t >= 60.0]
    assert len(moving_half) > len(stopped_half)


# ---------------------------------------------------------------------------
# Fixes that don't carry the fields a given signal needs (missing
# speed_kmh/course, or not valid+confirmed+positioned) are simply skipped
# by that signal rather than raising - _speed_and_turn_points() only
# consults fixes that are valid, confirmed, and have real coordinates.
# ---------------------------------------------------------------------------


def test_compute_adaptive_timestamps_ignores_invalid_and_unconfirmed_fixes():
    fixes = [
        _fix(0, speed_kmh=0.0, valid=True, confirmed=True),
        _fix(10, speed_kmh=200.0, valid=False, confirmed=True),
        _fix(20, speed_kmh=200.0, valid=True, confirmed=False),
        _fix(30, speed_kmh=0.0, valid=True, confirmed=True),
    ]

    # Should not raise, and should fall back toward even spacing since
    # the only two usable (valid+confirmed) fixes show no real speed
    # change between them.
    timestamps = compute_adaptive_timestamps(40.0, fixes, [], _START, count=4)
    assert len(timestamps) <= 4
    assert all(0.0 <= t <= 40.0 for t in timestamps)


def test_compute_adaptive_timestamps_handles_fixes_with_no_speed_or_course():
    fixes = [_fix(t) for t in range(0, 41, 5)]  # speed_kmh=None, course=None

    timestamps = compute_adaptive_timestamps(40.0, fixes, [], _START, count=4)

    assert timestamps == [4.5, 14.5, 24.5, 34.5]


def test_compute_adaptive_timestamps_handles_empty_gsensor_samples():
    timestamps = compute_adaptive_timestamps(30.0, [], [], _START, count=3)

    assert timestamps == [4.5, 14.5, 24.5]
