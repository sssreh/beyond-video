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
_ALTITUDE_CHANGE_DEADBAND_METERS = 2.0

# Second real-archive report (Christer, 2026-08-23, after the dead
# -band fix above was already live on bv-stats' own aggregated
# reports): "Elevation gain is to high, i want the difference for the
# lowest and highest value but remove crazy reading." The dead-band
# above only absorbs *small*, sub-meter-scale wobble - it does nothing
# against a single badly wrong altitude fix (multipath off a building,
# a momentary bad satellite geometry) that jumps far outside that
# band in one tick and then jumps straight back on the very next one.
# Such a reading used to get treated as fully real: it cleared the
# dead-band, so it moved the reference and got counted as a genuine
# climb/descent, then usually got counted *again* on the very next
# tick snapping back - a single bad fix could contribute close to
# double its own bogus jump to the total. _reject_altitude_outliers()
# below drops exactly that shape of reading (jumps away, and nothing
# after it confirms the vehicle actually went there) before the dead
# -band pass ever sees it. 30m in one ~1-second GPS tick is far beyond
# anything a car can climb or descend for real (even a steep 10% grade
# at motorway speed is on the order of 2-3m/s of real vertical
# movement - see _reject_altitude_outliers()'s own docstring) but well
# inside the range a single bad fix can produce.
_ALTITUDE_OUTLIER_JUMP_METERS = 30.0

# Third real-archive report (Christer, 2026-08-23, same day as the
# fix above): having reworked what was then elevation_gain_meters into
# a max-minus-min range so a single corrupted altitude fix couldn't
# inflate it any more, Christer pushed back on the *result* rather
# than the math: "How is that a gain?" - correctly, a range can't tell
# "drove up a hill and back down" (net change zero, but real climbing
# happened) apart from "just drove up once and stopped" the way the
# word "gain" is normally understood; it also can't ever go down,
# which read as suspicious on its own ("Up, up and away") even though
# it's mathematically inevitable for a range statistic. First fix: a
# true cumulative-ascent total (sum of every dead-banded climb,
# descents excluded), safe to bring back now that
# _reject_altitude_outliers() closes off the lone-bad-fix failure mode
# that had originally forced the switch away from a running total in
# the first place (that pass didn't exist yet the first time this was
# a running total).
#
# Fourth real-archive report, same day: "rename itto Elevation
# change." Renaming the *label* to "change" while the math still only
# ever summed climbs (never negative, same "up, up and away" shape as
# the range version) would have left the name and the behavior
# mismatched again - flagged this back to Christer via AskUserQuestion,
# who chose to redefine the math to match rather than just relabel it.
# `elevation_change_meters` (renamed from `elevation_gain_meters`
# throughout the codebase to match) is now the *net* change: the
# dead-banded reference's final position minus its starting position,
# after outlier rejection - can be positive (net climb), negative (net
# descent), or zero (round trip back to the same altitude, the exact
# "drove up a hill and back down" case from the third report above,
# now correctly reported as zero *change* rather than either a
# misleading zero *range* or a misleading 50m of *gain*). min/max still
# track the full dead-banded span independently, so "how high did it
# get" and "did it end up net higher or lower" stay two separate,
# equally answerable questions.
#
# Real-archive report (Christer, 2026-08-23): the Stats dashboard
# showed a max_speed_kmh of 322.3 km/h for a trip in a car whose real
# electronic limiter caps it at 250 km/h - obviously a spurious
# reading. speed_kmh is parsed straight from each GPS fix's own
# $GPRMC speed-over-ground field (telemetry/gps_reader.py's
# _parse_rmc()), not computed from consecutive lat/lon and elapsed
# time, so the classic "tiny elapsed time amplifies a small position
# jump" failure mode doesn't apply here - this is the receiver's own
# instantaneous speed estimate glitching for a single epoch
# (multipath, momentary bad satellite geometry), the same underlying
# GPS-noise class as the altitude spike this module already filters
# (see _ALTITUDE_OUTLIER_JUMP_METERS' own docstring just above).
# max_speed_kmh used to trust every raw reading directly, so one bad
# epoch could set the whole trip's reported max. 100 km/h of change
# within one ~1-second GPS tick (fixes arrive at roughly 1Hz - see
# compute_trip_stats()'s own docstring) is about 2.8g of longitudinal
# acceleration - far beyond anything a road car can do in a single
# tick (even a supercar's 0-100 km/h in under 2 seconds averages
# under 1.5g, and that's a sustained ramp across many ticks, not one
# instantaneous jump) - but well inside the range a single bad GPS fix
# can produce.
_SPEED_OUTLIER_JUMP_KMH = 100.0

