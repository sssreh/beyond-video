"""
Trip-level distance/speed statistics for bv-export's trip_info.txt.

Copyright (C) 2026 Christer R. (sssreh)

SPDX-License-Identifier: GPL-3.0-or-later
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from ..telemetry.gps_reader import GpsFix
from ..telemetry.movement import DEFAULT_SPEED_THRESHOLD_KMH

# Mean Earth radius in meters - the same well-known value
# osm_roads.py's own (module-private) constant uses for its bounding
# -box math. Duplicated here rather than imported, since that one is
# module-private by convention (leading underscore) and this module's
# use of it (a haversine distance, not a bounding box) is a genuinely
# separate concern.
_EARTH_RADIUS_METERS = 6_371_000.0

# Real-archive confirmation (Christer, 2026-08-23): a raw altitude
# reading from a single, non-differential automotive GPS receiver
# jitters roughly +-1-4m tick-to-tick even at a steady highway speed -
# ordinary vertical-fix noise (VDOP is routinely 2-3x worse than
# horizontal DOP for this class of receiver), not real elevation
# change. Left unfiltered, naively summing every positive raw delta
# between consecutive fixes turned a real ~3-minute clip whose
# altitude started and ended within 1m of itself into 91m of reported
# "elevation_gain_meters" (see WORKING_CONTEXT.md's per-recording
# stats note for the investigation). _hysteresis_altitude_stats()
# below re-bases against a reference altitude that only moves once a
# reading clears this dead-band, the same "significant change" filter
# barometric altimeters and GPS trip computers already use - chosen
# over smoothing (a moving average/median) specifically because a
# dead-band leaves genuinely large, real deltas (an actual hill)
# completely unaffected regardless of how few fixes span them, where
# a windowed smoothing filter would blur a short trip's every reading
# together. 2.0m sits comfortably above the observed per-tick noise
# floor (~1-4m) without being so wide it would swallow a real, modest
# climb.
_ALTITUDE_GAIN_DEADBAND_METERS = 2.0


@dataclass(frozen=True)
class TripStats:
    """Summary statistics for a trip's merged GPS fixes."""

    distance_km: float
    average_speed_kmh: float | None
    max_speed_kmh: float | None
    # Optional (default None, not 0.0) so any existing caller
    # constructing a TripStats without these still works unchanged -
    # see compute_trip_stats()'s own docstring for what "no speed data
    # at all" (None) means vs. a genuine zero.
    moving_seconds: float | None = None
    idle_seconds: float | None = None
    # None whenever the trip's fixes have no altitude_meters data at
    # all (see GpsFix.altitude_meters's own docstring - depends on the
    # camera's .gps file having $GPGGA sentences, which BlackVue
    # cameras emit every tick, but nothing guarantees that for every
    # adapter/camera this project might support in the future).
    # min/max are independent of elevation_gain_meters (each fix's own
    # raw altitude reading, same "not carried-forward" convention
    # average_speed_kmh/max_speed_kmh already use) - Christer wants
    # this for a stitch-video/playback overlay later, so both "the
    # range climbed" and "the total climbed" are worth keeping
    # separately rather than picking just one.
    min_altitude_meters: float | None = None
    max_altitude_meters: float | None = None
    elevation_gain_meters: float | None = None


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


