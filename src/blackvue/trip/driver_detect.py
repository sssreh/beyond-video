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
whether the car was left parked at the far end (his wife's Norra
Stationsgatan drop-off leaves the car parked there; Christer's own
Norra Stationsgatan run is a quick turnaround, no parking - same
destination, opposite parking signal). That shape - "same place,
disambiguated only by whether it was parked" - is why this module
treats parking status as a first-class, optional condition on a route
pattern rather than bolting it on separately.

(Originally this was a wall-clock stay-duration threshold - min/max
minutes parked at the far end, inferred from the gap between adjacent
trips' GPS fixes. Christer: "Long and short are not in the game
anymore, more like if you get a P file after its long" - a downloaded
Parking-mode (P) recording at the far end is direct camera evidence
the car was actually left there, unlike a wall-clock gap between two
trips, which conflates a real stay with an ordinary download/data gap.
See place_knowledge.py's own equivalent redesign of
stop_category()/CommonPlace for the same change on that module's
side.)

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
from ..adapters.telemetry_bridge import read_recording_gps
from ..adapters.telemetry_bridge import resolve_recording_gps_span
from ..export.geocoding import GeocodeResult, load_or_forward_geocode
from ..generate.media import MediaToolError
from .trip import Trip
from .trip_builder import TripBuilder

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
      Both shapes accept an optional requires_parking condition on the
      *other* leg of the same round trip (see match_driver()'s
      docstring for how that's checked) - Christer's wife's Norra
      Stationsgatan/Kråkbärsgränd patterns use requires_parking=True
      (car left parked there); Christer's own Norra Stationsgatan
      pattern uses requires_parking=False (quick turnaround, no
      parking) to disambiguate against the exact same place.
    """

    place: str | None = None
    kind: str = "commute"
    radius_meters: float = DEFAULT_RADIUS_METERS
    requires_parking: bool | None = None
    max_duration_minutes: float | None = None
    note: str = ""


@dataclass(frozen=True)
class HomePoint:
    """One additional place that counts as "home" for matching
    purposes, alongside the profile's own `home_query`/
    `home_radius_meters` - Christer: "Usually when we drive north from
    garage, the GPS finds us at Skansbrogatan" (his underground garage
    has no GPS signal, so a north-bound departure's first real fix
    lands at a consistent, different point instead of at the garage
    itself - confirmed against his real archive: a cluster 346m north
    of `home_query`'s own geocoded point, just outside its 200m
    `home_radius_meters`, which he'd already hand-labeled "Mitt
    garage" as its own Common Place on /drivers rather than recognizing
    it as a home-adjacent GPS artifact). Without this, every trip whose
    first/last resolvable fix lands here instead of at the real garage
    silently fails every `_near(..., home, home_radius_meters)` check
    match_driver()/resolve_via_point() do, not just this one named
    cluster - a departure that quietly starts 346m away from what the
    matcher treats as "home" never counts as a home-originating leg at
    all.

    `name` is a short label (used only as resolve_known_points()'
    dict key and in test/debug output, never shown to a driver).
    `query` is forward-geocoded exactly like a RoutePattern's own
    `place`. `radius_meters` defaults to DEFAULT_RADIUS_METERS, same
    as a RoutePattern - deliberately its own field rather than reusing
    `home_radius_meters`, since a GPS-cold-start artifact's own cluster
    tightness has nothing to do with the real garage's."""

    name: str
    query: str
    radius_meters: float = DEFAULT_RADIUS_METERS


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
    driver's own patterns.

    `default_from` (default: None) is a personal, opt-in floor on
    which recordings `bv-drivers build` ever considers - a
    LexicalTimeParser TIMESTAMP string (e.g. "20260706"), same format
    as the CLI's own `--from`. bv_drivers.py's `_run()` uses it as the
    fallback for `--from` whenever that flag is omitted; passing
    `--from` explicitly always overrides it for that one run. This
    field lives only in the JSON file on disk (default: None here, and
    christers_driver_profiles() below deliberately never sets it) -
    Christer, after asking how to stop himself from accidentally
    running the driver knowledge base against his own pre-2026-07-06
    data (before his sidecar downloads became complete - see
    bv_drivers.py's own P-ending-requirement filter comment for why
    that era's trip endings are unreliable), was firm that the date
    itself has no business in tracked source: "Notera att allt som rör
    6 juli, bara gäller mig, ingenting som skall in i github." - "No,
    i mean permanently for me" when offered a config-file-based
    default instead of a one-off CLI reminder. Setting it is a runtime
    edit to Christer's own driver_profiles.json, never a code change.

    `home_extra_points` (default: ()) is a tuple of HomePoint - see
    that dataclass's own docstring - additional places that also count
    as "home" for every `_near_home()` check (match_driver()'s
    home_to_place/place_to_home/short_local/via_round_trip conditions,
    resolve_via_point()'s own round-trip gate). Same personal-data-only
    convention as `default_from`: christers_driver_profiles() below
    never populates it, so it's only ever real data Christer (or a
    future user) hand-adds to their own driver_profiles.json once
    they've noticed their own GPS-cold-start artifact.
    """

    home_name: str
    home_query: str
    home_radius_meters: float
    drivers: tuple[DriverProfile, ...]
    default_from: str | None = None
    home_extra_points: tuple[HomePoint, ...] = ()


def default_driver_profiles_path(config_dir: Path) -> Path:
    """Where driver_profiles.json lives - alongside the other per-user
    config files under default_config_dir(), same convention
    known_places.jsonl/voice_asr.py's learned-places file already
    follows."""

    return config_dir / "driver_profiles.json"


def default_driver_profiles() -> DriverProfiles:
    """Generic, empty starting profile for write_default_driver_profiles()
    to seed a brand-new driver_profiles.json with - no real address, no
    drivers, nothing specific to Christer. Before this existed,
    write_default_driver_profiles() seeded every first-time user's file
    with christers_driver_profiles() (Christer's own real home address
    and driver names), which meant anyone else running this tool for
    the first time got a stranger's home town as their own starting
    point instead of a blank template - a real rough edge for the
    public repo, not just a cosmetic one.

    `home_query` is deliberately an obvious, unresolvable placeholder
    string rather than an empty one - an empty string could plausibly
    look like a config bug rather than a prompt to edit it, whereas
    this reads unmistakably as "replace me" and resolve_known_points()
    quietly skips it (nothing geocodes) until the user does. Every
    other field starts as empty/default: no drivers, no
    home_extra_points, no default_from - all opt-in, hand-added by
    each user to their own file exactly the way home_extra_points and
    default_from's own docstrings already describe."""

    return DriverProfiles(
        home_name="Home",
        home_query="REPLACE ME: your home address, e.g. 123 Main St, City",
        home_radius_meters=DEFAULT_RADIUS_METERS,
        drivers=(),
    )


def christers_driver_profiles() -> DriverProfiles:
    """The real starting profile data Christer gave verbatim: his
    wife's calm-driving routes and his own stressed/heavy-braking
    routes, home base Hammarby Sjöstad. Not hardcoded into
    match_driver() itself, so Christer can edit the file (add places,
    retune stay minutes, add a third driver) without touching code.

    This is Christer's own personal data, not a generic starting
    point - write_default_driver_profiles() seeds a fresh
    driver_profiles.json with default_driver_profiles() instead (see
    that function's own docstring for why: seeding every new user's
    first-ever file with Christer's real home address was a real
    rough edge for anyone else actually using this repo). This
    function stays only for tests that want realistic route data to
    exercise match_driver() against, and as the one-time seed for
    Christer's own already-existing file.
    """

    wife = DriverProfile(
        label="driver1",
        display_name="Dao",
        patterns=(
            RoutePattern(place="Åkervägen 100, 121 33 Enskededalen"),
            RoutePattern(place="Fallskärmsgatan, Skarpnäck"),
            RoutePattern(place="Orminge"),
            RoutePattern(
                place="Norra Stationsgatan",
                requires_parking=True,
                note="drop-off, car left parked (has a P recording)",
            ),
            RoutePattern(
                place="Kråkbärsgränd, Hässelby Villastad",
                requires_parking=True,
                note="car left parked (has a P recording)",
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
                requires_parking=False,
                note="quick turnaround, driving wife to work, no P recording",
            ),
        ),
    )

    return DriverProfiles(
        home_name="Hammarby Sjöstad",
        # Was "Hammarby Sjöstad, Stockholm" - a whole neighborhood
        # name, not a precise address. Its own geocoded point sat
        # ~750-760m from where Christer's car actually arrives/departs
        # home, which meant home_radius_meters had to stay huge (800m)
        # just to reach real home-adjacent GPS fixes at all - dropping
        # it to 300m once missed every one of them (0 away_points, 0
        # common places, see driver_knowledge.json's own "164 trip(s),
        # 0 place(s)" report). Christer: "My parking garage is next to
        # Heliosgatan 38, maybe that help." Geocoding this address
        # lands within 6m of the tight real-arrival cluster his own
        # trip data already implied (59.303215, 18.093528) - i.e. this
        # query resolves to his actual garage, not just the
        # neighborhood it's in.
        home_query="Heliosgatan 38, Stockholm",
        # 200.0, down from 800.0, now that home_query resolves to the
        # real garage rather than a neighborhood centroid. Verified
        # against Christer's real 164-trip archive: every real
        # home-adjacent GPS fix lands within 160m of the new geocoded
        # point (a clean, obvious cluster - the next-nearest distinct
        # place is 209m+ away), so 200m safely captures the whole
        # home cluster with margin while excluding everywhere else -
        # including "short trip" destinations well inside the old
        # 800m radius, which was the whole point of Christer's
        # original ask ("i often go to max hamburgers").
        home_radius_meters=200.0,
        drivers=(wife, christer),
    )


def _pattern_to_dict(pattern: RoutePattern) -> dict:
    data = {"kind": pattern.kind, "radius_meters": pattern.radius_meters}
    if pattern.place is not None:
        data["place"] = pattern.place
    if pattern.requires_parking is not None:
        data["requires_parking"] = pattern.requires_parking
    if pattern.max_duration_minutes is not None:
        data["max_duration_minutes"] = pattern.max_duration_minutes
    if pattern.note:
        data["note"] = pattern.note
    return data


def _pattern_from_dict(data: dict) -> RoutePattern:
    requires_parking = data.get("requires_parking")
    if requires_parking is None:
        # Backward-compat migration: a driver_profiles.json written
        # before Christer's "long and short doesn't exist any more"
        # redesign may still have the old min_stay_minutes/
        # max_stay_minutes fields - a min_stay condition meant "stayed
        # a while" (now: was parked, requires_parking=True), a
        # max_stay condition meant "quick turnaround" (now: no
        # parking, requires_parking=False). Both keys are ignored (not
        # written back) the next time this profile is saved - see
        # _pattern_to_dict() above, which only ever emits
        # requires_parking.
        if data.get("min_stay_minutes") is not None:
            requires_parking = True
        elif data.get("max_stay_minutes") is not None:
            requires_parking = False
    return RoutePattern(
        place=data.get("place"),
        kind=data.get("kind", "commute"),
        radius_meters=float(data.get("radius_meters", DEFAULT_RADIUS_METERS)),
        requires_parking=requires_parking,
        max_duration_minutes=data.get("max_duration_minutes"),
        note=data.get("note", ""),
    )


def _home_point_to_dict(point: HomePoint) -> dict:
    return {
        "name": point.name,
        "query": point.query,
        "radius_meters": point.radius_meters,
    }


def _home_point_from_dict(data: dict) -> HomePoint:
    return HomePoint(
        name=data.get("name", data.get("query", "")),
        query=data.get("query", ""),
        radius_meters=float(data.get("radius_meters", DEFAULT_RADIUS_METERS)),
    )


def driver_profiles_to_dict(profiles: DriverProfiles) -> dict:
    """Serialize to the plain-dict shape write_default_driver_profiles()/
    load_driver_profiles() read and write as JSON."""

    return {
        "home": {
            "name": profiles.home_name,
            "query": profiles.home_query,
            "radius_meters": profiles.home_radius_meters,
            "extra_points": [
                _home_point_to_dict(p) for p in profiles.home_extra_points
            ],
        },
        "default_from": profiles.default_from,
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
        default_from=data.get("default_from"),
        home_extra_points=tuple(
            _home_point_from_dict(p) for p in home.get("extra_points", [])
        ),
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
    """Seed `path` with a generic, empty starting profile
    (default_driver_profiles() - see that function's own docstring for
    why it's generic rather than christers_driver_profiles()) if
    nothing is there yet, and return whatever profiles now live at
    `path` either way - so callers (the bv-ls --trips wiring) can just
    call this unconditionally at startup instead of separately
    checking existence first."""

    existing = load_driver_profiles(path)
    if existing is not None:
        return existing

    profiles = default_driver_profiles()
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


def rename_driver(
    profiles: DriverProfiles, label: str, display_name: str
) -> DriverProfiles:
    """Return `profiles` with the driver whose opaque `label` matches
    given a new `display_name` - the pure half of /drivers' inline
    driver-rename form. `label` (not the old display_name) is the
    lookup key, same "opaque label is the real identity" convention
    add_driver() and every DriverMatch/TripKnowledge.driver_label
    reference already follow - display_name is presentation only, so
    renaming it never touches any stored override/place-rule, which
    all key off `label`. A `label` that doesn't match any driver
    leaves `profiles` unchanged (no-op, not an error - same
    forgiving-caller shape as this module's other pure functions).

    Note: display_name is also snapshotted onto each already-built
    TripKnowledge.display_name at resolve time (see
    _resolve_trip_driver() in place_knowledge.py) - callers need to
    re-run reresolve_trip_drivers() and re-save driver_knowledge.json
    after this, the same "profiles change, trips need a re-resolve
    pass" contract drivers_update_place() already follows for place
    rules."""

    updated = tuple(
        replace(driver, display_name=display_name) if driver.label == label else driver
        for driver in profiles.drivers
    )
    return replace(profiles, drivers=updated)


# --------------------------------------------------------------------
# Trip data the matcher needs, resolved once per trip by the caller
# --------------------------------------------------------------------


@dataclass(frozen=True)
class TripFix:
    """A trip's resolved start/end points and timestamps - all
    match_driver() needs, and deliberately nothing more (no
    Recording/adapter reference), so it can be built by hand in tests
    without a real archive. `start`/`end` are (lat, lon) or None if
    resolve_recording_gps_span() came up empty for that recording.

    `has_parking_footage` is whether this trip's own tail (its last
    recording) is a downloaded Parking-mode (P) recording - the one
    piece of non-GPS trip data match_driver() now needs, since a
    RoutePattern's requires_parking condition (see that dataclass's
    own docstring) reads it off `trip_fix`/`prev_fix` rather than a
    wall-clock dwell computation. Resolved once per trip here, same
    reason start/end/start_time/end_time are, so match_driver() itself
    still needs no Recording/adapter reference of its own.

    `via_point` is the furthest point from home reached anywhere along
    this trip's own GPS track, set only for a round trip (`start` and
    `end` both near home) whose track actually went somewhere - see
    resolve_via_point()'s own docstring. Christer, on why this exists:
    "When i drive my wife to work in the morning, the trip starts and
    stops at Heliosgatan... it would be nice if we could get where i
    went, even if i returned to the starting place." Left None for
    every trip resolve_trip_fix() builds on its own; only bv_drivers.py's
    build command (which has `home` and `home_radius_meters` in hand)
    computes and attaches it via `dataclasses.replace()` - see that
    module's own fixes-building loop."""

    start: tuple[float, float] | None
    end: tuple[float, float] | None
    start_time: datetime
    end_time: datetime
    has_parking_footage: bool = False
    via_point: tuple[float, float] | None = None


def _first_resolvable_start(
    adapter: CameraAdapter, recordings: tuple
) -> tuple[float, float] | None:
    """Scan `recordings` in order and return the first (lat, lon) any
    of them yields as a *start* fix, or None if none do.

    Needed because bv-drivers' own P-ending-trip filter (see
    bv_drivers.py's own docstring) guarantees every trip's *first*
    recording can be anything - it only constrains the *last* one.
    In practice the first recording is almost always a real driving
    (N/E) recording with a genuine GPS fix, so this loop typically
    returns on its first iteration - it only matters for the rarer
    trip that happens to open on a recording with no resolvable
    position of its own (a corrupted/empty .gps file, a brief motion
    trigger with no lock yet, ...)."""

    for recording in recordings:
        start_fix, _ = resolve_recording_gps_span(adapter, recording)
        if (
            start_fix is not None
            and start_fix.latitude is not None
            and start_fix.longitude is not None
        ):
            return (start_fix.latitude, start_fix.longitude)
    return None


def _last_resolvable_end(
    adapter: CameraAdapter, recordings: tuple
) -> tuple[float, float] | None:
    """Same as _first_resolvable_start(), scanning backward from the
    end for an *end* fix.

    This one matters far more often: bv-drivers' P-ending filter
    forces every trip's *last* recording to be a Parking-mode one, and
    Parking-mode recordings' own front video normally isn't downloaded
    at all (bv-download's policy: only E/M-kind video + the one right
    before each - see bv_drivers.py's own filter docstring), which
    knocks out resolve_recording_gps_span()'s EXIF/container-tag
    fallback path entirely, and a brief motion-triggered Parking clip
    often hasn't held a GPS lock long enough for its own .gps sidecar
    to carry a valid fix either. Confirmed on Christer's real archive
    (2026-08-31): with only the literal last recording's own fix
    trusted, 467 of 468 trips came back with no usable end point at
    all (0 resolved, 0 common places) even though the *driving* portion
    of nearly every one of those trips - the N/E recording(s) right
    before the car parked - has a perfectly good fix. Falling back to
    the last recording that actually has one is the real trip's own
    last known position either way; a Parking-mode tail recording with
    no fix of its own was never going to add real information."""

    for recording in reversed(recordings):
        _, end_fix = resolve_recording_gps_span(adapter, recording)
        if (
            end_fix is not None
            and end_fix.latitude is not None
            and end_fix.longitude is not None
        ):
            return (end_fix.latitude, end_fix.longitude)
    return None


def resolve_trip_fix(adapter: CameraAdapter, trip: Trip) -> TripFix:
    """Build a TripFix for `trip`: start point from the first
    recording (in order) that resolves one at all, end point from the
    last recording (in order) that resolves one at all - see
    _first_resolvable_start()/_last_resolvable_end()'s own docstrings
    for why this scans instead of only trusting trip.first_recording/
    trip.last_recording specifically, the same one-probe-per-recording
    approach cli/bv_ls.py's GPS column and the /location route already
    use via resolve_recording_gps_span() - see that function's own
    docstring for the real-telemetry-then-EXIF/container-tag fallback
    order and cost caveat).

    `has_parking_footage` mirrors bv_drivers.py's own P-ending trip
    filter check (`trip.last_recording.id.is_parking and any(asset.
    is_downloaded ...)`), not `Trip.has_parking_footage` (which checks
    every recording in the trip, not specifically the last, and
    doesn't require a downloaded asset) - see that filter's own
    docstring for why a generated-only P id doesn't count as camera
    evidence of a real stop."""

    start = _first_resolvable_start(adapter, trip.recordings)
    end = _last_resolvable_end(adapter, trip.recordings)

    last_recording = trip.last_recording
    has_parking_footage = last_recording.id.is_parking and any(
        asset.is_downloaded for asset in last_recording.assets
    )

    return TripFix(
        start=start,
        end=end,
        start_time=trip.start_timestamp,
        end_time=trip.end_timestamp,
        has_parking_footage=has_parking_footage,
    )


def resolve_via_point(
    adapter: CameraAdapter,
    trip: Trip,
    trip_fix: TripFix,
    known_points: dict[str, tuple[float, float]],
    profiles: DriverProfiles,
) -> tuple[float, float] | None:
    """Find the furthest point from home anywhere along `trip`'s own
    GPS track, for a round trip whose resolve_trip_fix()-resolved
    `start`/`end` are both near home (via `_near_home()` - see that
    function's own docstring for why this checks `home_extra_points`
    too, not just the primary home point) - see TripFix.via_point's
    own docstring for why this exists (Christer's morning drop-off:
    "the trip starts and stops at Heliosgatan").

    Deliberately does nothing (returns None immediately) unless
    `trip_fix.start` and `trip_fix.end` are both near home - a real
    one-way trip already gets a perfectly good away_point from
    resolve_trip_fix() alone (see place_knowledge._raw_trip_knowledge()),
    so there's no reason to pay this function's own cost (reading every
    recording's full GPS track, not just first/last - see TripFix's own
    docstring for why resolve_trip_fix() normally doesn't need to) on
    the common case.

    Scans every recording in the trip (in order, front-to-back doesn't
    matter here - unlike resolve_trip_fix()'s start/end search, every
    fix is a candidate) and keeps whichever confirmed, positioned fix
    sits furthest from the *primary* home point (known_points["home"],
    not an extra point - an extra point is just another way of being
    "at home", not a second reference for measuring "how far away") by
    the same haversine distance _near() uses. Only trusts
    `fix.confirmed` positions (mirrors trip_stats.py's own "Require
    confirmed positions" redesign - a lone GPS glitch during
    acquisition becoming this trip's away_point would be worse than
    missing one entirely).

    Returns None whenever there's nothing real to report: the primary
    home point is unknown, the trip isn't a round trip in the first
    place, no recording yields a single confirmed positioned fix, or
    the furthest fix found is still within `profiles.home_radius_meters`
    of home (a real "never actually left" round trip - e.g. a garage
    motion blip - the exact case away_point/dwell_at_destination() were
    originally designed to exclude; see dwell_at_destination()'s own
    docstring)."""

    home = known_points.get("home")
    if home is None:
        return None
    if not (
        _near_home(trip_fix.start, known_points, profiles)
        and _near_home(trip_fix.end, known_points, profiles)
    ):
        return None

    furthest_point: tuple[float, float] | None = None
    furthest_distance = 0.0

    for recording in trip.recordings:
        for fix in read_recording_gps(adapter, recording):
            if not fix.valid or not fix.confirmed:
                continue
            if fix.latitude is None or fix.longitude is None:
                continue
            point = (fix.latitude, fix.longitude)
            distance = _haversine_distance_meters(*point, *home)
            if distance > furthest_distance:
                furthest_distance = distance
                furthest_point = point

    if furthest_point is None or furthest_distance <= profiles.home_radius_meters:
        return None

    return furthest_point


@dataclass(frozen=True)
class DriverTripFilterCounts:
    """How many trips build_driver_trips() dropped at each of its two
    trust filters - purely informational, for a caller that wants to
    print a --debug line about it (see bv_drivers.py's own --debug
    output); build_driver_trips() itself never prints anything."""

    detected: int
    dropped_no_driving_evidence: int
    dropped_not_ending_in_parking: int


def build_driver_trips(
    recordings,
    *,
    max_gap: timedelta,
    gap_tolerance: timedelta,
) -> tuple[list[Trip], DriverTripFilterCounts]:
    """Build the same trip list bv-drivers.py's _run() does - the
    "sidecar trip" concept, not bv-ls's/bv-export's own "video trip"
    concept (see bv_ls.py's print_trips() docstring, which filters to
    recordings_with_front_video() first). Christer, on why these
    aren't interchangeable: "bv-ls and bv-export are building video
    trips, we are trying to get driver from sidecars. Not same trip
    concept."

    Factored out of bv_drivers.py's _run() so bv-ls's --drivers-trips
    column (see bv_ls.py) can show driver matches against this same
    trip concept directly, without needing a full bv-drivers build/
    driver_knowledge.json round-trip first - Christer asked for this
    after being reminded bv-ls --drivers itself uses the other trip
    concept and so isn't a real preview of what bv-drivers would
    decide.

    `recordings` is every recording in range - deliberately NOT
    filtered to ones with front video, same reasoning as bv_drivers.py
    _run()'s own trip-building comment. Two trust filters are applied
    after TripBuilder splits by gap, in order:

    1. Drop any trip where every recording is Parking-mode - a lone
       motion-triggered Parking blip (commonly one triggered in
       Christer's own underground home garage, which has no GPS
       signal) trivially satisfies filter 2 below on its own and would
       otherwise dominate the trip list with unresolvable noise.
    2. Drop any trip that doesn't end in a downloaded Parking-mode
       recording - the camera's own signal that the vehicle was
       actually left somewhere, as opposed to a gap that's just a
       data/download gap (see bv_drivers.py's own filter for Christer's
       full reasoning and the real-archive numbers behind it). The
       chronologically last trip is exempt - it may simply not have its
       Parking-mode sidecars downloaded yet.

    See bv_drivers.py's `_run()` for the identical logic this was
    extracted from (kept there rather than calling through to here, to
    avoid disturbing its own already-verified --debug reporting)."""

    trips = TripBuilder(max_gap=max_gap, gap_tolerance=gap_tolerance).build(recordings)
    detected = len(trips)

    trips = [
        trip
        for trip in trips
        if any(not recording.id.is_parking for recording in trip.recordings)
    ]
    dropped_no_driving_evidence = detected - len(trips)

    before_parking_filter = len(trips)
    trips = [
        trip
        for trip in trips
        if trip is trips[-1]
        or (
            trip.last_recording.id.is_parking
            and any(asset.is_downloaded for asset in trip.last_recording.assets)
        )
    ]
    dropped_not_ending_in_parking = before_parking_filter - len(trips)

    return trips, DriverTripFilterCounts(
        detected=detected,
        dropped_no_driving_evidence=dropped_no_driving_evidence,
        dropped_not_ending_in_parking=dropped_not_ending_in_parking,
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

    Returns a dict keyed "home", plus `"home_extra:{name}"` for each of
    `profiles.home_extra_points` (see HomePoint's own docstring),
    plus each pattern's own `place` string - each mapped to its
    resolved (lat, lon). Place names load_or_forward_geocode() can't
    resolve are silently omitted (a pattern - or a home extra point -
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

    for extra in profiles.home_extra_points:
        _resolve(f"home_extra:{extra.name}", extra.query)

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


def _near_home(
    point: tuple[float, float] | None,
    known_points: dict[str, tuple[float, float]],
    profiles: DriverProfiles,
) -> bool:
    """Every "is this point at home" check match_driver()/
    resolve_via_point() do, in one place - near the profile's own
    home_query/home_radius_meters, OR near any of its
    home_extra_points (see that field's own docstring: a GPS-cold-
    start artifact like Christer's north-bound "Skansbrogatan" garage
    exit is still, for matching purposes, home). Each extra point uses
    its own radius_meters, not the primary home's."""

    if _near(point, known_points.get("home"), profiles.home_radius_meters):
        return True
    for extra in profiles.home_extra_points:
        if _near(point, known_points.get(f"home_extra:{extra.name}"), extra.radius_meters):
            return True
    return False


def match_driver(
    trip_fix: TripFix,
    prev_fix: TripFix | None,
    next_fix: TripFix | None,
    profiles: DriverProfiles,
    known_points: dict[str, tuple[float, float]],
) -> tuple[DriverMatch, ...]:
    """Return every driver/pattern this trip plausibly matches.

    A requires_parking condition is about the *other* leg of the same
    round trip - whether the car was left parked at the far end.
    Concretely: if this trip runs home->place, the relevant parking
    signal is this trip's *own* has_parking_footage (its own tail is
    the stop at `place`); if this trip runs place->home, it's
    `prev_fix`'s has_parking_footage (the outbound leg, home->place,
    is what stopped at `place` - the arrival leg's own tail is the
    evidence, not this return leg's). That's why match_driver() takes
    the chronologically adjacent trips' fixes as well as this one's -
    Trip itself has no "next scheduled leg" concept (see trip/trip.py),
    so the caller (iterating trips in order, as bv-ls --trips already
    does) is what supplies them.

    When a requires_parking condition can't be checked at all (a
    place->home leg with no `prev_fix`), the pattern still matches but
    at reduced confidence with a reason noting parking status is
    unverified, rather than being silently dropped - a false "no
    match" is worse here than a lower-confidence "maybe" the caller
    can still surface.
    """

    home = known_points.get("home")
    matches: list[DriverMatch] = []

    for driver in profiles.drivers:
        for pattern in driver.patterns:
            if pattern.kind == "short_local":
                if not (
                    _near_home(trip_fix.start, known_points, profiles)
                    and _near_home(trip_fix.end, known_points, profiles)
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

            home_to_place = _near_home(
                trip_fix.start, known_points, profiles
            ) and _near(trip_fix.end, place_point, pattern.radius_meters)
            place_to_home = _near(
                trip_fix.start, place_point, pattern.radius_meters
            ) and _near_home(trip_fix.end, known_points, profiles)
            # A round trip - both ends near home, nowhere near `place`
            # by trip_fix.start/end alone - can still be a real
            # home<->place commute if the vehicle actually reached
            # `place` somewhere in the middle before returning (see
            # TripFix.via_point's own docstring: Christer's morning
            # drop-off at Heliosgatan is exactly this shape). Checked
            # as its own separate condition, not folded into
            # home_to_place/place_to_home above, so an ordinary one-way
            # trip's matching is completely unaffected by via_point
            # even existing.
            via_round_trip = (
                trip_fix.via_point is not None
                and _near_home(trip_fix.start, known_points, profiles)
                and _near_home(trip_fix.end, known_points, profiles)
                and _near(trip_fix.via_point, place_point, pattern.radius_meters)
            )

            if not home_to_place and not place_to_home and not via_round_trip:
                continue

            parking_signal: bool | None = None
            parking_verified = False
            if pattern.requires_parking is not None:
                if home_to_place:
                    # This trip's own tail is the stop at `place`.
                    parking_signal = trip_fix.has_parking_footage
                    parking_verified = True
                elif place_to_home and prev_fix is not None:
                    # The outbound leg (home->place) is what stopped at
                    # `place` - its own tail is the evidence, not this
                    # return leg's.
                    parking_signal = prev_fix.has_parking_footage
                    parking_verified = True
                # via_round_trip: there's no adjacent trip to check -
                # it's the same single trip there and back, with no
                # real "stop" at `place` at all (the vehicle never
                # parked, just turned around) - parking_verified stays
                # False, same as an unresolvable place_to_home leg
                # below, rather than guessing from trip_fix's own tail
                # (which is the arrival back home, not evidence about
                # `place`).

                if parking_verified and parking_signal != pattern.requires_parking:
                    continue

            confidence = 0.6
            if via_round_trip and not home_to_place and not place_to_home:
                reason = f"home -> {pattern.place} -> home (round trip)"
            else:
                reason = f"{'home -> ' + pattern.place if home_to_place else pattern.place + ' -> home'}"
            if pattern.requires_parking is not None:
                if parking_verified:
                    confidence = 0.9
                    reason += ", parked" if parking_signal else ", no parking file"
                else:
                    confidence = 0.4
                    reason += " (parking status unverified)"
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
