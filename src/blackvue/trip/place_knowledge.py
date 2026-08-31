"""
Driver-knowledge base: increment 2 of driver detection, building on
driver_detect.py's opaque driver1/driver2 route-pattern matcher
(increment 1).

Christer's follow-up ask, verbatim: duration spent at the travelled-to
site (excluding Hammarby Sjöstad/Heliosgatan, home base with
underground parking - a stay *at home* isn't a "stop" to report at
all), a stay-category flag, weekday and time (no DST math - see
local_weekday_and_time()'s own docstring for why), a "form of common
places" with a driver each Christer can fill in by hand per category, a
list of undecided common trips, and a per-trip override for
one-off/specific trips. Scoped to the live Kirby (2026) archive only -
Christer: "i dont [think] we will check up on trips for previous years
before 2026, the addresses will probably change over time" - so this
module (and driver_knowledge.json) never looks at older per-year
archives, and a place's label/driver are plain hand-edited text/labels,
not something meant to stay accurate forever.

The stay-category flag was originally a wall-clock 15-minute dwell
threshold ("short"/"long", inferred from the gap between adjacent
trips' GPS fixes). Christer later replaced that outright: "Long and
short are not in the game anymore, more like if you get a P file after
its long" - followed by an explicit correction when a follow-up
question tried to re-litigate it: "I have already told you to long and
short doesnt exists any more." The category is now whether the stop
ended in a downloaded Parking-mode (P) recording - direct camera
evidence the car was actually left there, unlike a wall-clock gap
between two trips, which conflates a real stay with an ordinary
download/data gap (the same signal bv_drivers.py's own P-ending trip
filter already relies on - see that module's docstring). See
stop_category()/_trip_has_downloaded_parking_footage() below.
dwell_minutes/dwell_at_destination() are kept as informational display
data only (the "Stay: ~N min" a trip shows) - they no longer drive the
category.

Two ideas drive the design:

1. "Common places" as the primary editable unit, not individual trips.
   Christer: "a form of common places, with a flag for short stay and
   long stay coupled to a specific driver" (see above for how "short/
   long" itself was later redefined) - so rather than a giant per-trip
   table to click through, a destination the vehicle visits more than
   once becomes one CommonPlace with (at most) two rules - "stops here
   with no parking file are always driver X", "stops here that end in
   a parking file are always driver Y" - and every trip to that place,
   past or future, inherits whichever rule matches its own
   stop_category. A trip whose destination never repeats (or whose
   place hasn't been assigned yet) falls through to
   driver_detect.match_driver()'s named-pattern candidates, and
   failing that, stays "undecided" for Christer to assign a one-off
   manual override on the trip itself (see TripKnowledge.source and
   `trip_overrides` throughout this module).

2. Places are identified by radius-based clustering, seeded from
   whatever's already in `existing_places` so a manual driver
   assignment survives a rebuild (a fresh `bv-drivers build` run)
   without persisting a separate model. This was originally a plain
   grid-cell rounding (place_key() alone) - simpler, and stable across
   reruns with zero extra bookkeeping - but Christer's own real
   registry showed the flaw: 0.001-degree cells are only ~111m x 57m
   at his latitude, so the *same* physical Sickla-area parking spot
   split into over a dozen single-visit "places" because each visit's
   GPS fix happened to land in a different cell, and each stayed
   below the min_visits=2 threshold to ever show up as "common" at
   all. Christer: "I am trying to get more common places, fewer trips
   to identify." See _assign_place_clusters()/_merge_nearby_places()
   below for the fix: a new stop within _CLUSTER_RADIUS_METERS of an
   already-known place snaps onto it instead of minting a new grid
   cell; place_key() itself is kept only to mint a fresh, readable key
   the first time a genuinely new place is seen.

Copyright (C) 2026 Christer R. (sssreh)

SPDX-License-Identifier: GPL-3.0-or-later
"""

from __future__ import annotations

import bisect
import json
import math
from collections.abc import Sequence
from dataclasses import dataclass, field, replace
from datetime import date
from datetime import datetime
from pathlib import Path

from ..adapters.base import CameraAdapter
from .driver_detect import DriverMatch
from .driver_detect import DriverProfiles
from .driver_detect import TripFix
from .driver_detect import match_driver
from .driver_detect import resolve_trip_fix
from .trip import Trip

# Same order of magnitude as driver_detect.DEFAULT_RADIUS_METERS
# (300m) - used here for the "did the adjacent trip start/end at the
# same place" dwell-time check, same role that constant plays in
# driver_detect.match_driver() itself, just without a named
# RoutePattern to carry its own radius_meters.
_SAME_PLACE_RADIUS_METERS = 300.0

# Grid-cell size used only to mint a readable key text (place_key())
# the first time a genuinely new place is seen - identity itself now
# comes from _assign_place_clusters()'s radius search, not from
# whether two points round to the same cell. ~111m of latitude and
# ~57m of longitude at Stockholm's latitude (59N) per 0.001 degrees.
_GRID_DECIMALS = 3

# How close a new stop has to be to an already-known place before
# _assign_place_clusters() snaps it onto that place instead of
# minting a new one. Chosen after checking Christer's own real
# driver_knowledge.json: the fragmented Sickla-area splinters were
# mostly 15-160m apart (a few pairs closer to 400-500m across a
# bigger lot, which even this radius won't fully re-merge in one
# pass - a known greedy-clustering tradeoff, acceptable for a
# hand-reviewed registry Christer can still merge by hand), while
# genuinely distinct places in his data are 2+km apart, so 150m
# leaves a wide safety margin against merging two real destinations
# that happen to be neighbors.
_CLUSTER_RADIUS_METERS = 150.0

