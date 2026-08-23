"""
bv-stats.

Aggregate an archive's RECORDING_STATS assets (<id>.stats.json,
written by `bv-generate --stats`) into a summary report over a
timestamp range, grouped by calendar period. The same LexicalTimeParser-
based recording selection every other bv-* command uses, applied first
to narrow the candidate recordings before their stats are read and
combined - see stats_report.py's own module docstring for why the
grouping/aggregation logic itself lives there rather than here (so a
future bv-web stats tab can call it directly).

Copyright (C) 2026 Christer R. (sssreh)

SPDX-License-Identifier: GPL-3.0-or-later
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime
from datetime import timedelta
from pathlib import Path

from ..adapters.registry import get_adapter
from .errors import run_cli
from ..core.camera_config import DEFAULT_ADAPTER_ID
from ..core.camera_config import default_config_dir
from ..core.camera_config import resolve_archive_path
from ..core.joblog import wrap_say
from ..core.joblog import wrap_warn
from ..lexicaltimeparser import LexicalTimeParser
from ..stats_report import DEFAULT_FIELDS
from ..stats_report import GPS_DEPENDENT_FIELDS
from ..stats_report import GROUPINGS
from ..stats_report import STAT_FIELDS
from ..stats_report import StatBucket
from ..stats_report import aggregate_recording_stats
from ..stats_report import count_recordings_without_gps
from ..stats_report import load_recording_stats

EXIT_OK = 0
EXIT_ARGS_ERROR = 1

TRACE_INTERVAL_RECORDINGS = 25

# Fields whose unit is a duration ("s") - formatted as H:MM:SS rather
# than a bare second count, matching trip_info.txt's own
# `timedelta(seconds=round(...))` convention (export/trip_info.py) so
# a person reading both doesn't have to mentally convert between two
# different duration styles for the same underlying value.
_SECONDS_FIELDS = frozenset({"duration_seconds", "moving_seconds", "idle_seconds"})

# Precision for every non-duration field, keyed by unit - also matches
# trip_info.txt's own per-unit precision (km to 2dp, km/h to 1dp, m to
# whole meters). g-force's own physical unit isn't confirmed (see
# telemetry/gsensor_reader.py's own docstring) so 2dp is just a
# reasonable, not a calibrated, precision.
_UNIT_PRECISION = {"km": 2, "km/h": 1, "m": 0, "g": 2}


class DotProgress:
    """A --trace progress indicator - see bv_search.py's own
    DotProgress for the full reasoning, mirrored here unchanged."""

    def __init__(self, interval: int = TRACE_INTERVAL_RECORDINGS) -> None:
        self._interval = interval
        self._count = 0
        self._dots_printed = 0

    def tick(self) -> None:
        self._count += 1
        dots_due = self._count // self._interval

        while self._dots_printed < dots_due:
            print(".", end="", flush=True)
            self._dots_printed += 1

    def finish(self) -> None:
        if self._dots_printed:
            print()


def _parse_fields(value: str) -> list[str]:
    """argparse `type=` for --fields FIELD1,FIELD2,... (or "all") -
    validated against STAT_FIELDS here so a typo is reported as a
    normal argparse usage error rather than surfacing later as a
    silent empty column."""

    if value == "all":
        return list(STAT_FIELDS)

    fields = [item.strip() for item in value.split(",") if item.strip()]
    unknown = [field for field in fields if field not in STAT_FIELDS]
    if unknown:
        raise argparse.ArgumentTypeError(
            f"unknown field(s): {', '.join(unknown)} - see --list-fields"
        )
    if not fields:
        raise argparse.ArgumentTypeError("--fields needs at least one field")
    return fields


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Parse command line arguments."""

    parser = argparse.ArgumentParser(
        prog="bv-stats",
        description=(
            "Aggregate an archive's per-recording Stats assets "
            "(bv-generate --stats) into a summary report, grouped by "
            "calendar period. Uses the same recording selection every "
            "other bv-* command does."
        ),
        allow_abbrev=False,
    )

    parser.add_argument(
        "path",
        nargs="?",
        default=".",
        help=(
            "Archive directory. Also accepts a camera system id (see "
            "bv-config), resolved to that camera's archive target - "
            "use an explicit ./name or .\\name to force a literal "
            "directory of the same name instead."
        ),
    )

    parser.add_argument(
        "--config-dir",
        type=Path,
        default=default_config_dir(),
        help=(
            "Directory camera configs live in, for resolving `path` "
            "as a camera id (default: %(default)s)."
        ),
    )

    parser.add_argument(
        "--from",
        dest="from_",
        metavar="TIMESTAMP",
        help="Only consider recordings from this timestamp.",
    )
    parser.add_argument(
        "--until",
        metavar="TIMESTAMP",
        help="Only consider recordings up to this timestamp.",
    )
    parser.add_argument(
        "--timestamp",
        metavar="TIMESTAMP",
        help="Only consider recordings matching this timestamp or prefix.",
    )

    parser.add_argument(
        "--group",
        choices=GROUPINGS,
        default="all",
        help=(
            "Calendar period to group recordings by (default: "
            "%(default)s). 'date' groups by exact calendar date; "
            "'weekday' groups by day-of-week name (Monday..Sunday), "
            "recurring across the whole selection rather than one "
            "bucket per date; 'monthday' groups by day-of-month "
            "number (01..31), recurring the same way."
        ),
    )
    parser.add_argument(
        "--fields",
        type=_parse_fields,
        default=list(DEFAULT_FIELDS),
        metavar="FIELD1,FIELD2,...",
        help=(
            "Comma-separated stats fields to report, or 'all' "
            "(default: " + ",".join(DEFAULT_FIELDS) + "). "
            "See --list-fields for every available field."
        ),
    )
    parser.add_argument(
        "--list-fields",
        action="store_true",
        help="Print every field --fields accepts, with its unit and how it's aggregated, then exit.",
    )

    parser.add_argument(
        "--summary",
        action="store_true",
        help=(
            "Also report an overall summary (totals across the whole "
            "selection, same as --group all) alongside the per-group "
            "breakdown. No effect when --group is already 'all' - "
            "that's already the whole selection in one bucket. Useful "
            "because a per-group breakdown (e.g. --group weekday) can "
            "make the grand total hard to eyeball from the individual "
            "group lines, especially if a chunk of the range is silently "
            "excluded for lacking a Stats asset (see the 'skipped' line)."
        ),
    )

    parser.add_argument(
        "--estimate-gaps",
        action="store_true",
        help=(
            "Fill in an estimated distance for recordings that have no "
            "GPS fix (see the 'no GPS fix' message above) but do have a "
            "duration, by multiplying their duration by an average "
            "speed derived from the recordings around them that do have "
            "real distance data. Parking-mode recordings are never "
            "estimated (they're stationary, not driving). The estimated "
            "portion is always shown separately from real, measured "
            "distance - see 'Fields' below."
        ),
    )

    parser.add_argument(
        "--json",
        action="store_true",
        help=(
            "Print the aggregated report as JSON instead of a "
            "human-readable table - for scripting, or a future "
            "bv-web stats tab consuming this over a subprocess call."
        ),
    )

    parser.add_argument(
        "--trace",
        action="store_true",
        help=(
            "Print a '.' every "
            f"{TRACE_INTERVAL_RECORDINGS} recordings scanned, so a "
            "long run shows it's still active (see bv-search's own "
            "--trace)."
        ),
    )

    return parser.parse_args(argv)


