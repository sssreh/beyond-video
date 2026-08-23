"""
Aggregated statistics reporting over an archive's RECORDING_STATS
assets (the per-recording <id>.stats.json files generate/stats.py's
compute_recording_stats() writes) - the library half of bv-stats
(see cli/bv_stats.py for the CLI wrapper).

Deliberately its own top-level module, not folded into generate/
stats.py (which *computes* one recording's stats) or cli/bv_stats.py
itself - Christer's own request for this feature was explicit that
it's "preparing for a stats tab in bv-web, with a summary and a nice
graph and clickable points for actual data and more", so the
aggregation logic has to be importable by bv-web directly (a Python
call, not a subprocess screen-scrape of the CLI's text output) the
same way search.py already is by both bv-search and bv-web's own
/archive routes. Nothing in this module touches argparse, `say`/
`warn`, or any other CLI-only concern - see bv_search.py's own module
docstring for the precedent this follows.

Copyright (C) 2026 Christer R. (sssreh)

SPDX-License-Identifier: GPL-3.0-or-later
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from datetime import datetime
from datetime import timedelta

from .archive.asset import Asset
from .archive.recording import Recording
from .archive.recording_id import RecordingId

# Mean Earth radius in meters - the same well-known value export/
# trip_stats.py's own _EARTH_RADIUS_METERS uses, duplicated rather than
# imported (that one's module-private by convention, and this module's
# own docstring is explicit about staying import-light - no `..export`
# dependency, so bv-web can import this module directly without pulling
# in export/'s own, much heavier dependency chain).
_EARTH_RADIUS_METERS = 6_371_000.0

# _boundary_bridge_km()'s own cutoff for "close enough in time to be
# the same GPS dropout, not two unrelated recordings" - reuses trip/
# trip_builder.py's own DEFAULT_MAX_GAP (5 minutes) *value*, not the
# import itself (this module deliberately doesn't reach into ..trip,
# same reasoning as _EARTH_RADIUS_METERS above - see
# _boundary_bridge_km()'s own docstring for why this threshold matters
# at all).
_MAX_BOUNDARY_BRIDGE_GAP_SECONDS = 300.0

# Every RECORDING_STATS field worth reporting on, each tagged with how
# to combine several recordings' own values of it into one bucket's
# figure - see StatBucket/aggregate_recording_stats() below for how
# each kind is actually combined. Deliberately excludes the handful of
# generate/stats.py fields that aren't a single reportable number at
# all (has_gps, start_gps, end_gps) - those describe one recording,
# not something that sums or averages across many.
#
# "sum" - total across every recording in the bucket (distance driven,
# time spent - each recording's own contributes additively to the
# bucket's whole).
# "avg" - the mean of each recording's own average (avg_speed_kmh/
# avg_gforce_* are already themselves a per-recording mean, so
# further averaging those together - rather than re-deriving a
# true trip-wide mean, which compute_recording_stats() doesn't
# keep the raw samples to recompute here anyway - is the same
# "average of averages" simplification a dashboard summary
# figure is expected to be, not a rigorous weighted mean).
# "max" - the largest single reading across every recording in the
# bucket (a peak speed or g-force is only meaningful as a peak,
# summing or averaging it away would hide the actual highlight).
# "min" - the smallest single reading (min_altitude_m only).
#
# elevation_change_m is a plain "sum" like distance_km/duration_seconds -
# each recording's own elevation_change_m is itself already a spike
# -resistant net-change figure (see export/trip_stats.py's
# _hysteresis_altitude_stats() docstring), so summing those across a
# bucket's recordings gives the bucket's own real net change, the
# same way summing per-recording distance gives total distance (a
# bucket spanning several recordings that each climbed a bit and each
# descended a bit nets those against each other, the same "ups and
# downs cancel" behavior a single multi-recording trip already has
# internally). This module briefly derived this field from the
# bucket's own raw max_altitude_m/min_altitude_m readings instead (a
# bucket-wide range, not a sum) - see aggregate_recording_stats()'s own
# docstring for why that detour happened and why it was reverted
# (Christer, 2026-08-23: "How is that a gain?"). The field itself was
# later renamed from elevation_gain_m to elevation_change_m and its
# math redefined from a cumulative-ascent total to a true net change,
# per Christer's own "and rename itto Elevation change" followed by
# "Redefine to true net change" - see trip_stats.py's own top-of-file
# comments for that full history.
@dataclass(frozen=True)
class StatField:
    """One reportable RECORDING_STATS field - its JSON key, a short
    display label, its unit, and how several recordings' own values
    combine into one bucket's aggregate (see STAT_FIELDS' own comment
    above for what each aggregate kind means)."""

    key: str
    label: str
    unit: str
    aggregate: str


STAT_FIELDS: dict[str, StatField] = {
    field.key: field
    for field in (
        StatField("duration_seconds", "Duration", "s", "sum"),
        StatField("distance_km", "Distance", "km", "sum"),
        StatField("moving_seconds", "Moving time", "s", "sum"),
        StatField("idle_seconds", "Idle time", "s", "sum"),
        StatField("avg_speed_kmh", "Avg speed", "km/h", "avg"),
        StatField("max_speed_kmh", "Max speed", "km/h", "max"),
        StatField("min_altitude_m", "Min altitude", "m", "min"),
        StatField("max_altitude_m", "Max altitude", "m", "max"),
        StatField("elevation_change_m", "Elevation change", "m", "sum"),
        StatField("max_gforce_x", "Max g-force X", "g", "max"),
        StatField("avg_gforce_x", "Avg g-force X", "g", "avg"),
        StatField("max_gforce_y", "Max g-force Y", "g", "max"),
        StatField("avg_gforce_y", "Avg g-force Y", "g", "avg"),
        StatField("max_gforce_z", "Max g-force Z", "g", "max"),
        StatField("avg_gforce_z", "Avg g-force Z", "g", "avg"),
    )
}

# Which STAT_FIELDS keys can only ever have a real reading when a
# recording had at least two positioned GPS fixes (RECORDING_STATS'
# own "has_gps" flag - see generate/stats.py's compute_recording_stats()
# docstring) - everything compute_trip_stats() derives from the GPS
# track: distance, both speeds, moving/idle time (which need a speed
# series to integrate), and all three altitude fields. duration_seconds
# is deliberately excluded even though .gps is the last resort in its
# own fallback chain (video span, then .3gf, then .gps) - most
# recordings' duration comes from one of the first two, so has_gps
# being False doesn't reliably predict a missing duration the way it
# does for the fields above. Every g-force field is also excluded -
# those come from the .3gf g-sensor sidecar, entirely independent of
# GPS. See count_recordings_without_gps()'s own docstring for what
# this set is actually for.
GPS_DEPENDENT_FIELDS: frozenset[str] = frozenset((
    "distance_km",
    "avg_speed_kmh",
    "max_speed_kmh",
    "moving_seconds",
    "idle_seconds",
    "min_altitude_m",
    "max_altitude_m",
    "elevation_change_m",
))

# bv-stats' own default --fields, printed when the flag is omitted -
# the handful most people actually want a trip/period summary of
# (duration, distance, speed, elevation), leaving the rarer g-force
# breakdown as something --fields has to be told to include.
DEFAULT_FIELDS: tuple[str, ...] = (
    "duration_seconds",
    "distance_km",
    "avg_speed_kmh",
    "max_speed_kmh",
    "elevation_change_m",
)

# Every grouping bv-stats understands, in the order Christer's own
# request listed them - see _bucket_key()'s own docstring for exactly
# what each one means.
GROUPINGS: tuple[str, ...] = (
    "all", "recording", "year", "month", "date", "week", "weekday", "monthday",
)

_WEEKDAY_ORDER = (
    "Monday", "Tuesday", "Wednesday", "Thursday",
    "Friday", "Saturday", "Sunday",
)


def load_recording_stats(recording: Recording) -> dict | None:
    """Read one recording's own RECORDING_STATS asset (its
    <id>.stats.json), or None if it doesn't have one, or the file on
    disk is missing/unreadable/not valid JSON.

    No existing shared helper does this - cli/bv_generate.py's own
    _do_stats() constructs the path itself and reads it directly
    because it's the only writer, but bv-stats is purely a reader,
    so this is that reader's one shared entry point (bv-web can reuse
    it directly later rather than re-deriving the path itself).

    Deliberately forgiving rather than raising: an archive scanned
    with --stats over some, but not all, of its recordings (or one
    with a stats.json a hand edit or partial write left corrupted) is
    an entirely expected, unremarkable state to see a stats report
    run against - see bv_stats.py's own _run() for how a missing/
    unreadable file is surfaced (skipped, optionally counted for
    --trace) rather than aborting the whole report.
    """

    asset_file = recording.file(Asset.RECORDING_STATS)
    if asset_file is None:
        return None

    try:
        return json.loads(asset_file.path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def count_recordings_without_gps(
    entries: list[tuple[RecordingId, dict]],
) -> int:
    """How many of `entries` (already-loaded RECORDING_STATS dicts -
    see load_recording_stats()) have no GPS fix at all (has_gps is
    False or missing), and therefore contribute nothing to any
    GPS_DEPENDENT_FIELDS aggregate no matter how many recordings a
    report's totals otherwise look like they cover.

    This exists because "N of M recording(s) in range have no Stats
    asset yet, skipped" (bv_stats.py's own message for a recording
    with no <id>.stats.json at all) only catches one kind of gap - a
    recording can have a perfectly real, present RECORDING_STATS file
    and still contribute nothing to distance_km/avg_speed_kmh/etc. if
    it simply never got a usable GPS fix (cold-start acquisition delay,
    a tunnel/parking-structure gap, or a Parking-mode clip recorded
    stationary somewhere with no signal at all - see the elevation-gain
    GPS-noise fix and the g-sensor-baseline note elsewhere in
    WORKING_CONTEXT.md for other examples of real, not corrupted,
    GPS-availability gaps). Christer's own real archive is a concrete
    case: `bv-generate --stats` had written a Stats asset for
    essentially every recording, yet only a majority-but-not-all of
    them actually had a `.gps` sidecar downloaded at all (`--mode`
    controls which recording *kinds* bv-download fetches, not video
    vs. sidecar separately, but a kind can still be download-complete
    with no GPS fix inside its .gps file, or with a .gps file that
    never resolved to two positioned fixes) - a whole-selection total
    that looked "a little short" wasn't a bug in the aggregation math,
    it was this gap being invisible in the report.
    """

    return sum(1 for _, stats in entries if not stats.get("has_gps"))


def _bucket_key(timestamp: datetime, grouping: str) -> str:
    """The bucket key one recording's own start timestamp falls into
    for `grouping` - see GROUPINGS' own comment for the full list.

    Never called for grouping == "recording" - aggregate_recording_stats()
    handles that one directly (each recording's own RecordingId *is*
    its bucket key, not something derived from its timestamp the way
    every calendar grouping below is), see that function's own
    docstring.

    "all" - a single bucket for the whole selection (an archive-wide,
    or --timestamp/--from/--until-range-wide, summary).
    "year" - one bucket per calendar year ("2026").
    "month" - one bucket per calendar year+month ("2026-08").
    "date" - one bucket per exact calendar date ("2026-08-23") - the
    finest-grained of the three date-hierarchy groupings above it,
    e.g. for a day-by-day trend line. (Named "date", not "monthday" -
    see the "monthday" entry below for why the two names swapped from
    an earlier version of this grouping set.)
    "week" - one bucket per ISO 8601 week ("2026-W34") - Monday-
    Sunday, the same definition Python's own date.isocalendar() uses,
    rather than a rolling 7-day window, so "this week" lines up with
    what a calendar app would call the same week.
    "weekday" - one bucket per day-of-week name ("Monday".."Sunday"),
    recurring across every date in the selection rather than tied to
    any one of them - answers "which day of the week do I drive most
    on", a genuinely different question from any of the four groupings
    above (which all partition the selection into disjoint spans of
    time; this one instead re-cuts the *same* whole selection by a
    repeating pattern within it).
    "monthday" - one bucket per day-of-month number ("01".."31"),
    recurring the same way "weekday" does (not to be confused with
    "date" above, which partitions into one bucket per exact date) -
    answers "which day of the month do I drive most on" / surfaces a
    single low-mileage day inside an otherwise normal month, the kind
    of thing a whole-month total averages away. Christer's own
    framing, after a first "which months have low mileage" pass
    turned out to mean this instead: "an x axis with 31 positions ...
    think like weekdays with 7 positions." Some months have fewer
    than 31 days, so buckets 29-31 will always have fewer contributing
    recordings than the rest - expected, not a bug. Named "monthday"
    (not the "dayofmonth" this grouping originally shipped under) to
    actually match "weekday"'s own naming pattern - "weekday" recurs
    by day-of-week, "monthday" recurs by day-of-month, both read as
    [unit]+"day"; the exact-date grouping above had to move off
    "monthday" onto "date" to free the name up, since Christer's
    original ask for this grouping was itself phrased as "monthday"
    and the two being swapped was exactly what caused an earlier
    round of confusion ("I dont think you understand what i meant
    with monthday").
    """

    if grouping == "all":
        return "all"
    if grouping == "year":
        return f"{timestamp:%Y}"
    if grouping == "month":
        return f"{timestamp:%Y-%m}"
    if grouping == "date":
        return f"{timestamp:%Y-%m-%d}"
    if grouping == "week":
        iso_year, iso_week, _ = timestamp.isocalendar()
        return f"{iso_year}-W{iso_week:02d}"
    if grouping == "weekday":
        return _WEEKDAY_ORDER[timestamp.weekday()]
    if grouping == "monthday":
        return f"{timestamp.day:02d}"

    raise ValueError(f"unknown grouping: {grouping!r}")


def _sort_key(bucket_key: str, grouping: str):
    """Sort order for a grouping's own bucket keys.

    "year"/"month"/"monthday"/"week" are all zero-padded, lexically-
    sortable-as-chronological strings already (_bucket_key() built
    them that way on purpose), so the key itself sorts correctly.
    "recording" is the same story for a different reason - its key is
    a RecordingId's own value string, already fixed-width and zero-
    padded by construction (see RecordingId's own docstring), not
    something _bucket_key() built at all.
    "weekday" is the one exception - "Monday" < "Tuesday" alphabetic-
    ally isn't Monday-first calendar order, so it's sorted by
    position in _WEEKDAY_ORDER instead. "all" only ever has the one
    bucket, so its sort key doesn't matter.
    """

    if grouping == "weekday":
        return _WEEKDAY_ORDER.index(bucket_key)
    return bucket_key


@dataclass(frozen=True)
class StatBucket:
    """One grouped-by period's aggregated stats.

    `recordings` is every recording's own RecordingId that landed in
    this bucket, in chronological order - kept (not just the
    aggregate numbers) specifically for Christer's "clickable points
    for actual data" requirement: a future bv-web stats-tab graph
    point for this bucket can link straight to the recordings that
    produced it, the same way a chart drill-down would.

    `values` holds one aggregated float per requested StatField key,
    or None if not a single recording contributing to this bucket had
    a real (non-None) reading for that field at all - see
    aggregate_recording_stats()'s own docstring for how each
    StatField.aggregate kind combines multiple readings, and why a
    bucket with recordings but no readings for a field is None rather
    than 0.0 (a real zero and "nothing to measure" must stay
    distinguishable, the same convention TripStats' own altitude
    fields already use - see export/trip_stats.py).

    `estimated_distance_km`/`estimated_recording_count` are only ever
    non-default when `aggregate_recording_stats()` was called with
    `estimate_gaps=True` and this bucket actually needed to fill a
    gap - see that function's own docstring for what "estimated" means
    here and why it's kept as a separate, visible figure rather than
    silently folded into `values["distance_km"]` with no trace. Both
    default to None/0 (no estimation happened, or happened but found
    nothing to fill) so every existing caller/test constructing a
    StatBucket without these two fields keeps working unchanged.

    `bridged_distance_km`/`bridged_recording_count` are the same idea
    for a different gap: distance recovered by _boundary_bridge_km()'s
    straight-line bridge between one recording's own last real GPS fix
    and the next recording's own first one, when a GPS dropout (a
    tunnel, a parking garage) straddles the boundary between two
    recordings rather than sitting entirely inside one - see
    aggregate_recording_stats()'s own docstring and
    _boundary_bridge_km()'s for the full "based off time, how much of
    the distance belongs to each recording id" design (Christer,
    2026-08-23). Unlike estimate_gaps, this always runs (it's a real
    haversine between two real GPS positions, the same technique
    export/trip_stats.py's own compute_trip_stats() already applies
    unconditionally for gaps *inside* one recording or one merged trip -
    this just extends the same idea across the one boundary that
    technique can't see past) - kept as its own visible figure anyway,
    for the same "never silently blend measured and bridged numbers"
    reason estimated_distance_km already is. None/0 when nothing in
    this bucket received a bridge contribution.
    """

    key: str
    recordings: tuple[RecordingId, ...]
    values: dict[str, float | None]
    estimated_distance_km: float | None = None
    estimated_recording_count: int = 0
    bridged_distance_km: float | None = None
    bridged_recording_count: int = 0


def _speed_basis_kmh(entries: list[tuple[RecordingId, dict]]) -> float | None:
    """Weighted average speed (km/h) - total real distance over total
    real duration - from whichever of `entries` have *both* a
    distance_km and a duration_seconds reading. This is the basis
    aggregate_recording_stats()'s `estimate_gaps` extrapolates a
    no-GPS recording's own distance from (that recording's own
    duration_seconds * this basis), not a plain mean of each
    recording's own avg_speed_kmh - a duration-weighted figure isn't
    skewed by many short recordings the way an unweighted mean of
    per-recording averages would be. None if nothing in `entries` has
    both readings at all - nothing to extrapolate from.
    """

    total_km = 0.0
    total_hours = 0.0
    for _, stats in entries:
        distance = stats.get("distance_km")
        duration = stats.get("duration_seconds")
        if distance is not None and duration:
            total_km += distance
            total_hours += duration / 3600.0

    if total_hours <= 0:
        return None
    return total_km / total_hours


def _haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance in km between two lat/lon points - the
    same formula export/trip_stats.py's own _haversine_distance_meters()
    uses (straight-line, not road-following - see that function's own
    docstring for why the difference is negligible for adjacent GPS
    fixes, and _boundary_bridge_km()'s own docstring for why it's a
    reasonable approximation here too, over a longer span)."""

    lat1_rad, lat2_rad = math.radians(lat1), math.radians(lat2)
    delta_lat = math.radians(lat2 - lat1)
    delta_lon = math.radians(lon2 - lon1)
    a = (
        math.sin(delta_lat / 2) ** 2
        + math.cos(lat1_rad) * math.cos(lat2_rad) * math.sin(delta_lon / 2) ** 2
    )
    return 2 * _EARTH_RADIUS_METERS * math.asin(math.sqrt(a)) / 1000.0