_WEEKDAY_NAMES = (
    "Monday",
    "Tuesday",
    "Wednesday",
    "Thursday",
    "Friday",
    "Saturday",
    "Sunday",
)

# Confidence assigned when a trip's driver comes from a place rule
# Christer set by hand, vs. a direct per-trip override - both above
# every automatic driver_detect.match_driver() candidate (which tops
# out at 0.9, a verified-dwell named-pattern match), since a human
# decision beats an inferred one. MANUAL_TRIP_CONFIDENCE is the
# highest of the two - overriding one specific trip is a strictly
# narrower, more deliberate action than setting a place-wide rule.
PLACE_RULE_CONFIDENCE = 0.95
MANUAL_TRIP_CONFIDENCE = 1.0

_EARTH_RADIUS_METERS = 6_371_000.0


def _haversine_distance_meters(
    lat1: float, lon1: float, lat2: float, lon2: float
) -> float:
    """Same duplicated-on-purpose copy driver_detect.py itself carries
    (see that module's own copy for the "genuinely separate concern"
    reasoning search.py originally gave) - this module is deliberately
    usable without importing driver_detect's private helpers."""

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


def _near(
    point: tuple[float, float] | None,
    target: tuple[float, float] | None,
    radius_meters: float,
) -> bool:
    if point is None or target is None:
        return False
    return _haversine_distance_meters(*point, *target) <= radius_meters


def _dwell_minutes(a: datetime | None, b: datetime | None) -> float | None:
    if a is None or b is None:
        return None
    return abs((b - a).total_seconds()) / 60.0


def local_weekday_and_time(timestamp: datetime) -> tuple[str, str]:
    """(weekday name, "HH:MM"), read straight off `timestamp` with no
    timezone/DST conversion at all.

    Christer, explaining why: "a change of time requires a camera
    reboot" - the camera's own clock doesn't reliably track real-world
    DST transitions the way a phone or PC does; it only updates
    whenever it's next rebooted after a change. That means a naive
    "detect DST from the calendar and subtract an hour in summer"
    normalization would just as often *introduce* an hour of error as
    remove one - there's no way to know from the timestamp alone
    whether this particular recording happened before or after the
    camera's clock caught up with a given spring/autumn change.
    Reporting the camera's own recorded wall-clock time exactly as-is
    is therefore both the simplest option and the only one that
    doesn't risk silently corrupting a time that was already right."""

    return (
        _WEEKDAY_NAMES[timestamp.weekday()],
        f"{timestamp.hour:02d}:{timestamp.minute:02d}",
    )


# Backward-compat migration for _trip_from_dict() - see its own call
# site below.
_STOP_CATEGORY_MIGRATION = {"short": "no-parking", "long": "parked"}


def _trip_has_downloaded_parking_footage(trip: Trip) -> bool:
    """True if `trip` ends in a Parking-mode (P) recording backed by
    at least one downloaded asset - the same "real camera evidence of
    a stop" check bv_drivers.py's own P-ending trip filter already
    uses (see that module's docstring for the .id.is_parking-alone-
    isn't-enough reasoning: a RecordingId can get registered from a
    bv-generate/bv-scribe-derived asset alone, which is evidence a
    tool ran, not evidence the camera actually captured a stop there).

    This, not a wall-clock dwell-minutes threshold, is the "was this a
    long stay?" signal Christer asked for: "if you get a P file after
    its long" - see stop_category()."""

    last = trip.last_recording
    return last.id.is_parking and any(
        asset.is_downloaded for asset in last.assets
    )


def stop_category(has_parking_footage: bool | None) -> str | None:
    """"parked" if the stop ended in a downloaded Parking-mode (P)
    recording, "no-parking" if it didn't, or None if it's not known at
    all (see _raw_trip_knowledge()'s own None case - a trip with no
    away leg at all, or whose adjacent trip isn't available to check).

    Replaces the original 15-minute wall-clock-gap threshold this used
    to gate on (dwell_at_destination()'s own dwell_minutes) - Christer:
    "Long and short are not in the game anymore, more like if you get
    a P file after its long." A downloaded Parking-mode recording is
    direct camera evidence the car was actually left there; a wall-
    clock gap between two trips' GPS fixes can't tell a real stay
    apart from an ordinary download/data gap."""

    if has_parking_footage is None:
        return None
    return "parked" if has_parking_footage else "no-parking"


def smoothness_raw_from_samples(samples: Sequence) -> float | None:
    """Mean lateral+accel/brake g-sensor magnitude across `samples` -
    the raw per-trip value smoothness_score() later ranks against
    every other trip's own value. Uses each GSensorSample's own
    x (lateral) and y (accel/brake) fields only, excluding z
    (vertical) - a bump in the road shows up on the vertical axis but
    isn't a driving-style signal the way steering/braking are (see
    telemetry/gsensor_reader.py's own field-convention docstring).

    None if `samples` is empty - a trip with no g-sensor data at all
    (missing .3gf, unsupported adapter) simply has no smoothness
    score, same "unknown, not zero" contract dwell_at_destination()
    and friends already use elsewhere in this module."""

    if not samples:
        return None
    magnitudes = [math.sqrt(sample.x**2 + sample.y**2) for sample in samples]
    return sum(magnitudes) / len(magnitudes)


