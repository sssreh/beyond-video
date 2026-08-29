"""
Route/dwell-time driver detection - "notice similar trips and ask
later" (Christer's own framing).

Copyright (C) 2026 Christer R. (sssreh)

SPDX-License-Identifier: GPL-3.0-or-later

Increment 1 of driver detection (see WORKING_CONTEXT.md's now-resolved
"detect driver" note). Christer drives noticeably differently from his
wife, but the raw signal that first suggested this (.3gf g-sensor data)
turned out to sit on a large, mount-angle-dependent baseline with only
~10-12 units of real wobble - not usable for classification as-is (see
WORKING_CONTEXT.md). This module sidesteps that entirely: it classifies
a trip by *where it goes and how long it stays*, using GPS endpoints
and inter-trip dwell time, both of which bv-search's own machinery
(load_or_forward_geocode(), resolve_recording_gps_span()) already
resolves reliably.

Christer described each driver's habits as a set of home<->place
commute pairs, two of which are only distinguishable from each other by
how long the car stayed at the far end (his wife's Norra Stationsgatan
drop-off waits >10 minutes; Christer's own Norra Stationsgatan run is a
quick turnaround - same destination, opposite stay-duration signal).
That shape - "same place, disambiguated only by dwell time" - is why
this module treats stay duration as a first-class, optional condition
on a route pattern rather than bolting it on separately.

Design choices carried over from the driver-detection notes this
increment finally implements:

- Driver labels are opaque ("driver1", "driver2", ...) with a
  separate `display_name` for UI/prose - never a real name baked into
  the stored label itself. Mirrors pyannote diarization's own
  SPEAKER_00/SPEAKER_01 convention (see generate/speech.py).
- Confidence is a first-class float, not a hard yes/no guess - a
  low-confidence match should read as "maybe", not silently become a
  fact. See DriverMatch.confidence.
- "Notice ... and ask later": this module only ever proposes
  candidate matches (match_driver() returns a tuple, possibly empty,
  possibly with more than one candidate for the same trip). It never
  writes a label anywhere - that confirm/persist loop is an explicitly
  deferred next increment (see WORKING_CONTEXT.md).

Matching itself is kept pure and synchronous-I/O-free on purpose: the
GPS/geocoding reads that produce a TripFix or a place's (lat, lon) are
done once by the caller (resolve_trip_fix(), resolve_known_points())
and handed in as plain data, so match_driver() itself can be unit
tested without a real archive, network, or CameraAdapter.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from pathlib import Path

from ..adapters.base import CameraAdapter
from ..adapters.telemetry_bridge import resolve_recording_gps_span
from ..export.geocoding import GeocodeResult, load_or_forward_geocode
from ..generate.media import MediaToolError
from .trip import Trip

# Used whenever a route pattern or the home place doesn't specify its
# own radius_meters - generous enough to absorb ordinary GPS noise and
# a recording's start/end fix not landing exactly on a doorstep, tight
# enough that two of Christer's places a couple hundred meters apart
# (e.g. different addresses in the same neighborhood) don't collide.
DEFAULT_RADIUS_METERS = 300.0

_EARTH_RADIUS_METERS = 6_371_000.0


def _haversine_distance_meters(
    lat1: float, lon1: float, lat2: float, lon2: float
) -> float:
    """Same formula as search.py's own (module-private) copy - kept
    duplicated here for the same "genuinely separate concern" reason
    search.py itself gives for not importing trip_stats.py's copy."""

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


# --------------------------------------------------------------------
# Config schema (driver_profiles.json)
# --------------------------------------------------------------------


