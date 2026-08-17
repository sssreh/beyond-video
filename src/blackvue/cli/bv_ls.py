from __future__ import annotations

import argparse
from datetime import timedelta
from pathlib import Path

from blackvue.adapters import registry
from blackvue.archive import Archive, Asset
from blackvue.cli.display_group import DisplayGroup
from blackvue.cli.display_group import source_name
from blackvue.cli.errors import run_cli
from blackvue.core.camera_config import DEFAULT_ADAPTER_ID
from blackvue.core.camera_config import default_config_dir
from blackvue.core.camera_config import resolve_archive_path
from blackvue.core.joblog import wrap_say
from blackvue.generate.media import read_duration_seconds
from blackvue.lexicaltimeparser import LexicalTimeParser
from blackvue.telemetry.movement import movement_bridges_gap
from blackvue.trip.trip_builder import DEFAULT_GAP_TOLERANCE
from blackvue.trip.trip_builder import DEFAULT_MAX_GAP
from blackvue.trip.trip_builder import TripBuilder
from blackvue.trip.trip_builder import recordings_with_front_video


def format_size(size: int) -> str:
    """Format a size in bytes."""

    units = ("B", "K", "M", "G", "T")

    value = float(size)

    for unit in units:
        if value < 1024 or unit == units[-1]:
            if unit == "B":
                return f"{int(value)}{unit}"
            return f"{value:.2f}{unit}"
        value /= 1024

    raise AssertionError


def _asset_group_spans(
    assets: list[Asset],
) -> list[tuple[str | None, list[Asset]]]:
    """Group consecutive assets that share the same header group label
    (e.g. TRANSCRIPT and TRANSCRIPT_DIARIZED both under "Transcript"),
    so bv-ls can print one label spanning both of their columns.

    Assets with no group (group is None) each get their own
    single-asset span.
    """

    spans: list[tuple[str | None, list[Asset]]] = []

    for asset in assets:
        if (
            asset.group is not None
            and spans
            and spans[-1][0] == asset.group
        ):
            spans[-1][1].append(asset)
        else:
            spans.append((asset.group, [asset]))

    return spans


def _source_column_needed(groups: list[DisplayGroup], root: Path) -> bool:
    """Only worth a whole extra column when it carries real
    information beyond the Recording column - see
    DisplayGroup.source_label()'s own docstring for the real report
    (GoPro recording ids risking a collision) this column exists for.
    A BlackVue archive's on-disk filenames are themselves derived from
    the recording id (e.g. "20260715_133255_NF.mp4" for id
    "20260715_133255_N"), so showing this column there would just
    repeat the Recording column on every single row; skip it rather
    than clutter output that's looked the same since before adapters
    existed. Checked across every recording behind every row (not just
    each row's first) since a --trips-style flag isn't in play here,
    but a mixed archive with some FolderAdapter-scanned rows and some
    not is at least theoretically possible."""

    for group in groups:
        for recording in group.recordings:
            asset_file = recording.file(Asset.FRONT)
            if asset_file is not None and not asset_file.path.name.startswith(
                str(recording.id)
            ):
                return True

    return False


def _assets_with_any_match(
    groups: list[DisplayGroup], assets: list[Asset]
) -> list[Asset]:
    """Filter `assets` down to only those with at least one X somewhere
    in `groups` - an asset column nobody has is dead width on every
    single row, most noticeably for GoPro/folder archives (no REAR,
    INT, GPS, or GSENSOR columns ever match - that telemetry lives
    inside FRONT itself, not a separate asset) but just as real for a
    BlackVue archive that's never had --describe-scene or --diarize
    run, whose Scene/diarized-Transcript columns are permanently
    blank. `--full` (see parse_args()) skips this filter entirely for
    someone who wants to see every possible column regardless of
    whether this particular archive happens to use it."""

    return [
        asset
        for asset in assets
        if any(group.has(asset) for group in groups)
    ]


def display_groups(
    archive: Archive,
    recordings,
    *,
    all: bool,
) -> list[DisplayGroup]:
    """Return the display groups."""

    if all:
        return [
            DisplayGroup((recording,))
            for recording in recordings
        ]
    

    return DisplayGroup.group(
        archive,
        recordings,
    )


