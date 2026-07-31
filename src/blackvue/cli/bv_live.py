"""
bv-live.

Copyright (C) 2026 Christer R. (sssreh)

SPDX-License-Identifier: GPL-3.0-or-later
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .errors import run_cli
from ..core.camera_config import CameraConfigError
from ..core.camera_config import config_path
from ..core.camera_config import default_config_dir
from ..core.camera_config import load_camera_config
from ..core.connection import CameraUnreachableError
from ..core.connection import connect
from ..live.gsensor_stream import DEFAULT_WINDOW_SECONDS
from ..live.map_stream import DEFAULT_ZOOM_METERS

EXIT_OK = 0
EXIT_CONFIG_ERROR = 1
EXIT_UNREACHABLE = 2
EXIT_MISSING_DEPENDENCY = 3


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Parse command line arguments."""

    parser = argparse.ArgumentParser(
        prog="bv-live",
        description=(
            "Serve a live, one-page browser dashboard for a BlackVue "
            "camera: its own front/rear video feed (switchable), a "
            "scrolling map following its current position, and a "
            "scrolling g-sensor strip - all fed live from the "
            "camera's own endpoints (see bv-config(1)) for as long as "
            "this command keeps running."
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
        "--host",
        default="127.0.0.1",
        help=(
            "Address to listen on (default: 127.0.0.1 - this is a "
            "personal, run-when-you-want-it tool, not something meant "
            "to sit reachable by anyone else on the network)."
        ),
    )

    parser.add_argument(
        "--port",
        type=int,
        default=8100,
        help=(
            "Port to listen on (default: %(default)s - deliberately "
            "different from bv-web's own default 8000, so both can "
            "run at once)."
        ),
    )

    parser.add_argument(
        "--map-zoom",
        type=float,
        default=DEFAULT_ZOOM_METERS,
        metavar="METERS",
        help=(
            "Live map follow-camera radius in meters (default: "
            "%(default)s)."
        ),
    )

    parser.add_argument(
        "--gsensor-window",
        type=float,
        default=DEFAULT_WINDOW_SECONDS,
        metavar="SECONDS",
        help=(
            "How many seconds of live g-sensor history the scrolling "
            "strip shows at once (default: %(default)s)."
        ),
    )

    return parser.parse_args(argv)


def _run(args: argparse.Namespace) -> int:
    """Run bv-live for already-parsed arguments."""

    path = config_path(args.config_dir, args.id)

    try:
        config = load_camera_config(path)
    except CameraConfigError as exc:
        print(f"bv-live: {exc}", file=sys.stderr)
        return EXIT_CONFIG_ERROR

    if not config.endpoints:
        print(
            f"bv-live: {path}: no [[endpoint]] entries found",
            file=sys.stderr,
        )
        return EXIT_CONFIG_ERROR

    try:
        endpoint, client = connect(config.endpoints, timeout=args.timeout)
    except CameraUnreachableError as exc:
        print(f"bv-live: {exc}", file=sys.stderr)
        return EXIT_UNREACHABLE

    try:
        import uvicorn
    except ImportError as exc:
        print(
            f"bv-live: uvicorn is not installed ({exc}) - "
            "pip install uvicorn fastapi",
            file=sys.stderr,
        )
        return EXIT_MISSING_DEPENDENCY

    # Imported here, not at module level - see live/__init__.py's
    # docstring: app.py pulls in fastapi, so it should only ever be
    # imported once bv-live itself actually runs (same convention
    # web/app.py already follows for bv-web).
    from ..live.app import create_live_app

    app = create_live_app(
        client,
        camera_name=config.name,
        osm_cache_dir=config.target / ".osm_cache",
        map_zoom_meters=args.map_zoom,
        gsensor_window_seconds=args.gsensor_window,
    )

    print(
        f"bv-live: serving {config.name} (via {endpoint.name}) at "
        f"http://{args.host}:{args.port}/ - press Ctrl-C to stop"
    )
    uvicorn.run(app, host=args.host, port=args.port)

    return EXIT_OK


def main(argv: list[str] | None = None) -> int:
    """Run bv-live."""

    args = parse_args(argv)
    return run_cli("bv-live", lambda: _run(args))


if __name__ == "__main__":
    raise SystemExit(main())