@dataclass(frozen=True)
class RoutePattern:
    """One labeled route a driver is known to take, exactly as
    Christer described it: a place name (forward-geocoded the same
    way bv-search's --place is) paired with one of three shapes.

    kind:
      "commute" - a leg between `place` and the profile's shared home
          point, either direction. Christer's plain from/to lists
          (Solna, Vårby Gård, Sickla, ...) are all this shape.
      "short_local" - both ends of the trip are near home itself, no
          `place` needed ("Any short trip in Hammarby Sjöstad").
          Requires max_duration_minutes.
      Both shapes accept an optional min_stay_minutes/max_stay_minutes
      dwell-time condition on the *other* leg of the same round trip
      (see match_driver()'s docstring for how that's computed) -
      Christer's wife's Norra Stationsgatan/Kråkbärsgränd patterns use
      min_stay_minutes; Christer's own Norra Stationsgatan pattern uses
      max_stay_minutes to disambiguate against the exact same place.
    """

    place: str | None = None
    kind: str = "commute"
    radius_meters: float = DEFAULT_RADIUS_METERS
    min_stay_minutes: float | None = None
    max_stay_minutes: float | None = None
    max_duration_minutes: float | None = None
    note: str = ""


@dataclass(frozen=True)
class DriverProfile:
    """One driver's known patterns. `label` is the opaque key
    ("driver1") this profile is stored under - kept on the instance
    too so a DriverMatch doesn't need the whole profiles dict to
    explain itself."""

    label: str
    display_name: str
    patterns: tuple[RoutePattern, ...] = field(default_factory=tuple)


@dataclass(frozen=True)
class DriverProfiles:
    """Top-level driver_profiles.json contents: the shared home place
    every driver's "commute" patterns are relative to, plus each
    driver's own patterns."""

    home_name: str
    home_query: str
    home_radius_meters: float
    drivers: tuple[DriverProfile, ...]


def default_driver_profiles_path(config_dir: Path) -> Path:
    """Where driver_profiles.json lives - alongside the other per-user
    config files under default_config_dir(), same convention
    known_places.jsonl/voice_asr.py's learned-places file already
    follows."""

    return config_dir / "driver_profiles.json"


def christers_driver_profiles() -> DriverProfiles:
    """The real starting profile data Christer gave verbatim: his
    wife's calm-driving routes and his own stressed/heavy-braking
    routes, home base Hammarby Sjöstad. Used to seed a fresh
    driver_profiles.json (see write_default_driver_profiles()) - not
    hardcoded into match_driver() itself, so Christer can edit the
    file (add places, retune stay minutes, add a third driver) without
    touching code.
    """

    wife = DriverProfile(
        label="driver1",
        display_name="Fru",
        patterns=(
            RoutePattern(place="Åkervägen 100, 121 33 Enskededalen"),
            RoutePattern(place="Fallskärmsgatan, Skarpnäck"),
            RoutePattern(place="Orminge"),
            RoutePattern(
                place="Norra Stationsgatan",
                min_stay_minutes=10,
                note="drop-off, stay longer than 10 minutes",
            ),
            RoutePattern(
                place="Kråkbärsgränd, Hässelby Villastad",
                min_stay_minutes=30,
                note="stay longer than 30 minutes",
            ),
        ),
    )

    christer = DriverProfile(
        label="driver2",
        display_name="Christer",
        patterns=(
            RoutePattern(place="Solna, Vintervägen 50"),
            RoutePattern(place="Vårby Gård"),
            RoutePattern(place="Masmo Tunnelbana"),
            RoutePattern(place="Västberga Handelsplats"),
            RoutePattern(place="Sickla"),
            RoutePattern(
                kind="short_local",
                max_duration_minutes=15,
                note="any short trip in Hammarby Sjöstad",
            ),
            RoutePattern(
                place="Norra Stationsgatan",
                max_stay_minutes=10,
                note="quick turnaround, driving wife to work",
            ),
        ),
    )

    return DriverProfiles(
        home_name="Hammarby Sjöstad",
        home_query="Hammarby Sjöstad, Stockholm",
        home_radius_meters=800.0,
        drivers=(wife, christer),
    )


def _pattern_to_dict(pattern: RoutePattern) -> dict:
    data = {"kind": pattern.kind, "radius_meters": pattern.radius_meters}
    if pattern.place is not None:
        data["place"] = pattern.place
    if pattern.min_stay_minutes is not None:
        data["min_stay_minutes"] = pattern.min_stay_minutes
    if pattern.max_stay_minutes is not None:
        data["max_stay_minutes"] = pattern.max_stay_minutes
    if pattern.max_duration_minutes is not None:
        data["max_duration_minutes"] = pattern.max_duration_minutes
    if pattern.note:
        data["note"] = pattern.note
    return data


