"""
Pre-record buffer detection for bv-export.

BlackVue Event/Manual recordings can include several seconds of real
footage from *before* the actual trigger - the camera keeps a rolling
buffer and flushes it into the saved file the moment an event fires.
That means the tail of the immediately preceding recording's own
content is duplicated at the head of the following Event/Manual one -
confirmed as a real, non-hypothetical effect on Christer's own
archive (see WORKING_CONTEXT.md's g-sensor/map sync entries, which
independently ran into this same duplication as a *position* bug
before it was ever measured directly here).

detect_prebuffer_seconds() measures the real duration of that overlap
directly from two consecutive recordings' own raw g-sensor (.3gf)
samples - not GPS, not audio. G-sensor was picked over GPS because it
still carries a real signal when the vehicle is stationary (a very
common state right at the moment of an actual trigger event - someone
bumping a parked car, say), where GPS speed/position barely changes
at all and gives almost nothing to correlate against. Audio would
likely work too (more so, even) but needs decoding first; g-sensor
data is already small, already downloaded via bv-download's own
sidecar probing, and needs no extra tooling to read.

Copyright (C) 2026 Christer R. (sssreh)

SPDX-License-Identifier: GPL-3.0-or-later
"""

from __future__ import annotations

from ..telemetry.gsensor_reader import GSensorSample

# Validated against a real N -> M (Normal -> Manual) recording pair
# from Christer's own archive (20260802_103513_N / 20260802_103545_M -
# see tests/fixtures/gsensor/ and WORKING_CONTEXT.md): comparing a
# fixed WINDOW_SECONDS-long slice of the following recording's head
# against every candidate slice of the preceding recording's tail,
# at 0.02s (50Hz) steps out to MAX_SECONDS, produced one sharp,
# isolated peak (score ~0.98) at the true offset (~5.1s), with every
# other candidate sitting in a noise floor around +-0.15 to +-0.2.
# That held steady whether the window itself was tried at 1.5s, 2s,
# 3s, or 4s - same answer every time, which is why a single fixed
# default is used here rather than something adaptive.
DEFAULT_WINDOW_SECONDS = 3.0
DEFAULT_MAX_SECONDS = 12.0
DEFAULT_GRID_SECONDS = 0.02

# MIN_SCORE sits comfortably above the observed noise floor (+-0.2)
# and well below the confirmed true-positive score (~0.98) - the
# threshold isn't "pick whatever scores highest," it's "refuse to
# trim anything unless a candidate clears a real confidence bar." No
# candidate beating this at all means "this doesn't look like a real
# match" - the caller gets None back and leaves the recording alone,
# the same conservative default _align_front_rear_durations() and
# _ensure_recording_audio() already use for anything they can't
# confidently act on (see trip_export.py).
DEFAULT_MIN_SCORE = 0.75


def _series(samples: tuple[GSensorSample, ...]) -> tuple[
    list[float], list[float], list[float], list[float]
]:
    """Unpack a recording's g-sensor samples into four parallel lists
    (offset-in-seconds, x, y, z) - the shape every helper below
    actually works with. GSensorSample.offset is already a timedelta
    counting up from that recording's own start (see
    telemetry/gsensor_reader.py), and read_gsensor() already returns
    samples in file order, which is itself time order."""

    t = [sample.offset.total_seconds() for sample in samples]
    x = [float(sample.x) for sample in samples]
    y = [float(sample.y) for sample in samples]
    z = [float(sample.z) for sample in samples]
    return t, x, y, z


def _interp(t: float, xs: list[float], ys: list[float]) -> float:
    """Linear interpolation of ys at t, given ys sampled at ascending
    xs - clamped to the first/last real sample outside [xs[0], xs[-1]]
    rather than extrapolating. A plain binary search for the
    bracketing pair; called a lot (every grid point, every candidate
    offset, every axis) so this avoids a linear scan per call."""

    if t <= xs[0]:
        return ys[0]
    if t >= xs[-1]:
        return ys[-1]

    lo, hi = 0, len(xs) - 1
    while hi - lo > 1:
        mid = (lo + hi) // 2
        if xs[mid] <= t:
            lo = mid
        else:
            hi = mid

    x0, x1 = xs[lo], xs[hi]
    if x1 == x0:
        return ys[lo]

    fraction = (t - x0) / (x1 - x0)
    return ys[lo] + fraction * (ys[hi] - ys[lo])


