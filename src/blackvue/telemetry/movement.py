"""
GPS/g-sensor movement heuristics used to decide whether a time gap
between two recordings should still be treated as one trip.

Policy (confirmed with the user): time-gap stays the *primary* trip
split rule (see TripBuilder). Movement evidence only ever *bridges* a
gap that would otherwise split the trip - it never splits a trip that
the time-gap rule alone would have kept together.

Two independent signals are checked, either one is enough:

  - GPS speed: if a fix near the end of the earlier recording, or
    near the start of the later one, shows speed above
    DEFAULT_SPEED_THRESHOLD_KMH, the vehicle was moving right at the
    edge of the gap.

  - g-sensor variance: the physical unit of the raw X/Y/Z values
    isn't confirmed (see gsensor_reader), so this can't use a fixed
    g-force threshold. Instead it's self-calibrating: the file is cut
    into DEFAULT_EDGE_WINDOW-sized chunks, the quietest chunk's
    variance becomes that recording's own "stationary" baseline, and
    the edge chunk (last chunk for the earlier recording, first chunk
    for the later one) counts as movement if its variance is at least
    DEFAULT_VARIANCE_RATIO_THRESHOLD times that baseline.

Either recording missing its GPS/g-sensor files, or having too little
data to compute a signal, is treated as "no evidence" (not as
"stationary") - a missing file never forces a split.

Copyright (C) 2026 Christer R. (sssreh)

SPDX-License-Identifier: GPL-3.0-or-later
"""

from __future__ import annotations

import math
from datetime import timedelta

from ..adapters.base import CameraAdapter
from ..adapters.telemetry_bridge import read_recording_gps
from ..adapters.telemetry_bridge import read_recording_gsensor
from ..adapters.telemetry_bridge import resolve_recording_gps_span
from ..archive.recording import Recording
from .gps_reader import GpsFix
from .gsensor_reader import GSensorSample

DEFAULT_SPEED_THRESHOLD_KMH = 5.0
DEFAULT_EDGE_WINDOW = timedelta(seconds=15)
DEFAULT_VARIANCE_RATIO_THRESHOLD = 3.0

# No real vehicle a dashcam/action-cam rides in reaches this speed -
# generous on purpose. gps_implies_impossible_jump() only has two GPS
# points to work with (often a single fallback fix on each side, see
# resolve_recording_gps_span()), not a real track, so a tight threshold
# risks flagging ordinary GPS position error (tens of meters) across a
# short gap as an "impossible" jump. 300 km/h clears commercial
# aviation ground speeds too, which is deliberate: a stock/downloaded
# clip mixed into an archive (the case this check exists for) is
# almost always off by hundreds or thousands of km, not by being
# merely implausibly fast.
DEFAULT_MAX_PLAUSIBLE_SPEED_KMH = 300.0

# Below this, elapsed time is too small relative to typical GPS
# position error to compute a meaningful speed - see
# gps_implies_impossible_jump()'s own docstring.
_MIN_ELAPSED_SECONDS_FOR_SPEED_CHECK = 60.0

_EARTH_RADIUS_METERS = 6_371_000.0  # duplicated from export/trip_stats.py's
# own private _haversine_distance_meters() - see that module's own
# comment for why this is intentionally not shared/imported.


def _haversine_distance_meters(
    lat1: float, lon1: float, lat2: float, lon2: float
) -> float:
    """Great-circle distance between two lat/lon points, in meters."""

    lat1_rad, lon1_rad = math.radians(lat1), math.radians(lon1)
    lat2_rad, lon2_rad = math.radians(lat2), math.radians(lon2)
    dlat = lat2_rad - lat1_rad
    dlon = lon2_rad - lon1_rad
    a = (
        math.sin(dlat / 2) ** 2
        + math.cos(lat1_rad) * math.cos(lat2_rad) * math.sin(dlon / 2) ** 2
    )
    c = 2 * math.asin(min(1.0, math.sqrt(a)))
    return _EARTH_RADIUS_METERS * c


def _magnitude(sample: GSensorSample) -> float:
    return (sample.x**2 + sample.y**2 + sample.z**2) ** 0.5


def _variance(values: list[float]) -> float:
    if len(values) < 2:
        return 0.0
    mean = sum(values) / len(values)
    return sum((value - mean) ** 2 for value in values) / len(values)


def _windowed_variances(
    samples: tuple[GSensorSample, ...], window: timedelta
) -> list[float]:
    """Split samples into non-overlapping window-sized chunks and
    return the magnitude-variance of each chunk with at least 2
    samples."""

    if not samples:
        return []

    variances = []
    chunk: list[GSensorSample] = []
    chunk_end = samples[0].offset + window

    for sample in samples:
        if sample.offset >= chunk_end:
            if len(chunk) >= 2:
                variances.append(_variance([_magnitude(s) for s in chunk]))
            chunk = []
            while sample.offset >= chunk_end:
                chunk_end += window
        chunk.append(sample)

    if len(chunk) >= 2:
        variances.append(_variance([_magnitude(s) for s in chunk]))

    return variances