def smoothness_score(
    raw: float | None, population: Sequence[float]
) -> int | None:
    """`raw`'s percentile rank within `population`, bucketed 0 (smooth)
    to 9 (aggressive) - Christer's own follow-up ask ("anything else
    you can do to make it easier for me to decide driver"), scoped to
    a driving-smoothness score. Deliberately relative-to-your-own-trips
    rather than a fixed g-force threshold, since the g-sensor's raw
    physical unit is unconfirmed (see telemetry/gsensor_reader.py's own
    docstring) - only ever meaningful compared to `population`, the set
    of every other trip's own smoothness_raw.

    None if `raw` itself is None (no g-sensor data for this trip) or
    `population` is empty (nothing to rank against yet)."""

    if raw is None or not population:
        return None

    values = sorted(population)
    index = bisect.bisect_left(values, raw)
    return min(9, int(index / len(values) * 10))


def _time_of_day_distance_minutes(a: str, b: str) -> int:
    """Circular distance in minutes between two "HH:MM" times of day -
    e.g. 23:50 and 00:10 are 20 minutes apart, not 1420, since a trip
    just before midnight and one just after are close in the way that
    matters here (which of a household's regular routines this trip
    looks like), not literal clock distance."""

    a_hour, a_minute = (int(part) for part in a.split(":"))
    b_hour, b_minute = (int(part) for part in b.split(":"))
    a_total = a_hour * 60 + a_minute
    b_total = b_hour * 60 + b_minute
    diff = abs(a_total - b_total)
    return min(diff, 1440 - diff)


def suggest_closest_decided_trip(
    entry: TripKnowledge, trips: Sequence[TripKnowledge]
) -> TripKnowledge | None:
    """The already-*decided* trip in `trips` that most resembles
    `entry` - Christer's own follow-up ask ("anything else you can do
    to make it easier for me to decide driver"), the "closest past
    match" idea: when an undecided trip has no automatic candidate at
    all, the nearest look-alike among trips Christer (or a rule)
    already resolved is still a useful hint ("this looks like that
    other Tuesday morning trip to the same place, which you assigned
    to Christer").

    Ranked by (same place, same weekday, closeness in time of day), in
    that priority order - a decided trip to the *same place* always
    outranks one merely on the same weekday, regardless of how close
    the times of day are; among same-place candidates, one on the same
    weekday outranks one that merely shares a similar time. Only
    considers trips with a resolved driver (source != "undecided") and
    excludes `entry` itself; returns None if there's no other decided
    trip to compare against at all."""

    candidates = [
        other
        for other in trips
        if other.trip_label != entry.trip_label and other.source != "undecided"
    ]
    if not candidates:
        return None

    def score(other: TripKnowledge) -> tuple[bool, bool, int]:
        same_place = (
            entry.away_place_key is not None
            and other.away_place_key == entry.away_place_key
        )
        same_weekday = other.weekday == entry.weekday
        time_distance = _time_of_day_distance_minutes(
            entry.start_time_of_day, other.start_time_of_day
        )
        return (same_place, same_weekday, -time_distance)

    return max(candidates, key=score)


def place_key(point: tuple[float, float]) -> str:
    """Deterministic grid-cell id text for a brand-new place. No
    longer *identity itself* (see this module's own docstring, point
    2) - _assign_place_clusters() decides whether a point belongs to
    an existing place before ever calling this; it's only reached to
    mint a fresh, readable key the first time a place is truly new."""

    lat, lon = point
    return f"{round(lat, _GRID_DECIMALS)},{round(lon, _GRID_DECIMALS)}"


def dwell_at_destination(
    trip_fix: TripFix,
    prev_fix: TripFix | None,
    next_fix: TripFix | None,
    home: tuple[float, float] | None,
    home_radius_meters: float,
) -> float | None:
    """How long the vehicle stayed at this trip's *non-home* end -
    generalized from driver_detect.match_driver()'s own pattern-scoped
    dwell computation (same home-leg/away-leg pairing logic, see that
    function's docstring) but computed for every trip, not just ones
    that happen to match a named RoutePattern, and always excluding a
    stay at home itself (Christer: "excluding hammarby sjöstad around
    heliosgatan, where we live and have base parking underground" - a
    trip that starts and ends near home has no "away" leg to measure
    at all, so this returns None for it rather than a bogus 0).

    Informational only - TripKnowledge.dwell_minutes (the "Stay: ~N
    min" a trip displays) still comes from this function, but
    stop_category() no longer gates on it; see that function's own
    docstring for why (_trip_has_downloaded_parking_footage() instead).

    Returns None whenever it can't be computed: both ends near home,
    neither end near home (an inter-place leg, not modeled here),
    missing adjacent trip, or the adjacent trip's own endpoint isn't
    near the same place (the vehicle went somewhere else in between -
    a false "verified" dwell is worse than an honest "unknown" one,
    same reasoning driver_detect.match_driver() itself already uses
    for its own dwell verification)."""

    start_near_home = _near(trip_fix.start, home, home_radius_meters)
    end_near_home = _near(trip_fix.end, home, home_radius_meters)

    if start_near_home == end_near_home:
        return None

    if end_near_home:
        # away -> home: the dwell happened *before* this trip, the gap
        # since the previous trip's own end - only if that trip
        # dropped the vehicle off at the same place this one now
        # returns from.
        if prev_fix is None:
            return None
        if not _near(prev_fix.end, trip_fix.start, _SAME_PLACE_RADIUS_METERS):
            return None
        return _dwell_minutes(prev_fix.end_time, trip_fix.start_time)

    # home -> away: the dwell happens *after* this trip, the gap until
    # the next trip's own start - only if it departs from the same
    # place this one just arrived at.
    if next_fix is None:
        return None
    if not _near(next_fix.start, trip_fix.end, _SAME_PLACE_RADIUS_METERS):
        return None
    return _dwell_minutes(trip_fix.end_time, next_fix.start_time)