def _default_warn(message: str) -> None:
    print(message, file=sys.stderr)


def _format_value(field_key: str, value: float) -> str:
    """Render one aggregated value the way trip_info.txt already
    renders the same underlying fields - see _SECONDS_FIELDS/
    _UNIT_PRECISION above for the shared convention this mirrors."""

    if field_key in _SECONDS_FIELDS:
        return str(timedelta(seconds=round(value)))

    unit = STAT_FIELDS[field_key].unit
    precision = _UNIT_PRECISION.get(unit, 2)
    return f"{value:.{precision}f} {unit}"


def _print_list_fields(say) -> None:
    say("Available --fields:")
    for field in STAT_FIELDS.values():
        say(f"  {field.key:<18} {field.label} ({field.unit}, {field.aggregate})")


def _print_bucket(say, label: str, bucket: StatBucket, fields: list[str]) -> None:
    say("")
    say(f"{label} ({len(bucket.recordings)} recording(s))")
    for field_key in fields:
        value = bucket.values.get(field_key)
        field_label = STAT_FIELDS[field_key].label
        if value is None:
            say(f"  {field_label}: -")
            continue
        line = f"  {field_label}: {_format_value(field_key, value)}"
        if field_key == "distance_km" and bucket.estimated_recording_count:
            estimated = _format_value("distance_km", bucket.estimated_distance_km)
            line += (
                f" (includes ~{estimated} estimated from "
                f"{bucket.estimated_recording_count} recording(s) with "
                "no GPS fix)"
            )
        say(line)


def _print_text_report(
    say,
    buckets: list[StatBucket],
    fields: list[str],
    *,
    summary_bucket: StatBucket | None = None,
) -> None:
    if not buckets:
        say("bv-stats: no recordings with Stats data in range.")
        return

    if summary_bucket is not None:
        _print_bucket(say, "Summary", summary_bucket, fields)

    for bucket in buckets:
        _print_bucket(say, bucket.key, bucket, fields)


