"""
Driver-knowledge base: increment 2 of driver detection, building on
driver_detect.py's opaque driver1/driver2 route-pattern matcher
(increment 1).

Christer's follow-up ask, verbatim: duration spent at the travelled-to
site (excluding Hammarby Sjöstad/Heliosgatan, home base with
underground parking - a stay *at home* isn't a "stop" to report at
all), a short(<15min)/long(>=15min) stay flag, weekday and time (no
DST math - see local_weekday_and_time()'s own docstring for why), a
"form of common places" with a short-stay/long-stay driver each
Christer can fill in by hand, a list of undecided common trips, and a
per-trip override for one-off/specific trips. Scoped to the live
Kirby (2026) archive only - Christer: "i dont [think] we will check up
on trips for previous years before 2026, the addresses will probably
change over time" - so this module (and driver_knowledge.json) never
looks at older per-year archives, and a place's label/short_stay_driver/
long_stay_driver are plain hand-edited text/labels, not something
meant to stay accurate forever.

Two ideas drive the design:

1. "Common places" as the primary editable unit, not individual trips.
   Christer: "a form of common places, with a flag for short stay and
   long stay coupled to a specific driver" - so rather than a giant
   per-trip table to click through, a destination the vehicle visits
   more than once becomes one CommonPlace with (at most) two rules -
   "short stays here are always driver X", "long stays here are
   always driver Y" - and every trip to that place, past or future,
   inherits whichever rule matches its own stop_category. A trip whose
   destination never repeats (or whose place hasn't been assigned yet)
   falls through to driver_detect.match_driver()'s named-pattern
   candidates, and failing that, stays "undecided" for Christer to
   assign a one-off manual override on the trip itself (see
   TripKnowledge.source and `trip_overrides` throughout this module).

2. Places are identified by a deterministic grid cell, not clustered
   by a stateful algorithm. See place_key()'s own docstring - this is
   what lets a manual short_stay_driver/long_stay_driver assignment
   survive a rebuild (a fresh `bv-drivers build` run) without needing
   to persist/match previous cluster centroids at all: the same
   destination always hashes to the same key, so
   build_common_places(existing=...) just carries the prior CommonPlace's
   label/driver fields forward under that same key.

Copyright (C) 2026 Christer R. (sssreh)

SPDX-License-Identifier: GPL-3.0-or-later
"""

from __future__ import annotations

import json
import math
from collections.abc import Sequence
from dataclasses import dataclass, field, replace
from datetime import datetime
from pathlib import Path

from ..adapters.base import CameraAdapter
from .driver_detect import DriverMatch
from .driver_detect import DriverProfiles
from .driver_detect import TripFix
from .driver_detect import match_driver
from .driver_detect import resolve_trip_fix
from .trip import Trip

# A stay shorter than this is "short", at/above it is "long" -
# Christer's own threshold ("we might also decide if its a short
# stop(less than 15 minutes) and longer stops(above 15 minutes)").
STOP_THRESHOLD_MINUTES = 15.0

# Same order of magnitude as driver_detect.DEFAULT_RADIUS_METERS
# (300m) - used here for the "did the adjacent trip start/end at the
# same place" dwell-time check, same role that constant plays in
# driver_detect.match_driver() itself, just without a named
# RoutePattern to carry its own radius_meters.
_SAME_PLACE_RADIUS_METERS = 300.0

# Grid-cell size for common-place identity (place_key()) - plain
# decimal rounding, not a real clustering algorithm. ~111m of
# latitude and ~57m of longitude at Stockholm's latitude (59N) per
# 0.001 degrees - close enough to _SAME_PLACE_RADIUS_METERS/
# driver_detect's own 300m default that two real visits to the same
# parking spot should almost always land in the same cell. Tradeoff:
# a destination whose GPS fix happens to straddle a cell boundary on
# two different visits can occasionally split into two adjacent
# places - acceptable for a hand-reviewed registry (Christer merges/
# renames in the form) but worth knowing if "the same place" ever
# shows up twice in the places list.
_GRID_DECIMALS = 3

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


def stop_category(dwell_minutes: float | None) -> str | None:
    """"short" (< STOP_THRESHOLD_MINUTES), "long" (>=), or None if
    `dwell_minutes` itself is None (unknown - see
    dwell_at_destination())."""

    if dwell_minutes is None:
        return None
    return "short" if dwell_minutes < STOP_THRESHOLD_MINUTES else "long"


def place_key(point: tuple[float, float]) -> str:
    """Deterministic grid-cell id for a destination point - see this
    module's own docstring (point 2) for why identity is a plain
    rounding rather than a stateful clustering algorithm."""

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


@dataclass(frozen=True)
class CommonPlace:
    """One destination grid cell (see place_key()) Christer's vehicle
    has visited away from home, with his own short_stay_driver/
    long_stay_driver rules once he's set them - both start as None
    ("undecided"), the exact state undecided_places() below surfaces
    for the web form."""

    key: str
    point: tuple[float, float]
    label: str
    visit_count: int
    short_stay_count: int
    long_stay_count: int
    short_stay_driver: str | None = None
    long_stay_driver: str | None = None


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
) -> list[TripKnowledge]:
    home = known_points.get("home")
    home_radius = profiles.home_radius_meters

    entries: list[TripKnowledge] = []
    for index, trip in enumerate(trips):
        trip_fix = fixes[index]
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
        category = stop_category(dwell)
        candidates = match_driver(trip_fix, prev_fix, next_fix, profiles, known_points)

        entries.append(
            TripKnowledge(
                trip_label=trip.label,
                start_time=trip.start_timestamp,
                end_time=trip.end_timestamp,
                weekday=weekday,
                start_time_of_day=time_of_day,
                away_place_key=place_key(away_point) if away_point is not None else None,
                away_point=away_point,
                dwell_minutes=dwell,
                stop_category=category,
                candidates=candidates,
                start_point=trip_fix.start,
                end_point=trip_fix.end,
                first_recording_id=trip.first_recording.id.value,
                last_recording_id=trip.last_recording.id.value,
                camera_id=camera_id,
            )
        )

    return entries