def _pattern_from_dict(data: dict) -> RoutePattern:
    return RoutePattern(
        place=data.get("place"),
        kind=data.get("kind", "commute"),
        radius_meters=float(data.get("radius_meters", DEFAULT_RADIUS_METERS)),
        min_stay_minutes=data.get("min_stay_minutes"),
        max_stay_minutes=data.get("max_stay_minutes"),
        max_duration_minutes=data.get("max_duration_minutes"),
        note=data.get("note", ""),
    )


def driver_profiles_to_dict(profiles: DriverProfiles) -> dict:
    """Serialize to the plain-dict shape write_default_driver_profiles()/
    load_driver_profiles() read and write as JSON."""

    return {
        "home": {
            "name": profiles.home_name,
            "query": profiles.home_query,
            "radius_meters": profiles.home_radius_meters,
        },
        "drivers": {
            driver.label: {
                "display_name": driver.display_name,
                "patterns": [_pattern_to_dict(p) for p in driver.patterns],
            }
            for driver in profiles.drivers
        },
    }


def driver_profiles_from_dict(data: dict) -> DriverProfiles:
    home = data.get("home", {})
    drivers = tuple(
        DriverProfile(
            label=label,
            display_name=driver_data.get("display_name", label),
            patterns=tuple(
                _pattern_from_dict(p) for p in driver_data.get("patterns", [])
            ),
        )
        for label, driver_data in data.get("drivers", {}).items()
    )
    return DriverProfiles(
        home_name=home.get("name", "Home"),
        home_query=home.get("query", home.get("name", "Home")),
        home_radius_meters=float(home.get("radius_meters", DEFAULT_RADIUS_METERS)),
        drivers=drivers,
    )


def load_driver_profiles(path: Path) -> DriverProfiles | None:
    """Load driver_profiles.json, or None if it doesn't exist yet -
    same "absent is normal, not an error" contract as
    known_places_from_learned()."""

    if not path.exists():
        return None

    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None

    return driver_profiles_from_dict(data)


def write_default_driver_profiles(path: Path) -> DriverProfiles:
    """Seed `path` with Christer's real profile data
    (christers_driver_profiles()) if nothing is there yet, and return
    whatever profiles now live at `path` either way - so callers (the
    bv-ls --trips wiring) can just call this unconditionally at
    startup instead of separately checking existence first."""

    existing = load_driver_profiles(path)
    if existing is not None:
        return existing

    profiles = christers_driver_profiles()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(driver_profiles_to_dict(profiles), indent=2, ensure_ascii=False)
        + "\n",
        encoding="utf-8",
    )
    return profiles


def save_driver_profiles(path: Path, profiles: DriverProfiles) -> None:
    """Persist `profiles` back to `path` - the write half of
    load_driver_profiles(), needed once /drivers grew an "Add a
    driver" form (Christer: "How do i add a driver") rather than only
    ever reading driver_profiles.json. Same plain
    driver_profiles_to_dict() shape write_default_driver_profiles()
    already writes, just callable with an already-in-memory
    DriverProfiles instead of only the fixed seed data."""

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(driver_profiles_to_dict(profiles), indent=2, ensure_ascii=False)
        + "\n",
        encoding="utf-8",
    )


def add_driver(profiles: DriverProfiles, display_name: str) -> DriverProfiles:
    """Append a new, pattern-less DriverProfile to `profiles` and
    return the updated instance - the pure half of the /drivers "Add a
    driver" form. The new driver's opaque `label` is the next unused
    "driverN" in sequence (never reusing a number even if an earlier
    driver were ever removed, so old driver_knowledge.json overrides
    referencing a stale label can't collide with a new one) - same
    opaque-label convention every existing profile already follows
    (see this module's own docstring). Route matching only ever
    happens through `patterns`, so a driver added this way starts out
    only usable via the place-rule/bulk-assign/per-trip paths on
    /drivers, same as any driver whose patterns haven't been tuned
    yet - patterns are deliberately left to hand-editing
    driver_profiles.json, same as before this function existed."""

    existing_numbers = []
    for driver in profiles.drivers:
        if driver.label.startswith("driver"):
            suffix = driver.label[len("driver") :]
            if suffix.isdigit():
                existing_numbers.append(int(suffix))
    next_number = max(existing_numbers, default=0) + 1
    new_driver = DriverProfile(label=f"driver{next_number}", display_name=display_name)

    return replace(profiles, drivers=(*profiles.drivers, new_driver))