def _hysteresis_altitude_stats(altitudes: list[float]) -> tuple[float, float, float]:
    """min/max/elevation-gain over a sequence of raw altitude
    readings, using dead-band re-basing (this module's own
    _ALTITUDE_GAIN_DEADBAND_METERS) rather than trusting each raw
    reading directly - see that constant's own docstring for why.

    A reference altitude starts at the first reading and only ever
    moves once a later reading differs from it by more than the
    dead-band; only that move counts toward the returned gain (climbs
    only - descents move the reference down but add nothing, the same
    "ignore descents" convention compute_trip_stats() already
    documents). min/max track the *reference*'s own path rather than
    each individual raw reading, so on their own a single noisy
    outlier reading can't move either statistic - it has to actually
    clear the dead-band to register at all. A real, sustained climb or
    descent still passes through essentially unfiltered: each step
    only needs to clear the (small) dead-band once, so this only ever
    suppresses the sub-dead-band back-and-forth pure sensor noise
    produces, not a genuine trend.

    `altitudes` must be non-empty - callers only ever call this once
    they already know there's at least one reading, mirroring every
    other "only compute if there's data at all" guard in this module.
    """

    reference = altitudes[0]
    track_min = track_max = reference
    gain = 0.0

    for altitude in altitudes[1:]:
        delta = altitude - reference
        if delta > _ALTITUDE_GAIN_DEADBAND_METERS:
            gain += delta
            reference = altitude
        elif delta < -_ALTITUDE_GAIN_DEADBAND_METERS:
            reference = altitude
        track_min = min(track_min, reference)
        track_max = max(track_max, reference)

    return track_min, track_max, gain