# --------------------------------------------------------------------
# Data shapes
# --------------------------------------------------------------------


@dataclass(frozen=True)
class TripKnowledge:
    """Everything computed/resolved for one trip. The driver_label/
    display_name/confidence/source fields start out "undecided" (see
    _raw_trip_knowledge()) and are filled in by _resolve_trip_driver()
    once this trip's CommonPlace (if any) and any per-trip override
    are known - kept on the same frozen dataclass (via
    dataclasses.replace()) rather than a second wrapper type, so
    driver_knowledge.json only ever has one shape per trip."""

    trip_label: str
    start_time: datetime
    end_time: datetime
    weekday: str
    start_time_of_day: str
    away_place_key: str | None
    away_point: tuple[float, float] | None
    dwell_minutes: float | None
    stop_category: str | None
    candidates: tuple[DriverMatch, ...]
    driver_label: str | None = None
    display_name: str | None = None
    confidence: float = 0.0
    source: str = "undecided"
    # The trip's own start/end GPS points (as opposed to away_point,
    # which is whichever single end is away from home) plus the
    # camera id and first/last recording id needed to link straight to
    # a trip's first and last video - Christer's own follow-up ask
    # ("a link to first and last video with the adress of start and
    # stop"). All five default to None so a pre-existing
    # driver_knowledge.json (written before this field set existed)
    # still loads - see _trip_from_dict()'s own .get() calls below.
    # bv-web's /drivers page reverse-geocodes start_point/end_point
    # live (same load_or_reverse_geocode() the archive browser's own
    # /location route already uses) rather than this module doing that
    # I/O itself - see this module's docstring for why it stays pure.
    start_point: tuple[float, float] | None = None
    end_point: tuple[float, float] | None = None
    first_recording_id: str | None = None
    last_recording_id: str | None = None
    camera_id: str | None = None
    # Mean lateral+accel/brake g-sensor magnitude pooled across every
    # recording in this trip (see bv_drivers.py's own computation) -
    # unitless raw scale (the g-sensor's own physical unit is
    # unconfirmed, see telemetry/gsensor_reader.py's docstring), only
    # ever compared to *other trips'* own smoothness_raw via
    # smoothness_score() below, never against a fixed threshold. None
    # if this trip has no g-sensor data at all (missing asset,
    # unsupported adapter, or a pre-existing driver_knowledge.json
    # written before this field existed).
    smoothness_raw: float | None = None


@dataclass(frozen=True)
class CommonPlace:
    """One destination grid cell (see place_key()) Christer's vehicle
    has visited away from home, with his own `driver` rule once he's
    set it - starts as None ("undecided"), the exact state
    undecided_places() below surfaces for the web form.

    One rule per place, not one per stop_category (parked/no-parking).
    That split existed briefly but Christer found the resulting "No
    parking file" column confusing, and rightly so: bv_drivers.py's
    own P-ending trip filter (see that module's docstring) already
    drops any trip that doesn't end in a downloaded Parking-mode
    recording, except the single most-recent trip in the whole
    archive - so a place's no-parking-category visits are essentially
    always zero (confirmed against Christer's own real
    driver_knowledge.json: 0 across all 51 places). Christer,
    confirming the collapse: "If no parking sidecars, then there is no
    trip." TripKnowledge.stop_category is still computed and shown per
    trip (informational - "Parked"/"No parking file" next to a trip's
    own Stay column) since it's real, just rare; it no longer drives a
    separate place-level rule."""

    key: str
    point: tuple[float, float]
    label: str
    visit_count: int
    driver: str | None = None


# --------------------------------------------------------------------
# Building (pure - given already-resolved trips/fixes/profiles/points)
# --------------------------------------------------------------------


def _raw_trip_knowledge(
    trips: Sequence[Trip],
    fixes: Sequence[TripFix],
    profiles: DriverProfiles,
    known_points: dict[str, tuple[float, float]],
    *,
    camera_id: str | None = None,
    smoothness_values: Sequence[float | None] | None = None,
) -> list[TripKnowledge]:
    home = known_points.get("home")
    home_radius = profiles.home_radius_meters

    entries: list[TripKnowledge] = []
    for index, trip in enumerate(trips):
        trip_fix = fixes[index]
        smoothness_raw = (
            smoothness_values[index] if smoothness_values is not None else None
        )
        prev_fix = fixes[index - 1] if index > 0 else None
        next_fix = fixes[index + 1] if index + 1 < len(fixes) else None

        weekday, time_of_day = local_weekday_and_time(trip.start_timestamp)

        start_near_home = _near(trip_fix.start, home, home_radius)
        end_near_home = _near(trip_fix.end, home, home_radius)
        away_point: tuple[float, float] | None = None
        if start_near_home and not end_near_home:
            away_point = trip_fix.end
        elif end_near_home and not start_near_home:
            away_point = trip_fix.start

        dwell = dwell_at_destination(trip_fix, prev_fix, next_fix, home, home_radius)

        # "Was this stop parked?" - checked on whichever trip's own
        # tail actually sat at the away place, same direction logic
        # dwell_at_destination() uses just above (see that function's
        # own docstring): a home->away trip's stop is its *own* tail;
        # an away->home trip's stop was the *previous* trip's tail, and
        # only counts if that previous trip actually ended at the same
        # place this one now returns from (the _near() check mirrors
        # dwell_at_destination()'s own same-place guard, so a vehicle
        # that went somewhere else in between doesn't borrow the wrong
        # trip's P status).
        has_parking_footage: bool | None = None
        if start_near_home and not end_near_home:
            has_parking_footage = _trip_has_downloaded_parking_footage(trip)
        elif end_near_home and not start_near_home:
            if index > 0 and _near(
                prev_fix.end if prev_fix is not None else None,
                trip_fix.start,
                _SAME_PLACE_RADIUS_METERS,
            ):
                has_parking_footage = _trip_has_downloaded_parking_footage(
                    trips[index - 1]
                )

        category = stop_category(has_parking_footage)
        candidates = match_driver(trip_fix, prev_fix, next_fix, profiles, known_points)

        entries.append(
            TripKnowledge(
                trip_label=trip.label,
                start_time=trip.start_timestamp,
                end_time=trip.end_timestamp,
                weekday=weekday,
                start_time_of_day=time_of_day,
                # Filled in afterward by _assign_place_clusters() -
                # deciding which place a point belongs to needs to see
                # every trip's away_point (and the prior registry)
                # together, not one trip in isolation. See this
                # module's own docstring, point 2.
                away_place_key=None,
                away_point=away_point,
                dwell_minutes=dwell,
                stop_category=category,
                candidates=candidates,
                start_point=trip_fix.start,
                end_point=trip_fix.end,
                first_recording_id=trip.first_recording.id.value,
                last_recording_id=trip.last_recording.id.value,
                camera_id=camera_id,
                smoothness_raw=smoothness_raw,
            )
        )

    return entries


