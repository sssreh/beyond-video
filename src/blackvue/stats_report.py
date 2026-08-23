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
# time spent, meters climbed - each recording's own contributes
# additively to the bucket's whole).
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
        StatField("elevation_gain_m", "Elevation gain", "m", "sum"),
        StatField("max_gforce_x", "Max g-force X", "g", "max"),
        StatField("avg_gforce_x", "Avg g-force X", "g", "avg"),
        StatField("max_gforce_y", "Max g-force Y", "g", "max"),
        StatField("avg_gforce_y", "Avg g-force Y", "g", "avg"),
        StatField("max_gforce_z", "Max g-force Z", "g", "max"),
        StatField("avg_gforce_z", "Avg g-force Z", "g", "avg"),
    )
}

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
    "all", "year", "month", "monthday", "week", "weekday",
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


def _bucket_key(timestamp: datetime, grouping: str) -> str:
    """The bucket key one recording's own start timestamp falls into
    for `grouping` - see GROUPINGS' own comment for the full list.

    "all" - a single bucket for the whole selection (an archive-wide,
    or --timestamp/--from/--until-range-wide, summary).
    "year" - one bucket per calendar year ("2026").
    "month" - one bucket per calendar year+month ("2026-08").
    "monthday" - one bucket per exact calendar date ("2026-08-23") -
    the finest-grained of the three date-hierarchy groupings above
    it, e.g. for a day-by-day trend line.
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
    """

    if grouping == "all":
        return "all"
    if grouping == "year":
        return f"{timestamp:%Y}"
    if grouping == "month":
        return f"{timestamp:%Y-%m}"
    if grouping == "monthday":
        return f"{timestamp:%Y-%m-%d}"
    if grouping == "week":
        iso_year, iso_week, _ = timestamp.isocalendar()
        return f"{iso_year}-W{iso_week:02d}"
    if grouping == "weekday":
        return _WEEKDAY_ORDER[timestamp.weekday()]

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
    """

    key: str
    recordings: tuple[RecordingId, ...]
    values: dict[str, float | None]


def aggregate_recording_stats(
    entries: list[tuple[RecordingId, dict]],
    *,
    grouping: str,
    fields: list[str],
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
    """

    buckets: dict[str, list[tuple[RecordingId, dict]]] = {}
    for recording_id, stats in entries:
        key = _bucket_key(recording_id.timestamp, grouping)
        buckets.setdefault(key, []).append((recording_id, stats))

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

        result.append(
            StatBucket(key=key, recordings=recording_ids, values=values)
        )

    return result
