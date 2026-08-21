"""
bv-gps.

Copyright (C) 2026 Christer R. (sssreh)

SPDX-License-Identifier: GPL-3.0-or-later
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .errors import run_cli
from ..core.blackvue_client import SNAPSHOT_DIRECTIONS
from ..core.blackvue_client import BlackVueClient
from ..core.blackvue_client import NoGpsDataError
from ..core.camera_config import CameraConfigError
from ..core.camera_config import config_path
from ..core.camera_config import default_config_dir
from ..core.camera_config import load_camera_config
from ..core.connection import CameraUnreachableError
from ..core.connection import connect
from ..core.endpoint import Endpoint
from ..core.joblog import wrap_say
from ..core.joblog import wrap_warn
from ..domain.live_gps_fix import LiveGpsFix
from ..export.geocoding import reverse_geocode
from ..generate.media import MediaToolError
from ..snap import save_snapshots

EXIT_OK = 0
EXIT_CONFIG_ERROR = 1
EXIT_UNREACHABLE = 2
EXIT_NO_FIX = 3
EXIT_PROTOCOL_ERROR = 4
EXIT_NO_SNAPSHOTS = 5


def coordinate_pair(fix: LiveGpsFix) -> str:
    """Format a fix as "latitude,longitude" - the literal string
    Google Maps' own search box accepts pasted directly, at whatever
    precision blackvue_livedata.cgi itself reported (no rounding)."""

    return f"{fix.latitude},{fix.longitude}"


def google_maps_url(fix: LiveGpsFix) -> str:
    """Format a fix as a clickable Google Maps link."""

    return f"https://www.google.com/maps?q={coordinate_pair(fix)}"


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Parse command line arguments."""

    parser = argparse.ArgumentParser(
        prog="bv-gps",
        description=(
            "Fetch a BlackVue camera's current GPS reading live, over "
            "blackvue_livedata.cgi, while connected to one of its "
            "configured endpoints (see bv-config(1)), or a bare "
            "--host for a camera that hasn't been set up with "
            "bv-config yet. Prints the coordinates as a pasteable "
            "pair and a Google Maps link, plus a reverse-geocoded "
            "address. With --snap, grabs a one-shot camera snapshot "
            "instead (see --snap's own help) - the same one-off "
            "action bv-snap(1) is a dedicated command for, offered "
            "here too since this command already has a live camera "
            "connection open."
        ),
        # See bv_export.py's own ArgumentParser for why: argparse's
        # default prefix-abbreviation matching silently breaks the
        # moment a sibling flag sharing a prefix gets added later.
        allow_abbrev=False,
    )

    # Exactly one of these two ways to say "which camera" - id (looked
    # up via bv-config's own .cfg file, tried endpoint by endpoint) or
    # a bare --host (skips bv-config entirely, useful for probing a
    # list of candidate IPs - e.g. from scan_blackvue_endpoints.py -
    # one at a time, the same way that script already takes a raw
    # host with no setup required). required=True here is what turns
    # "neither given" and "both given" into the same clean argparse
    # usage error, instead of a second manual check in _run().
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
            "bv-config'd camera id - no --config-dir lookup, no "
            "[[endpoint]] fallback list, just this one address. "
            "Mutually exclusive with id."
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
        "--no-address",
        action="store_true",
        help=(
            "Skip the reverse-geocoding lookup (Nominatim) and print "
            "only the coordinates and Google Maps link."
        ),
    )

    parser.add_argument(
        "--snap",
        action="store_true",
        help=(
            "One-shot mode: grab a camera snapshot instead of a GPS "
            "reading (requires --output). See bv-snap(1) for a "
            "dedicated standalone command doing the same thing."
        ),
    )

    parser.add_argument(
        "--output",
        "-o",
        type=Path,
        default=None,
        help="Directory to save --snap's .jpg files into. Required with --snap.",
    )

    parser.add_argument(
        "--direction",
        choices=SNAPSHOT_DIRECTIONS,
        action="append",
        default=None,
        help=(
            "With --snap, only snap this direction - repeatable. "
            f"Default: every direction ({', '.join(SNAPSHOT_DIRECTIONS)})."
        ),
    )

    args = parser.parse_args(argv)

    if args.snap and args.output is None:
        parser.error("--snap requires --output")
    if not args.snap and args.output is not None:
        parser.error("--output only applies with --snap")
    if not args.snap and args.direction is not None:
        parser.error("--direction only applies with --snap")

    return args