# Real-archive report (Christer, 2026-08-23, second follow-up after the
# lone-bad-fix filter above and its leading/trailing-edge extension
# both shipped): the Stats dashboard's archive-wide max speed was
# STILL 322.3 km/h - the exact same number, on the exact same
# recording (20260730_162351_N), even after both fixes. Tracing that
# recording's raw .gps file found why: this isn't a single bad tick
# that snaps straight back (the shape both filters above catch) - it's
# a brief *decaying* run of bad readings (174.046kn/322.3km/h, then
# 94.089kn/174.3km/h, then 43.438kn/80.4km/h) immediately after three
# consecutive $GPRMC sentences reporting no fix at all (mode indicator
# 'N'), while the vehicle's own real speed the whole time - per every
# surrounding reading and the GPGGA position, essentially unmoving -
# was close to zero. The receiver briefly hallucinated a burst of fast
# movement while reacquiring a fix, then correctly reported near-zero
# again a few ticks later. Because each of those three readings
# differs from the one before it (322 -> 174 -> 80, decaying rather
# than snapping back in one tick), _reject_speed_outliers()'s "the
# very next reading returns within threshold of the last accepted one"
# confirmation never fires for any of them - a multi-tick decay
# defeats a filter built to catch a single-tick spike-and-instant
# -return, interior or edge.
#
# This is the same "the receiver needs a moment after reacquiring
# before its own readings can be trusted" problem
# SNAPSHOT_WARMUP_FRAMES already exists to work around for camera
# snapshots (bv_gps.py) - applied here to GPS speed instead. Whenever
# at least one genuinely *invalid* fix (GpsFix.valid False - the
# receiver reporting mode 'N', no position at all) sits between two
# positioned fixes, that means a real GPS dropout just ended - and the
# first few positioned readings to arrive after it have their
# speed_kmh excluded from average/max entirely, regardless of what
# their own mode indicator claims (the very first reading after this
# real dropout had mode 'A' - technically "valid" per GpsFix.valid's
# own docstring - while its older, ignored status field still said
# 'V'; the receiver's own signals disagreed with each other, so mode
# alone isn't a reliable enough signal to lean on here).
#
# An earlier version of this fix triggered on elapsed time between
# consecutive positioned fixes instead (more than a couple of normal
# ~1Hz tick intervals apart) - simpler, and it looked equivalent on
# this one real recording, but it was wrong in general: a large gap
# between two positioned fixes can also just mean a recording has
# naturally sparse GPS (infrequent fixes, no dropout at all), which is
# legitimate and already anticipated elsewhere in this codebase's own
# moving/idle carry-forward logic. That version incorrectly discarded
# a perfectly good, sparse-but-real reading whenever two positioned
# fixes just happened to be far apart in time - caught by an existing
# test (test_stats.py's test_compute_recording_stats_gps_and_gsensor_fields,
# whose two fixes are 60s apart with no dropout at all) failing before
# this ever shipped. Checking for an actually-invalid fix in between,
# rather than just a time gap, is what tells the two cases apart.
#
# Position/altitude are untouched by this - only speed_kmh sampling.
# Simulated against the real culprit recording's raw .gps file before
# shipping: this drops its own max_speed_kmh from 322.3 to a
# plausible 41.9 km/h, matching the range every neighboring recording
# that same day already showed.
_GPS_REACQUISITION_SETTLE_READINGS = 3


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
    # min/max are independent of elevation_change_meters (each fix's
    # own raw altitude reading, same "not carried-forward" convention
    # average_speed_kmh/max_speed_kmh already use) - Christer wants
    # this for a stitch-video/playback overlay later, so both "the
    # range climbed" and "the net change" are worth keeping separately
    # rather than picking just one.
    min_altitude_meters: float | None = None
    max_altitude_meters: float | None = None
    elevation_change_meters: float | None = None


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


