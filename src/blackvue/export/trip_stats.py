"""
Trip-level distance/speed statistics for bv-export's trip_info.txt.

Copyright (C) 2026 Christer R. (sssreh)

SPDX-License-Identifier: GPL-3.0-or-later
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime

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
# -band pass ever sees it.
#
# Originally a flat 30m-in-one-tick jump threshold (a car-specific
# rule of thumb, not stated as an elapsed-time rate). Superseded by
# the rate-based redesign below (see _ALTITUDE_IMPLAUSIBLE_RATE_MPS'
# own docstring, further down this file) once a later real-archive
# report showed that same style of fixed-magnitude threshold doesn't
# generalize worldwide - _reject_altitude_outliers() now measures the
# same underlying "did the vehicle really move that much" question
# against real elapsed time instead of a bare per-tick number, which
# is what lets one mechanism serve both the lone-spike shape this
# report is about and the multi-reading glitch-cluster shape the later
# report found.

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
# Fifth real-archive report (Christer, 2026-08-24, on the same
# Kirby_2025 archive whose speed readings motivated
# _SPEED_IMPLAUSIBLE_CEILING_KMH above): archive-wide elevation change
# for the year was -10513 m - Christer, after two prior rounds of
# oddball recordings each dominating an aggregate: "I start to feel
# that the stats are 99% good for every recordingid, but when you
# summarize over a year, some oddballs destroys it." Traced to
# 20250409_122447_E (elevation_change_m: -7397.4) - its raw .gps file
# shows the exact same failure mechanism as the speed ceiling's own
# report, just on altitude instead: four consecutive $GPGGA readings
# (7479.4/7475.9/7467.8/7462.4m, decaying gently, mutually
# self-corroborating rather than spiking-and-snapping-back) arrive
# during a brief mode='A'/status='V' window in the middle of what's
# otherwise a ~45-second real dropout, before the receiver reacquires
# properly moments later at the correct, mundane ~80m altitude.
# _reject_altitude_outliers() (single-tick spike-and-snap-back) can't
# catch a self-corroborating cluster any more than
# _reject_speed_outliers() could for speed - see that function's own
# docstring for why - and unlike speed, altitude has no settle-window
# pass at all yet, so this cluster sailed straight into the dead-band
# re-basing pass and became the "highest" reading the whole trip's
# elevation_change_meters got measured against.
#
# An initial fix (shipped briefly, same day) added a flat, absolute
# ceiling: any single reading above 3000m rejected outright (safely
# above the highest paved road in Europe a car could plausibly be
# driven on, ~2802m). Christer flagged the same worldwide-validity
# problem here as for the speed ceiling above (Beyond Video isn't just
# for Christer's own car and Sweden/Europe's roads - a fixed number
# tied to "the highest road on one continent" isn't a valid general
# rule) and asked for a duration-based check instead: "is it plausible
# to change elevation with 3000m in 3 minutes."
#
# Unlike speed, altitude has no second independent GPS-derived channel
# to cross-check against (no lat/lon-implied "altitude" the way
# position implies speed) - so this can't be replaced with a cross
# -check the way _reject_speed_position_mismatches() replaced the
# speed ceiling. What it *can* do is exactly what Christer asked:
# check the rate of change against real elapsed time
# (GpsFix.timestamp) as the PRIMARY mechanism, rather than trusting an
# absolute value. A genuine climb or descent, however steep, has a
# physically bounded vertical rate - see _ALTITUDE_IMPLAUSIBLE_RATE_MPS'
# own docstring below for that bound. The glitch cluster this was
# built for implies ~164 m/s of vertical rate bridging the ~45s
# dropout to the recording's own correct post-dropout readings - two
# orders of magnitude past anything a real car's suspension and tires
# could survive on any road, anywhere.
#
# But rate alone turns out not to be sufficient on its own, proven by
# this exact glitch cluster: it's 4 readings, all internally
# self-corroborating (small tick-to-tick deltas among themselves), so
# splitting the sequence by rate produces exactly two segments - the
# 4-reading bad cluster and the 3-reading good cluster after the
# dropout - with only ONE boundary between them, and nothing else in
# this short (~1-3 minute) recording to compare either side against.
# Rate math alone is symmetric: it can tell you the two segments don't
# reconcile with each other, but not which one is the real one,
# and (confirmed by this exact case) segment *length* isn't a safe
# tiebreaker either - a short recording can easily have its bad
# cluster be as long as, or longer than, its remaining good data.
# Christer's own suggestion once this was explained: use the absolute
# check as a fallback specifically for this kind of otherwise
# -ambiguous split, rather than as the first-line filter it used to
# be. _ALTITUDE_IMPLAUSIBLE_CEILING_METERS below is that fallback -
# recalibrated from "highest road in Europe" (Christer's original,
# regional complaint) to the highest *motorable road on Earth*
# (Mig La Pass, Ladakh, ~5,913m as of late 2025 - Border Roads
# Organisation), which is a geographic fact rather than a
# region-or-vehicle-specific assumption, so it stays worldwide-valid
# even though it's still an absolute number.
#
# Confirmed against a second, genuinely independent sensor too: this
# recording's own raw .3gf g-sensor track sits in a flat,
# idling/parked-vehicle band (roughly X=118-124, Y=-4 to -12,
# Z=24-30) for the entire window the GPS claims a climb to ~7479m -
# nothing resembling the sustained pitch/vibration change a real climb
# of any size would produce, let alone one of that magnitude. Same
# caveats as the speed constant's own report above apply to why this
# isn't wired into the filter itself (uncalibrated units, not every
# adapter records g-sensor data at all) - it's independent confirmation
# the diagnosis is right, not a load-bearing part of the fix.
_ALTITUDE_IMPLAUSIBLE_RATE_MPS = 30.0
# ^ Primary mechanism. A generous, vehicle-and-geography-agnostic
# ceiling on real-world vertical rate of change for anything on
# wheels: even a very steep real road (30% grade, steeper than almost
# any paved road on Earth) taken at 100 km/h implies under 8 m/s of
# vertical rate; a mountain switchback taken slowly is far less than
# that. 30 m/s (deliberately the same number this module's old flat
# _ALTITUDE_OUTLIER_JUMP_METERS used, now correctly reinterpreted as a
# rate rather than an assumed-~1Hz per-tick magnitude) leaves nearly
# 4x that much headroom over the steepest plausible real case, comfortably
# above GPS's own ordinary altitude noise (+-1-4m tick-to-tick, i.e. a
# few m/s at most - see _ALTITUDE_CHANGE_DEADBAND_METERS' own
# docstring), for any vehicle/road combination this project might see
# worldwide, while still being over two orders of magnitude below the
# ~164 m/s the real glitch cluster implies (or the ~2,460 m/s implied
# by this fix's own compressed-timing unit test - see
# test_compute_trip_stats_elevation_rejects_a_self_corroborating_glitch_cluster).
_ALTITUDE_GLITCH_SEGMENT_MAX_READINGS = 1
# ^ _reject_altitude_outliers() below splits a trip's altitude
# readings into segments wherever the implied vertical rate between
# two consecutive readings exceeds _ALTITUDE_IMPLAUSIBLE_RATE_MPS.
# Genuinely lone bad fixes - a single reading, isolated by an
# implausible rate on both sides - are always exactly this size (1)
# and get dropped outright as the primary rule, the direct rate-based
# descendant of this module's original lone-spike-and-snap-back check.
# Multi-reading clusters (2+) are deliberately NOT auto-discarded by
# size alone here - see _ALTITUDE_IMPLAUSIBLE_CEILING_METERS' own
# docstring for why that specific heuristic was tried and abandoned -
# they're instead caught by the ceiling fallback when their values are
# extreme enough to matter, same as the real cluster this was built
# for.
_ALTITUDE_IMPLAUSIBLE_CEILING_METERS = 6500.0
# ^ Fallback only - see the "Fifth real-archive report" comment above
# for the full reasoning on why rate-based segmentation alone can't
# always resolve which side of an implausible-rate split is real, and
# why Christer asked for this to be a fallback rather than the
# first-line filter it used to be. 6,500m sits ~600m above the highest
# motorable road anywhere on Earth as of this writing (Mig La Pass,
# Ladakh, ~5,913m) - a geographic fact, not a per-region or
# per-vehicle assumption - while staying comfortably below the real
# glitch cluster's ~7,460-7,479m. _reject_altitude_outliers() only
# ever consults this when rate-based segmentation alone leaves the
# result ambiguous (no segment survives the primary size rule, or a
# surviving segment is itself above this ceiling) - a normal trip,
# even a very high-altitude one, never reaches values anywhere near
# this number and never triggers the fallback at all.
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
# (see _ALTITUDE_IMPLAUSIBLE_RATE_MPS' own docstring just above).
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

# Real-archive report (Christer, 2026-08-24, on a separate 2025 archive
# freshly stats'd for the first time): archive-wide max speed was
# 305.9 km/h - his car's own electronic limiter caps it at 250 km/h
# (see this constant's own report above), so this is implausible on
# its face regardless of shape. Traced to 20250730_070613_E: the
# recording's own first four $GPRMC readings (163.4/163.4/165.1/164.1
# knots = ~302.7/302.6/305.9/304.0 km/h) are all mutually consistent
# with EACH OTHER - not a lone spike that jumps away and snaps back
# (_reject_speed_outliers()'s shape) and not a decay after a dropout
# ends (_speeds_excluding_reacquisition_settle()'s shape) - immediately
# followed by a genuine ~45-second dropout (mode 'N'), after which the
# receiver reacquires at the *correct* real position with normal,
# plausible speeds. The real position barely moves across those four
# ticks (a few meters), so this is the receiver's speed-over-ground
# estimate hallucinating a sustained high value for several seconds
# right as it's about to lose lock entirely - the mirror image of the
# post-dropout settle problem, but *before* the dropout instead of
# after, and self-corroborating rather than decaying, so neither
# existing filter's "does the neighbor confirm or contradict" logic
# catches it.
#
# An initial fix (shipped briefly, same day) added a flat, absolute
# ceiling - any single reading above 260 km/h (10 km/h of headroom
# above Christer's own car's 250 km/h limiter) rejected outright.
# Christer flagged the real problem with that approach himself: Beyond
# Video is meant to be used worldwide, by people with different cars
# (no 250 km/h limiter at all) and different roads, so a ceiling tied
# to one specific car's specs isn't a valid general rule - and pushed
# for a duration/rate-based check instead ("is it plausible to change
# elevation with 3000m in 3 minutes" was his framing for the altitude
# counterpart below, same principle applies here). For speed
# specifically, replaying "how fast is the vehicle *really* going"
# against real elapsed time doesn't need a rate bound at all - GPS
# already reports two independent ways to know that: the receiver's
# own instantaneous speed-over-ground estimate (speed_kmh, what this
# whole module has been filtering so far), and the vehicle's actual
# lat/lon position from one fix to the next, which _reject_speed
# _position_mismatches() below cross-checks against. Confirmed against
# a second, genuinely independent sensor too: this recording's own raw
# .3gf g-sensor track sits in a flat, idling-vehicle band (roughly
# X=100-140, Y=+-20, Z=10-60) for the entire window the GPS claims
# ~305 km/h - nothing resembling the vibration/G signature of a car
# actually moving that fast. g-sensor units are uncalibrated (see
# telemetry/gsensor_reader.py's own docstring) so this can't be turned
# into a numeric threshold here, and not every camera/adapter this
# project supports even records g-sensor data (the GoPro adapter has
# none) - so it isn't wired into the filter itself - but it's real,
# independent confirmation that this glitch is exactly what
# _reject_speed_position_mismatches() below assumes it is: the
# receiver's speed estimate hallucinating, not the vehicle actually
# moving.
_SPEED_POSITION_CROSS_CHECK_MAX_ELAPSED_SECONDS = 5.0
# ^ Only cross-check between fixes close enough together in time that
# a straight-line (haversine) chord is still a good stand-in for the
# vehicle's actual path - a curve or hill can make the chord
# noticeably shorter than the real distance driven, but only over
# several seconds or more; at BlackVue's normal ~1Hz tick rate this
# window comfortably covers ordinary consecutive ticks without
# reaching across a real GPS dropout (multi-second-to-minutes gap),
# where an averaged "implied speed" across the whole gap wouldn't mean
# anything about either edge's own instantaneous reading anyway.
_SPEED_POSITION_CROSS_CHECK_FACTOR = 5.0
_SPEED_POSITION_CROSS_CHECK_MARGIN_KMH = 20.0
# ^ A reading is rejected only if it exceeds its own position-implied
# speed by more than this factor-and-margin - generous on purpose.
# Real fast driving isn't perfectly straight-line even over ~1s (a
# gentle curve, a fix that lands slightly off the road centerline),
# and the margin absorbs ordinary position jitter at low real speeds
# (a parked or idling car's GPS position can drift a few meters
# between ticks purely from noise, which would otherwise imply a
# small-but-nonzero speed and unfairly flag a legitimate low but
# nonzero reported reading). 5x + 20 km/h comfortably passes any real
# acceleration or cornering while still catching the glitch cluster
# above by roughly 15-30x, not a borderline call.

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


def _reject_altitude_outliers(
    altitude_fixes: list[tuple[datetime, float]],
) -> list[float]:
    """Split `altitude_fixes` (each reading's own timestamp paired with
    its altitude) into segments wherever the implied vertical rate of
    change between two consecutive readings exceeds
    _ALTITUDE_IMPLAUSIBLE_RATE_MPS, then decide which segment(s) to
    keep in two passes - see _ALTITUDE_IMPLAUSIBLE_RATE_MPS' and
    _ALTITUDE_IMPLAUSIBLE_CEILING_METERS' own docstrings for the full
    real-archive reasoning behind why two passes are needed rather
    than one.

    Pass 1 (primary, rate-based): any segment of exactly
    _ALTITUDE_GLITCH_SEGMENT_MAX_READINGS (1) reading, isolated by an
    implausible rate on both sides, is a lone bad fix and gets dropped
    outright - the direct duration-aware descendant of this module's
    original "jumps away and snaps back in one tick" check. Every
    longer segment survives this pass, including a bad multi-reading
    cluster - rate alone can't yet tell a real short stretch of driving
    apart from a short run of mutually-corroborating bad readings.

    Pass 2 (fallback, absolute): only runs if pass 1 leaves nothing
    (every segment was a lone spike, or the sequence was one single
    segment with no split at all) or if a segment pass 1 kept is
    itself flatly impossible (any reading over
    _ALTITUDE_IMPLAUSIBLE_CEILING_METERS - higher than any road on
    Earth reaches). When it runs, every segment (not just the ones
    pass 1 kept) is filtered by the ceiling alone, discarding any
    segment containing even one reading above it. This is what
    actually resolves the real-archive glitch this function was
    rewritten for: a 4-reading bad cluster and a 3-reading good
    cluster, both surviving pass 1 (neither is a lone spike), with the
    bad one being at or above the size of the good one - length can't
    arbitrate that split, but the bad cluster's own ~7,460-7,479m
    readings can, once measured against a real, worldwide fact instead
    of a per-region or per-vehicle guess.

    If everything is filtered out by both passes (a fully degenerate
    recording where every reading, in every segment, is somehow above
    the ceiling), the single largest segment is returned anyway -
    reporting something rather than nothing, the same "give the most
    plausible answer available" spirit this module has always used for
    genuinely unresolvable inputs.

    Segments are kept or discarded *whole*, not reading-by-reading
    within a kept segment - every reading in a surviving segment passes
    through unfiltered, the same "not a smoothing filter" property the
    original lone-spike check had. A recording with two or more
    genuine GPS dropouts, each separated by a real reacquisition, keeps
    every one of its long, legitimate segments in the normal case
    (pass 1 alone, ceiling fallback never triggered).
    """

    if not altitude_fixes:
        return []

    segments: list[list[float]] = [[altitude_fixes[0][1]]]
    for (previous_ts, previous_alt), (ts, alt) in zip(
        altitude_fixes, altitude_fixes[1:]
    ):
        elapsed = (ts - previous_ts).total_seconds()
        rate = abs(alt - previous_alt) / elapsed if elapsed > 0 else float("inf")
        if rate > _ALTITUDE_IMPLAUSIBLE_RATE_MPS:
            segments.append([])
        segments[-1].append(alt)

    kept = [
        segment
        for segment in segments
        if len(segment) > _ALTITUDE_GLITCH_SEGMENT_MAX_READINGS
    ]

    needs_ceiling_fallback = not kept or any(
        altitude > _ALTITUDE_IMPLAUSIBLE_CEILING_METERS
        for segment in kept
        for altitude in segment
    )
    if needs_ceiling_fallback:
        kept = [
            segment
            for segment in segments
            if all(
                altitude <= _ALTITUDE_IMPLAUSIBLE_CEILING_METERS
                for altitude in segment
            )
        ]

    if not kept:
        kept = [max(segments, key=len)]

    return [altitude for segment in kept for altitude in segment]


def _reject_speed_outliers(speeds: list[float]) -> list[float]:
    """Drop a lone bad GPS speed-over-ground reading from `speeds`
    before average_speed_kmh/max_speed_kmh ever see it - see
    _SPEED_OUTLIER_JUMP_KMH's own docstring for the real-archive
    report this fixes.