def _merge_nearby_places(
    existing: dict[str, CommonPlace],
) -> dict[str, CommonPlace]:
    """Consolidates any leftover fragmented places in a previously-
    saved registry - multiple keys whose points fall within
    _CLUSTER_RADIUS_METERS of each other, the exact grid-rounding
    artifact this radius-based redesign replaces (Christer's own real
    registry had two separately-keyed "Hemmet för gamla" entries only
    46m apart) - into one canonical entry before a fresh
    `bv-drivers build` reclusters every trip against it via
    _assign_place_clusters() below.

    Processes places largest-visit_count-first so the canonical
    anchor for a merged group is whichever place has already
    accumulated the most real visits (ties broken by key text for
    determinism); a smaller place's visit_count folds into the
    anchor's own (recomputed properly from fresh trip data right
    after anyway - see build_common_places()'s own docstring - so
    this number is only used to decide merge order, not kept). The
    anchor's label and driver both win untouched, *unless* the anchor
    has no driver set and the place being merged away does - in which
    case that already-made decision of Christer's carries over rather
    than silently vanishing."""

    ordered = sorted(existing.values(), key=lambda p: (-p.visit_count, p.key))
    canonical: list[CommonPlace] = []
    for place in ordered:
        match_index = next(
            (
                i
                for i, anchor in enumerate(canonical)
                if _haversine_distance_meters(*place.point, *anchor.point)
                <= _CLUSTER_RADIUS_METERS
            ),
            None,
        )
        if match_index is None:
            canonical.append(place)
            continue
        anchor = canonical[match_index]
        canonical[match_index] = replace(
            anchor,
            visit_count=anchor.visit_count + place.visit_count,
            driver=anchor.driver if anchor.driver is not None else place.driver,
        )
    return {place.key: place for place in canonical}


def _assign_place_clusters(
    entries: list[TripKnowledge],
    existing: dict[str, CommonPlace] | None,
) -> list[TripKnowledge]:
    """Assigns each entry's away_place_key by radius-based clustering
    instead of place_key()'s plain grid rounding - see this module's
    own docstring, point 2, and _CLUSTER_RADIUS_METERS's own comment
    for why (Christer's real Sickla-area splinters).

    Seeds one cluster per already-known place (`existing`, already
    deduplicated by _merge_nearby_places() above) so a place's key -
    and any driver rule Christer set on it - survives a rebuild; then
    walks `entries` in the given (chronological) order, snapping each
    trip's away_point onto whichever existing-or-just-created cluster
    is *nearest* and within _CLUSTER_RADIUS_METERS, or anchoring a
    brand new cluster (keyed by place_key() of this trip's own point)
    if none qualifies. Deterministic given a fixed trip order and a
    fixed `existing` snapshot - the same real archive always
    reclusters the same way."""

    clusters: list[tuple[str, tuple[float, float]]] = [
        (place.key, place.point) for place in (existing or {}).values()
    ]

    updated: list[TripKnowledge] = []
    for entry in entries:
        if entry.away_point is None:
            updated.append(entry)
            continue

        nearest_key: str | None = None
        nearest_distance: float | None = None
        for key, anchor in clusters:
            distance = _haversine_distance_meters(*entry.away_point, *anchor)
            if distance <= _CLUSTER_RADIUS_METERS and (
                nearest_distance is None or distance < nearest_distance
            ):
                nearest_key, nearest_distance = key, distance

        if nearest_key is None:
            nearest_key = place_key(entry.away_point)
            clusters.append((nearest_key, entry.away_point))

        updated.append(replace(entry, away_place_key=nearest_key))

    return updated


