"""
bv-export CLI - scan an archive, detect trips, and assemble each one
into its own folder under --target (concatenated video/audio/text,
merged GPX track, merged g-sensor log).

Copyright (C) 2026 Christer R. (sssreh)

SPDX-License-Identifier: GPL-3.0-or-later
"""

from __future__ import annotations

import argparse
import shutil
import sys
from datetime import timedelta
from pathlib import Path

from blackvue.archive import Archive
from blackvue.cli.errors import run_cli
from blackvue.export import export_trip
from blackvue.export import folder_name_for_trip
from blackvue.generate.media import MediaToolError
from blackvue.generate.media import read_duration_seconds
from blackvue.lexicaltimeparser import LexicalTimeParser
from blackvue.telemetry.movement import movement_bridges_gap
from blackvue.trip.trip_builder import DEFAULT_GAP_TOLERANCE
from blackvue.trip.trip_builder import DEFAULT_MAX_GAP
from blackvue.trip.trip_builder import TripBuilder


def _interactive() -> bool:
    """Return True if running attached to a real terminal."""

    return sys.stdin.isatty() and sys.stdout.isatty()


def _ask_wipe_existing(folder: Path) -> bool:
    answer = input(
        f"{folder.name} already exists. Wipe and rebuild trip folders "
        "from scratch this run, or keep existing files and only "
        "update what each run actually produces? [w/K] "
    ).strip().lower()
    return answer in ("w", "wipe")