Uses the same one-sided "jumps more than the threshold away from
    the last *accepted* reading, and the very next raw reading snaps
    back within that same threshold" signature
    _reject_altitude_outliers() originally used for its own lone-spike
    case, applied to speed_kmh instead of altitude_meters (a real,
    sustained acceleration or braking keeps moving away across
    consecutive readings; a single bad fix spikes once and snaps
    straight back). _reject_altitude_outliers() itself has since been
    rewritten into a duration-aware rate-and-ceiling segment filter
    (see its own docstring) rather than this exact interior-snap-back
    shape, so the two no longer share an implementation - only the
    underlying "spike vs. sustained trajectory" idea. Sequences
    shorter than 3 readings pass through unchanged (no interior point
    to evaluate at all).

    Unlike a plain interior-only version of this check, the leading
    and trailing readings *are* checked here (real-archive report, Christer,
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
    poisoned_indices: frozenset[int] = frozenset(),
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

    `poisoned_indices` (indices into `fixes`, from
    _reject_speed_position_mismatches()) are skipped entirely - their
    speed_kmh reading is dropped the same as if it were None - but the
    fix itself still counts as positioned for settle-window purposes
    (only its speed estimate was shown to be untrustworthy; its
    position, which is what the cross-check validated it against, is
    not in question).
    """

    speeds: list[float] = []
    settle_remaining = 0
    saw_invalid_since_last_positioned = False

    for index, fix in enumerate(fixes):
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

        if index in poisoned_indices:
            continue

        if fix.speed_kmh is not None:
            speeds.append(fix.speed_kmh)

    return speeds


def _reject_speed_position_mismatches(fixes: tuple[GpsFix, ...]) -> frozenset[int]:
    """Return the set of indices into `fixes` whose own reported
    speed_kmh disagrees implausibly with how far the vehicle's GPS
    *position* actually moved to its nearest positioned neighbor(s) in
    real elapsed time - see _SPEED_POSITION_CROSS_CHECK_MAX_ELAPSED
    _SECONDS' own docstring for the real-archive report this fixes and
    why this replaced an earlier flat speed ceiling.

    For each positioned fix with a speed_kmh reading, this looks at
    whichever of its immediate positioned neighbors (the one before,
    the one after, or both) are within
    _SPEED_POSITION_CROSS_CHECK_MAX_ELAPSED_SECONDS of it, and computes
    the haversine-distance-implied speed to each - a close-in-time,
    genuinely independent second estimate of how fast the vehicle was
    moving, derived from *where* the GPS says the vehicle was rather
    than from the receiver's own instantaneous speed-over-ground
    estimate. A reading is flagged only if it exceeds every available
    implied speed by more than _SPEED_POSITION_CROSS_CHECK_FACTOR (with
    _SPEED_POSITION_CROSS_CHECK_MARGIN_KMH of flat headroom) - i.e. the
    position evidence, from *both* sides where both are available,
    disagrees with the reported reading by a wide margin, not just a
    normal amount of straight-line-vs-real-path slack.

    A fix with no positioned neighbor close enough in time to check
    against (e.g. sitting right next to a real GPS dropout on both
    sides, or the only positioned fix in a very short recording) can't
    be cross-checked at all and is left alone - there's nothing to
    disagree with it.
    """

    positioned_indices = [
        index
        for index, fix in enumerate(fixes)
        if fix.valid and fix.latitude is not None and fix.longitude is not None
    ]

    poisoned: set[int] = set()

    for position, index in enumerate(positioned_indices):
        fix = fixes[index]
        if fix.speed_kmh is None:
            continue

        implied_speeds_kmh: list[float] = []

        if position > 0:
            neighbor = fixes[positioned_indices[position - 1]]
            elapsed = (fix.timestamp - neighbor.timestamp).total_seconds()
            if 0 < elapsed <= _SPEED_POSITION_CROSS_CHECK_MAX_ELAPSED_SECONDS:
                distance = _haversine_distance_meters(
                    neighbor.latitude, neighbor.longitude, fix.latitude, fix.longitude
                )
                implied_speeds_kmh.append(distance / elapsed * 3.6)

        if position < len(positioned_indices) - 1:
            neighbor = fixes[positioned_indices[position + 1]]
            elapsed = (neighbor.timestamp - fix.timestamp).total_seconds()
            if 0 < elapsed <= _SPEED_POSITION_CROSS_CHECK_MAX_ELAPSED_SECONDS:
                distance = _haversine_distance_meters(
                    fix.latitude, fix.longitude, neighbor.latitude, neighbor.longitude
                )
                implied_speeds_kmh.append(distance / elapsed * 3.6)

        if not implied_speeds_kmh:
            continue

        max_plausible_kmh = (
            max(implied_speeds_kmh) * _SPEED_POSITION_CROSS_CHECK_FACTOR
            + _SPEED_POSITION_CROSS_CHECK_MARGIN_KMH
        )
        if fix.speed_kmh > max_plausible_kmh:
            poisoned.add(index)

    return frozenset(poisoned)


def _hysteresis_altitude_stats(
    altitude_fixes: list[tuple[datetime, float]],
) -> tuple[float, float, float, int]:
    """min/max/net-elevation-change over a sequence of timestamped
    altitude readings, using outlier rejection then dead-band
    re-basing rather than trusting each raw reading directly. The
    fourth return value is the *filtered* reading count (post
    -_reject_altitude_outliers(), not len(altitude_fixes)) - callers
    need this to correctly decide whether "change" is even meaningful,
    since a 2-reading input where one reading gets filtered out as an
    outlier leaves only a single real reading behind, same as if the
    caller had only ever passed one reading in the first place.

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

    `altitude_fixes` must be non-empty - callers only ever call this
    once they already know there's at least one reading, mirroring
    every other "only compute if there's data at all" guard in this
    module.
    """

    filtered = _reject_altitude_outliers(altitude_fixes)

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

    return track_min, track_max, net_change, len(filtered)


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
    in turn run on _speeds_excluding_reacquisition_settle()'s output -
    itself already stripped of any reading
    _reject_speed_position_mismatches() flagged as disagreeing
    implausibly with the vehicle's own real GPS position movement -
    rather than the raw sequence directly. Three independent passes,
    since a real archive produced three different shapes of bad
    reading: a single tick that spikes and snaps straight back
    (_reject_speed_outliers()), a multi-tick decay right after a GPS
    dropout ends (_speeds_excluding_reacquisition_settle()), and
    several consecutive readings that agree with each other but
    disagree with how far the vehicle's position actually moved
    (_reject_speed_position_mismatches()) - see each function's own
    docstring, and _SPEED_OUTLIER_JUMP_KMH's/the comment above
    _GPS_REACQUISITION_SETTLE_READINGS'/
    _SPEED_POSITION_CROSS_CHECK_MAX_ELAPSED_SECONDS' own real-archive
    reports (322.3 km/h and 305.9 km/h, both in a car limited to 250)
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
    own docstring, and _reject_altitude_outliers()'s,
    _ALTITUDE_IMPLAUSIBLE_RATE_MPS' and
    _ALTITUDE_IMPLAUSIBLE_CEILING_METERS' own docstrings, for the
    two-pass rate-then-ceiling filtering this applies and why - a
    naive raw-reading sum badly overstates "change" from ordinary GPS
    altitude noise, and a self-corroborating cluster of badly-wrong
    altitude readings can overstate it far worse still, both confirmed
    against real archives). `elevation_change_meters` is the net
    change (final dead-banded altitude minus starting altitude, can be
    positive, negative, or zero) - see _hysteresis_altitude_stats()'s
    own docstring for the earlier definitions this field has had and
    why each was abandoned. The altitude sequence fed into it is every
    present `altitude_meters` reading among the trip's positioned
    fixes, paired with that fix's own timestamp (used by the rate
    check), in order, with any fix that lacks a reading simply dropped
    from the sequence rather than fragmenting it into disconnected
    segments around each gap - the same "bridge across gaps using the
    nearest real reading, don't silently drop the span" philosophy
    `moving_seconds`/`idle_seconds` above already use for missing speed
    readings, applied here to missing altitude readings instead.
    `elevation_change_meters` additionally needs at least *two* present
    readings to mean anything (a single reading has no delta to
    measure) - `min_altitude_meters`/`max_altitude_meters` only need
    one. All three are None if there's no altitude_meters reading
    anywhere in the trip at all (see GpsFix.altitude_meters's own
    docstring for when that's the case).

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

    poisoned_speed_indices = _reject_speed_position_mismatches(fixes)
    speeds = _speeds_excluding_reacquisition_settle(fixes, poisoned_speed_indices)
    filtered_speeds = _reject_speed_outliers(speeds)
    average_speed_kmh = (
        sum(filtered_speeds) / len(filtered_speeds) if filtered_speeds else None
    )
    max_speed_kmh = max(filtered_speeds) if filtered_speeds else None

    altitude_fixes = [
        (fix.timestamp, fix.altitude_meters)
        for fix in positioned
        if fix.altitude_meters is not None
    ]
    if altitude_fixes:
        min_altitude_meters, max_altitude_meters, elevation_change_meters, filtered_count = (
            _hysteresis_altitude_stats(altitude_fixes)
        )
        # A single reading has no delta to measure "change" from at
        # all - see this function's own docstring on why
        # elevation_change_meters needs two readings where min/max
        # only need one. Checked against the *filtered* count, not
        # len(altitude_fixes) - if outlier rejection drops one of only
        # two raw readings (e.g. the surviving one paired with a
        # ceiling-rejected glitch), only a single real reading is left
        # to compute a delta from, same as if the caller had only ever
        # passed one reading in.
        if filtered_count < 2:
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
