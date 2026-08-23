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
from dataclasses import dataclass
from datetime import datetime

from .archive.asset import Asset
from .archive.recording import Recording
from .archive.recording_id import RecordingId

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
# "range" - elevation_gain_m only (see its own STAT_FIELDS entry
# below): the bucket's own highest max_altitude_m minus its own
# lowest min_altitude_m, computed straight from those two raw fields
# rather than by combining each recording's own already-computed
# elevation_gain_m readings - see aggregate_recording_stats()'s "range"
# branch for why summing those instead would grow without bound.
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
        StatField("elevation_gain_m", "Elevation gain", "m", "range"),
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
    "elevation_gain_m",
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
    "elevation_gain_m",
)

# Every grouping bv-stats understands, in the order Christer's own
# request listed them - see _bucket_key()'s own docstring for exactly
# what each one means.
GROUPINGS: tuple[str, ...] = (
    "all", "year", "month", "date", "week", "weekday", "monthday",
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
    """

    key: str
    recordings: tuple[RecordingId, ...]
    values: dict[str, float | None]
    estimated_distance_km: float | None = None
    estimated_recording_count: int = 0


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

    "range" (elevation_gain_m only) is the one exception to "combine
    each recording's own reading of this same field": Christer, after
    a long-range "all" summary reported 39302m of elevation gain -
    "what goes up must come down" - pointed out that summing each
    recording's own already-computed max-min gain across potentially
    thousands of short recordings measures cumulative churn, not net
    elevation change, and grows without bound the more there is to
    aggregate. elevation_gain_m is instead derived fresh at bucket
    scope from the bucket's own max_altitude_m/min_altitude_m readings
    (max of every recording's own max_altitude_m, minus the min of
    every recording's own min_altitude_m) - the same "highest minus
    lowest" definition _hysteresis_altitude_stats() already uses per
    recording (see its own docstring), just widened to however many
    recordings the bucket actually spans, so it stays bounded by the
    real terrain range regardless of how much driving is in the
    selection.

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
    """

    buckets: dict[str, list[tuple[RecordingId, dict]]] = {}
    for recording_id, stats in entries:
        key = _bucket_key(recording_id.timestamp, grouping)
        buckets.setdefault(key, []).append((recording_id, stats))

    global_basis = _speed_basis_kmh(entries) if estimate_gaps else None

    result = []
    for key in sorted(buckets, key=lambda k: _sort_key(k, grouping)):
        bucket_entries = buckets[key]
        recording_ids = tuple(
            recording_id for recording_id, _ in bucket_entries
        )

        values: dict[str, float | None] = {}
        for field_key in fields:
            field = STAT_FIELDS[field_key]

            if field.aggregate == "range":
                # elevation_gain_m only - see this function's own
                # docstring for why it's derived from the bucket's own
                # max_altitude_m/min_altitude_m readings directly
                # rather than by combining each recording's own
                # already-computed elevation_gain_m (that would sum
                # unboundedly across many recordings instead of
                # reporting the bucket's real net elevation span).
                # Reads the two underlying fields straight off the raw
                # per-recording stats dicts, independent of whether
                # min_altitude_m/max_altitude_m were themselves also
                # requested in `fields`.
                max_readings = [
                    stats["max_altitude_m"]
                    for _, stats in bucket_entries
                    if stats.get("max_altitude_m") is not None
                ]
                min_readings = [
                    stats["min_altitude_m"]
                    for _, stats in bucket_entries
                    if stats.get("min_altitude_m") is not None
                ]
                if max_readings and min_readings:
                    values[field_key] = max(max_readings) - min(min_readings)
                else:
                    values[field_key] = None
                continue

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

        result.append(
            StatBucket(
                key=key,
                recordings=recording_ids,
                values=values,
                estimated_distance_km=estimated_distance_km,
                estimated_recording_count=estimated_recording_count,
            )
        )

    return result
