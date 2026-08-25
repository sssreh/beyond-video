"""
Telemetry-weighted frame-timestamp selection for describe_scene()'s
adaptive sampling mode (SceneOptions.adaptive_sampling).

See WORKING_CONTEXT.md's "no single --map-zoom default is really
right"-adjacent "adaptive/GPS+g-sensor-driven frame sampling for scene
description" note for the original design discussion. Christer's idea,
in short: today's --describe-scene hands the whole clip to
qwen_vl_utils with a fixed fps=1.0/max_frames=16 and lets it pick
evenly-spaced frames internally. Real driving footage isn't evenly
"interesting" though - a long stop at a red light doesn't need one
frame per second the same way a sharp turn or a hard brake does. This
module computes a small per-second "how much is happening here" weight
from a recording's own GPS speed/heading and g-sensor readings, then
picks `count` timestamps biased toward the high-weight spans while
still covering the whole clip (a quiet stretch never drops to zero
weight, it's just less likely to get more than its fair share of the
frame budget).

Deliberately telemetry-source-agnostic: takes plain GpsFix/GSensorSample
sequences, not a Recording/CameraAdapter - callers (cli/bv_generate.py)
are responsible for fetching those via adapters/telemetry_bridge.py (or
not - see compute_adaptive_timestamps()'s own docstring for the
graceful "no telemetry at all" fallback), keeping this module's only
dependency the plain dataclasses themselves, the same boundary
generate/stats.py draws around compute_recording_stats().
"""

from __future__ import annotations

import bisect
import statistics
from dataclasses import dataclass
from datetime import datetime
from typing import Sequence

from ..telemetry.gps_reader import GpsFix
from ..telemetry.gsensor_reader import GSensorSample

# Bin resolution for the priority curve - fine enough to isolate a
# single quick event (a hard brake, a sharp turn) from its quiet
# surroundings without exploding compute on a long recording.
_BIN_SECONDS = 1.0

# Every bin starts at this weight regardless of telemetry - the "one
# frame covers the whole span" floor from the WORKING_CONTEXT.md note:
# a bin can be made more likely to be picked, never impossible to pick.
# With no telemetry at all every bin stays at exactly this value, which
# collapses _systematic_sample() below to plain even spacing - the
# "graceful fallback to today's uniform sampling when telemetry is
# missing" behavior the note calls for, for free, rather than as a
# special case.
_FLOOR_WEIGHT = 1.0

# How many extra priority points a bin's own local signals can add on
# top of the floor above, once each signal is normalized against the
# largest deviation seen anywhere in this same recording (see
# _apply_normalized_signal()) - so an eventful bin ends up several
# times more likely to be picked than a quiet one, without needing to
# know any signal's real-world units up front.
_SPEED_CHANGE_WEIGHT = 2.0
_TURN_WEIGHT = 2.0
_GFORCE_WEIGHT = 2.0

# A stretch where consecutive fixes both read below this speed is
# treated as effectively stopped (a red light, a queue, idling in
# Parking mode) - damps whatever weight it already has a little
# further, on top of (not instead of) the universal floor above.
_LOW_SPEED_KMH = 3.0
_LOW_SPEED_DAMPING = 0.5


def _circular_delta_degrees(a: float, b: float) -> float:
    """Smallest angle between two compass headings, 0-180 - handles
    the 359-degrees-to-1-degree wraparound a plain subtraction would
    overstate."""

    diff = abs(a - b) % 360.0
    return diff if diff <= 180.0 else 360.0 - diff