def gps_shows_movement_at_end(
    fixes: tuple[GpsFix, ...],
    *,
    window: timedelta = DEFAULT_EDGE_WINDOW,
    speed_threshold_kmh: float = DEFAULT_SPEED_THRESHOLD_KMH,
) -> bool | None:
    """Return True if a fix in the last `window` of valid fixes shows
    speed above the threshold, False if not, or None if there's no
    usable fix data to decide from."""

    valid = [f for f in fixes if f.valid and f.speed_kmh is not None]
    if not valid:
        return None

    cutoff = valid[-1].timestamp - window
    edge = [f for f in valid if f.timestamp >= cutoff]

    return any(f.speed_kmh >= speed_threshold_kmh for f in edge)


def gps_shows_movement_at_start(
    fixes: tuple[GpsFix, ...],
    *,
    window: timedelta = DEFAULT_EDGE_WINDOW,
    speed_threshold_kmh: float = DEFAULT_SPEED_THRESHOLD_KMH,
) -> bool | None:
    """Same as gps_shows_movement_at_end but for the first `window` of
    valid fixes."""

    valid = [f for f in fixes if f.valid and f.speed_kmh is not None]
    if not valid:
        return None

    cutoff = valid[0].timestamp + window
    edge = [f for f in valid if f.timestamp <= cutoff]

    return any(f.speed_kmh >= speed_threshold_kmh for f in edge)


def gsensor_shows_movement_at_end(
    samples: tuple[GSensorSample, ...],
    *,
    window: timedelta = DEFAULT_EDGE_WINDOW,
    variance_ratio_threshold: float = DEFAULT_VARIANCE_RATIO_THRESHOLD,
) -> bool | None:
    """Return True if the last `window` of samples is significantly
    noisier than this recording's own quietest window, False if not,
    or None if there isn't enough data to decide from."""

    baseline_windows = _windowed_variances(samples, window)
    if not baseline_windows:
        return None
    baseline = min(baseline_windows)

    cutoff = samples[-1].offset - window
    edge = [s for s in samples if s.offset >= cutoff]
    if len(edge) < 2:
        return None
    edge_variance = _variance([_magnitude(s) for s in edge])

    if baseline == 0:
        return edge_variance > 0

    return edge_variance >= baseline * variance_ratio_threshold


def gsensor_shows_movement_at_start(
    samples: tuple[GSensorSample, ...],
    *,
    window: timedelta = DEFAULT_EDGE_WINDOW,
    variance_ratio_threshold: float = DEFAULT_VARIANCE_RATIO_THRESHOLD,
) -> bool | None:
    """Same as gsensor_shows_movement_at_end but for the first
    `window` of samples."""

    baseline_windows = _windowed_variances(samples, window)
    if not baseline_windows:
        return None
    baseline = min(baseline_windows)

    cutoff = samples[0].offset + window
    edge = [s for s in samples if s.offset <= cutoff]
    if len(edge) < 2:
        return None
    edge_variance = _variance([_magnitude(s) for s in edge])

    if baseline == 0:
        return edge_variance > 0

    return edge_variance >= baseline * variance_ratio_threshold


def _recording_shows_movement(
    recording: Recording,
    *,
    adapter: CameraAdapter,
    at_start: bool,
    speed_threshold_kmh: float,
    window: timedelta,
    variance_ratio_threshold: float,
) -> str | None:
    """Return a short, human-readable description of the movement
    evidence found at this recording's start/end edge (GPS speed or
    g-sensor variance - whichever fired first), or None if neither
    shows any. The description is meant to end up in bv-export's own
    trip log (see trip_builder.TripBuilder's `reasons` output) so a
    surprising bridge decision can be traced back to exactly which
    signal caused it, not just that "something" did.
    """

    edge = "start" if at_start else "end"

    fixes = read_recording_gps(adapter, recording)
    if fixes:
        check = (
            gps_shows_movement_at_start
            if at_start
            else gps_shows_movement_at_end
        )
        result = check(
            fixes, window=window, speed_threshold_kmh=speed_threshold_kmh
        )
        if result:
            return (
                f"GPS speed at/above {speed_threshold_kmh:g} km/h near the "
                f"{edge} of {recording.id}"
            )

    samples = read_recording_gsensor(adapter, recording)
    if samples:
        check = (
            gsensor_shows_movement_at_start
            if at_start
            else gsensor_shows_movement_at_end
        )
        result = check(
            samples,
            window=window,
            variance_ratio_threshold=variance_ratio_threshold,
        )
        if result:
            return (
                f"g-sensor variance near the {edge} of {recording.id} "
                "exceeded its own stationary baseline"
            )

    return None


