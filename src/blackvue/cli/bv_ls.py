from __future__ import annotations

import argparse
from pathlib import Path

from blackvue.archive import Archive, Asset


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


def bv_ls(path: str | Path = ".") -> int:
    archive = Archive(path)

    assets = Asset.display_order()

    recording_width = max(
        len("Recording"),
        *(len(str(recording.id)) for recording in archive),
    )

    widths = {
        asset: max(len(asset.label), 3)
        for asset in assets
    }

    size_width = max(
        len("Size"),
        *(len(format_size(r.size)) for r in archive),
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

    for recording in archive:
        print(f"{recording.id!s:<{recording_width}}", end="  ")

        for asset in assets:
            mark = "X" if recording.has(asset) else ""
            print(f"{mark:^{widths[asset]}}", end=" ")

        print(f"{format_size(recording.size):>{size_width}}")

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

    args = parser.parse_args(argv)
    return bv_ls(args.path)


if __name__ == "__main__":
    raise SystemExit(main())