def _gps_point(gps: dict | None) -> tuple[float, float, datetime] | None:
    """Parse one recording's own start_gps/end_gps dict (see
    generate/stats.py's compute_recording_stats()) into (lat, lon,
    time), or None if `gps` is None (no positioned fix at all) or -
    the one thing that can't be assumed for every already-on-disk
    .stats.json - missing the "time" key a pre-2026-08-23 file won't
    have yet (self-heals the next time `bv-generate --stats` runs over
    that recording, see _do_stats()'s own read-merge-write docstring;
    until then, _boundary_bridge_km() just can't bridge using that
    recording's own boundary, the same "nothing to work with" fallback
    as no GPS at all)."""

    if gps is None:
        return None
    time_str = gps.get("time")
    if time_str is None:
        return None
    try:
        return gps["lat"], gps["lon"], datetime.fromisoformat(time_str)
    except (KeyError, TypeError, ValueError):
        return None


def _boundary_bridge_km(
    entries: list[tuple[RecordingId, dict]],
) -> dict[RecordingId, float]:
    """How much extra distance to credit to each recording in `entries`
    from bridging a GPS dropout that straddles the boundary between it
    and the chronologically-*next* recording - Christer's own follow-up
    once he'd confirmed the same technique already recovers distance
    for a dropout entirely *inside* one recording (or one merged trip -
    see export/trip_stats.py's compute_trip_stats()): "our stats file
    does not contain first and last gps position, that could help a
    little if you have a previous recording and a next recording gps
    position, yes i know, but a straight line is better than nothing"
    then, once start_gps/end_gps turned out to already be there just
    missing a timestamp: "based of time can you see how much of the
    distance belong to each recording id".

    For each adjacent pair (A, then B) in `entries` sorted chronologic-
    ally: if A's own end_gps and B's own start_gps are both present
    (see _gps_point()) and no more than
    _MAX_BOUNDARY_BRIDGE_GAP_SECONDS (5 minutes, the same value trip/
    trip_builder.py's own DEFAULT_MAX_GAP already uses to decide "same
    trip or not" - a dropout inside one continuous drive should be well
    under that, and not bridging a longer gap avoids inventing a
    straight-line "drive" across what's actually two unrelated trips,
    e.g. a multi-hour parked gap between them) apart in time, a single
    straight-line distance is computed between those two points -
    exactly export/trip_stats.py's own gap-bridging technique, just
    applied across the one boundary that per-recording computation
    can't see past.

    That one distance is then split between A and B by *time*, not
    evenly: how much of the total gap falls before A's own video
    actually ends (duration_seconds past A's own start) versus after
    B's own video actually starts. A recording whose own GPS dropped
    out well before its video stopped recording (a tunnel entered with
    a while still left on the clip) gets more of the bridge than one
    whose GPS was still live until nearly the end. Any portion of the
    gap that falls *between* the two videos themselves (neither was
    actually recording, e.g. a brief file-rotation pause) is left
    unattributed to either - there's no recording there to credit it
    to, the same "no coverage, nothing counted" honesty gaps inside one
    recording already get.

    Parking-mode recordings (RecordingId.is_parking) are never bridged
    from or to - they're stationary by definition (triggered by g
    -sensor motion while parked), so a straight line to/from one would
    invent movement that never happened, the same reason estimate_gaps
    already excludes them from its own extrapolation.

    Returns a dict of only the recordings that actually received a
    (non-zero) bridge contribution - most entries won't appear in it at
    all, the same "sparse, not every key present" convention every
    other per-recording lookup in this module uses.
    """

    bonus: dict[RecordingId, float] = {}

    ordered = sorted(entries, key=lambda entry: entry[0])
    for (id_a, stats_a), (id_b, stats_b) in zip(ordered, ordered[1:]):
        if id_a.is_parking or id_b.is_parking:
            continue

        point_a = _gps_point(stats_a.get("end_gps"))
        point_b = _gps_point(stats_b.get("start_gps"))
        if point_a is None or point_b is None:
            continue

        lat_a, lon_a, time_a = point_a
        lat_b, lon_b, time_b = point_b

        total_gap_seconds = (time_b - time_a).total_seconds()
        if total_gap_seconds <= 0 or total_gap_seconds > _MAX_BOUNDARY_BRIDGE_GAP_SECONDS:
            continue

        duration_a = stats_a.get("duration_seconds")
        if duration_a is None:
            continue
        video_end_a = id_a.timestamp + timedelta(seconds=duration_a)

        tail_seconds = max(0.0, (video_end_a - time_a).total_seconds())
        head_seconds = max(0.0, (time_b - id_b.timestamp).total_seconds())
        if tail_seconds <= 0 and head_seconds <= 0:
            continue

        bridge_km = _haversine_km(lat_a, lon_a, lat_b, lon_b)
        if bridge_km <= 0:
            continue

        a_share = bridge_km * min(1.0, tail_seconds / total_gap_seconds)
        b_share = bridge_km * min(1.0, head_seconds / total_gap_seconds)

        if a_share > 0:
            bonus[id_a] = bonus.get(id_a, 0.0) + a_share
        if b_share > 0:
            bonus[id_b] = bonus.get(id_b, 0.0) + b_share

    return bonus