def build_common_places(
    knowledge: Sequence[TripKnowledge],
    *,
    existing: dict[str, CommonPlace] | None = None,
) -> dict[str, CommonPlace]:
    """Aggregate `knowledge` into one CommonPlace per distinct away
    destination grid cell. `existing` (a previously-saved registry -
    see load_knowledge_base()) supplies each place's own label and any
    manual short_stay_driver/long_stay_driver Christer already set;
    those survive a rebuild untouched - only visit_count/
    short_stay_count/long_stay_count (and, for a place `existing` has
    never seen, point/label) are recomputed from `knowledge`."""

    existing = existing or {}
    points: dict[str, tuple[float, float]] = {}
    visit_counts: dict[str, int] = {}
    short_counts: dict[str, int] = {}
    long_counts: dict[str, int] = {}

    for entry in knowledge:
        key = entry.away_place_key
        if key is None:
            continue
        points.setdefault(key, entry.away_point)  # type: ignore[arg-type]
        visit_counts[key] = visit_counts.get(key, 0) + 1
        if entry.stop_category == "short":
            short_counts[key] = short_counts.get(key, 0) + 1
        elif entry.stop_category == "long":
            long_counts[key] = long_counts.get(key, 0) + 1

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
            short_stay_count=short_counts.get(key, 0),
            long_stay_count=long_counts.get(key, 0),
            short_stay_driver=(prior.short_stay_driver if prior is not None else None),
            long_stay_driver=(prior.long_stay_driver if prior is not None else None),
        )

    return places


def _resolve_trip_driver(
    entry: TripKnowledge,
    place: CommonPlace | None,
    profiles: DriverProfiles,
    override_label: str | None,
) -> TripKnowledge:
    """Resolution order: (1) a manual override on this specific trip
    always wins; (2) else the matching short/long-stay rule on this
    trip's CommonPlace, if Christer has set one; (3) else the best
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

    if place is not None and entry.stop_category is not None:
        rule_driver = (
            place.short_stay_driver
            if entry.stop_category == "short"
            else place.long_stay_driver
        )
        if rule_driver is not None:
            return replace(
                entry,
                driver_label=rule_driver,
                display_name=display_names.get(rule_driver, rule_driver),
                confidence=PLACE_RULE_CONFIDENCE,
                source="place-rule",
            )

    if entry.candidates:
        best = max(entry.candidates, key=lambda candidate: candidate.confidence)
        return replace(
            entry,
            driver_label=best.driver_label,
            display_name=best.display_name,
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
) -> tuple[list[TripKnowledge], dict[str, CommonPlace]]:
    """The whole pure pipeline: raw per-trip fields -> common places
    (carrying forward `existing_places`' own labels/rules) -> each
    trip resolved to a driver via the order _resolve_trip_driver()
    documents. `fixes` must be the same length as `trips`, in the same
    chronological order (index-aligned - see resolve_all_trip_fixes()).
    `camera_id` (the resolved CameraConfig.id bv_drivers.py's own
    resolve_archive_path() call already produces, or None for a
    literal/unconfigured archive path) is stamped onto every resulting
    TripKnowledge so bv-web's /drivers page can link a trip's first/
    last recording straight to /archive/{camera_id}/{recording_id}."""

    trip_overrides = trip_overrides or {}
    raw = _raw_trip_knowledge(trips, fixes, profiles, known_points, camera_id=camera_id)
    places = build_common_places(raw, existing=existing_places)
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


def undecided_places(
    places: dict[str, CommonPlace], *, min_visits: int = 2
) -> list[CommonPlace]:
    """Places visited at least `min_visits` times ("common") that have
    at least one stay-length category with no driver rule set yet -
    what the web form's "common places to fill in" section lists."""

    return [
        place
        for place in places.values()
        if place.visit_count >= min_visits
        and (
            (place.short_stay_count > 0 and place.short_stay_driver is None)
            or (place.long_stay_count > 0 and place.long_stay_driver is None)
        )
    ]


def undecided_trips(trips: Sequence[TripKnowledge]) -> list[TripKnowledge]:
    """Trips with no resolved driver at all - covers both a one-off
    destination (never clustered into a place worth a rule) and a
    common place Christer hasn't filled in yet. What the web form's
    "specific trips" per-trip override list shows."""

    return [entry for entry in trips if entry.source == "undecided"]


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
        "short_stay_count": place.short_stay_count,
        "long_stay_count": place.long_stay_count,
        "short_stay_driver": place.short_stay_driver,
        "long_stay_driver": place.long_stay_driver,
    }


def _place_from_dict(key: str, data: dict) -> CommonPlace:
    point = data.get("point") or [0.0, 0.0]
    return CommonPlace(
        key=key,
        point=(float(point[0]), float(point[1])),
        label=data.get("label", key),
        visit_count=int(data.get("visit_count", 0)),
        short_stay_count=int(data.get("short_stay_count", 0)),
        long_stay_count=int(data.get("long_stay_count", 0)),
        short_stay_driver=data.get("short_stay_driver"),
        long_stay_driver=data.get("long_stay_driver"),
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
        stop_category=data.get("stop_category"),
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