def _zscore(values: list[float]) -> list[float]:
    """Demean and unit-scale a window of samples - cross-correlation
    needs to compare the *shape* of two signals, not their absolute
    values, which matters here specifically because the g-sensor
    format's own physical unit isn't confirmed (see
    gsensor_reader.py's module docstring) and each axis likely carries
    a different constant offset (e.g. whichever axis gravity mostly
    loads). A window with no real variance at all (a perfectly flat
    read, or too few samples) z-scores to all zeros rather than
    dividing by ~zero."""

    n = len(values)
    mean = sum(values) / n
    variance = sum((value - mean) ** 2 for value in values) / n
    sd = variance ** 0.5

    if sd < 1e-9:
        return [0.0] * n

    return [(value - mean) / sd for value in values]


def _axis_vector(
    series: tuple[list[float], list[float], list[float], list[float]],
    start: float,
    grid: list[float],
) -> list[float]:
    """Sample x, y, and z over [start, start + grid[-1]] at every
    offset in `grid`, z-score each axis independently, and
    concatenate the three into one vector - one comparable "shape
    fingerprint" for this window, regardless of which recording or
    which candidate offset it came from."""

    t, x, y, z = series
    vector: list[float] = []

    for values in (x, y, z):
        window = [_interp(start + step, t, values) for step in grid]
        vector.extend(_zscore(window))

    return vector


def _dot(a: list[float], b: list[float]) -> float:
    return sum(ai * bi for ai, bi in zip(a, b))


def detect_prebuffer_seconds(
    preceding: tuple[GSensorSample, ...],
    following: tuple[GSensorSample, ...],
    *,
    window_seconds: float = DEFAULT_WINDOW_SECONDS,
    max_seconds: float = DEFAULT_MAX_SECONDS,
    grid_seconds: float = DEFAULT_GRID_SECONDS,
    min_score: float = DEFAULT_MIN_SCORE,
) -> float | None:
    """Return the detected pre-record-buffer overlap, in seconds,
    between two consecutive recordings' own g-sensor tracks - or None
    if nothing found clears `min_score`, meaning there's no confident
    match and the caller should leave the recording untrimmed rather
    than guess.

    Method: take a fixed `window_seconds`-long slice of `following`'s
    own head (its first `window_seconds`, always at candidate offset
    zero - this is the reference: whatever duplicate content it
    contains starts right at its own beginning). Slide a same-length
    window across `preceding`'s tail, from `window_seconds` up to
    `max_seconds` seconds before its own end, comparing each
    candidate slice against `following`'s head window via a
    z-scored, three-axis (x, y, z concatenated) dot-product
    correlation. The candidate offset with the highest score - if it
    clears `min_score` - is the detected overlap: `following`'s first
    P seconds are (almost certainly) the same real content as
    `preceding`'s last P seconds.

    Returns None (refuses to guess) whenever there isn't enough data
    to run a meaningful comparison at all: fewer than 2 samples in
    either track, `preceding` shorter than `window_seconds`, or
    `following` shorter than `window_seconds` - not just when no
    candidate clears the confidence threshold.

    See this module's own docstring for why g-sensor (over GPS/audio)
    and the default constants' own values - both are grounded in a
    real validated case, not picked arbitrarily.
    """

    if len(preceding) < 2 or len(following) < 2:
        return None

    preceding_series = _series(preceding)
    following_series = _series(following)

    preceding_end = preceding_series[0][-1]
    following_end = following_series[0][-1]

    if following_end < window_seconds:
        return None

    search_max = min(max_seconds, preceding_end)
    if search_max <= window_seconds:
        return None

    step_count = int((search_max - window_seconds) / grid_seconds)
    grid = [step * grid_seconds for step in range(int(window_seconds / grid_seconds) + 1)]

    following_vector = _axis_vector(following_series, 0.0, grid)
    denominator = len(following_vector) or 1

    best_offset: float | None = None
    best_score = min_score

    for step in range(step_count + 1):
        candidate = window_seconds + step * grid_seconds
        preceding_vector = _axis_vector(
            preceding_series, preceding_end - candidate, grid
        )
        score = _dot(preceding_vector, following_vector) / denominator

        if score > best_score:
            best_score = score
            best_offset = candidate

    return best_offset
