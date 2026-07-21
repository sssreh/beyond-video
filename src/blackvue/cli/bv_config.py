"""
bv-config.

Copyright (C) 2026 Christer R. (sssreh)

SPDX-License-Identifier: GPL-3.0-or-later
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .errors import run_cli
from ..core.camera_config import CameraConfig
from ..core.camera_config import CameraConfigError
from ..core.camera_config import config_path
from ..core.camera_config import default_config_dir
from ..core.camera_config import load_camera_config
from ..core.camera_config import save_camera_config
from ..core.camera_config import validate_id
from ..core.camera_config import validate_name
from ..core.endpoint import Endpoint

EXIT_OK = 0
EXIT_INVALID_ID = 1
EXIT_CONFIG_ERROR = 2


def prompt(question: str, default: str = "") -> str:
    """Ask a question, showing a default the user can accept with Enter."""

    suffix = f" [{default}]" if default else ""
    answer = input(f"{question}{suffix}: ").strip()

    return answer or default


def edit_endpoints(existing: list[Endpoint]) -> list[Endpoint]:
    """Interactively edit an endpoint list, in try order.

    Existing endpoints are reviewed one by one (Enter keeps the
    current value, typing 'remove' drops the endpoint), then new
    endpoints can be appended. Order given here is the order the
    endpoints are tried in.
    """

    endpoints: list[Endpoint] = []

    for number, endpoint in enumerate(existing, start=1):
        print(f"Endpoint {number} (currently {endpoint.name}, {endpoint.address}):")

        address = prompt("  Address (or 'remove')", default=endpoint.address)

        if address.strip().lower() == "remove":
            continue

        name = prompt("  Name", default=endpoint.name)

        endpoints.append(Endpoint(name=name, address=address))

    print("Add another endpoint? Leave the address blank to stop.")

    while True:
        number = len(endpoints) + 1

        address = input("  New endpoint address: ").strip()

        if not address:
            break

        name = prompt("  Name", default=f"EP{number}")

        endpoints.append(Endpoint(name=name, address=address))

    return endpoints


def run_wizard(id_: str, existing: CameraConfig | None) -> CameraConfig:
    """Run the interactive question-and-answer wizard."""

    default_name = existing.name if existing else id_
    default_target = str(existing.target) if existing else ""
    existing_endpoints = existing.endpoints if existing else []

    while True:
        name = prompt("Name", default=default_name)
        try:
            validate_name(name)
            break
        except CameraConfigError as exc:
            print(f"  {exc}")

    while True:
        target = prompt("Target (download path)", default=default_target)
        if target:
            break
        print("  Target must not be empty.")

    endpoints = edit_endpoints(existing_endpoints)

    return CameraConfig(
        id=id_,
        name=name,
        target=Path(target),
        endpoints=endpoints,
    )


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Parse command line arguments."""

    parser = argparse.ArgumentParser(
        prog="bv-config",
        description=(
            "Create or edit a camera's configuration: name, endpoints "
            "(tried in order), and where downloads are saved. Re-running "
            "this on an existing id edits it, defaulting every question "
            "to the current value."
        ),
    )

    parser.add_argument(
        "id",
        help="Camera system id (ASCII alphanumeric, max 128 characters).",
    )

    parser.add_argument(
        "--config-dir",
        type=Path,
        default=default_config_dir(),
        help="Directory camera configs live in (default: %(default)s).",
    )

    return parser.parse_args(argv)


def _run(args: argparse.Namespace) -> int:
    """Run bv-config for already-parsed arguments."""

    try:
        validate_id(args.id)
    except CameraConfigError as exc:
        print(f"bv-config: {exc}", file=sys.stderr)
        return EXIT_INVALID_ID

    path = config_path(args.config_dir, args.id)

    existing: CameraConfig | None = None

    if path.exists():
        try:
            existing = load_camera_config(path)
        except CameraConfigError as exc:
            print(f"bv-config: {exc}", file=sys.stderr)
            return EXIT_CONFIG_ERROR

        print(f"Editing existing config: {path}")
    else:
        print(f"Creating new config: {path}")

    config = run_wizard(args.id, existing)

    save_camera_config(path, config)

    print(f"Saved {path}")

    return EXIT_OK


def main(argv: list[str] | None = None) -> int:
    """Run bv-config."""

    args = parse_args(argv)
    return run_cli("bv-config", lambda: _run(args))


if __name__ == "__main__":
    raise SystemExit(main())