def build_common_places(
    knowledge: Sequence[TripKnowledge],
    *,
    existing: dict[str, CommonPlace] | None = None,
) -> dict[str, CommonPlace]:
    """Aggregate `knowledge` into one CommonPlace per distinct
    away_place_key - by this point already assigned by radius-based
    clustering (_assign_place_clusters()), not a raw grid cell; this
    function itself just counts. `existing` (a previously-saved
    registry - see load_knowledge_base()) supplies each place's own
    label and any manual `driver` rule Christer already set; those
    survive a rebuild untouched - only visit_count (and, for a place
    `existing` has never seen, point/label) is recomputed from
    `knowledge`."""

    existing = existing or {}
    points: dict[str, tuple[float, float]] = {}
    visit_counts: dict[str, int] = {}

    for entry in knowledge:
        key = entry.away_place_key
        if key is None:
            continue
        points.setdefault(key, entry.away_point)  # type: ignore[arg-type]
        visit_counts[key] = visit_counts.get(key, 0) + 1

    places: dict[str, CommonPlace] = {}
    for key, visit_count in visit_counts.items():
        prior = existing.get(key)
        point = points[key]
        places[key] = CommonPlace(
            key=key,
            point=point,
            label=(
                prior.label
                if prior is not None
                else f"Place near {point[0]:.3f}, {point[1]:.3f}"
            ),
            visit_count=visit_count,
            driver=(prior.driver if prior is not None else None),
        )

    return places


def _resolve_trip_driver(
    entry: TripKnowledge,
    place: CommonPlace | None,
    profiles: DriverProfiles,
    override_label: str | None,
) -> TripKnowledge:
    """Resolution order: (1) a manual override on this specific trip
    always wins; (2) else this trip's CommonPlace's own `driver` rule,
    if Christer has set one; (3) else the best
    driver_detect.match_driver() named-pattern candidate, if any;
    (4) else stays "undecided" (entry's own defaults, unchanged)."""

    display_names = {driver.label: driver.display_name for driver in profiles.drivers}

    if override_label is not None:
        return replace(
            entry,
            driver_label=override_label,
            display_name=display_names.get(override_label, override_label),
            confidence=MANUAL_TRIP_CONFIDENCE,
            source="manual-trip",
        )

    if place is not None and place.driver is not None:
        return replace(
            entry,
            driver_label=place.driver,
            display_name=display_names.get(place.driver, place.driver),
            confidence=PLACE_RULE_CONFIDENCE,
            source="place-rule",
        )

    if entry.candidates:
        best = max(entry.candidates, key=lambda candidate: candidate.confidence)
        return replace(
            entry,
            driver_label=best.driver_label,
            # Not best.display_name: entry.candidates is match_driver()'s
            # own snapshot, taken (and persisted to driver_knowledge.json)
            # at whatever build/rescan filled it in - it never gets
            # refreshed by a rename. reresolve_trip_drivers() re-runs this
            # function against the *current* `profiles` precisely so an
            # edit (a rename, a new place rule, ...) takes effect without a
            # full rescan - using the stale candidate's own display_name
            # here defeated that for the pattern-match branch specifically
            # (the override/place-rule branches above already look this up
            # fresh via `display_names`). Christer: renamed "Fru" to "Dao"
            # via /drivers' inline rename form, then couldn't find her
            # trips filtering the Specific trips list by "Dao" - the
            # driver_label itself was still correct (rename never touches
            # labels), but every pattern-match-resolved trip kept showing
            # "Fru" everywhere its own persisted display_name was read
            # directly instead of through app.py's driver_display_by_label
            # lookup, which is what made it look like the trips had
            # vanished rather than just being mislabeled.
            display_name=display_names.get(best.driver_label, best.driver_label),
            confidence=best.confidence,
            source="pattern-match",
        )

    return entry


def build_knowledge_base(
    trips: Sequence[Trip],
    fixes: Sequence[TripFix],
    profiles: DriverProfiles,
    known_points: dict[str, tuple[float, float]],
    *,
    existing_places: dict[str, CommonPlace] | None = None,
    trip_overrides: dict[str, str] | None = None,
    camera_id: str | None = None,
    smoothness_values: Sequence[float | None] | None = None,
) -> tuple[list[TripKnowledge], dict[str, CommonPlace]]:
    """The whole pure pipeline: raw per-trip fields -> place identity
    assigned by radius-based clustering (_assign_place_clusters(),
    seeded from `existing_places` after _merge_nearby_places() has
    consolidated any leftover fragmentation in it) -> common places
    (carrying forward those same places' own labels/rules) -> each
    trip resolved to a driver via the order _resolve_trip_driver()
    documents. `fixes` must be the same length as `trips`, in the same
    chronological order (index-aligned - see resolve_all_trip_fixes()).
    `camera_id` (the resolved CameraConfig.id bv_drivers.py's own
    resolve_archive_path() call already produces, or None for a
    literal/unconfigured archive path) is stamped onto every resulting
    TripKnowledge so bv-web's /drivers page can link a trip's first/
    last recording straight to /archive/{camera_id}/{recording_id}."""

    trip_overrides = trip_overrides or {}
    merged_existing = (
        _merge_nearby_places(existing_places) if existing_places else None
    )
    raw = _raw_trip_knowledge(
        trips, fixes, profiles, known_points,
        camera_id=camera_id, smoothness_values=smoothness_values,
    )
    raw = _assign_place_clusters(raw, merged_existing)
    places = build_common_places(raw, existing=merged_existing)
    resolved = [
        _resolve_trip_driver(
            entry, places.get(entry.away_place_key), profiles,
            trip_overrides.get(entry.trip_label),
        )
        for entry in raw
    ]
    return resolved, places