# --------------------------------------------------------------------
# Trip data the matcher needs, resolved once per trip by the caller
# --------------------------------------------------------------------


@dataclass(frozen=True)
class TripFix:
    """A trip's resolved start/end points and timestamps - all
    match_driver() needs, and deliberately nothing more (no
    Recording/adapter reference), so it can be built by hand in tests
    without a real archive. `start`/`end` are (lat, lon) or None if
    resolve_recording_gps_span() came up empty for that recording."""

    start: tuple[float, float] | None
    end: tuple[float, float] | None
    start_time: datetime
    end_time: datetime


def resolve_trip_fix(adapter: CameraAdapter, trip: Trip) -> TripFix:
    """Build a TripFix for `trip`: start point from its first
    recording, end point from its last (the same one-probe-per-end
    approach cli/bv_ls.py's GPS column and the /location route already
    use via resolve_recording_gps_span() - see that function's own
    docstring for the real-telemetry-then-EXIF/container-tag fallback
    order and cost caveat)."""

    start_fix, _ = resolve_recording_gps_span(adapter, trip.first_recording)
    _, end_fix = resolve_recording_gps_span(adapter, trip.last_recording)

    start = (
        (start_fix.latitude, start_fix.longitude)
        if start_fix is not None
        and start_fix.latitude is not None
        and start_fix.longitude is not None
        else None
    )
    end = (
        (end_fix.latitude, end_fix.longitude)
        if end_fix is not None
        and end_fix.latitude is not None
        and end_fix.longitude is not None
        else None
    )

    return TripFix(
        start=start,
        end=end,
        start_time=trip.start_timestamp,
        end_time=trip.end_timestamp,
    )


def resolve_known_points(
    profiles: DriverProfiles, cache_dir: Path
) -> dict[str, tuple[float, float]]:
    """Forward-geocode `profiles`' home place and every distinct place
    name across all drivers' patterns, once, via the same
    load_or_forward_geocode() bv-search's --place and bv-web's live
    coordinate preview already use (and cache to the same on-disk
    cache - see web/app.py's geocode_preview_voice_search() docstring
    for why cache_dir must be a writable location, not archive_path).

    Returns a dict keyed "home" plus each pattern's own `place` string,
    each mapped to its resolved (lat, lon) - place names load_or_
    forward_geocode() can't resolve are silently omitted (a pattern
    referencing a missing key just never matches, rather than this
    function raising)."""

    points: dict[str, tuple[float, float]] = {}

    def _resolve(key: str, query: str) -> None:
        try:
            result: GeocodeResult | None = load_or_forward_geocode(query, cache_dir)
        except MediaToolError:
            return
        if result is not None:
            points[key] = result.point

    _resolve("home", profiles.home_query)

    seen_places: set[str] = set()
    for driver in profiles.drivers:
        for pattern in driver.patterns:
            if pattern.place and pattern.place not in seen_places:
                seen_places.add(pattern.place)
                _resolve(pattern.place, pattern.place)

    return points


# --------------------------------------------------------------------
# Matching (pure)
# --------------------------------------------------------------------


@dataclass(frozen=True)
class DriverMatch:
    """One candidate "this trip looks like driver X" result -
    deliberately a candidate, not a verdict (see module docstring's
    "notice ... and ask later"). A trip can produce zero, one, or more
    than one DriverMatch (e.g. two drivers sharing a place with
    opposite stay-duration conditions, if the dwell time can't be
    computed to disambiguate them)."""

    driver_label: str
    display_name: str
    place: str
    confidence: float
    reason: str


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