def _bin_index(offset_seconds: float, bin_count: int) -> int:
    return max(0, min(bin_count - 1, int(offset_seconds // _BIN_SECONDS)))


def _apply_normalized_signal(
    weights: list[float], points: Sequence[tuple[float, float]], max_weight: float
) -> None:
    """Add `max_weight * (value / peak)` to whichever bin each
    (offset_seconds, value) point falls into, where `peak` is the
    largest value anywhere in `points` - i.e. every signal is scaled
    relative to its own recording's own most extreme moment, not a
    fixed absolute threshold. This sidesteps needing to know any
    signal's real physical units or a cross-recording calibration
    (g-sensor raw units in particular are known to be baseline- and
    mounting-dependent - see generate/stats.py's own real-data finding
    on this) - a recording with a genuinely gentle max deviation still
    gets its *relative* peak highlighted, which is exactly what a
    frame-budget reallocation needs, without pretending to know an
    absolute "this counts as an event" cutoff that would need
    recalibrating per camera/mount anyway.

    A `points` sequence that's empty, or whose largest value is <= 0
    (every reading identical - most commonly all-zero deltas), leaves
    `weights` untouched rather than dividing by zero."""

    if not points:
        return

    peak = max(value for _, value in points)
    if peak <= 0:
        return

    bin_count = len(weights)
    for offset_seconds, value in points:
        weights[_bin_index(offset_seconds, bin_count)] += max_weight * (value / peak)


def _speed_and_turn_points(
    gps_fixes: Sequence[GpsFix], recording_start: datetime | None, duration_seconds: float
) -> tuple[list[tuple[float, float]], list[tuple[float, float]], set[int], int]:
    """Return (speed_change_points, turn_points, low_speed_bins,
    bin_count) from consecutive valid+confirmed+positioned GPS fixes,
    each point tagged at its own interval's midpoint offset so it
    naturally lands in whichever bin spans that interval.

    Fixes need `recording_start` to convert their own absolute
    `timestamp` into a recording-relative offset - with no start time
    to anchor against (recording_start is None), GPS fixes carry no
    usable position-in-clip information at all, so this returns empty
    signals rather than guessing. Fixes that land outside
    [0, duration_seconds] once converted (clock skew, or a sidecar
    that slightly outlasts its own video) are dropped - an
    out-of-range interval wouldn't land in a real bin anyway."""

    bin_count = max(1, int(-(-duration_seconds // _BIN_SECONDS))) if duration_seconds > 0 else 1

    if recording_start is None or not gps_fixes:
        return [], [], set(), bin_count

    points = sorted(
        (
            ((fix.timestamp - recording_start).total_seconds(), fix)
            for fix in gps_fixes
            if fix.valid
            and fix.confirmed
            and fix.latitude is not None
            and fix.longitude is not None
        ),
        key=lambda pair: pair[0],
    )
    points = [(t, fix) for t, fix in points if 0.0 <= t <= duration_seconds]

    speed_points: list[tuple[float, float]] = []
    turn_points: list[tuple[float, float]] = []
    low_speed_bins: set[int] = set()

    for (t0, f0), (t1, f1) in zip(points, points[1:]):
        if t1 <= t0:
            continue
        mid = (t0 + t1) / 2

        if f0.speed_kmh is not None and f1.speed_kmh is not None:
            speed_points.append((mid, abs(f1.speed_kmh - f0.speed_kmh)))
            if f0.speed_kmh < _LOW_SPEED_KMH and f1.speed_kmh < _LOW_SPEED_KMH:
                for bin_idx in range(_bin_index(t0, bin_count), _bin_index(t1, bin_count) + 1):
                    low_speed_bins.add(bin_idx)

        if f0.course is not None and f1.course is not None:
            turn_points.append((mid, _circular_delta_degrees(f0.course, f1.course)))

    return speed_points, turn_points, low_speed_bins, bin_count


def _gforce_points(
    gsensor_samples: Sequence[GSensorSample], duration_seconds: float
) -> list[tuple[float, float]]:
    """Return (offset_seconds, deviation) points, where deviation is
    each sample's magnitude minus this recording's own median
    magnitude - a per-recording baseline subtraction, since raw g-
    sensor readings sit on a nonzero, mounting-dependent baseline (see
    generate/stats.py's own real-.3gf finding: "all three g-sensor axes
    sit on a nonzero baseline ... consistent with gravity split across
    axes by mounting angle plus idle/road vibration, not driving
    events"). Median rather than mean so a handful of genuine hard-
    event samples don't drag the baseline itself toward them."""

    if not gsensor_samples:
        return []

    magnitudes = [
        (sample.x**2 + sample.y**2 + sample.z**2) ** 0.5 for sample in gsensor_samples
    ]
    baseline = statistics.median(magnitudes)

    points = []
    for sample, magnitude in zip(gsensor_samples, magnitudes):
        offset_seconds = sample.offset.total_seconds()
        if 0.0 <= offset_seconds <= duration_seconds:
            points.append((offset_seconds, abs(magnitude - baseline)))
    return points


def _binned_weights(
    duration_seconds: float,
    gps_fixes: Sequence[GpsFix],
    gsensor_samples: Sequence[GSensorSample],
    recording_start: datetime | None,
) -> list[float]:
    speed_points, turn_points, low_speed_bins, bin_count = _speed_and_turn_points(
        gps_fixes, recording_start, duration_seconds
    )

    weights = [_FLOOR_WEIGHT] * bin_count
    _apply_normalized_signal(weights, speed_points, _SPEED_CHANGE_WEIGHT)
    _apply_normalized_signal(weights, turn_points, _TURN_WEIGHT)
    _apply_normalized_signal(
        weights, _gforce_points(gsensor_samples, duration_seconds), _GFORCE_WEIGHT
    )

    for bin_idx in low_speed_bins:
        weights[bin_idx] *= _LOW_SPEED_DAMPING

    return weights


def _systematic_sample(weights: list[float], count: int, duration_seconds: float) -> list[float]:
    """Pick `count` bin-center timestamps via systematic (stratified
    inverse-CDF) resampling over `weights` - the same technique
    particle filters use to resample proportionally to a weight
    density without any randomness, so this is fully deterministic
    (same telemetry always picks the same timestamps, important for
    testability and for a --dry-run-style preview to mean anything).

    With every weight equal (the no-telemetry/all-floor case), this
    degenerates to exactly evenly-spaced timestamps across
    [0, duration_seconds] - see module docstring.

    Duplicate bin picks (a very sharply peaked weight curve wanting
    more distinct frames from one bin than exist) collapse via the
    caller's own de-duplication - describe_scene() already treats "up
    to count frames" as acceptable on a short clip, matching
    _extract_full_res_frames()'s own existing behavior."""

    bin_count = len(weights)
    if count <= 0 or bin_count == 0:
        return []

    total = sum(weights)
    if total <= 0:
        # Shouldn't happen (every weight starts at _FLOOR_WEIGHT > 0),
        # but fall back to plain even spacing rather than dividing by
        # zero if it somehow does.
        step = duration_seconds / count
        return sorted({min(duration_seconds, (i + 0.5) * step) for i in range(count)})

    cumulative = []
    running = 0.0
    for weight in weights:
        running += weight
        cumulative.append(running)

    step = total / count
    timestamps = []
    bin_idx = 0
    for i in range(count):
        target = step * (i + 0.5)
        bin_idx = bisect.bisect_left(cumulative, target, lo=bin_idx)
        bin_idx = min(bin_idx, bin_count - 1)
        center = (bin_idx + 0.5) * _BIN_SECONDS
        timestamps.append(min(center, duration_seconds))

    return sorted(set(timestamps))


def compute_adaptive_timestamps(
    duration_seconds: float,
    gps_fixes: Sequence[GpsFix],
    gsensor_samples: Sequence[GSensorSample],
    recording_start: datetime | None,
    count: int,
) -> list[float]:
    """Return up to `count` timestamps (seconds from the start of the
    clip, ascending, deduplicated) biased toward this recording's own
    most eventful spans - see module docstring for the weighting.

    Graceful fallback, by construction rather than as a special case:
    with no GPS fixes, no g-sensor samples, and/or no `recording_start`
    to anchor GPS fixes against, every bin keeps the same floor weight
    and the result is plain even spacing across [0, duration_seconds] -
    i.e. describe_scene()'s adaptive mode degrades to (approximately)
    its own non-adaptive uniform sampling rather than erroring or
    clustering nonsensically, whenever a recording's adapter has no
    telemetry for it (or the caller simply didn't fetch any).

    [] for a non-positive duration_seconds or count - nothing sensible
    to return."""

    if duration_seconds <= 0 or count <= 0:
        return []

    weights = _binned_weights(duration_seconds, gps_fixes, gsensor_samples, recording_start)
    return _systematic_sample(weights, count, duration_seconds)
