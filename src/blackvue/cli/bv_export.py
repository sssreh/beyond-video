"""
bv-export CLI - scan an archive, detect trips, and assemble each one
into its own folder under --target (concatenated video/audio/text,
merged GPX track, merged g-sensor log).

Copyright (C) 2026 Christer R. (sssreh)

SPDX-License-Identifier: GPL-3.0-or-later
"""

from __future__ import annotations

import argparse
import shlex
import shutil
import sys
from datetime import timedelta
from pathlib import Path

from blackvue.archive import Archive
from blackvue.archive.recording_id import RecordingId
from blackvue.cli.errors import run_cli
from blackvue.export import export_trip
from blackvue.export import folder_name_for_trip
from blackvue.export.osm_roads import DEFAULT_ZOOM_RADIUS_METERS
from blackvue.export.stitch import ALL_LAYOUTS
from blackvue.export.stitch import AUTO_LAYOUT
from blackvue.export.stitch import DEFAULT_GSENSOR_POSITION
from blackvue.export.stitch import DEFAULT_GSENSOR_SIZE_PERCENT
from blackvue.export.stitch import DEFAULT_MIRROR_SIZE_PERCENT
from blackvue.export.stitch import MAX_GSENSOR_SIZE_PERCENT
from blackvue.export.stitch import MAX_MIRROR_SIZE_PERCENT
from blackvue.export.stitch import MIN_GSENSOR_SIZE_PERCENT
from blackvue.export.stitch import MIN_MIRROR_SIZE_PERCENT
from blackvue.export.stitch import parse_gsensor_position
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


def _parse_resolution(value: str) -> tuple[int, int]:
    try:
        width_str, height_str = value.lower().split("x")
        return int(width_str), int(height_str)
    except ValueError:
        raise argparse.ArgumentTypeError(
            f"invalid resolution {value!r} (expected WIDTHxHEIGHT, "
            "e.g. 320x240)"
        )


def _parse_gsensor_size(value: str) -> float:
    try:
        size = float(value)
    except ValueError:
        raise argparse.ArgumentTypeError(f"invalid size {value!r} (expected a number)")

    if not (MIN_GSENSOR_SIZE_PERCENT <= size <= MAX_GSENSOR_SIZE_PERCENT):
        raise argparse.ArgumentTypeError(
            f"size {value!r} out of range "
            f"({MIN_GSENSOR_SIZE_PERCENT:g}-{MAX_GSENSOR_SIZE_PERCENT:g})"
        )

    return size


def _parse_mirror_size(value: str) -> float:
    try:
        size = float(value)
    except ValueError:
        raise argparse.ArgumentTypeError(f"invalid size {value!r} (expected a number)")

    if not (MIN_MIRROR_SIZE_PERCENT <= size <= MAX_MIRROR_SIZE_PERCENT):
        raise argparse.ArgumentTypeError(
            f"size {value!r} out of range "
            f"({MIN_MIRROR_SIZE_PERCENT:g}-{MAX_MIRROR_SIZE_PERCENT:g})"
        )

    return size