def reresolve_trip_drivers(
    trips: Sequence[TripKnowledge],
    places: dict[str, CommonPlace],
    profiles: DriverProfiles,
    trip_overrides: dict[str, str] | None = None,
) -> list[TripKnowledge]:
    """Re-apply _resolve_trip_driver()'s resolution order to already-
    computed TripKnowledge entries, without re-scanning the archive or
    re-resolving any GPS fix - every raw field (weekday, dwell,
    candidates, away_place_key, ...) is unchanged, only which driver
    (if any) each trip resolves to might be. This is what bv-web's
    /drivers page uses after Christer edits a place's short/long-stay
    rule or sets/clears a specific trip's manual override: a full
    `bv-drivers build` re-scan is unnecessary (and, per bv_drivers.py's
    own docstring, can be slow) just to react to an edit that only
    touches `places`/`trip_overrides`, both already fully loaded from
    driver_knowledge.json.

    Each entry is first reset to "undecided" before resolving again -
    unlike build_knowledge_base()'s own per-entry calls (which start
    from _raw_trip_knowledge()'s always-undecided output),
    `trips` here may already carry a *previous* resolution (an old
    place-rule match, a stale override, ...), and _resolve_trip_driver()'s
    own fall-through case ("else stays undecided, entry's own defaults,
    unchanged") assumes it was handed a still-undecided entry - so
    without this reset, removing a place's driver rule or clearing a
    trip's override would leave the old resolution stuck rather than
    reverting to undecided."""

    trip_overrides = trip_overrides or {}
    reset = [
        replace(
            entry, driver_label=None, display_name=None,
            confidence=0.0, source="undecided",
        )
        for entry in trips
    ]
    return [
        _resolve_trip_driver(
            entry, places.get(entry.away_place_key), profiles,
            trip_overrides.get(entry.trip_label),
        )
        for entry in reset
    ]


def bulk_assign_undecided_trips(
    trips: Sequence[TripKnowledge],
    trip_overrides: dict[str, str],
    *,
    from_date: date,
    until_date: date,
    driver_label: str,
) -> dict[str, str]:
    """Return a new `trip_overrides` dict with `driver_label` set for
    every currently-undecided trip (`source == "undecided"`) whose
    `start_time` falls within `[from_date, until_date]` inclusive -
    Christer's "I want to minimize add driver ... only I was driving
    since wife was out of town for 4 days" ask: a way to clear a whole
    stretch of Specific-trips rows in one submit instead of picking a
    driver one row at a time.

    Deliberately scoped to *undecided* trips only, not every trip in
    the range: a trip already resolved via a place-rule or the
    increment-1 pattern matcher reflects a more specific signal than
    "this whole date range was one driver" (Christer chose this scope
    explicitly over overriding everything in range, so a household's
    own known routine - e.g. a place rule that's usually right - isn't
    silently clobbered just because its date happens to fall inside a
    bulk-assign window). Trips outside the range, and any existing
    override on a trip whose source *isn't* "undecided" (an edge case
    that shouldn't normally arise, since undecided is reset before
    resolving - see reresolve_trip_drivers()), are left untouched.

    Pure, like every other function in this module - no I/O, no read
    of the real trip list beyond what's passed in - so the web route
    calling this still owns loading/saving driver_knowledge.json
    itself, same division of labor as drivers_update_place()/
    drivers_update_trip()."""

    updated = dict(trip_overrides)
    for entry in trips:
        if entry.source != "undecided":
            continue
        if from_date <= entry.start_time.date() <= until_date:
            updated[entry.trip_label] = driver_label
    return updated


def undecided_places(
    places: dict[str, CommonPlace], *, min_visits: int = 2
) -> list[CommonPlace]:
    """Places visited at least `min_visits` times ("common") with no
    driver rule set yet - what the web form's "common places to fill
    in" section lists."""

    return [
        place
        for place in places.values()
        if place.visit_count >= min_visits and place.driver is None
    ]


def undecided_trips(trips: Sequence[TripKnowledge]) -> list[TripKnowledge]:
    """Trips with no resolved driver at all - covers both a one-off
    destination (never clustered into a place worth a rule) and a
    common place Christer hasn't filled in yet. What the web form's
    "specific trips" per-trip override list shows."""

    return [entry for entry in trips if entry.source == "undecided"]


def group_trips_by_place(
    trips: Sequence[TripKnowledge],
) -> dict[str, list[TripKnowledge]]:
    """Every trip that resolved to a given away-place, keyed by
    CommonPlace.key and sorted most-recent-first - Christer's own
    follow-up ask ("common places should show each trip with all what
    that means"): a CommonPlace row's visit_count only says *how many*
    trips went there, not *which* ones, so the web form groups the
    same TripKnowledge entries
    build_common_places() already counts back out into actual per-trip
    lists to show under each place."""

    grouped: dict[str, list[TripKnowledge]] = {}
    for entry in trips:
        if entry.away_place_key is None:
            continue
        grouped.setdefault(entry.away_place_key, []).append(entry)
    for entries in grouped.values():
        entries.sort(key=lambda entry: entry.start_time, reverse=True)
    return grouped


# --------------------------------------------------------------------
# I/O wrappers
# --------------------------------------------------------------------


def resolve_all_trip_fixes(
    adapter: CameraAdapter, trips: Sequence[Trip]
) -> list[TripFix]:
    """resolve_trip_fix() for every trip, in order - see
    build_knowledge_base()'s own docstring for why `fixes` must stay
    index-aligned with `trips`."""

    return [resolve_trip_fix(adapter, trip) for trip in trips]


def default_driver_knowledge_path(config_dir: Path) -> Path:
    """Where driver_knowledge.json lives - alongside driver_profiles.json
    under the same config_dir (see driver_detect.default_driver_profiles_path()),
    written by `bv-drivers build`, read by bv-web's /drivers page."""

    return config_dir / "driver_knowledge.json"


def _place_to_dict(place: CommonPlace) -> dict:
    return {
        "point": list(place.point),
        "label": place.label,
        "visit_count": place.visit_count,
        "driver": place.driver,
    }