def print_trips(
    recordings,
    *,
    max_gap: timedelta,
    use_movement: bool = False,
    use_duration: bool = True,
    gap_tolerance: timedelta = DEFAULT_GAP_TOLERANCE,
    say=print,
) -> None:
    """Print one row per detected trip instead of one row per
    recording/group.

    Trip detection's primary rule is a time-gap heuristic (see
    TripBuilder) - consecutive recordings less than max_gap (plus
    gap_tolerance, a small fixed noise margin) apart belong to the
    same trip. When use_duration is True (the default), a recording's
    real .duration.txt span (if bv-generate --get-duration has been
    run for it) is folded in before that gap is compared to max_gap,
    so a long recording isn't mistaken for a gap to the one after it.
    When use_movement is True (off by default - see --movement),
    a gap that still exceeds max_gap after that can be bridged into
    one trip if GPS or g-sensor data shows the vehicle was still
    moving at the edge of the gap (see blackvue.telemetry.movement) -
    e.g. the camera briefly stopped recording at a long light or in a
    tunnel. Off by default: this heuristic has no ceiling on how large
    a gap it'll bridge - confirmed on a real archive to bridge a
    genuine 6-day gap into one trip off a single GPS speed reading at
    the very start of a later recording.

    Only recordings with a Front asset are considered - see
    recordings_with_front_video()'s own docstring for why (GPS/g
    -sensor/thumbnail-only recordings, common for an archive that
    isn't downloaded in full, used to be able to chain-bridge a real
    gap into one trip and to skew a trip's own GPS-derived data past
    what its video actually covers). A recording with no video simply
    isn't part of any trip here - it still shows up in a plain,
    non-`--trips` bv-ls listing.

    `say` is injectable (default: real stdout via print) so bv-web's
    job runner (see web/jobs.py) can capture bv-ls's table output into
    a job's transcript instead of the real terminal, the same reason
    every other bv-* command's own core function accepts it - bv-ls
    has no warnings/prompts of its own, so unlike bv_gps.py's `_run()`
    there's no `warn`/`ask` to thread through here.
    """

    bridge = movement_bridges_gap if use_movement else None
    recording_duration = read_duration_seconds if use_duration else None
    trips = TripBuilder(
        max_gap=max_gap,
        bridge=bridge,
        recording_duration=recording_duration,
        gap_tolerance=gap_tolerance,
    ).build(recordings_with_front_video(recordings))

    trip_width = max(
        [len("Trip")] + [len(trip.label) for trip in trips],
        default=len("Trip"),
    )

    size_width = max(
        [len("Size")]
        + [
            len(format_size(sum(r.size for r in trip)))
            for trip in trips
        ],
        default=len("Size"),
    )

    header = (
        f'{"Trip":<{trip_width}}  {"Start":<19}  {"End":<19}  '
        f'{"Duration":>8}  {"Recs":>4}  {"Size":>{size_width}}'
    )
    say(header)
    say("-" * len(header))

    for trip in trips:
        size = format_size(sum(r.size for r in trip))
        say(
            f"{trip.label:<{trip_width}}  "
            f"{trip.start_timestamp:%Y-%m-%d %H:%M:%S}  "
            f"{trip.end_timestamp:%Y-%m-%d %H:%M:%S}  "
            f"{str(trip.duration):>8}  "
            f"{len(trip):>4}  "
            f"{size:>{size_width}}"
        )


def bv_ls(
    path: str | Path = ".",
    *,
    all: bool = False,
    from_: str | None = None,
    until: str | None = None,
    timestamp: str | None = None,
    source: str | None = None,
    trips: bool = False,
    max_gap_minutes: int | None = None,
    movement: bool = False,
    duration: bool = True,
    gap_tolerance_seconds: int | None = None,
    adapter_id: str = DEFAULT_ADAPTER_ID,
    full: bool = False,
    say=print,
) -> int:
    """List recordings.

    `adapter_id` selects which CameraAdapter (see adapters/registry.py
    and docs/CAMERA_ADAPTERS.md) scans `path` - defaults to "blackvue"
    (DEFAULT_ADAPTER_ID), same as an un-migrated CameraConfig with no
    `adapter` key. _run() passes the resolved camera's own
    CameraConfig.adapter when `path` came from a configured camera id
    (see resolve_archive_path()); a literal directory path with no
    camera config behind it always uses the default. The adapter
    returns an Archive-shaped object (see CameraAdapter.open_archive()'s
    own docstring on duck-type compatibility) - everything below this
    line is unchanged from when it only ever saw a real Archive.

    `full` shows every possible asset column even if nothing in this
    archive/filter ever matches it (default: off - columns with zero
    matches across every displayed row are dropped, see
    _assets_with_any_match()'s own docstring for why).

    `source` is the reverse of `timestamp`: `timestamp`/`from_`/`until`
    filter by the (possibly synthesized) recording id, but for a
    GoPro/folder-adapter archive that id carries no relationship to
    the real on-disk filename (see the Source column, shown when
    `_source_column_needed()` says it's worth it). `source` matches a
    substring against that real filename instead - e.g. `--source
    GH010023` to find which recording id a specific GoPro file
    resolved to - and combines with any timestamp filter rather than
    replacing it.

    `say` is injectable (default: real stdout via print) - see
    print_trips()'s own docstring above for why. The grouped-table
    path below used to build each row across several print(...,
    end=...) calls rather than one call per line; a `say` that
    appends one Job.output entry per call (as bv-web's job runner
    supplies - see web/jobs.py) has no way to represent a partial,
    still-open line, so each row is now built up as a plain string
    first and handed to `say` once, complete - the printed result is
    byte-for-byte the same as before, just assembled differently."""

    archive = registry.get_adapter(adapter_id).open_archive(Path(path))
    archive_root = Path(path)

    try:
        interval = LexicalTimeParser(
            timestamp=timestamp,
            from_=from_,
            until=until,
        ).parse()
    except ValueError as exc:
        raise SystemExit(str(exc))