def _parse_gsensor_position(value: str) -> str:
    try:
        parse_gsensor_position(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(str(exc))

    return value


def _parse_gsensor_xy(value: str) -> tuple[float, float]:
    try:
        x_str, y_str = value.split(",")
        return float(x_str), float(y_str)
    except ValueError:
        raise argparse.ArgumentTypeError(
            f"invalid position {value!r} (expected X,Y as percentages, "
            "e.g. 80,10)"
        )


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
    map_zoom_meters: float | None = None,
    render_gsensor: bool = False,
    stitch_layout: str | None = None,
    stitch_resolution: tuple[int, int] | None = None,
    stitch_bitrate: str | None = None,
    stitch_mirror_size: float = DEFAULT_MIRROR_SIZE_PERCENT,
    stitch_map: str | None = None,
    stitch_map_side: str | None = None,
    stitch_gsensor: bool = False,
    stitch_gsensor_size: float = DEFAULT_GSENSOR_SIZE_PERCENT,
    stitch_gsensor_pos: str | None = None,
    stitch_gsensor_xy: tuple[float, float] | None = None,
    stitch_subtitles: bool = False,
    stitch_subtitles_background: bool = True,
    overwrite: bool = False,
    dry_run: bool = False,
    debug: bool = False,
    command_line: str | None = None,
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

    `debug=True` prints wall-clock timing to stderr for each trip's
    concatenation/map/stitch phases, plus which decode method (nvdec
    or cpu) --stitch actually used and how long it took - diagnostic
    breadcrumbs for tracking down where time went on a slow run, off
    by default since most runs don't need them.

    `--timestamp`/`--from`/`--until` select *trips*, not recordings:
    trips are detected across the whole archive first, and a trip is
    included if any of its own recordings fall inside the requested
    range - the whole trip is then exported, including whatever
    recordings pushed it before or after the range's own boundaries.
    Filtering recordings by the range *before* detecting trips (the
    original approach) could silently truncate a trip that merely
    overlaps the requested window - e.g. a long continuous drive that
    started a few minutes before a `--timestamp` window opens would
    lose its earlier recordings entirely, since they'd never even
    reach TripBuilder, and the exported "trip" would be missing real
    footage that belongs to it. The trade-off: trip detection (and
    anything it reads per recording, like `.duration.txt` for
    `--no-duration`'s opposite, or GPS/g-sensor data for movement
    bridging) now runs across the *entire* archive on every run, not
    just the requested range - a real cost on a very large archive,
    accepted here in favor of never silently truncating a trip.

    `command_line`, if given, is written verbatim into every trip's
    own trip.log as the exact command that produced it - main() below
    reconstructs it from sys.argv/argv before calling here.
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
    # Populated in place by build() with one membership-reasoning
    # entry per recording (see TripBuilder.build()'s own docstring) -
    # forwarded to every trip's own trip.log below so a surprising
    # trip membership decision (e.g. a recording that seems to belong
    # to the wrong trip) can be checked against the real reasoning
    # that produced it.
    reasons: dict[RecordingId, str] = {}
    all_trips = TripBuilder(
        max_gap=max_gap,
        bridge=bridge,
        recording_duration=recording_duration,
        gap_tolerance=gap_tolerance,
    ).build(archive.recordings, reasons=reasons)

    trips = [
        trip
        for trip in all_trips
        if any(recording.id.value in interval for recording in trip)
    ]

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
                map_zoom_meters=map_zoom_meters,
                render_gsensor=render_gsensor,
                stitch_layout=stitch_layout,
                stitch_resolution=stitch_resolution,
                stitch_bitrate=stitch_bitrate,
                stitch_mirror_size=stitch_mirror_size,
                stitch_map=stitch_map,
                stitch_map_side=stitch_map_side,
                stitch_gsensor=stitch_gsensor,
                stitch_gsensor_size=stitch_gsensor_size,
                stitch_gsensor_pos=stitch_gsensor_pos,
                stitch_gsensor_xy=stitch_gsensor_xy,
                stitch_subtitles=stitch_subtitles,
                stitch_subtitles_background=stitch_subtitles_background,
                command_line=command_line,
                reasons=reasons,
                debug=debug,
            )
        except MediaToolError as exc:
            print(f"bv-export: {trip.label}: {exc}", file=sys.stderr)
            exit_code = 1
            continue

        written = [
            written_path
            for written_path in (
                result.front_video, result.rear_video, result.audio,
                result.gpx, result.gsensor, result.map, result.map_zoom,
                result.gsensor_video, result.stitch, result.srt, result.lrc,
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
        help=(
            "Export every trip that has at least one recording from "
            "this timestamp onward, in full - including any of that "
            "trip's own recordings that fall before it."
        ),
    )

    parser.add_argument(
        "--until",
        metavar="TIMESTAMP",
        help=(
            "Export every trip that has at least one recording up to "
            "this timestamp, in full - including any of that trip's "
            "own recordings that fall after it."
        ),
    )

    parser.add_argument(
        "--timestamp",
        metavar="TIMESTAMP",
        help=(
            "Export every trip that has at least one recording "
            "matching this timestamp or prefix, in full - including "
            "any of that trip's own recordings that fall outside it."
        ),
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
            "an OpenStreetMap road basemap for each trip, framing the "
            "whole trip at once (a static overview). Off by default - "
            "the first trip through a given area needs a one-time "
            "network fetch of that area's road data (cached under "
            "--target/.osm_cache afterward, then fully offline), and "
            "rendering adds real time per trip. See --map-zoom for a "
            "closer, scrolling 'follow camera' view instead (a "
            "separate file, works with or without --map). The "
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
            "Use a custom image as the position marker on --map and/or "
            "--map-zoom instead of the default arrow, rotated each "
            "frame to match the GPS course over ground. A PNG with "
            "transparency, drawn pointing 'up'/north in its own file, "
            "works best."
        ),
    )

    parser.add_argument(
        "--map-zoom",
        dest="map_zoom_meters",
        type=float,
        nargs="?",
        const=DEFAULT_ZOOM_RADIUS_METERS,
        default=None,
        metavar="METERS",
        help=(
            "Also render map_zoom_METERSm.mp4: a 'follow camera' view "
            "of real-world half-width METERS, centered on the "
            "vehicle's current position every frame, scrolling/panning "
            "as it moves - a separate file from --map's static "
            "whole-trip overview, and independent of it (works with or "
            "without --map given too). Defaults to "
            f"{DEFAULT_ZOOM_RADIUS_METERS:g}m if given with no value."
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
        "--stitch",
        action="store_true",
        help=(
            "Also render stitch.mp4: the trip's front and rear video "
            "composed into one, side by side, stacked, or as a "
            "rearview-mirror inset (see --stitch-layout), optionally "
            "with a map panel (see --stitch-map), a g-sensor overlay "
            "(see --stitch-gsensor), and/or burned-in subtitles (see "
            "--stitch-subtitles). A trip with only one camera falls "
            "back to a plain copy of whichever one exists, ignoring "
            "all of those too. Auto-picking a layout from the trip's "
            "own geometry is still planned for later."
        ),
    )

    parser.add_argument(
        "--stitch-layout",
        choices=[*ALL_LAYOUTS, AUTO_LAYOUT],
        default=AUTO_LAYOUT,
        help=(
            "Camera arrangement for --stitch: 'side_by_side' (front | "
            "rear), 'top_down' (front / rear), or 'rearview_mirror' "
            "(front full-frame, rear flipped horizontally and shrunk "
            "into a mirror-style inset overlaid top-center - see "
            "--stitch-mirror-size). Only used together with --stitch. "
            "Default: 'auto' - picks side_by_side or top_down from the "
            "trip's own north-south/east-west GPS extent (falls back "
            "to side_by_side with a warning if there's no GPS data). "
            "rearview_mirror is never auto-picked - name it explicitly "
            "to use it."
        ),
    )

    parser.add_argument(
        "--stitch-mirror-size",
        type=_parse_mirror_size,
        default=DEFAULT_MIRROR_SIZE_PERCENT,
        metavar="PERCENT",
        help=(
            f"Mirror inset size as a percentage of the composite's own "
            f"width ({MIN_MIRROR_SIZE_PERCENT:g}-"
            f"{MAX_MIRROR_SIZE_PERCENT:g}). Only meaningful with "
            f"--stitch-layout rearview_mirror. Default: "
            f"{DEFAULT_MIRROR_SIZE_PERCENT:g}."
        ),
    )

    parser.add_argument(
        "--stitch-resolution",
        type=_parse_resolution,
        default=None,
        metavar="WIDTHxHEIGHT",
        help=(
            "Scale stitch.mp4 to this resolution (e.g. 320x240) "
            "instead of leaving it at front's own resolution - a fast "
            "small test render instead of waiting on a full-size "
            "encode. Only used together with --stitch."
        ),
    )

    parser.add_argument(
        "--stitch-bitrate",
        default=None,
        metavar="RATE",
        help=(
            "Target video bitrate for stitch.mp4 (e.g. 256k, 2M), "
            "passed straight to ffmpeg and capped there (-b:v/"
            "-maxrate/-bufsize all set to RATE). Only used together "
            "with --stitch."
        ),
    )

    parser.add_argument(
        "--stitch-map",
        nargs="?",
        choices=["map", "zoom"],
        const="map",
        default=None,
        help=(
            "Also compose a map panel alongside the camera composite "
            "in stitch.mp4, rendered fresh at whatever size fits the "
            "composite (not a copy of --map's own map.mp4) - bare flag "
            "uses a static whole-trip overview, --stitch-map zoom uses "
            "a follow-camera view instead (reusing --map-zoom METERS "
            "as its radius - --map-zoom must also be given for that "
            "variant). Only used together with --stitch."
        ),
    )

    parser.add_argument(
        "--stitch-map-side",
        choices=["left", "right", "top", "down"],
        default=None,
        help=(
            "Override --stitch-map's panel side. Default: left for "
            "--stitch-layout top_down, down for side_by_side or "
            "rearview_mirror (capped at 30%% of width/height in "
            "rearview_mirror specifically, vs. the general 50%%)."
        ),
    )

    parser.add_argument(
        "--stitch-gsensor",
        action="store_true",
        help=(
            "Also composite gsensor.mp4 (see --gsensor-video) as a "
            "transparent overlay on top of the camera footage in "
            "stitch.mp4. Unlike --stitch-map, this never generates "
            "gsensor.mp4 itself - it must already exist (this run's "
            "own --gsensor-video, or an earlier run's), or the "
            "overlay is skipped with a warning. Only used together "
            "with --stitch."
        ),
    )

    parser.add_argument(
        "--stitch-gsensor-size",
        type=_parse_gsensor_size,
        default=DEFAULT_GSENSOR_SIZE_PERCENT,
        metavar="PERCENT",
        help=(
            f"Overlay size as a percentage of the camera composite's "
            f"width ({MIN_GSENSOR_SIZE_PERCENT:g}-"
            f"{MAX_GSENSOR_SIZE_PERCENT:g}). Default: "
            f"{DEFAULT_GSENSOR_SIZE_PERCENT:g}."
        ),
    )

    gsensor_position_group = parser.add_mutually_exclusive_group()
    gsensor_position_group.add_argument(
        "--stitch-gsensor-pos",
        type=_parse_gsensor_position,
        default=None,
        metavar="POSITION",
        help=(
            "Named overlay position: any combination of left/right/"
            "top/down/center (e.g. top-right, plain center). Defined "
            "relative to the camera footage only, excluding whatever "
            "space --stitch-map's panel occupies. Default: "
            f"{DEFAULT_GSENSOR_POSITION}. Mutually exclusive with "
            "--stitch-gsensor-xy."
        ),
    )
    gsensor_position_group.add_argument(
        "--stitch-gsensor-xy",
        type=_parse_gsensor_xy,
        default=None,
        metavar="X,Y",
        help=(
            "Explicit overlay position as X,Y percentages (not "
            "pixels) of the footage region's top-left corner, e.g. "
            "80,10. A deliberate override - allowed to land anywhere, "
            "including on top of --stitch-map's panel. Mutually "
            "exclusive with --stitch-gsensor-pos."
        ),
    )

    parser.add_argument(
        "--stitch-subtitles",
        action="store_true",
        help=(
            "Also burn this trip's own trip.srt into stitch.mp4's "
            "final frame (never trip.lrc, which has no real per-line "
            "duration) - centered, near the bottom, after any "
            "g-sensor overlay/map panel. Unlike --stitch-gsensor, "
            "there's nothing to render first: trip.srt is written "
            "automatically whenever the trip has any transcript data "
            "at all. If it doesn't, the burn-in is skipped with a "
            "warning. Only used together with --stitch."
        ),
    )

    parser.add_argument(
        "--no-subtitles-bg",
        dest="subtitles_bg",
        action="store_false",
        help=(
            "Disable the dark, semi-transparent background bar behind "
            "burned-in subtitle text (on by default when "
            "--stitch-subtitles is given)."
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

    parser.add_argument(
        "--debug",
        action="store_true",
        help=(
            "Print wall-clock timing to stderr for each trip's "
            "concatenation/map/stitch phases, plus which decode "
            "method (nvdec or cpu) --stitch used and how long it "
            "took - useful for tracking down where time went on a "
            "slow run."
        ),
    )

    args = parser.parse_args(argv)

    # The exact invoking command, written verbatim into every trip's
    # own trip.log (see export_trip()'s docstring) - reconstructed
    # from argv rather than args, since args has already been through
    # argparse's own parsing/defaulting and wouldn't necessarily read
    # back as what Christer actually typed.
    raw_argv = argv if argv is not None else sys.argv[1:]
    command_line = "bv-export " + shlex.join(raw_argv)

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
        map_zoom_meters=args.map_zoom_meters,
        render_gsensor=args.render_gsensor,
        stitch_layout=args.stitch_layout if args.stitch else None,
        stitch_resolution=args.stitch_resolution,
        stitch_bitrate=args.stitch_bitrate,
        stitch_mirror_size=args.stitch_mirror_size,
        stitch_map=args.stitch_map if args.stitch else None,
        stitch_map_side=args.stitch_map_side,
        stitch_gsensor=args.stitch_gsensor if args.stitch else False,
        stitch_gsensor_size=args.stitch_gsensor_size,
        stitch_gsensor_pos=args.stitch_gsensor_pos,
        stitch_gsensor_xy=args.stitch_gsensor_xy,
        stitch_subtitles=args.stitch_subtitles if args.stitch else False,
        stitch_subtitles_background=args.subtitles_bg,
        overwrite=args.overwrite,
        dry_run=args.dry_run,
        debug=args.debug,
        command_line=command_line,
    ))


if __name__ == "__main__":
    raise SystemExit(main())
