"""
bv-snap.

Copyright (C) 2026 Christer R. (sssreh)

SPDX-License-Identifier: GPL-3.0-or-later
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .errors import run_cli
from ..core.blackvue_client import SNAPSHOT_DIRECTIONS
from ..core.camera_config import CameraConfigError
from ..core.camera_config import config_path
from ..core.camera_config import default_config_dir
from ..core.camera_config import load_camera_config
from ..core.connection import CameraUnreachableError
from ..core.connection import connect
from ..core.endpoint import Endpoint
from ..core.joblog import wrap_say
from ..core.joblog import wrap_warn
from ..snap import save_snapshots

EXIT_OK = 0
EXIT_CONFIG_ERROR = 1
EXIT_UNREACHABLE = 2
EXIT_NO_SNAPSHOTS = 3


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Parse command line arguments."""

    parser = argparse.ArgumentParser(
        prog="bv-snap",
        description=(
            "Grab one live snapshot per camera direction (Front/Rear/"
            "Interior by default) over blackvue_live.cgi, while "
            "connected to one of a camera's configured endpoints (see "
            "bv-config(1)), or a bare --host for a camera that hasn't "
            "been set up with bv-config yet. Saves each direction as "
            "its own .jpg file - Christer: \"I would like to have a "
            "snap function that takes 1 snapshot for camera F, R and "
            "I.\""
        ),
        # See bv_export.py's own ArgumentParser for why: argparse's
        # default prefix-abbreviation matching silently breaks the
        # moment a sibling flag sharing a prefix gets added later.
        allow_abbrev=False,
    )

    # Same "id or bare --host, exactly one" shape as bv-gps's own
    # parse_args() - see its comment for why required=True here beats
    # a second manual check in _run().
    target_group = parser.add_mutually_exclusive_group(required=True)
    target_group.add_argument(
        "id",
        nargs="?",
        default=None,
        help="Camera system id (see bv-config).",
    )
    target_group.add_argument(
        "--host",
        metavar="HOST[:PORT]",
        default=None,
        help=(
            "Connect directly to this IP (or host:port) instead of a "
            "bv-config'd camera id. Mutually exclusive with id."
        ),
    )

    parser.add_argument(
        "--config-dir",
        type=Path,
        default=default_config_dir(),
        help=(
            "Directory camera configs live in (default: %(default)s). "
            "Ignored when --host is given."
        ),
    )

    parser.add_argument(
        "--timeout",
        type=int,
        default=5,
        help="Per-endpoint connection timeout in seconds (default: %(default)s).",
    )

    parser.add_argument(
        "--output",
        "-o",
        type=Path,
        required=True,
        help=(
            "Directory to save the snapshot .jpg files into (created "
            "if it doesn't exist yet) - kept separate from the "
            "recording archive rather than defaulting into it, since "
            "a snap is a one-off grab, not part of a recording."
        ),
    )

    parser.add_argument(
        "--direction",
        choices=SNAPSHOT_DIRECTIONS,
        action="append",
        default=None,
        help=(
            "Only snap this direction - repeatable (e.g. --direction "
            "F --direction R). Default: every direction "
            f"({', '.join(SNAPSHOT_DIRECTIONS)})."
        ),
    )

    return parser.parse_args(argv)


def _default_warn(message: str) -> None:
    print(message, file=sys.stderr)


def _run(
    args: argparse.Namespace, *, say=print, warn=_default_warn
) -> int:
    """Run bv-snap for already-parsed arguments.

    `say`/`warn` are injectable (default: real stdout/stderr via
    print) so bv-web's job runner (see web/jobs.py) can capture this
    command's output into a job's transcript - same contract as
    bv-gps's own `_run()`, which this function's connection-setup
    block is a direct copy of.
    """

    if args.host is not None:
        endpoints = [Endpoint(name=args.host, address=args.host)]
    else:
        path = config_path(args.config_dir, args.id)

        try:
            config = load_camera_config(path)
        except CameraConfigError as exc:
            warn(f"bv-snap: {exc}")
            return EXIT_CONFIG_ERROR

        if not config.endpoints:
            warn(f"bv-snap: {path}: no [[endpoint]] entries found")
            return EXIT_CONFIG_ERROR

        endpoints = config.endpoints

    try:
        endpoint, client = connect(endpoints, timeout=args.timeout)
    except CameraUnreachableError as exc:
        warn(f"bv-snap: {exc}")
        return EXIT_UNREACHABLE

    directions = tuple(args.direction) if args.direction else SNAPSHOT_DIRECTIONS

    # Christer: "the snapshot part times out even when i set it to
    # 15 s" - see bv_gps.py's _run_snap() for the full explanation of
    # why this exists (same shared BlackVueClient.snapshot() call this
    # function was always a near-duplicate of).
    failed_directions: set[str] = set()

    def _on_snapshot_error(direction: str, message: str) -> None:
        failed_directions.add(direction)
        warn(f"bv-snap: {endpoint.name}: {direction}: {message}")

    snapshots = client.snapshot(directions, on_error=_on_snapshot_error)

    if not snapshots:
        warn(
            f"bv-snap: {endpoint.name}: no snapshot received for any "
            f"direction ({', '.join(directions)})"
        )
        return EXIT_NO_SNAPSHOTS

    # id-or-host - whichever one was actually given, for the filename
    # (Christer: "I would also like the id or host be in the output
    # name of the files"). save_snapshots() sanitizes it before use.
    label = args.id if args.id is not None else args.host

    paths = save_snapshots(snapshots, args.output, label=label)
    for direction, path in paths.items():
        say(f"{direction}: saved {path}")

    missing = [
        d for d in directions if d not in snapshots and d not in failed_directions
    ]
    for direction in missing:
        warn(f"bv-snap: {endpoint.name}: no snapshot received for direction {direction}")

    return EXIT_OK


def main(argv: list[str] | None = None) -> int:
    """Run bv-snap."""

    args = parse_args(argv)
    # See bv_gps.py's own main() for why - wrap_say()/wrap_warn()
    # (core/joblog.py) mirror every printed line into the persistent
    # output log alongside the real terminal output.
    say = wrap_say("bv-snap")
    warn = wrap_warn("bv-snap", _default_warn)
    return run_cli(
        "bv-snap", lambda: _run(args, say=say, warn=warn), argv=argv
    )


if __name__ == "__main__":
    raise SystemExit(main())