#    print(interval.first)
#    print(interval.last)

    recordings = [
        recording
        for recording in archive.recordings
        if recording.id.value in interval
        and (
            source is None
            or source in source_name(recording, archive_root)
        )
    ]

    if trips:
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
        print_trips(
            recordings,
            max_gap=max_gap,
            use_movement=movement,
            use_duration=duration,
            gap_tolerance=gap_tolerance,
            say=say,
        )
        return 0

    groups = display_groups(
        archive,
        recordings,
        all=all,
    )

    assets = Asset.display_order()
    if not full:
        assets = _assets_with_any_match(groups, assets)

    recording_width = max(
        [len("Recording")]
        + [len(group.label) for group in groups],
        default=len("Recording"),
    )

    show_source = _source_column_needed(groups, archive_root)
    source_width = (
        max(
            [len("Source")]
            + [len(group.source_label(archive_root)) for group in groups],
            default=len("Source"),
        )
        if show_source
        else 0
    )
    source_prefix = f'{"":<{source_width}}' + "  " if show_source else ""

    widths = {
        asset: max(len(asset.label), 3)
        for asset in assets
    }

    size_width = max(
        [len("Size")]
        + [len(format_size(group.size)) for group in groups],
        default=len("Size"),
    )

    group_header = f'{"":<{recording_width}}' + "  " + source_prefix
    for group_label, span in _asset_group_spans(assets):
        width = sum(widths[asset] for asset in span) + (len(span) - 1)
        group_header += f"{group_label or '':^{width}}" + " "
    say(group_header)

    asset_header = f'{"Recording":<{recording_width}}' + "  "
    if show_source:
        asset_header += f'{"Source":<{source_width}}' + "  "
    for asset in assets:
        asset_header += f"{asset.label:^{widths[asset]}}" + " "
    asset_header += f'{"Size":>{size_width}}'
    say(asset_header)

    say(
        "-"
        * (
            recording_width
            + 2
            + (source_width + 2 if show_source else 0)
            + sum(widths.values())
            + len(widths)
            + size_width
            + 1
        )
    )

    for group in groups:
        row = f"{group.label:<{recording_width}}" + "  "

        if show_source:
            row += f"{group.source_label(archive_root):<{source_width}}" + "  "

        for asset in assets:
            mark = "X" if group.has(asset) else ""
            row += f"{mark:^{widths[asset]}}" + " "

        row += f"{format_size(group.size):>{size_width}}"
        say(row)

    return 0


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Parse command line arguments - split out from main() so bv-web's
    job runner (see web/jobs.py) can build the same argparse.Namespace
    _run() takes, the same way every other bv-* command's own
    parse_args() is already used from JobRunner.start_bv_*()."""

    parser = argparse.ArgumentParser(
        prog="bv-ls",
        description="List recordings in a BlackVue archive.",
        # See bv_export.py's own ArgumentParser for why: argparse's
        # default prefix-abbreviation matching silently breaks the
        # moment a sibling flag sharing a prefix gets added later.
        allow_abbrev=False,
    )

    parser.add_argument(
        "path",
        nargs="?",
        default=".",
        help=(
            "Archive directory, or a configured camera system id (see "
            "bv-config) - resolved to that camera's own archive "
            "directory. A path containing a separator (e.g. ./Kirby) "
            "is always used literally, never as an id, so a real "
            "directory sharing a camera's name is never ambiguous."
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
        "--all",
        action="store_true",
        help="Show every recording instead of grouped output.",
    )

    parser.add_argument(
        "--full",
        action="store_true",
        help=(
            "Show every possible asset column, even ones nothing in "
            "this listing ever matches. By default, a column with no "
            "X anywhere in the current output is dropped - most "
            "archives only ever populate a subset of columns (e.g. a "
            "GoPro/folder-adapter archive never has Rear/Int/GPS/"
            "G-sensor columns, and a BlackVue archive that's never had "
            "--describe-scene or --diarize run never has those either)."
        ),
    )

    parser.add_argument(
        "--from",
        dest="from_",
        metavar="TIMESTAMP",
        help="Show recordings from this timestamp.",
    )

    parser.add_argument(
        "--until",
        metavar="TIMESTAMP",
        help="Show recordings up to this timestamp.",
    )

    parser.add_argument(
        "--timestamp",
        metavar="TIMESTAMP",
        help="Show recordings matching this timestamp or timestamp prefix.",
    )

    parser.add_argument(
        "--source",
        metavar="PATTERN",
        help=(
            "Show only recordings whose real on-disk filename contains "
            "PATTERN - the reverse of --timestamp: given a fragment of "
            "a GoPro/folder-adapter file's actual name (e.g. "
            "GH010023.MP4), find which recording id it resolved to. "
            "Combines with --timestamp/--from/--until rather than "
            "replacing them. No effect for adapters whose filenames "
            "are already id-derived (e.g. BlackVue)."
        ),
    )

    parser.add_argument(
        "--trips",
        action="store_true",
        help=(
            "List detected trips (one row per trip: start, end, "
            "duration, recording count) instead of individual "
            "recordings. A trip is a run of recordings with no gap "
            "longer than --max-gap between them."
        ),
    )

    parser.add_argument(
        "--max-gap",
        dest="max_gap_minutes",
        type=int,
        metavar="MINUTES",
        default=None,
        help=(
            "With --trips, the largest gap (in minutes) between two "
            "recordings that still counts as the same trip. "
            f"Default: {int(DEFAULT_MAX_GAP.total_seconds() // 60)}."
        ),
    )

    parser.add_argument(
        "--movement",
        dest="movement",
        action="store_true",
        default=False,
        help=(
            "With --trips, use GPS/g-sensor data to bridge a gap over "
            "--max-gap into one trip anyway, if the vehicle looks "
            "like it was still moving at the edge of the gap. Off by "
            "default: this heuristic has no ceiling on how large a "
            "gap it'll bridge - confirmed on a real archive to bridge "
            "a genuine 6-day gap into one trip off a single GPS speed "
            "reading. Until that has a fix, --max-gap (plus "
            "--gap-tolerance and --duration) is the sole trip "
            "-splitting rule unless you opt into this."
        ),
    )

    parser.add_argument(
        "--no-duration",
        dest="duration",
        action="store_false",
        help=(
            "With --trips, ignore .duration.txt files and measure "
            "gaps from each recording's start timestamp only. By "
            "default, a recording's real span (from bv-generate "
            "--get-duration, if it's been run) is added to its start "
            "before comparing the gap to the next recording against "
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
            "With --trips, a small fixed margin (in seconds) added on "
            "top of --max-gap before a gap counts as a split - "
            "absorbs measurement noise (duration/timestamp rounding, "
            "brief file-rotation overhead), not a detection setting "
            f"like --max-gap. Default: "
            f"{int(DEFAULT_GAP_TOLERANCE.total_seconds())}."
        ),
    )

    return parser.parse_args(argv)


def _run(args: argparse.Namespace, *, say=print) -> int:
    """Run bv-ls for already-parsed arguments.

    `say` is injectable (default: real stdout via print) so bv-web's
    job runner (see web/jobs.py) can capture bv-ls's table output into
    a job's transcript instead of the real terminal - bv-ls has no
    warnings/prompts of its own, so unlike bv_config.py's `_run()`
    there's no `ask`/`warn` to thread through here."""

    archive_path, camera_config = resolve_archive_path(args.path, args.config_dir)
    adapter_id = camera_config.adapter if camera_config is not None else DEFAULT_ADAPTER_ID

    return bv_ls(
        path=archive_path,
        all=args.all,
        from_=args.from_,
        until=args.until,
        timestamp=args.timestamp,
        source=args.source,
        trips=args.trips,
        max_gap_minutes=args.max_gap_minutes,
        movement=args.movement,
        duration=args.duration,
        gap_tolerance_seconds=args.gap_tolerance_seconds,
        adapter_id=adapter_id,
        full=args.full,
        say=say,
    )


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    # wrap_say()/wrap_warn() (see core/joblog.py) mirror every line
    # into the persistent output log alongside the real terminal
    # output - the direct-CLI half of "I would also want a logfile of
    # all the output" (see WORKING_CONTEXT.md); bv-web's job runner
    # gets the same coverage via Job.append_output() itself instead of
    # this per-command wiring, since every job already funnels through
    # that one function.
    say = wrap_say("bv-ls")
    return run_cli("bv-ls", lambda: _run(args, say=say), argv=argv)


if __name__ == "__main__":
    raise SystemExit(main())