def _place_from_dict(key: str, data: dict) -> CommonPlace:
    # Backward-compat migration, two generations deep. A
    # driver_knowledge.json written before the parked/no-parking split
    # was collapsed back into one `driver` field (Christer found the
    # "No parking file" column confusing - see CommonPlace's own
    # docstring) still has parked_driver/no_parking_driver keys;
    # prefer parked_driver (checked against Christer's real registry:
    # 6 places had parked_driver set, 0 had no_parking_driver, so this
    # loses nothing for him) and fall back to no_parking_driver. Older
    # still, a driver_knowledge.json written before *that* redesign
    # (see stop_category()'s own docstring) has long_stay_driver/
    # short_stay_driver instead - "long stay" loosely corresponds to
    # "parked". New `driver` key always wins if present.
    point = data.get("point") or [0.0, 0.0]
    return CommonPlace(
        key=key,
        point=(float(point[0]), float(point[1])),
        label=data.get("label", key),
        visit_count=int(data.get("visit_count", 0)),
        driver=data.get(
            "driver",
            data.get(
                "parked_driver",
                data.get("no_parking_driver", data.get("long_stay_driver")),
            ),
        ),
    )


def _candidate_to_dict(candidate: DriverMatch) -> dict:
    return {
        "driver_label": candidate.driver_label,
        "display_name": candidate.display_name,
        "place": candidate.place,
        "confidence": candidate.confidence,
        "reason": candidate.reason,
    }


def _candidate_from_dict(data: dict) -> DriverMatch:
    return DriverMatch(
        driver_label=data["driver_label"],
        display_name=data["display_name"],
        place=data["place"],
        confidence=float(data["confidence"]),
        reason=data.get("reason", ""),
    )


def _trip_to_dict(entry: TripKnowledge) -> dict:
    return {
        "trip_label": entry.trip_label,
        "start_time": entry.start_time.isoformat(),
        "end_time": entry.end_time.isoformat(),
        "weekday": entry.weekday,
        "start_time_of_day": entry.start_time_of_day,
        "away_place_key": entry.away_place_key,
        "away_point": list(entry.away_point) if entry.away_point is not None else None,
        "dwell_minutes": entry.dwell_minutes,
        "stop_category": entry.stop_category,
        "candidates": [_candidate_to_dict(c) for c in entry.candidates],
        "driver_label": entry.driver_label,
        "display_name": entry.display_name,
        "confidence": entry.confidence,
        "source": entry.source,
        "start_point": list(entry.start_point) if entry.start_point is not None else None,
        "end_point": list(entry.end_point) if entry.end_point is not None else None,
        "first_recording_id": entry.first_recording_id,
        "last_recording_id": entry.last_recording_id,
        "camera_id": entry.camera_id,
        "smoothness_raw": entry.smoothness_raw,
    }


def _trip_from_dict(data: dict) -> TripKnowledge:
    return TripKnowledge(
        trip_label=data["trip_label"],
        start_time=datetime.fromisoformat(data["start_time"]),
        end_time=datetime.fromisoformat(data["end_time"]),
        weekday=data["weekday"],
        start_time_of_day=data["start_time_of_day"],
        away_place_key=data.get("away_place_key"),
        away_point=(
            (float(data["away_point"][0]), float(data["away_point"][1]))
            if data.get("away_point") is not None
            else None
        ),
        dwell_minutes=data.get("dwell_minutes"),
        # Backward-compat migration: a driver_knowledge.json written
        # before the P-file-based redesign (see stop_category()'s own
        # docstring) may still have the old "short"/"long" category
        # values - "long" loosely corresponds to what's now "parked".
        # This only affects already-persisted entries; a fresh
        # `bv-drivers build` always recomputes stop_category from
        # scratch via the new signal.
        stop_category=_STOP_CATEGORY_MIGRATION.get(
            data.get("stop_category"), data.get("stop_category")
        ),
        candidates=tuple(
            _candidate_from_dict(c) for c in data.get("candidates", [])
        ),
        driver_label=data.get("driver_label"),
        display_name=data.get("display_name"),
        confidence=float(data.get("confidence", 0.0)),
        source=data.get("source", "undecided"),
        start_point=(
            (float(data["start_point"][0]), float(data["start_point"][1]))
            if data.get("start_point") is not None
            else None
        ),
        end_point=(
            (float(data["end_point"][0]), float(data["end_point"][1]))
            if data.get("end_point") is not None
            else None
        ),
        first_recording_id=data.get("first_recording_id"),
        last_recording_id=data.get("last_recording_id"),
        camera_id=data.get("camera_id"),
        smoothness_raw=data.get("smoothness_raw"),
    )


def save_knowledge_base(
    path: Path,
    *,
    trips: list[TripKnowledge],
    places: dict[str, CommonPlace],
    trip_overrides: dict[str, str],
) -> None:
    data = {
        "generated_at": datetime.now().isoformat(),
        "places": {key: _place_to_dict(place) for key, place in places.items()},
        "trip_overrides": dict(trip_overrides),
        "trips": [_trip_to_dict(entry) for entry in trips],
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )


def load_knowledge_base(
    path: Path,
) -> tuple[list[TripKnowledge], dict[str, CommonPlace], dict[str, str]] | None:
    """None if `path` doesn't exist yet or can't be read/parsed - same
    "absent is normal, not an error" contract as driver_detect.
    load_driver_profiles()."""

    if not path.exists():
        return None

    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None

    places = {
        key: _place_from_dict(key, place_data)
        for key, place_data in data.get("places", {}).items()
    }
    trip_overrides = dict(data.get("trip_overrides", {}))
    trips = [_trip_from_dict(entry) for entry in data.get("trips", [])]

    return trips, places, trip_overrides
