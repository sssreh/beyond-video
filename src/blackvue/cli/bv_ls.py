from __future__ import annotations

import argparse
import functools
from datetime import timedelta
from pathlib import Path

from blackvue.adapters import registry
from blackvue.adapters.base import CameraAdapter
from blackvue.adapters.telemetry_bridge import recording_gps_available
from blackvue.archive import Archive, Asset, Recording
from blackvue.cli.display_group import DisplayGroup
from blackvue.cli.display_group import source_name
from blackvue.cli.errors import run_cli
from blackvue.core.camera_config import DEFAULT_ADAPTER_ID
from blackvue.core.camera_config import default_config_dir
from blackvue.core.camera_config import resolve_archive_path
from blackvue.core.joblog import wrap_say
from blackvue.generate.media import photo_aware_duration
from blackvue.generate.media import read_duration_seconds
from blackvue.lexicaltimeparser import LexicalTimeParser
from blackvue.telemetry.movement import gps_implies_impossible_jump
from blackvue.telemetry.movement import movement_bridges_gap
from blackvue.trip.driver_detect import DriverMatch
from blackvue.trip.driver_detect import build_driver_trips
from blackvue.trip.driver_detect import default_driver_profiles_path
from blackvue.trip.driver_detect import match_driver
from blackvue.trip.driver_detect import resolve_known_points
from blackvue.trip.driver_detect import resolve_trip_fix
from blackvue.trip.driver_detect import write_default_driver_profiles
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


def _group_has_gps(group: DisplayGroup, adapter: CameraAdapter) -> bool:
    """Same all-recordings-in-the-group contract as DisplayGroup.has(),
    just backed by telemetry_bridge.recording_gps_available()'s real
    probe (real-adapter-read-then-EXIF/container-tag-fallback - see its
    own docstring for the exact order, deliberately more thorough than
    web/app.py's own archive_recording_location() route used to be
    before it was fixed to use the same shared check - and for the
    real report, Christer's own "No gps from", that motivated it)
    instead of a plain asset-file-exists check."""

    return all(
        recording_gps_available(adapter, recording)
        for recording in group.recordings
    )