def match_driver(
    trip_fix: TripFix,
    prev_fix: TripFix | None,
    next_fix: TripFix | None,
    profiles: DriverProfiles,
    known_points: dict[str, tuple[float, float]],
) -> tuple[DriverMatch, ...]:
    """Return every driver/pattern this trip plausibly matches.

    Stay-duration conditions (min_stay_minutes/max_stay_minutes) are
    about the *other* leg of the same round trip - the gap the car
    spent parked at the far end. Concretely: if this trip runs
    home->place, the relevant dwell is the gap between this trip's own
    end and `next_fix`'s start (the return leg, place->home); if this
    trip runs place->home, it's the gap between `prev_fix`'s end and
    this trip's own start (the outbound leg, home->place). That's why
    match_driver() takes the chronologically adjacent trips' fixes as
    well as this one's - Trip itself has no "next scheduled leg"
    concept (see trip/trip.py), so the caller (iterating trips in
    order, as bv-ls --trips already does) is what supplies them.

    When a stay-duration condition can't be checked at all (missing
    adjacent trip, or the adjacent trip's own endpoint isn't near the
    same place - e.g. the vehicle went somewhere else in between),
    the pattern still matches but at reduced confidence with a reason
    noting the dwell time is unverified, rather than being silently
    dropped - a false "no match" is worse here than a lower-confidence
    "maybe" the caller can still surface.
    """

    home = known_points.get("home")
    matches: list[DriverMatch] = []

    for driver in profiles.drivers:
        for pattern in driver.patterns:
            if pattern.kind == "short_local":
                if not (
                    _near(trip_fix.start, home, profiles.home_radius_meters)
                    and _near(trip_fix.end, home, profiles.home_radius_meters)
                ):
                    continue
                duration_minutes = (
                    trip_fix.end_time - trip_fix.start_time
                ).total_seconds() / 60.0
                if (
                    pattern.max_duration_minutes is not None
                    and duration_minutes > pattern.max_duration_minutes
                ):
                    continue
                matches.append(
                    DriverMatch(
                        driver_label=driver.label,
                        display_name=driver.display_name,
                        place=profiles.home_name,
                        confidence=0.7,
                        reason=(
                            pattern.note
                            or f"short local trip near {profiles.home_name}"
                        ),
                    )
                )
                continue

            if pattern.place is None:
                continue
            place_point = known_points.get(pattern.place)
            if place_point is None or home is None:
                continue

            home_to_place = _near(
                trip_fix.start, home, profiles.home_radius_meters
            ) and _near(trip_fix.end, place_point, pattern.radius_meters)
            place_to_home = _near(
                trip_fix.start, place_point, pattern.radius_meters
            ) and _near(trip_fix.end, home, profiles.home_radius_meters)

            if not home_to_place and not place_to_home:
                continue

            dwell_minutes: float | None = None
            dwell_verified = False
            if pattern.min_stay_minutes is not None or pattern.max_stay_minutes is not None:
                if home_to_place and next_fix is not None:
                    if _near(next_fix.start, place_point, pattern.radius_meters):
                        dwell_minutes = _dwell_minutes(
                            trip_fix.end_time, next_fix.start_time
                        )
                        dwell_verified = dwell_minutes is not None
                elif place_to_home and prev_fix is not None:
                    if _near(prev_fix.end, place_point, pattern.radius_meters):
                        dwell_minutes = _dwell_minutes(
                            prev_fix.end_time, trip_fix.start_time
                        )
                        dwell_verified = dwell_minutes is not None

                if dwell_verified:
                    if (
                        pattern.min_stay_minutes is not None
                        and dwell_minutes < pattern.min_stay_minutes
                    ):
                        continue
                    if (
                        pattern.max_stay_minutes is not None
                        and dwell_minutes > pattern.max_stay_minutes
                    ):
                        continue

            confidence = 0.6
            reason = f"{'home -> ' + pattern.place if home_to_place else pattern.place + ' -> home'}"
            if pattern.min_stay_minutes is not None or pattern.max_stay_minutes is not None:
                if dwell_verified:
                    confidence = 0.9
                    reason += f", stayed ~{dwell_minutes:.0f} min"
                else:
                    confidence = 0.4
                    reason += " (stay duration unverified)"
            if pattern.note:
                reason += f" - {pattern.note}"

            matches.append(
                DriverMatch(
                    driver_label=driver.label,
                    display_name=driver.display_name,
                    place=pattern.place,
                    confidence=confidence,
                    reason=reason,
                )
            )

    return tuple(matches)