def bv_export(
    path: str | Path = ".",
    *,
    target: str | Path,
    prefix: str | None = None,
    from_: str | None = None,
    until: str | None = None,
    timestamp: str | None = None,
    max_gap_minutes: int | None = None,
    movement: bool = True,
    duration: bool = True,
    gap_tolerance_seconds: int | None = None,
    render_map: bool = False,
    map_icon: str | Path | None = None,
    render_gsensor: bool = False,
    overwrite: bool = False,
    dry_run: bool = False,
) -> int:
    """Export every detected trip in `path` to its own folder under
    `target`. Returns 0 on success, 1 if any trip failed.

    A trip folder that already exists from a previous run is, by
    default, left in place - this run only overwrites whatever files
    it actually regenerates, so an output that's expensive to redo
    (--map in particular) survives a later run that doesn't ask for
    it again. `--overwrite` wipes and rebuilds every trip folder from
    scratch instead, without asking. Without `--overwrite`: an
    interactive run asks once (on the first trip folder that already
    exists) whether to wipe or keep, and reuses that answer for every
    other trip folder touched this run; a non-interactive run (cron/
    batch) always keeps, since there's no one to ask.
    """

    archive = Archive(path)

    try:
        interval = LexicalTimeParser(
            timestamp=timestamp,
            from_=from_,
            until=until,
        ).parse()
    except ValueError as exc:
        raise SystemExit(str(exc))

    recordings = [
        recording
        for recording in archive.recordings
        if recording.id.value in interval
    ]

    max_gap = (
        timedelta(minutes=max_gap_minutes)
        if max_gap_minutes is not None
        else DEFAULT_MAX_GAP
    )
    gap_tolerance = (
        timedelta(seconds=gap_tolerance_seconds)
        if gap_tolerance_seconds is not None
        else DEFAULT_GAP_TOLERANCE
    )
    bridge = movement_bridges_gap if movement else None
    recording_duration = read_duration_seconds if duration else None
    trips = TripBuilder(
        max_gap=max_gap,
        bridge=bridge,
        recording_duration=recording_duration,
        gap_tolerance=gap_tolerance,
    ).build(recordings)

    if not trips:
        print("bv-export: no recordings found in range - nothing to export.")
        return 0

    target_path = Path(target)
    # Shared across every trip in this run (and across runs) rather
    # than living inside any one trip's own folder, so it survives
    # even a --overwrite wipe of an individual trip folder - see
    # export_trip()'s map_cache_dir docstring.
    map_cache_dir = target_path / ".osm_cache"
    map_icon_path = Path(map_icon) if map_icon else None
    exit_code = 0
    # Cached on the first existing trip folder this run encounters,
    # then reused for every other one - so an interactive run only
    # asks once, the same "ask once per run" pattern bv-generate uses
    # for its own overwrite prompt.
    wipe_decision: bool | None = None

    for trip in trips:
        folder = target_path / folder_name_for_trip(trip, prefix)

        if dry_run:
            if not folder.exists():
                action = "create"
            elif overwrite:
                action = "wipe and rebuild"
            else:
                action = "update in place"
            print(f"bv-export: [dry run] would {action} {folder} "
                  f"({len(trip)} recording(s))")
            continue

        if folder.exists():
            if overwrite:
                shutil.rmtree(folder)
            else:
                if wipe_decision is None:
                    wipe_decision = (
                        _ask_wipe_existing(folder) if _interactive() else False
                    )
                if wipe_decision:
                    shutil.rmtree(folder)

        try:
            result = export_trip(
                trip,
                folder,
                render_map=render_map,
                map_cache_dir=map_cache_dir,
                map_icon=map_icon_path,
                render_gsensor=render_gsensor,
            )
        except MediaToolError as exc:
            print(f"bv-export: {trip.label}: {exc}", file=sys.stderr)
            exit_code = 1
            continue

        written = [
            written_path
            for written_path in (
                result.front_video, result.rear_video, result.audio,
                result.gpx, result.gsensor, result.map, result.gsensor_video,
                result.srt, result.lrc,
            )
            if written_path is not None
        ] + list(result.text)

        print(f"bv-export: {folder} - {len(written)} file(s) written")

        for warning in result.warnings:
            print(f"bv-export: {trip.label}: warning: {warning}", file=sys.stderr)

    return exit_code


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="bv-export",
        description=(
            "Detect trips in a BlackVue archive and export each one "
            "(concatenated video/audio/text, merged GPX track, merged "
            "g-sensor log) into its own folder."
        ),
    )

    parser.add_argument(
        "path",
        nargs="?",
        default=".",
        help="Archive directory.",
    )

    parser.add_argument(
        "--target",
        required=True,
        metavar="DIR",
        help="Directory to create trip subfolders in.",
    )

    parser.add_argument(
        "--prefix",
        metavar="PREFIX",
        default=None,
        help=(
            "Prepend PREFIX_ to each trip's folder name, e.g. "
            "--prefix Holiday -> "
            "Holiday_trip_20260715_133458_20260715_141235."
        ),
    )

    parser.add_argument(
        "--from",
        dest="from_",
        metavar="TIMESTAMP",
        help="Export recordings from this timestamp.",
    )

    parser.add_argument(
        "--until",
        metavar="TIMESTAMP",
        help="Export recordings up to this timestamp.",
    )

    parser.add_argument(
        "--timestamp",
        metavar="TIMESTAMP",
        help="Export recordings matching this timestamp or prefix.",
    )

    parser.add_argument(
        "--max-gap",
        dest="max_gap_minutes",
        type=int,
        metavar="MINUTES",
        default=None,
        help=(
            "The largest gap (in minutes) between two recordings "
            "that still counts as the same trip. "
            f"Default: {int(DEFAULT_MAX_GAP.total_seconds() // 60)}."
        ),
    )

    parser.add_argument(
        "--no-movement",
        dest="movement",
        action="store_false",
        help=(
            "Ignore GPS/g-sensor data and use the pure --max-gap "
            "time rule only when detecting trips."
        ),
    )

    parser.add_argument(
        "--no-duration",
        dest="duration",
        action="store_false",
        help=(
            "Ignore .duration.txt files and measure gaps from each "
            "recording's start timestamp only. By default, a "
            "recording's real span (from bv-generate --get-duration, "
            "if it's been run) is added to its start before "
            "comparing the gap to the next recording against "
            "--max-gap, so a long recording isn't mistaken for a gap."
        ),
    )

    parser.add_argument(
        "--gap-tolerance",
        dest="gap_tolerance_seconds",
        type=int,
        metavar="SECONDS",
        default=None,
        help=(
            "A small fixed margin (in seconds) added on top of "
            "--max-gap before a gap counts as a split - absorbs "
            "measurement noise (duration/timestamp rounding, brief "
            "file-rotation overhead), not a detection setting like "
            f"--max-gap. Default: "
            f"{int(DEFAULT_GAP_TOLERANCE.total_seconds())}."
        ),
    )

    parser.add_argument(
        "--map",
        dest="render_map",
        action="store_true",
        help=(
            "Also render map.mp4: a route/position/speed overlay on "
            "an OpenStreetMap road basemap for each trip. Off by "
            "default - the first trip through a given area needs a "
            "one-time network fetch of that area's road data (cached "
            "under --target/.osm_cache afterward, then fully "
            "offline), and rendering adds real time per trip. The "
            "current-position marker is an arrow rotated to match "
            "the GPS course over ground by default - see --map-icon "
            "to use a custom image instead."
        ),
    )

    parser.add_argument(
        "--map-icon",
        metavar="PATH",
        default=None,
        help=(
            "Use a custom image as --map's position marker instead "
            "of the default arrow, rotated each frame to match the "
            "GPS course over ground. A PNG with transparency, drawn "
            "pointing 'up'/north in its own file, works best. Only "
            "used together with --map."
        ),
    )

    parser.add_argument(
        "--gsensor-video",
        dest="render_gsensor",
        action="store_true",
        help=(
            "Also render gsensor.mp4: a dot moving around a gauge, "
            "tracking the trip's g-sensor (x, y) readings with a "
            "short fading trail, on a flat chroma-key green "
            "background meant for compositing over the front/rear "
            "footage later. No network involved, but off by default "
            "- it adds real render time per trip."
        ),
    )

    parser.add_argument(
        "--overwrite",
        action="store_true",
        help=(
            "Wipe and rebuild each trip's folder from scratch, without "
            "asking. Without this: an interactive run asks once whether "
            "to wipe or keep existing trip folders (the answer applies "
            "to every trip folder touched this run); a non-interactive "
            "run always keeps them, only overwriting whatever files it "
            "actually regenerates - useful since some outputs (--map in "
            "particular) are expensive to redo."
        ),
    )

    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show which trip folders would be created/refreshed "
             "without writing anything.",
    )

    args = parser.parse_args(argv)

    return run_cli("bv-export", lambda: bv_export(
        path=args.path,
        target=args.target,
        prefix=args.prefix,
        from_=args.from_,
        until=args.until,
        timestamp=args.timestamp,
        max_gap_minutes=args.max_gap_minutes,
        movement=args.movement,
        duration=args.duration,
        gap_tolerance_seconds=args.gap_tolerance_seconds,
        render_map=args.render_map,
        map_icon=args.map_icon,
        render_gsensor=args.render_gsensor,
        overwrite=args.overwrite,
        dry_run=args.dry_run,
    ))


if __name__ == "__main__":
    raise SystemExit(main())
