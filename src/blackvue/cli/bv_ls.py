from __future__ import annotations

import argparse
from datetime import timedelta
from pathlib import Path

from blackvue.archive import Archive, Asset
from blackvue.cli.display_group import DisplayGroup
from blackvue.cli.errors import run_cli
from blackvue.generate.media import read_duration_seconds
from blackvue.lexicaltimeparser import LexicalTimeParser
from blackvue.telemetry.movement import movement_bridges_gap
from blackvue.trip.trip_builder import DEFAULT_GAP_TOLERANCE
from blackvue.trip.trip_builder import DEFAULT_MAX_GAP
from blackvue.trip.trip_builder import TripBuilder


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
    """

    bridge = movement_bridges_gap if use_movement else None
    recording_duration = read_duration_seconds if use_duration else None
    trips = TripBuilder(
        max_gap=max_gap,
        bridge=bridge,
        recording_duration=recording_duration,
        gap_tolerance=gap_tolerance,
    ).build(recordings)

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
    print(header)
    print("-" * len(header))

    for trip in trips:
        size = format_size(sum(r.size for r in trip))
        print(
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
    trips: bool = False,
    max_gap_minutes: int | None = None,
    movement: bool = False,
    duration: bool = True,
    gap_tolerance_seconds: int | None = None,
) -> int:
    """List recordings."""

    archive = Archive(path)

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
        )
        return 0

    groups = display_groups(
        archive,
        recordings,
        all=all,
    )

    assets = Asset.display_order()

    recording_width = max(
        [len("Recording")]
        + [len(group.label) for group in groups],
        default=len("Recording"),
    )

    widths = {
        asset: max(len(asset.label), 3)
        for asset in assets
    }

    size_width = max(
        [len("Size")]
        + [len(format_size(group.size)) for group in groups],
        default=len("Size"),
    )

    print(f'{"":<{recording_width}}', end="  ")

    for group_label, span in _asset_group_spans(assets):
        width = sum(widths[asset] for asset in span) + (len(span) - 1)
        print(f"{group_label or '':^{width}}", end=" ")

    print()

    print(f'{"Recording":<{recording_width}}', end="  ")

    for asset in assets:
        print(f"{asset.label:^{widths[asset]}}", end=" ")

    print(f'{"Size":>{size_width}}')

    print(
        "-"
        * (
            recording_width
            + 2
            + sum(widths.values())
            + len(widths)
            + size_width
            + 1
        )
    )

    for group in groups:
        print(f"{group.label:<{recording_width}}", end=" ")

        for asset in assets:
            mark = "X" if group.has(asset) else ""
            print(f"{mark:^{widths[asset]}}", end=" ")

        print(f"{format_size(group.size):>{size_width}}")

    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="bv-ls",
        description="List recordings in a BlackVue archive.",
    )

    parser.add_argument(
        "path",
        nargs="?",
        default=".",
        help="Archive directory.",
    )

    parser.add_argument(
        "--all",
        action="store_true",
        help="Show every recording instead of grouped output.",
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

    args = parser.parse_args(argv)

    return run_cli("bv-ls", lambda: bv_ls(
        path=args.path,
        all=args.all,
        from_=args.from_,
        until=args.until,
        timestamp=args.timestamp,
        trips=args.trips,
        max_gap_minutes=args.max_gap_minutes,
        movement=args.movement,
        duration=args.duration,
        gap_tolerance_seconds=args.gap_tolerance_seconds,
    ))


if __name__ == "__main__":
    raise SystemExit(main())