def _reject_altitude_outliers(altitudes: list[float]) -> list[float]:
    """Drop a lone bad GPS altitude fix from `altitudes` before
    anything downstream (dead-band re-basing, min/max, change) ever
    sees it - see _ALTITUDE_OUTLIER_JUMP_METERS' own docstring for the
    real-archive report this fixes.

    A reading is rejected only if it jumps more than
    _ALTITUDE_OUTLIER_JUMP_METERS away from the last *accepted*
    reading AND the very next raw reading lands back within that same
    threshold of that last accepted reading - i.e. neither the reading
    itself nor anything after it is confirmed by what comes next, the
    signature of a single bad fix that snaps back on its own. A real
    climb or descent doesn't behave this way: consecutive readings
    keep moving away together, they don't jump once and immediately
    snap back to where they started. This is deliberately a one-sided
    check against the previously *accepted* value, not the raw
    previous reading, so two bad fixes in a row can't each "confirm"
    the other and both slip through.

    The first and last readings in the sequence are never rejected -
    each only has one neighbor, not enough evidence either way to
    tell a real trip endpoint from a bad fix, and rejecting a
    sequence's own edges would need guessing at data outside the
    sequence entirely. Sequences shorter than 3 readings have no
    interior point to evaluate at all and pass through unchanged.
    """

    if len(altitudes) < 3:
        return list(altitudes)

    accepted = [altitudes[0]]
    for index in range(1, len(altitudes) - 1):
        last_accepted = accepted[-1]
        current = altitudes[index]
        following = altitudes[index + 1]
        is_lone_spike = (
            abs(current - last_accepted) > _ALTITUDE_OUTLIER_JUMP_METERS
            and abs(following - last_accepted) <= _ALTITUDE_OUTLIER_JUMP_METERS
        )
        if not is_lone_spike:
            accepted.append(current)
    accepted.append(altitudes[-1])

    return accepted


def _reject_speed_outliers(speeds: list[float]) -> list[float]:
    """Drop a lone bad GPS speed-over-ground reading from `speeds`
    before average_speed_kmh/max_speed_kmh ever see it - see
    _SPEED_OUTLIER_JUMP_KMH's own docstring for the real-archive
    report this fixes.

    Reuses _reject_altitude_outliers()' exact one-sided "jumps more
    than the threshold away from the last *accepted* reading, and the
    very next raw reading snaps back within that same threshold"
    signature, applied to speed_kmh instead of altitude_meters - see
    that function's own docstring for the full reasoning (a real,
    sustained acceleration or braking keeps moving away across
    consecutive readings; a single bad fix spikes once and snaps
    straight back). Sequences shorter than 3 readings pass through
    unchanged (no interior point to evaluate at all).

    Unlike _reject_altitude_outliers(), the leading and trailing
    readings *are* checked here (real-archive report, Christer,
    2026-08-23: after the interior-only version of this filter
    shipped, an archive-wide max_speed_kmh of 322.3 km/h - the exact
    same number as before the filter existed - was still showing up,
    which only makes sense if the bad fix sat at a recording's own
    first or last GPS reading, where the interior check never even
    looks). Each ~1-3 minute recording gets its own fresh fix
    sequence, and a receiver's very first fix right after it
    (re)acquires satellites at the start of a new segment is exactly
    where a bad instantaneous speed estimate is most likely - the
    interior check's "confirmed by a snap-back on the very next
    reading" signature doesn't apply at an edge (there's no reading on
    the far side to snap back to), so it's corroborated the other way
    instead: the edge reading is dropped only if it jumps away from
    its one neighbor AND that neighbor is itself confirmed by *its*
    next reading (i.e. the neighbor looks like a real, continuing
    trajectory, making the edge reading's isolated jump away from it
    the anomaly, not the neighbor). This needs at least 3 readings to
    evaluate at either edge, same threshold as the interior check.
    """

    if len(speeds) < 3:
        return list(speeds)

    n = len(speeds)
    start = 0
    if (
        abs(speeds[0] - speeds[1]) > _SPEED_OUTLIER_JUMP_KMH
        and abs(speeds[1] - speeds[2]) <= _SPEED_OUTLIER_JUMP_KMH
    ):
        start = 1

    end = n
    if (
        abs(speeds[n - 1] - speeds[n - 2]) > _SPEED_OUTLIER_JUMP_KMH
        and abs(speeds[n - 2] - speeds[n - 3]) <= _SPEED_OUTLIER_JUMP_KMH
    ):
        end = n - 1

    trimmed = speeds[start:end]
    if len(trimmed) < 3:
        return trimmed

    accepted = [trimmed[0]]
    for index in range(1, len(trimmed) - 1):
        last_accepted = accepted[-1]
        current = trimmed[index]
        following = trimmed[index + 1]
        is_lone_spike = (
            abs(current - last_accepted) > _SPEED_OUTLIER_JUMP_KMH
            and abs(following - last_accepted) <= _SPEED_OUTLIER_JUMP_KMH
        )
        if not is_lone_spike:
            accepted.append(current)
    accepted.append(trimmed[-1])

    return accepted