def compute_trip_stats(fixes: tuple[GpsFix, ...]) -> TripStats | None:
    """Compute distance/average/max speed from a trip's merged GPS
    fixes.

    Distance is the sum of the great-circle distance between each pair
    of consecutive valid, positioned fixes - a straight-line
    approximation between fixes, not the road-following distance a
    routing engine would give, but fixes are frequent enough (roughly
    1Hz - see telemetry/gps_reader.py) that the difference is
    negligible for any normal driving speed.

    `average_speed_kmh` is the mean of each fix's own instantaneous
    `speed_kmh` reading (not distance/duration) - deliberately, so a
    long stationary stretch (traffic, a red light, parked with the
    engine running) correctly pulls the average down via its own
    near-zero readings, the same way a GPS trip computer usually
    reports "average speed". `max_speed_kmh` is the highest single
    reading. Both are None if no fix in the trip has a `speed_kmh`
    reading at all (some GPS sentence types don't carry one).

    `moving_seconds`/`idle_seconds` split the time between consecutive
    positioned fixes into "the vehicle was moving" vs. "it wasn't",
    using the same DEFAULT_SPEED_THRESHOLD_KMH (5.0) cutoff
    telemetry/movement.py already uses to decide whether GPS evidence
    shows movement at a trip-gap edge - reused here rather than
    picking a new, unrelated number. Each gap between two consecutive
    fixes is classified by the mean of each fix's own speed reading if
    it has one, or otherwise its *carried-forward* speed - the most
    recent earlier fix in the trip that did have a reading (see the
    forward-fill loop below). Confirmed against a real archive
    (Christer, 2026-07-24): a fix having a valid position but no speed
    reading of its own turns out to be common enough in practice - a
    long, otherwise perfectly GPS-tracked ~28-minute city drive showed
    barely 40% of its span reflected in moving_seconds+idle_seconds
    before this fix, because a gap between two speed-less fixes was
    previously skipped outright (counted toward neither bucket)
    instead of falling back to nearby data - silently discarding most
    of a real drive's duration from the breakdown without any
    indication in trip_info.txt that anything was missing. Only a
    fix with genuinely no earlier speed reading anywhere before it in
    the trip (i.e. no real reading has been seen yet at all) still
    contributes no classifiable segment - unavoidable, since there's
    truly nothing to carry forward from yet. Both moving_seconds/
    idle_seconds are None under the same condition average_speed_kmh/
    max_speed_kmh are None - no speed data at all anywhere in the
    trip. (average_speed_kmh/max_speed_kmh themselves are NOT
    carried-forward - they're deliberately each fix's own raw,
    unfilled reading only, same as before this fix.)

    `min_altitude_meters`/`max_altitude_meters`/`elevation_gain_meters`
    are all computed together by _hysteresis_altitude_stats() (see its
    own docstring for the dead-band re-basing this applies, and why -
    a naive raw-reading sum badly overstates "gain" from ordinary GPS
    altitude noise, confirmed against a real archive). The altitude
    sequence fed into it is every present `altitude_meters` reading
    among the trip's positioned fixes, in order, with any fix that
    lacks one simply dropped from the sequence rather than fragmenting
    it into disconnected segments around each gap - the same "bridge
    across gaps using the nearest real reading, don't silently drop
    the span" philosophy `moving_seconds`/`idle_seconds` above already
    use for missing speed readings, applied here to missing altitude
    readings instead. `elevation_gain_meters` additionally needs at
    least *two* present readings to mean anything (a single reading
    has no delta to measure) - `min_altitude_meters`/
    `max_altitude_meters` only need one. All three are None if there's
    no altitude_meters reading anywhere in the trip at all (see
    GpsFix.altitude_meters's own docstring for when that's the case).

    Returns None if there are fewer than two valid, positioned fixes -
    not enough to measure any distance from, the same "nothing to
    work with" convention render_map_video() and write_gpx() already
    use.
    """

    positioned = tuple(
        fix
        for fix in fixes
        if fix.valid and fix.latitude is not None and fix.longitude is not None
    )

    if len(positioned) < 2:
        return None

    # Forward-fill: each fix's own speed_kmh reading if it has one,
    # otherwise the most recent earlier reading in the trip (None
    # until the very first real reading appears in `positioned`) -
    # see the moving_seconds/idle_seconds docstring above for why.
    effective_speeds: list[float | None] = []
    last_known_speed_kmh: float | None = None
    for fix in positioned:
        if fix.speed_kmh is not None:
            last_known_speed_kmh = fix.speed_kmh
        effective_speeds.append(last_known_speed_kmh)

    total_meters = 0.0
    moving_seconds = 0.0
    idle_seconds = 0.0
    any_speed_data = False

    for index, (previous, current) in enumerate(zip(positioned, positioned[1:])):
        total_meters += _haversine_distance_meters(
            previous.latitude, previous.longitude,
            current.latitude, current.longitude,
        )

        segment_speeds = [
            speed
            for speed in (effective_speeds[index], effective_speeds[index + 1])
            if speed is not None
        ]
        if not segment_speeds:
            continue
        any_speed_data = True

        elapsed_seconds = (current.timestamp - previous.timestamp).total_seconds()
        segment_speed_kmh = sum(segment_speeds) / len(segment_speeds)
        if segment_speed_kmh < DEFAULT_SPEED_THRESHOLD_KMH:
            idle_seconds += elapsed_seconds
        else:
            moving_seconds += elapsed_seconds

    speeds = [fix.speed_kmh for fix in positioned if fix.speed_kmh is not None]
    average_speed_kmh = sum(speeds) / len(speeds) if speeds else None
    max_speed_kmh = max(speeds) if speeds else None

    altitudes = [
        fix.altitude_meters for fix in positioned if fix.altitude_meters is not None
    ]
    if altitudes:
        min_altitude_meters, max_altitude_meters, elevation_gain_meters = (
            _hysteresis_altitude_stats(altitudes)
        )
        # A single reading has no delta to measure "gain" from at all -
        # see this function's own docstring on why elevation_gain_meters
        # needs two readings where min/max only need one.
        if len(altitudes) < 2:
            elevation_gain_meters = None
    else:
        min_altitude_meters = max_altitude_meters = elevation_gain_meters = None

    return TripStats(
        distance_km=total_meters / 1000,
        average_speed_kmh=average_speed_kmh,
        max_speed_kmh=max_speed_kmh,
        moving_seconds=moving_seconds if any_speed_data else None,
        idle_seconds=idle_seconds if any_speed_data else None,
        min_altitude_meters=min_altitude_meters,
        max_altitude_meters=max_altitude_meters,
        elevation_gain_meters=elevation_gain_meters,
    )