def movement_bridges_gap(
    previous: Recording,
    current: Recording,
    *,
    adapter: CameraAdapter,
    speed_threshold_kmh: float = DEFAULT_SPEED_THRESHOLD_KMH,
    window: timedelta = DEFAULT_EDGE_WINDOW,
    variance_ratio_threshold: float = DEFAULT_VARIANCE_RATIO_THRESHOLD,
) -> str | None:
    """Return a short description of the GPS or g-sensor evidence
    suggesting the vehicle was still moving at the end of `previous` or
    the start of `current` - meaning the gap between them should be
    bridged into one trip instead of splitting - or None if neither
    recording shows any such evidence. Still usable as a plain bool
    (any non-None string is truthy, None is falsy) by callers that
    only care about the yes/no answer, like TripBuilder.build().

    Missing or unreadable GPS/g-sensor files are treated as "no
    evidence" and never force a split on their own. `adapter` is
    required (not optional/defaulted) since this function is always
    reached via a caller-constructed callable (see bv_export.py's own
    `bridge = functools.partial(movement_bridges_gap, adapter=adapter)
    if movement else None`), never called with no adapter context.
    """

    reason = _recording_shows_movement(
        previous,
        adapter=adapter,
        at_start=False,
        speed_threshold_kmh=speed_threshold_kmh,
        window=window,
        variance_ratio_threshold=variance_ratio_threshold,
    )
    if reason is not None:
        return reason

    return _recording_shows_movement(
        current,
        adapter=adapter,
        at_start=True,
        speed_threshold_kmh=speed_threshold_kmh,
        window=window,
        variance_ratio_threshold=variance_ratio_threshold,
    )


def gps_implies_impossible_jump(
    previous: Recording,
    current: Recording,
    *,
    adapter: CameraAdapter,
    max_speed_kmh: float = DEFAULT_MAX_PLAUSIBLE_SPEED_KMH,
) -> str | None:
    """Return a short description of why the GPS position at the end
    of `previous` and the start of `current` implies an impossible
    jump - meaning these two recordings almost certainly don't belong
    in the same trip even though the ordinary time-gap rule would
    otherwise keep them together (e.g. a stock/downloaded clip mixed
    into a GoPro archive, sitting in the same small time gap as real
    footage but shot somewhere else entirely) - or None if the
    evidence doesn't support that.

    Inverts movement_bridges_gap()'s own logic: that function looks
    for movement evidence to *bridge* a gap the time-gap rule would
    otherwise split; this looks for position evidence implausible
    enough to *force* a split the time-gap rule would otherwise have
    kept together. The two are meant to be mutually exclusive per
    TripBuilder.build()'s own decision chain (see trip_builder.py) -
    this is checked unconditionally, before the ordinary gap
    threshold, and never handed to `bridge`.

    Uses resolve_recording_gps_span() for both recordings - real
    telemetry preferred, EXIF/container-tag fallback otherwise - so
    this catches exactly the kind of stock-clip case that fallback was
    built for (see that function's own docstring). This is a real
    per-pair probe (up to two EXIF reads / ffprobe subprocesses) on
    top of TripBuilder's ordinary bookkeeping, which is why it's only
    ever wired in behind an opt-in flag (see bv_export.py's/bv_ls.py's
    own --gps-split wiring), not run unconditionally like
    `timestamp_reliable`.

    Returns None (treated as "no evidence", never forces a split) when
    either recording has no resolvable GPS fix at all, when the two
    fixes' timestamps aren't far enough apart to compute a meaningful
    speed (see _MIN_ELAPSED_SECONDS_FOR_SPEED_CHECK - guards against a
    tiny/zero elapsed time blowing up an ordinary GPS position error
    into an apparent absurd speed), or when the implied speed is under
    `max_speed_kmh`.
    """

    _, previous_fix = resolve_recording_gps_span(adapter, previous)
    current_fix, _ = resolve_recording_gps_span(adapter, current)

    if previous_fix is None or current_fix is None:
        return None

    if (
        previous_fix.latitude is None
        or previous_fix.longitude is None
        or current_fix.latitude is None
        or current_fix.longitude is None
    ):
        return None

    elapsed_seconds = (
        current_fix.timestamp - previous_fix.timestamp
    ).total_seconds()
    if elapsed_seconds < _MIN_ELAPSED_SECONDS_FOR_SPEED_CHECK:
        return None

    distance_meters = _haversine_distance_meters(
        previous_fix.latitude,
        previous_fix.longitude,
        current_fix.latitude,
        current_fix.longitude,
    )
    implied_speed_kmh = (distance_meters / 1000.0) / (elapsed_seconds / 3600.0)

    if implied_speed_kmh <= max_speed_kmh:
        return None

    return (
        f"implied speed of {implied_speed_kmh:,.0f} km/h between the end "
        f"of {previous.id} and the start of {current.id} "
        f"({distance_meters / 1000.0:,.1f} km in "
        f"{elapsed_seconds / 60.0:,.1f} min) exceeds the "
        f"{max_speed_kmh:g} km/h plausibility ceiling"
    )
