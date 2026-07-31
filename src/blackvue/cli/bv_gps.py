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
from ..core.blackvue_client import NoGpsDataError
from ..core.camera_config import CameraConfigError
from ..core.camera_config import config_path
from ..core.camera_config import default_config_dir
from ..core.camera_config import load_camera_config
from ..core.connection import CameraUnreachableError
from ..core.connection import connect
from ..domain.live_gps_fix import LiveGpsFix
from ..export.geocoding import reverse_geocode
from ..generate.media import MediaToolError

EXIT_OK = 0
EXIT_CONFIG_ERROR = 1
EXIT_UNREACHABLE = 2
EXIT_NO_FIX = 3
EXIT_PROTOCOL_ERROR = 4


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
            "configured endpoints (see bv-config(1)). Prints the "
            "coordinates as a pasteable pair and a Google Maps link, "
            "plus a reverse-geocoded address."
        ),
        # See bv_export.py's own ArgumentParser for why: argparse's
        # default prefix-abbreviation matching silently breaks the
        # moment a sibling flag sharing a prefix gets added later.
        allow_abbrev=False,
    )

    parser.add_argument(
        "id",
        help="Camera system id (see bv-config).",
    )

    parser.add_argument(
        "--config-dir",
        type=Path,
        default=default_config_dir(),
        help="Directory camera configs live in (default: %(default)s).",
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

    return parser.parse_args(argv)


def _run(args: argparse.Namespace) -> int:
    """Run bv-gps for already-parsed arguments."""

    path = config_path(args.config_dir, args.id)

    try:
        config = load_camera_config(path)
    except CameraConfigError as exc:
        print(f"bv-gps: {exc}", file=sys.stderr)
        return EXIT_CONFIG_ERROR

    if not config.endpoints:
        print(
            f"bv-gps: {path}: no [[endpoint]] entries found",
            file=sys.stderr,
        )
        return EXIT_CONFIG_ERROR

    try:
        endpoint, client = connect(config.endpoints, timeout=args.timeout)
    except CameraUnreachableError as exc:
        print(f"bv-gps: {exc}", file=sys.stderr)
        return EXIT_UNREACHABLE

    try:
        fix = client.live_gps()
    except NoGpsDataError as exc:
        print(f"bv-gps: {exc}", file=sys.stderr)
        return EXIT_PROTOCOL_ERROR

    if not fix.has_fix:
        print(
            f"bv-gps: {config.name}: no GPS fix currently available",
            file=sys.stderr,
        )
        return EXIT_NO_FIX

    print(f"Coordinates: {coordinate_pair(fix)}")
    print(f"Google Maps: {google_maps_url(fix)}")

    if not args.no_address:
        try:
            address = reverse_geocode(fix.latitude, fix.longitude)
        except MediaToolError as exc:
            print(f"Address: unavailable ({exc})")
        else:
            print(f"Address: {address or 'no address found for this location'}")

    return EXIT_OK


def main(argv: list[str] | None = None) -> int:
    """Run bv-gps."""

    args = parse_args(argv)
    return run_cli("bv-gps", lambda: _run(args))


if __name__ == "__main__":
    raise SystemExit(main())
