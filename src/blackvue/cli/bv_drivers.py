"""
bv-drivers.

Build (or refresh) the driver-knowledge base: scans an archive's
detected trips (same TripBuilder logic bv-ls --trips/bv-export use),
computes each trip's weekday/time, away-from-home stop duration and
short/long category, and its trip/driver_detect.py candidate driver
matches, clusters recurring destinations into trip/place_knowledge.py's
CommonPlace registry, and writes the result to driver_knowledge.json
under --config-dir - read by bv-web's /drivers page, where Christer
fills in each common place's short-stay/long-stay driver by hand (see
place_knowledge.py's own module docstring for the full design).

Re-running this command is meant to be routine (Christer: scoped to
the live Kirby (2026) archive, "the addresses will probably change
over time") - every place's label and short_stay_driver/long_stay_driver,
and every per-trip manual override, survive a rebuild untouched (see
build_knowledge_base()'s own docstring); only visit counts and the
trip list itself are refreshed from the archive's current state.

Copyright (C) 2026 Christer R. (sssreh)

SPDX-License-Identifier: GPL-3.0-or-later
"""

from __future__ import annotations

import argparse
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

from ..adapters.registry import get_adapter
from ..core.camera_config import DEFAULT_ADAPTER_ID
from ..core.camera_config import default_config_dir
from ..core.camera_config import resolve_archive_path
from ..core.joblog import wrap_say
from ..core.joblog import wrap_warn
from ..lexicaltimeparser import LexicalTimeParser
from ..trip.driver_detect import default_driver_profiles_path
from ..trip.driver_detect import resolve_known_points
from ..trip.driver_detect import resolve_trip_fix
from ..trip.driver_detect import write_default_driver_profiles
from ..trip.place_knowledge import default_driver_knowledge_path
from ..trip.place_knowledge import build_knowledge_base
from ..trip.place_knowledge import load_knowledge_base
from ..trip.place_knowledge import save_knowledge_base
from ..trip.place_knowledge import undecided_places
from ..trip.place_knowledge import undecided_trips
from ..trip.trip_builder import DEFAULT_GAP_TOLERANCE
from ..trip.trip_builder import DEFAULT_MAX_GAP
from ..trip.trip_builder import TripBuilder
from ..trip.trip_builder import recordings_with_front_video
from .errors import run_cli

EXIT_OK = 0
EXIT_ARGS_ERROR = 1

TRACE_INTERVAL_TRIPS = 10


