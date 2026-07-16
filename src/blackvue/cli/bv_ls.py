from __future__ import annotations

import argparse
from pathlib import Path

from blackvue.archive import Archive, Asset


def bv_ls(path: str | Path = ".") -> int:
    archive = Archive(path)

    assets = Asset.display_order()

    recording_width = max(
        len("Recording"),
        *(len(str(recording.id)) for recording in archive),
    )

    widths = {
        asset: max(len(asset.value), 3)
        for asset in assets
    }

    #
    # Header
    #
    print(f'{"Recording":<{recording_width}}', end="  ")

    for asset in assets:
        print(f"{asset.value:^{widths[asset]}}", end=" ")

    print()

    print(
        "-" * (
            recording_width
            + 2
            + sum(widths.values())
            + len(widths)
        )
    )

    #
    # Rows
    #
    for recording in archive:
        print(f"{str(recording.id):<{recording_width}}", end="  ")

        for asset in assets:
            mark = "✔" if recording.has(asset) else ""
            print(f"{mark:^{widths[asset]}}", end=" ")

        print()

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
