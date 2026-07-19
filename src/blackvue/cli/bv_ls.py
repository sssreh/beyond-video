from __future__ import annotations

import argparse
from pathlib import Path

from blackvue.archive import Archive, Asset
from blackvue.cli.display_group import DisplayGroup
from blackvue.timeparser import TimeParser


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


def bv_ls(
    path: str | Path = ".",
    *,
    all: bool = False,
    from_: str | None = None,
    until: str | None = None,
    timestamp: str | None = None,
) -> int:
    """List recordings."""

    archive = Archive(path)

    try:
        interval = TimeParser(
            timestamp=timestamp,
            from_=from_,
            until=until,
        ).parse()
    except ValueError as exc:
        raise SystemExit(str(exc))

    print(interval.first)
    print(interval.last)

    recordings = [
        recording
        for recording in archive.recordings
        if recording.id.value in interval
        
    ]
    

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

    args = parser.parse_args(argv)

    return bv_ls(
        path=args.path,
        all=args.all,
        from_=args.from_,
        until=args.until,
        timestamp=args.timestamp,
    )


if __name__ == "__main__":
    raise SystemExit(main())