class DotProgress:
    """A --trace progress indicator - same shape as bv_search.py's and
    bv_stats.py's own DotProgress, mirrored here unchanged. Resolving
    every trip's TripFix is a real per-recording GPS probe (see
    trip/driver_detect.py's resolve_trip_fix()), so a large archive's
    worth of trips can take a while - this is the only feedback a
    plain terminal run gets while that's happening."""

    def __init__(self, interval: int = TRACE_INTERVAL_TRIPS) -> None:
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


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Parse command line arguments."""

    parser = argparse.ArgumentParser(
        prog="bv-drivers",
        description=(
            "Build/refresh the driver-knowledge base: detects trips "
            "in an archive, computes weekday/time/stop-duration for "
            "each, and clusters recurring destinations into a common-"
            "places registry Christer fills in by hand via bv-web's "
            "/drivers page. Safe to re-run - manual place rules and "
            "per-trip overrides survive a rebuild."
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
            "Directory camera configs, driver_profiles.json, and "
            "driver_knowledge.json live in (default: %(default)s)."
        ),
    )

    parser.add_argument(
        "--from", dest="from_", metavar="TIMESTAMP",
        help="Only consider recordings from this timestamp.",
    )
    parser.add_argument(
        "--until", metavar="TIMESTAMP",
        help="Only consider recordings up to this timestamp.",
    )
    parser.add_argument(
        "--timestamp", metavar="TIMESTAMP",
        help="Only consider recordings matching this timestamp or prefix.",
    )

    parser.add_argument(
        "--max-gap", type=int, dest="max_gap_minutes", default=None,
        help=(
            "Largest gap (minutes) between two recordings that still "
            f"counts as the same trip (default: {DEFAULT_MAX_GAP.total_seconds() / 60:.0f})."
        ),
    )
    parser.add_argument(
        "--gap-tolerance", type=int, dest="gap_tolerance_seconds", default=None,
        help=(
            "Small fixed margin (seconds) added on top of --max-gap "
            f"(default: {DEFAULT_GAP_TOLERANCE.total_seconds():.0f})."
        ),
    )

    parser.add_argument(
        "--min-visits", type=int, default=2,
        help=(
            "How many visits make a place \"common\" enough to list "
            "as undecided (default: %(default)s) - see bv-drivers(1)."
        ),
    )

    parser.add_argument(
        "--trace", action="store_true",
        help=(
            f"Print a '.' every {TRACE_INTERVAL_TRIPS} trips resolved, "
            "so a long run shows it's still active."
        ),
    )
    parser.add_argument(
        "--debug", action="store_true",
        help="Print elapsed time for each phase (see bv-drivers(1)).",
    )

    return parser.parse_args(argv)


def _default_warn(message: str) -> None:
    print(message, file=sys.stderr)


def _run(args: argparse.Namespace, *, say=print, warn=_default_warn) -> int:
    """Run bv-drivers for already-parsed arguments. `say`/`warn` are
    injectable (default: real stdout/stderr) - same pattern as every
    other bv-* CLI's own `_run()`, so bv-web's JobRunner can capture
    this into a job's live output (see bv-stats.py's own `_run()` for
    the closest precedent this follows)."""

    started_at = datetime.now()
    started_monotonic = time.monotonic()
    say(f"bv-drivers: started {started_at:%H:%M:%S}")

    archive_path, camera_config = resolve_archive_path(args.path, args.config_dir)
    adapter_id = camera_config.adapter if camera_config is not None else DEFAULT_ADAPTER_ID
    adapter = get_adapter(adapter_id)

    scan_start = time.monotonic()
    archive = adapter.open_archive(archive_path)
    if args.debug:
        say(f"bv-drivers: debug: scanned archive in {time.monotonic() - scan_start:.2f}s")

    try:
        try:
            interval = LexicalTimeParser(
                timestamp=args.timestamp, from_=args.from_, until=args.until,
            ).parse()
        except ValueError as exc:
            warn(f"bv-drivers: {exc}")
            return EXIT_ARGS_ERROR

        recordings = [
            recording for recording in archive.recordings
            if recording.id.value in interval
        ]

        max_gap = (
            timedelta(minutes=args.max_gap_minutes)
            if args.max_gap_minutes is not None
            else DEFAULT_MAX_GAP
        )
        gap_tolerance = (
            timedelta(seconds=args.gap_tolerance_seconds)
            if args.gap_tolerance_seconds is not None
            else DEFAULT_GAP_TOLERANCE
        )

        build_start = time.monotonic()
        trips = TripBuilder(
            max_gap=max_gap, gap_tolerance=gap_tolerance,
        ).build(recordings_with_front_video(recordings))
        if args.debug:
            say(
                f"bv-drivers: debug: detected {len(trips)} trip(s) in "
                f"{time.monotonic() - build_start:.2f}s"
            )

        if not trips:
            say(f"bv-drivers: {archive_path} - no trips found in range.")
            return EXIT_OK

        profiles_path = default_driver_profiles_path(args.config_dir)
        profiles = write_default_driver_profiles(profiles_path)

        geocode_start = time.monotonic()
        known_points = resolve_known_points(profiles, args.config_dir / ".osm_cache")
        if args.debug:
            say(
                f"bv-drivers: debug: resolved {len(known_points)} known "
                f"place(s) in {time.monotonic() - geocode_start:.2f}s"
            )

        fixes_start = time.monotonic()
        progress = DotProgress() if args.trace else None
        fixes = []
        for trip in trips:
            fixes.append(resolve_trip_fix(adapter, trip))
            if progress is not None:
                progress.tick()
        if progress is not None:
            progress.finish()
        if args.debug:
            say(
                f"bv-drivers: debug: resolved {len(fixes)} trip fix(es) "
                f"in {time.monotonic() - fixes_start:.2f}s"
            )

        knowledge_path = default_driver_knowledge_path(args.config_dir)
        existing = load_knowledge_base(knowledge_path)
        existing_places = existing[1] if existing is not None else None
        trip_overrides = existing[2] if existing is not None else {}

        resolve_start = time.monotonic()
        resolved, places = build_knowledge_base(
            trips, fixes, profiles, known_points,
            existing_places=existing_places, trip_overrides=trip_overrides,
        )
        if args.debug:
            say(
                f"bv-drivers: debug: resolved drivers/places in "
                f"{time.monotonic() - resolve_start:.2f}s"
            )

        save_knowledge_base(
            knowledge_path, trips=resolved, places=places,
            trip_overrides=trip_overrides,
        )

        decided = sum(1 for entry in resolved if entry.source != "undecided")
        undecided_place_list = undecided_places(places, min_visits=args.min_visits)
        undecided_trip_list = undecided_trips(resolved)

        say(f"bv-drivers: {len(trips)} trip(s), {len(places)} place(s)")
        say(f"bv-drivers: {decided}/{len(resolved)} trip(s) resolved to a driver")
        say(
            f"bv-drivers: {len(undecided_place_list)} common place(s) "
            f"(>= {args.min_visits} visits) still need a short/long-stay "
            "driver rule"
        )
        say(f"bv-drivers: {len(undecided_trip_list)} trip(s) still undecided")
        say(f"bv-drivers: wrote {knowledge_path}")

        return EXIT_OK
    finally:
        elapsed_seconds = time.monotonic() - started_monotonic
        finished_at = datetime.now()
        say(f"bv-drivers: finished {finished_at:%H:%M:%S} ({elapsed_seconds:.1f}s)")


def main(argv: list[str] | None = None) -> int:
    """Run bv-drivers."""

    args = parse_args(argv)
    say = wrap_say("bv-drivers")
    warn = wrap_warn("bv-drivers", _default_warn)
    return run_cli(
        "bv-drivers", lambda: _run(args, say=say, warn=warn), argv=argv
    )


if __name__ == "__main__":
    raise SystemExit(main())