def aggregate_recording_stats(
    entries: list[tuple[RecordingId, dict]],
    *,
    grouping: str,
    fields: list[str],
    estimate_gaps: bool = False,
) -> list[StatBucket]:
    """Group `entries` (each recording's id paired with its already-
    loaded RECORDING_STATS dict - see load_recording_stats()) by
    `grouping` (one of GROUPINGS) and aggregate `fields` (each a
    STAT_FIELDS key) within each resulting bucket.

    Buckets are returned in the grouping's own natural chronological
    order (see _sort_key()), not insertion order - a --group month
    report should read Jan, Feb, Mar..., regardless of which month's
    recordings happened to appear first in `entries`.

    `grouping="recording"` is the one non-calendar grouping: every
    recording gets its own single-recording bucket instead of being
    combined with any others at all - Christer's own request, once he
    had a working grouped chart, for "the graph a recording id at a
    time for non-grouped graphs": every other grouping answers "how
    does this add up over some period," this one instead plots the
    raw, ungrouped per-recording data point by point, which the
    existing bucket/chart machinery already supports for free once a
    bucket can hold exactly one recording - no separate code path
    needed in bv_stats.py or the web dashboard, both already render
    whatever StatBucket.key/values a grouping happens to produce.
    Chronological order still holds (RecordingId's own zero-padded
    string sorts correctly), and every aggregate kind still runs the
    same way it would for a bigger bucket - "sum"/"avg"/"max"/"min"
    over a single recording's own one reading is just that reading
    itself, elevation_change_m included. Meant for a
    narrow selection (a day, a trip, a week) - --timestamp/--from/
    --until controls how many points this produces the same way it
    does for every other grouping; nothing here caps it, so a
    --group recording run over a whole archive plots one point per
    recording, which is exactly as much data as was asked for, just
    not necessarily a readable chart.

    Each field's multiple per-recording readings combine according to
    its own STAT_FIELDS[...].aggregate: "sum"/"avg"/"max"/"min" over
    every *present* (non-None) reading among the bucket's recordings -
    a recording missing a field entirely (no GPS, no g-sensor, or a
    duration-only stats.json from before a later bv-generate --stats
    run added a field this report now asks for) simply doesn't
    contribute to that field's aggregate, rather than being treated as
    a zero or excluding the whole recording from every other field.
    A bucket where *no* recording has a present reading for a field
    at all reports None for it - see StatBucket's own docstring for
    why that's kept distinct from a genuine 0.0.

    elevation_change_m (still named elevation_gain_m at the time)
    briefly had its own bucket-scope exception here ("range": max of
    every recording's own max_altitude_m, minus min of every
    recording's own min_altitude_m, rather than summing each
    recording's own reading) - Christer, after a long-range "all"
    summary reported 39302m of elevation gain, "what goes up must come
    down", pointed out that summing each recording's own already
    -computed value (at the time, a *net* max-min span per recording)
    across potentially thousands of short recordings measures
    cumulative churn, not net elevation change, and grows without
    bound the more there is to aggregate - that fix was correct for
    what the field meant back then.

    export/trip_stats.py's own definition has since gone through two
    more revisions. First, back to a genuine cumulative-ascent total,
    with the outlier rejection the original definition never had (see
    _hysteresis_altitude_stats()'s own docstring) - Christer's "How is
    that a gain?" (2026-08-23) pointed out a net span can't tell a real
    climb-then-descend trip apart from one that never climbed at all,
    which isn't what "gain" means. Summing that per-recording figure
    across a bucket was the *correct* combine for it, not the same
    unbounded-churn mistake as before: each recording's own reading was
    already "how much this recording climbed", so summing many of them
    was exactly "how much climbing happened across the whole bucket" -
    the same relationship distance_km's own per-recording readings have
    to a bucket's total distance.

    Then, same day, once Christer asked to rename the label itself to
    "Elevation change" ("and rename itto Elevation change"): a plain
    rename would have left a label implying negative values possible
    paired with math that could never produce one, so the field was
    renamed elevation_gain_m -> elevation_change_m *and* redefined to
    a true net change (final dead-banded altitude minus starting
    dead-banded altitude - can be positive, negative, or zero), per
    Christer's explicit "Redefine to true net change." Summing each
    recording's own net change across a bucket is still the correct
    "sum" combine - a multi-recording bucket's total net change is the
    sum of each leg's own net change, the same additive relationship
    every other "sum" field in this table has, just no longer
    guaranteed non-negative. A large positive or negative total over a
    long selection is real net elevation change (a mountain trip that
    ended lower than it started, or higher), not a bug - unlike
    Strava/Garmin's own "elevation gain" figures (which only ever grow,
    by design), this field can now shrink back toward, or past, zero
    exactly when a bucket's driving did.

    `estimate_gaps`, when True and `distance_km` is one of `fields`,
    fills in an *estimated* distance for every recording in a bucket
    that has no real distance_km reading (no GPS fix at all, or -
    the rarer one-fix edge case - too few fixes for compute_trip_stats()
    to derive anything) but does have a duration_seconds reading and
    isn't a Parking-mode recording (see RecordingId.is_parking -
    Parking clips are triggered while stationary, so extrapolating a
    moving average speed onto one would invent distance that was never
    driven, not fill a real gap). Each such recording's own duration is
    multiplied by a speed basis (see _speed_basis_kmh() above) - that
    bucket's own real distance/duration ratio if it has any, otherwise
    the whole selection's - and summed into `values["distance_km"]`
    alongside the real readings, with the estimated portion and the
    count of recordings it came from kept separately on
    `StatBucket.estimated_distance_km`/`estimated_recording_count` so
    a caller can always show what was measured versus inferred, never
    silently blend them with no trace. A bucket where nothing anywhere
    in the whole selection has both readings (nothing to build a speed
    basis from at all) is left exactly as it would be without
    `estimate_gaps` - there's nothing to extrapolate from.

    Distance recovered across a recording-boundary GPS dropout (see
    _boundary_bridge_km()'s own docstring) always runs, unlike
    `estimate_gaps` - it's a real straight-line distance between two
    real GPS fixes, the same technique export/trip_stats.py's own
    compute_trip_stats() already applies unconditionally for gaps
    inside one recording, just extended across the one boundary that
    per-recording computation can't see past, not a speed-based
    extrapolation. It's still kept as its own visible figure
    (`StatBucket.bridged_distance_km`/`bridged_recording_count`) rather
    than folded silently into `values["distance_km"]`, for the same
    "never blend measured and inferred with no trace" reason
    `estimated_distance_km` already is. A recording that received a
    boundary-bridge contribution is excluded from `estimate_gaps`' own
    "missing" list for the same bucket - the one case where both could
    otherwise apply to the very same recording is a single-fix
    recording sitting right at a boundary (has a real start_gps/
    end_gps, but too few fixes for compute_trip_stats() to derive a
    distance_km at all), and the boundary bridge's real, GPS-anchored
    figure is preferable to a cruder whole-duration speed-basis guess
    for it.
    """

    buckets: dict[str, list[tuple[RecordingId, dict]]] = {}
    for recording_id, stats in entries:
        if grouping == "recording":
            # One bucket per recording, key'd by the recording's own
            # id (unique, and already a zero-padded fixed-width string
            # that sorts chronologically - see RecordingId's own
            # docstring) - not derived from its timestamp via
            # _bucket_key() the way every calendar grouping is.
            key = str(recording_id)
        else:
            key = _bucket_key(recording_id.timestamp, grouping)
        buckets.setdefault(key, []).append((recording_id, stats))

    global_basis = _speed_basis_kmh(entries) if estimate_gaps else None
    boundary_bonus_km = (
        _boundary_bridge_km(entries) if "distance_km" in fields else {}
    )

    result = []
    for key in sorted(buckets, key=lambda k: _sort_key(k, grouping)):
        bucket_entries = buckets[key]
        recording_ids = tuple(
            recording_id for recording_id, _ in bucket_entries
        )

        values: dict[str, float | None] = {}
        for field_key in fields:
            field = STAT_FIELDS[field_key]

            readings = [
                stats[field_key]
                for _, stats in bucket_entries
                if stats.get(field_key) is not None
            ]
            if not readings:
                values[field_key] = None
            elif field.aggregate == "sum":
                values[field_key] = sum(readings)
            elif field.aggregate == "avg":
                values[field_key] = sum(readings) / len(readings)
            elif field.aggregate == "max":
                values[field_key] = max(readings)
            elif field.aggregate == "min":
                values[field_key] = min(readings)
            else:
                raise ValueError(
                    f"unknown aggregate kind: {field.aggregate!r}"
                )

        estimated_distance_km: float | None = None
        estimated_recording_count = 0
        if estimate_gaps and "distance_km" in fields:
            basis = _speed_basis_kmh(bucket_entries)
            if basis is None:
                basis = global_basis
            if basis is not None:
                missing = [
                    (recording_id, stats)
                    for recording_id, stats in bucket_entries
                    if stats.get("distance_km") is None
                    and stats.get("duration_seconds")
                    and not recording_id.is_parking
                    and boundary_bonus_km.get(recording_id, 0.0) <= 0
                ]
                if missing:
                    estimated_distance_km = sum(
                        stats["duration_seconds"] / 3600.0 * basis
                        for _, stats in missing
                    )
                    estimated_recording_count = len(missing)
                    values["distance_km"] = (
                        values["distance_km"] or 0.0
                    ) + estimated_distance_km

        bridged_distance_km: float | None = None
        bridged_recording_count = 0
        if boundary_bonus_km:
            contributing = [
                boundary_bonus_km[recording_id]
                for recording_id in recording_ids
                if boundary_bonus_km.get(recording_id, 0.0) > 0
            ]
            if contributing:
                bridged_distance_km = sum(contributing)
                bridged_recording_count = len(contributing)
                values["distance_km"] = (
                    values["distance_km"] or 0.0
                ) + bridged_distance_km

        result.append(
            StatBucket(
                key=key,
                recordings=recording_ids,
                values=values,
                estimated_distance_km=estimated_distance_km,
                estimated_recording_count=estimated_recording_count,
                bridged_distance_km=bridged_distance_km,
                bridged_recording_count=bridged_recording_count,
            )
        )

    return result