def _speeds_excluding_reacquisition_settle(
    fixes: tuple[GpsFix, ...],
) -> list[float]:
    """Return the trip's positioned fixes' own speed_kmh readings, with
    the first _GPS_REACQUISITION_SETTLE_READINGS of them excluded
    after any real gap in GPS validity - see
    _GPS_REACQUISITION_SETTLE_READINGS' own docstring for the
    real-archive report this fixes.

    Takes the *raw* `fixes` sequence (before compute_trip_stats()'s own
    "positioned" filtering), not just the positioned ones, specifically
    so it can tell "a real dropout just ended" (at least one actually
    -invalid fix - GpsFix.valid False - sits between two positioned
    fixes) apart from "this recording just has naturally sparse GPS
    fixes" (no invalid fix in between at all, e.g. a short recording
    with only a start and end reading, still perfectly legitimate).
    An early version of this filter used only the elapsed time between
    consecutive positioned fixes as its trigger, which looked
    equivalent on the one real recording that motivated it but turned
    out to be wrong in general: a large gap alone doesn't mean a
    dropout happened, and that version incorrectly discarded a
    perfectly good, sparse-but-real reading whenever two positioned
    fixes just happened to be far apart in time.

    The receiver's own speed estimate needs a few ticks to reconverge
    after reacquiring a real dropout, regardless of what those first
    readings' own mode indicator claims (see
    _GPS_REACQUISITION_SETTLE_READINGS' docstring - the very first
    reading after a real dropout can itself report a "valid" mode
    while still being garbage). Runs before _reject_speed_outliers()
    gets a chance to look at the sequence at all - the settling
    readings this drops can decay across several ticks rather than
    spiking once and snapping back, which is exactly the shape
    _reject_speed_outliers() can't catch on its own (see its own
    docstring).
    """

    speeds: list[float] = []
    settle_remaining = 0
    saw_invalid_since_last_positioned = False

    for fix in fixes:
        is_positioned = (
            fix.valid and fix.latitude is not None and fix.longitude is not None
        )
        if not is_positioned:
            saw_invalid_since_last_positioned = True
            continue

        if saw_invalid_since_last_positioned:
            settle_remaining = _GPS_REACQUISITION_SETTLE_READINGS
        saw_invalid_since_last_positioned = False

        if settle_remaining > 0:
            settle_remaining -= 1
            continue

        if fix.speed_kmh is not None:
            speeds.append(fix.speed_kmh)

    return speeds


