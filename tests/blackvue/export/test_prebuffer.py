from datetime import timedelta
from pathlib import Path

from blackvue.export.prebuffer import DEFAULT_MIN_SCORE
from blackvue.export.prebuffer import detect_prebuffer_seconds
from blackvue.telemetry.gsensor_reader import GSensorSample
from blackvue.telemetry.gsensor_reader import read_gsensor

FIXTURES = Path(__file__).parent.parent.parent / "fixtures" / "gsensor"


def test_detect_prebuffer_seconds_finds_the_real_overlap_in_a_confirmed_pair():
    # 20260802_103513_N / 20260802_103545_M: a real N -> M recording
    # pair from Christer's own archive (see WORKING_CONTEXT.md), where
    # he independently confirmed a real prebuffer overlap exists.
    # Validated by hand before this function was written: a fixed-
    # window cross-correlation swept across candidate offsets produced
    # one sharp, isolated peak at ~5.1s (score ~0.98), stable whether
    # the window itself was tried at 1.5s, 2s, 3s, or 4s - everything
    # else sat in a noise floor around +-0.15 to +-0.2. This is that
    # same method, now the real implementation.
    preceding = read_gsensor(FIXTURES / "20260802_103513_N.3gf")
    following = read_gsensor(FIXTURES / "20260802_103545_M.3gf")

    offset = detect_prebuffer_seconds(preceding, following)

    assert offset is not None
    assert 5.0 <= offset <= 5.2


def _flat_samples(count: int, step_ms: int = 100) -> tuple[GSensorSample, ...]:
    """A g-sensor track with zero real variance - every sample
    identical. Used to exercise the "not enough signal to say
    anything" path without needing a synthetic-but-plausible motion
    signal."""

    return tuple(
        GSensorSample(offset=timedelta(milliseconds=i * step_ms), x=100, y=-50, z=1000)
        for i in range(count)
    )


def _distinct_random_samples(
    count: int, seed: int, step_ms: int = 100
) -> tuple[GSensorSample, ...]:
    import random

    rng = random.Random(seed)
    return tuple(
        GSensorSample(
            offset=timedelta(milliseconds=i * step_ms),
            x=rng.randint(-2000, 2000),
            y=rng.randint(-2000, 2000),
            z=rng.randint(-2000, 2000),
        )
        for i in range(count)
    )


def test_detect_prebuffer_seconds_returns_none_for_unrelated_tracks():
    # Two tracks built from different random seeds share no real
    # overlap at all - nothing should clear DEFAULT_MIN_SCORE, so this
    # should refuse to guess rather than pick whatever scored highest
    # by chance.
    preceding = _distinct_random_samples(200, seed=1)
    following = _distinct_random_samples(200, seed=2)

    assert detect_prebuffer_seconds(preceding, following) is None


def test_detect_prebuffer_seconds_returns_none_for_flat_tracks():
    # No real variance in either track at all - z-scoring a
    # zero-variance window returns all zeros (see _zscore()'s own
    # docstring), so the dot product is 0 for every candidate, well
    # under min_score.
    preceding = _flat_samples(200)
    following = _flat_samples(200)

    assert detect_prebuffer_seconds(preceding, following) is None


def test_detect_prebuffer_seconds_returns_none_when_preceding_is_too_short():
    preceding = _distinct_random_samples(5, seed=1)  # ~0.5s of data
    following = _distinct_random_samples(200, seed=2)

    assert detect_prebuffer_seconds(preceding, following) is None


def test_detect_prebuffer_seconds_returns_none_when_following_is_too_short():
    preceding = _distinct_random_samples(200, seed=1)
    following = _distinct_random_samples(5, seed=2)  # ~0.5s of data

    assert detect_prebuffer_seconds(preceding, following) is None


def test_detect_prebuffer_seconds_returns_none_for_too_few_samples():
    preceding = _distinct_random_samples(1, seed=1)
    following = _distinct_random_samples(200, seed=2)

    assert detect_prebuffer_seconds(preceding, following) is None


def test_detect_prebuffer_seconds_finds_a_synthetic_overlap():
    # Build `preceding` as 6 seconds of a distinctive, non-repeating
    # pattern (not random noise - detect_prebuffer_seconds() compares
    # shape, so the "signal" needs real structure a window-shifted
    # copy of it will actually match). `following` starts with an
    # exact copy of preceding's last 2 seconds, then diverges.
    step_ms = 100
    pattern = [
        GSensorSample(
            offset=timedelta(milliseconds=i * step_ms),
            x=int(500 * ((i * 37) % 11)),
            y=int(300 * ((i * 13) % 7)),
            z=int(200 * ((i * 19) % 5)),
        )
        for i in range(60)  # 6.0s at 100ms steps
    ]
    preceding = tuple(pattern)

    overlap_samples = 20  # 2.0s
    following_head = [
        GSensorSample(offset=timedelta(milliseconds=i * step_ms), x=s.x, y=s.y, z=s.z)
        for i, s in enumerate(pattern[-overlap_samples:])
    ]
    following_tail = [
        GSensorSample(
            offset=timedelta(milliseconds=(overlap_samples + i) * step_ms),
            x=-s.x,
            y=-s.y,
            z=-s.z,
        )
        for i, s in enumerate(pattern[:20])
    ]
    following = tuple(following_head + following_tail)

    offset = detect_prebuffer_seconds(preceding, following, window_seconds=1.5)

    assert offset is not None
    assert 1.8 <= offset <= 2.2


def test_default_min_score_is_between_the_confirmed_noise_floor_and_true_positive():
    # A cheap regression guard against accidentally loosening the
    # confidence threshold enough to start accepting noise - see the
    # module docstring for the real numbers this is based on.
    assert 0.4 < DEFAULT_MIN_SCORE < 0.95