def _default_warn(message: str) -> None:
    """`_run()`'s default `warn` - real stderr, the CLI's normal
    error-output contract. See bv_config.py's own `_default_warn` for
    why this is a named function rather than a lambda."""

    print(message, file=sys.stderr)


def _run(
    args: argparse.Namespace, *, say=print, warn=_default_warn
) -> int:
    """Run bv-gps for already-parsed arguments.

    `say`/`warn` are injectable (default: real stdout/stderr via
    print) so bv-web's job runner (see web/jobs.py) can capture this
    command's output into a job's transcript instead of the real
    terminal - bv-gps has no interactive prompts, so unlike
    bv_config.py's `_run()` there's no `ask` to thread through here.
    """

    if args.host is not None:
        # Skip bv-config entirely - a single synthetic Endpoint whose
        # name is just the host itself, so connect()'s own error
        # message ("<name> (<address>): <error>") still reads sensibly
        # with only one candidate instead of a configured list.
        endpoints = [Endpoint(name=args.host, address=args.host)]
    else:
        path = config_path(args.config_dir, args.id)

        try:
            config = load_camera_config(path)
        except CameraConfigError as exc:
            warn(f"bv-gps: {exc}")
            return EXIT_CONFIG_ERROR

        if not config.endpoints:
            warn(f"bv-gps: {path}: no [[endpoint]] entries found")
            return EXIT_CONFIG_ERROR

        endpoints = config.endpoints

    try:
        endpoint, client = connect(endpoints, timeout=args.timeout)
    except CameraUnreachableError as exc:
        warn(f"bv-gps: {exc}")
        return EXIT_UNREACHABLE

    if args.snap:
        return _run_snap(args, endpoint, client, say=say, warn=warn)

    try:
        fix = client.live_gps()
    except NoGpsDataError as exc:
        warn(f"bv-gps: {exc}")
        return EXIT_PROTOCOL_ERROR

    if not fix.has_fix:
        # endpoint.name (not config.name) - defined on both the id
        # and --host paths, since config itself only exists on the id
        # path. Resolves to the configured endpoint's own name (e.g.
        # "home") on that path, or the bare host string when --host
        # was given directly - either way, a sensible label for which
        # camera this was.
        warn(f"bv-gps: {endpoint.name}: no GPS fix currently available")
        return EXIT_NO_FIX

    say(f"Coordinates: {coordinate_pair(fix)}")
    say(f"Google Maps: {google_maps_url(fix)}")

    if not args.no_address:
        try:
            address = reverse_geocode(fix.latitude, fix.longitude)
        except MediaToolError as exc:
            say(f"Address: unavailable ({exc})")
        else:
            say(f"Address: {address or 'no address found for this location'}")

    return EXIT_OK


def _run_snap(
    args: argparse.Namespace,
    endpoint: Endpoint,
    client: BlackVueClient,
    *,
    say=print,
    warn=_default_warn,
) -> int:
    """Handle bv-gps --snap: grab F/R/I snapshots over an already-
    connected client and save them to args.output - the --snap
    counterpart to bv-snap.py's own standalone `_run()`, sharing its
    exact capture/save/report logic (see save_snapshots() and
    BlackVueClient.snapshot() for why per-direction failures are
    dropped silently rather than failing the whole call)."""

    directions = tuple(args.direction) if args.direction else SNAPSHOT_DIRECTIONS

    snapshots = client.snapshot(directions)

    if not snapshots:
        warn(
            f"bv-gps: {endpoint.name}: no snapshot received for any "
            f"direction ({', '.join(directions)})"
        )
        return EXIT_NO_SNAPSHOTS

    paths = save_snapshots(snapshots, args.output)
    for direction, path in paths.items():
        say(f"{direction}: saved {path}")

    missing = [d for d in directions if d not in snapshots]
    for direction in missing:
        warn(f"bv-gps: {endpoint.name}: no snapshot received for direction {direction}")

    return EXIT_OK


def main(argv: list[str] | None = None) -> int:
    """Run bv-gps."""

    args = parse_args(argv)
    # See bv_scribe.py's own main() for why - wrap_say()/wrap_warn()
    # (core/joblog.py) mirror every printed line into the persistent
    # output log alongside the real terminal output.
    say = wrap_say("bv-gps")
    warn = wrap_warn("bv-gps", _default_warn)
    return run_cli(
        "bv-gps", lambda: _run(args, say=say, warn=warn), argv=argv
    )


if __name__ == "__main__":
    raise SystemExit(main())