def _assets_with_any_match(
    groups: list[DisplayGroup],
    assets: list[Asset],
    gps_marks: list[bool],
) -> list[Asset]:
    """Filter `assets` down to only those with at least one X somewhere
    in `groups` - an asset column nobody has is dead width on every
    single row, most noticeably for GoPro/folder archives (no REAR or
    INT columns ever match - that telemetry lives inside FRONT itself,
    not a separate asset) but just as real for a BlackVue archive that
    's never had --describe-scene or --diarize run, whose Scene/
    diarized-Transcript columns are permanently blank. `--full` (see
    parse_args()) skips this filter entirely for someone who wants to
    see every possible column regardless of whether this particular
    archive happens to use it.

    GPS is special-cased to `gps_marks` (one already-computed
    _group_has_gps() result per group, in the same order as `groups`)
    rather than group.has(Asset.GPS) - see that function's own
    docstring for why a plain file-existence check would always read
    False for a GoPro/folder archive even when the live EXIF/
    container-tag fallback would find a real fix."""

    return [
        asset
        for asset in assets
        if (
            any(gps_marks)
            if asset is Asset.GPS
            else any(group.has(asset) for group in groups)
        )
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
    use_gps_split: bool = False,
    gap_tolerance: timedelta = DEFAULT_GAP_TOLERANCE,
    adapter: CameraAdapter | None = None,
    show_drivers: bool = False,
    use_driver_trips: bool = False,
    config_dir: Path | None = None,
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

    When use_gps_split is True (off by default - see --gps-split), a
    consecutive pair of recordings whose GPS position implies an
    impossible jump forces a trip split even when the ordinary gap
    rule alone would have kept them together - see
    telemetry.movement.gps_implies_impossible_jump()'s own docstring.
    Requires `adapter` (the same CameraAdapter bv_ls() already opened
    the archive with) to actually resolve GPS fixes; a no-op if
    use_gps_split is True but adapter is None (defensive - every real
    caller passes both together).

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

    `show_drivers` (off by default - see --drivers) adds a Driver
    column: for each trip, every candidate driver/pattern match from
    trip/driver_detect.py's match_driver() - Christer's own "notice
    similar trips and ask later" request, surfaced the same way every
    other --trips column already is (read-only, no write-back; see
    driver_detect.py's module docstring for why this increment stops
    there). Needs `adapter` (for the GPS reads match_driver() itself
    needs) and forward-geocodes every place name in driver_profiles.json
    once per call via resolve_known_points() - real network I/O
    (cached to `config_dir`/.osm_cache - see geocode_preview_voice_
    search() in web/app.py for why that must be a writable location,
    not archive_path), which is why this is opt-in rather than a
    normal always-on column like Size or Recs. A trip with no
    candidate match at all (or when `adapter` is None) prints "-".

    `use_driver_trips` (off by default - see --drivers-trips) swaps
    which trips are shown for bv-drivers' own "sidecar trip" concept
    (trip/driver_detect.py's build_driver_trips()) instead of the
    front-video-filtered TripBuilder run above - Christer, after
    seeing --drivers work against this function's normal trip list:
    "bv-ls and bv-export are building video trips, we are trying to
    get driver from sidecars. Not same trip concept." Without this
    flag, --drivers shows driver candidates for the wrong trip
    boundaries - a real preview of what `bv-drivers build` will
    actually decide needs the same all-recordings/P-ending-filtered
    trip list bv-drivers.py's own _run() computes, not this function's
    video-trip one. Implies `show_drivers` - there is no reason to ask
    for the sidecar trip list without also wanting to see what it
    matches to. Every other --trips flag (--movement, --gps-split,
    --no-duration, --max-gap/--gap-tolerance) is ignored in this mode:
    build_driver_trips() always uses TripBuilder's own plain gap logic
    with no bridging/force-split, matching bv-drivers.py exactly."""

    if use_driver_trips:
        show_drivers = True
        trips, _ = build_driver_trips(
            recordings, max_gap=max_gap, gap_tolerance=gap_tolerance,
        )
    else:
        bridge = movement_bridges_gap if use_movement else None
        force_split = (
            functools.partial(gps_implies_impossible_jump, adapter=adapter)
            if use_gps_split and adapter is not None
            else None
        )
        recording_duration = (
            photo_aware_duration(read_duration_seconds) if use_duration else None
        )
        trips = TripBuilder(
            max_gap=max_gap,
            bridge=bridge,
            recording_duration=recording_duration,
            gap_tolerance=gap_tolerance,
            force_split=force_split,
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

    driver_labels: list[str] = []
    if show_drivers and adapter is not None and trips:
        driver_labels = _driver_column_labels(
            trips, adapter=adapter, config_dir=config_dir or default_config_dir()
        )

    driver_width = max(
        [len("Driver")] + [len(label) for label in driver_labels],
        default=len("Driver"),
    )

    header = (
        f'{"Trip":<{trip_width}}  {"Start":<19}  {"End":<19}  '
        f'{"Duration":>8}  {"Recs":>4}  {"Size":>{size_width}}'
    )
    if show_drivers:
        header += f'  {"Driver":<{driver_width}}'
    say(header)
    say("-" * len(header))

    for index, trip in enumerate(trips):
        size = format_size(sum(r.size for r in trip))
        row = (
            f"{trip.label:<{trip_width}}  "
            f"{trip.start_timestamp:%Y-%m-%d %H:%M:%S}  "
            f"{trip.end_timestamp:%Y-%m-%d %H:%M:%S}  "
            f"{str(trip.duration):>8}  "
            f"{len(trip):>4}  "
            f"{size:>{size_width}}"
        )
        if show_drivers:
            label = driver_labels[index] if index < len(driver_labels) else "-"
            row += f"  {label:<{driver_width}}"
        say(row)


def _driver_column_labels(
    trips,
    *,
    adapter: CameraAdapter,
    config_dir: Path,
) -> list[str]:
    """Resolve one Driver-column string per trip in `trips` (same
    order) for print_trips()'s `show_drivers` path - "Christer 90%",
    "Fru 90%/Christer 40%" when a trip has more than one candidate
    (see match_driver()'s own docstring for when that happens), or
    "-" for a trip with no candidate at all.

    Seeds driver_profiles.json with Christer's own real route data on
    first use (write_default_driver_profiles()) rather than requiring
    a separate setup step - the file is meant to be hand-edited
    afterward (add places, retune stay minutes, add a third driver),
    not regenerated.
    """

    profiles = write_default_driver_profiles(default_driver_profiles_path(config_dir))
    known_points = resolve_known_points(profiles, config_dir / ".osm_cache")

    fixes = [resolve_trip_fix(adapter, trip) for trip in trips]

    labels: list[str] = []
    for index, _trip in enumerate(trips):
        prev_fix = fixes[index - 1] if index > 0 else None
        next_fix = fixes[index + 1] if index + 1 < len(fixes) else None
        matches = match_driver(fixes[index], prev_fix, next_fix, profiles, known_points)
        labels.append(_format_driver_matches(matches))

    return labels


def _format_driver_matches(matches: tuple[DriverMatch, ...]) -> str:
    """"Fru 90%/Christer 40%" (best match per driver, highest
    confidence first) or "-" for no candidates - kept as its own
    function so bv-web's job-page output (which just shows this same
    text) doesn't need to know DriverMatch's shape."""

    if not matches:
        return "-"

    best_per_driver: dict[str, DriverMatch] = {}
    for match in matches:
        current = best_per_driver.get(match.driver_label)
        if current is None or match.confidence > current.confidence:
            best_per_driver[match.driver_label] = match

    ranked = sorted(best_per_driver.values(), key=lambda m: m.confidence, reverse=True)
    return "/".join(f"{m.display_name} {m.confidence:.0%}" for m in ranked)


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
    gps_split: bool = False,
    duration: bool = True,
    gap_tolerance_seconds: int | None = None,
    drivers: bool = False,
    drivers_trip: bool = False,
    config_dir: Path | None = None,
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

    `drivers` (only meaningful with `trips` - see --drivers) adds
    print_trips()'s Driver column; see that function's own docstring
    for the cost/why. `drivers_trip` (see --drivers-trips) additionally
    swaps which trips are shown for bv-drivers' own sidecar-trip
    concept instead of this command's normal video-trip one - see
    print_trips()'s own docstring for why these differ. `config_dir`
    selects where driver_profiles.json and its geocode cache live -
    defaults to default_config_dir(),
    the same directory --config-dir already governs for camera config.

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

    adapter = registry.get_adapter(adapter_id)
    archive = adapter.open_archive(Path(path))
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
            use_gps_split=gps_split,
            gap_tolerance=gap_tolerance,
            adapter=adapter,
            show_drivers=drivers,
            use_driver_trips=drivers_trip,
            config_dir=config_dir,
            say=say,
        )
        return 0

    groups = display_groups(
        archive,
        recordings,
        all=all,
    )

    assets = Asset.display_order()

    # A real per-group probe (see recording_gps_available()'s own
    # docstring for the cost/why) - computed once here, up front, and
    # reused for both the --full column-inclusion filter below and
    # each row's own mark in the render loop, rather than probing the
    # same recording twice.
    gps_marks = [_group_has_gps(group, adapter) for group in groups]

    if not full:
        assets = _assets_with_any_match(groups, assets, gps_marks)

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

    for group, gps_mark in zip(groups, gps_marks):
        row = f"{group.label:<{recording_width}}" + "  "

        if show_source:
            row += f"{group.source_label(archive_root):<{source_width}}" + "  "

        for asset in assets:
            if asset is Asset.GPS:
                mark = "X" if gps_mark else ""
            else:
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
        "--gps-split",
        dest="gps_split",
        action="store_true",
        default=False,
        help=(
            "With --trips, force a split between two recordings whose "
            "GPS position implies an impossible jump (e.g. a stock/"
            "downloaded clip mixed into the archive that happens to "
            "land within --max-gap of real footage, but was shot "
            "somewhere else entirely). Off by default: this adds a "
            "real per-pair GPS probe (an EXIF read and/or an ffprobe "
            "subprocess) to every consecutive pair of recordings, not "
            "just ones near an already-ambiguous gap."
        ),
    )

    parser.add_argument(
        "--drivers",
        dest="drivers",
        action="store_true",
        default=False,
        help=(
            "With --trips, add a Driver column: candidate driver "
            "matches from driver_profiles.json (seeded on first use "
            "with Christer's own route data - see trip/driver_detect.py) "
            "based on each trip's start/end location and, where a "
            "pattern specifies one, how long the vehicle stayed at the "
            "far end. Read-only - 'notice similar trips and ask "
            "later', not an automatic label. Off by default: forward-"
            "geocodes every place in driver_profiles.json (cached, but "
            "still real network I/O) and needs a GPS-capable adapter."
        ),
    )

    parser.add_argument(
        "--drivers-trips",
        dest="drivers_trip",
        action="store_true",
        default=False,
        help=(
            "With --trips, show driver candidates against bv-drivers' "
            "own trip concept (every recording, filtered to trips "
            "ending in a downloaded Parking-mode recording) instead of "
            "this command's normal video-trip one (recordings with "
            "front video only) - a real preview of what `bv-drivers "
            "build` will decide, not a different grouping that happens "
            "to also show a Driver column. Implies --drivers; other "
            "--trips flags (--movement, --gps-split, --no-duration, "
            "--max-gap/--gap-tolerance) are ignored in this mode."
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
        gps_split=args.gps_split,
        duration=args.duration,
        gap_tolerance_seconds=args.gap_tolerance_seconds,
        drivers=args.drivers,
        drivers_trip=args.drivers_trip,
        config_dir=args.config_dir,
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