def _hysteresis_altitude_stats(altitudes: list[float]) -> tuple[float, float, float]:
    """min/max/net-elevation-change over a sequence of raw altitude
    readings, using outlier rejection then dead-band re-basing rather
    than trusting each raw reading directly.

    Two passes: _reject_altitude_outliers() first drops any lone bad
    GPS fix (see its own docstring), then a dead-band re-basing pass
    (this module's own _ALTITUDE_CHANGE_DEADBAND_METERS) absorbs
    ordinary sub-meter GPS altitude jitter - a reference altitude
    starts at the first (filtered) reading and only ever moves once a
    later reading differs from it by more than the dead-band, in
    either direction. min/max track that reference's own path rather
    than each individual raw reading, so on their own the remaining
    sub-dead-band noise can't move either statistic.

    `elevation_change_meters` is the *net* change: the dead-banded
    reference's final position minus its starting position - can be
    positive (net climb), negative (net descent), or zero (a round
    trip back to the same altitude). This field has gone through two
    earlier definitions, both abandoned for the same underlying reason
    (see this module's own top-of-file comments for the full history
    of each real-archive report that drove each change): a running
    total of every dead-banded climb with descents excluded (overstated
    trips whenever a bad fix slipped past the dead-band, before
    _reject_altitude_outliers() existed to catch it), then a
    max-minus-min range (couldn't distinguish a genuine climb-then
    -descend from never having climbed at all, and its label - first
    "Elevation gain," Christer's own later request to rename it
    "Elevation change" - stopped matching a value that could never go
    negative). Net change is what "change" actually means, is safe
    against the same lone-spike failure mode as both earlier
    definitions (outlier rejection already ran above), and correctly
    reports zero for a there-and-back trip rather than either a
    misleading zero range or a misleading positive "gain".

    `altitudes` must be non-empty - callers only ever call this once
    they already know there's at least one reading, mirroring every
    other "only compute if there's data at all" guard in this module.
    """

    filtered = _reject_altitude_outliers(altitudes)

    reference = filtered[0]
    start_reference = reference
    track_min = track_max = reference

    for altitude in filtered[1:]:
        delta = altitude - reference
        if abs(delta) > _ALTITUDE_CHANGE_DEADBAND_METERS:
            reference = altitude
        track_min = min(track_min, reference)
        track_max = max(track_max, reference)

    net_change = reference - start_reference

    return track_min, track_max, net_change


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
    reading at all (some GPS sentence types don't carry one). Both are
    computed over _reject_speed_outliers()'s filtered readings, which
    in turn run on _speeds_excluding_reacquisition_settle()'s own
    output rather than the raw sequence directly - two independent
    passes, since a real archive produced two different shapes of bad
    reading: a single tick that spikes and snaps straight back
    (_reject_speed_outliers()), and a multi-tick decay right after a
    GPS dropout ends (_speeds_excluding_reacquisition_settle()) - see
    both functions' and _SPEED_OUTLIER_JUMP_KMH's/the comment above
    _GPS_REACQUISITION_SETTLE_READINGS' own real-archive reports (both
    a reported max_speed_kmh of 322.3 km/h in a car limited to 250)
    each one fixes.

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

    `min_altitude_meters`/`max_altitude_meters`/`elevation_change_meters`
    are all computed together by _hysteresis_altitude_stats() (see its
    own docstring, and _reject_altitude_outliers()' and
    _ALTITUDE_OUTLIER_JUMP_METERS' own docstrings, for the two-pass
    filtering this applies and why - a naive raw-reading sum badly
    overstates "change" from ordinary GPS altitude noise, and a single
    badly-wrong altitude fix can overstate it far worse still, both
    confirmed against real archives). `elevation_change_meters` is the
    net change (final dead-banded altitude minus starting altitude,
    can be positive, negative, or zero) - see
    _hysteresis_altitude_stats()'s own docstring for the two earlier
    definitions this field has had and why each was abandoned. The
    altitude sequence fed into it is every present
    `altitude_meters` reading among the trip's positioned fixes, in
    order, with any fix that
    lacks one simply dropped from the sequence rather than fragmenting
    it into disconnected segments around each gap - the same "bridge
    across gaps using the nearest real reading, don't silently drop
    the span" philosophy `moving_seconds`/`idle_seconds` above already
    use for missing speed readings, applied here to missing altitude
    readings instead. `elevation_change_meters` additionally needs at
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

    speeds = _speeds_excluding_reacquisition_settle(fixes)
    filtered_speeds = _reject_speed_outliers(speeds)
    average_speed_kmh = (
        sum(filtered_speeds) / len(filtered_speeds) if filtered_speeds else None
    )
    max_speed_kmh = max(filtered_speeds) if filtered_speeds else None

    altitudes = [
        fix.altitude_meters for fix in positioned if fix.altitude_meters is not None
    ]
    if altitudes:
        min_altitude_meters, max_altitude_meters, elevation_change_meters = (
            _hysteresis_altitude_stats(altitudes)
        )
        # A single reading has no delta to measure "change" from at
        # all - see this function's own docstring on why
        # elevation_change_meters needs two readings where min/max
        # only need one.
        if len(altitudes) < 2:
            elevation_change_meters = None
    else:
        min_altitude_meters = max_altitude_meters = elevation_change_meters = None

    return TripStats(
        distance_km=total_meters / 1000,
        average_speed_kmh=average_speed_kmh,
        max_speed_kmh=max_speed_kmh,
        moving_seconds=moving_seconds if any_speed_data else None,
        idle_seconds=idle_seconds if any_speed_data else None,
        min_altitude_meters=min_altitude_meters,
        max_altitude_meters=max_altitude_meters,
        elevation_change_meters=elevation_change_meters,
    )