def _buckets_to_json(buckets: list[StatBucket]) -> list[dict]:
    result = []
    for bucket in buckets:
        entry = {
            "key": bucket.key,
            "recordings": [str(recording_id) for recording_id in bucket.recordings],
            "values": bucket.values,
        }
        if bucket.estimated_recording_count:
            entry["estimated_distance_km"] = bucket.estimated_distance_km
            entry["estimated_recording_count"] = bucket.estimated_recording_count
        result.append(entry)
    return result


def _run(args: argparse.Namespace, *, say=print, warn=_default_warn) -> int:
    """Run bv-stats for already-parsed arguments. `say`/`warn` are
    injectable (default: real stdout/stderr), same pattern as every
    other bv-* CLI's own `_run()` - see bv_search.py's own `_run()`
    for the precedent this follows, including --list-fields being
    handled up front, before the archive is even opened (it needs no
    archive at all, the same way bv-search's --place geocoding is
    resolved before the timed section starts).
    """

    if args.list_fields:
        _print_list_fields(say)
        return EXIT_OK

    archive_path, camera_config = resolve_archive_path(args.path, args.config_dir)
    adapter_id = camera_config.adapter if camera_config is not None else DEFAULT_ADAPTER_ID
    adapter = get_adapter(adapter_id)
    archive = adapter.open_archive(archive_path)

    started_at = datetime.now()
    started_monotonic = time.monotonic()
    say(f"bv-stats: started {started_at:%H:%M:%S}")

    try:
        try:
            interval = LexicalTimeParser(
                timestamp=args.timestamp, from_=args.from_, until=args.until,
            ).parse()
        except ValueError as exc:
            warn(f"bv-stats: {exc}")
            return EXIT_ARGS_ERROR

        recordings = [
            recording for recording in archive.recordings
            if recording.id.value in interval
        ]

        if not recordings:
            say(f"bv-stats: {archive_path} - no recordings found in range.")
            return EXIT_OK

        progress = DotProgress() if args.trace else None
        entries: list[tuple] = []
        skipped = 0

        for recording in recordings:
            if progress is not None:
                progress.tick()

            stats = load_recording_stats(recording)
            if stats is None:
                skipped += 1
                continue
            entries.append((recording.id, stats))

        if progress is not None:
            progress.finish()

        if not entries:
            say(
                f"bv-stats: {archive_path} - none of the "
                f"{len(recordings)} recording(s) in range have a "
                "Stats asset yet (run bv-generate --stats first)."
            )
            return EXIT_OK

        if skipped:
            say(
                f"bv-stats: {skipped} of {len(recordings)} recording(s) "
                "in range have no Stats asset yet, skipped."
            )

        if GPS_DEPENDENT_FIELDS.intersection(args.fields):
            no_gps = count_recordings_without_gps(entries)
            if no_gps:
                say(
                    f"bv-stats: {no_gps} of {len(entries)} recording(s) "
                    "with Stats data have no GPS fix - distance/speed/"
                    "altitude fields won't include them."
                )

        buckets = aggregate_recording_stats(
            entries, grouping=args.group, fields=args.fields,
            estimate_gaps=args.estimate_gaps,
        )

        summary_bucket = None
        if args.summary and args.group != "all":
            summary_bucket = aggregate_recording_stats(
                entries, grouping="all", fields=args.fields,
                estimate_gaps=args.estimate_gaps,
            )[0]

        if args.json:
            payload: dict | list = {
                "buckets": _buckets_to_json(buckets),
            }
            if summary_bucket is not None:
                payload["summary"] = _buckets_to_json([summary_bucket])[0]
            if summary_bucket is None:
                # No --summary given: keep the original flat-list shape
                # for backward compatibility with anything already
                # parsing this output (e.g. a future bv-web stats tab
                # calling stats_report directly wouldn't hit this path
                # at all, but a script shelling out to this CLI would).
                payload = _buckets_to_json(buckets)
            say(json.dumps(payload, indent=2))
        else:
            _print_text_report(say, buckets, args.fields, summary_bucket=summary_bucket)

        return EXIT_OK
    finally:
        elapsed_seconds = time.monotonic() - started_monotonic
        finished_at = datetime.now()
        say(f"bv-stats: finished {finished_at:%H:%M:%S} ({elapsed_seconds:.1f}s)")


def main(argv: list[str] | None = None) -> int:
    """Run bv-stats."""

    args = parse_args(argv)
    say = wrap_say("bv-stats")
    warn = wrap_warn("bv-stats", _default_warn)
    return run_cli(
        "bv-stats", lambda: _run(args, say=say, warn=warn), argv=argv
    )


if __name__ == "__main__":
    raise SystemExit(main())
