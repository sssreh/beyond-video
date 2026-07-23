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

from datetime import timedelta

from ..archive.asset import Asset
from ..archive.recording import Recording
from ..generate.media import MediaToolError
from .gps_reader import GpsFix
from .gps_reader import read_gps
from .gsensor_reader import GSensorSample
from .gsensor_reader import read_gsensor

DEFAULT_SPEED_THRESHOLD_KMH = 5.0
DEFAULT_EDGE_WINDOW = timedelta(seconds=15)
DEFAULT_VARIANCE_RATIO_THRESHOLD = 3.0


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

    gps_file = recording.file(Asset.GPS)
    if gps_file is not None:
        try:
            fixes = read_gps(gps_file.path)
        except MediaToolError:
            fixes = ()
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

    gsensor_file = recording.file(Asset.GSENSOR)
    if gsensor_file is not None:
        try:
            samples = read_gsensor(gsensor_file.path)
        except MediaToolError:
            samples = ()
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
    evidence" and never force a split on their own.
    """

    reason = _recording_shows_movement(
        previous,
        at_start=False,
        speed_threshold_kmh=speed_threshold_kmh,
        window=window,
        variance_ratio_threshold=variance_ratio_threshold,
    )
    if reason is not None:
        return reason

    return _recording_shows_movement(
        current,
        at_start=True,
        speed_threshold_kmh=speed_threshold_kmh,
        window=window,
        variance_ratio_threshold=variance_ratio_threshold,
    )
