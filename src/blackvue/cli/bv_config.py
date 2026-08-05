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


def prompt(question: str, default: str = "", *, ask=input) -> str:
    """Ask a question, showing a default the user can accept with Enter.

    `ask` is injectable (default: the real `input`) so bv-web's job
    runner (see web/jobs.py) can drive this same wizard from a
    browser instead of a real terminal - it just needs to match
    input()'s own signature (a prompt string in, one line of text
    out). Real terminal use is completely unchanged, since that's
    exactly what the default does.
    """

    suffix = f" [{default}]" if default else ""
    answer = ask(f"{question}{suffix}: ").strip()

    return answer or default


def edit_endpoints(
    existing: list[Endpoint], *, ask=input, say=print
) -> list[Endpoint]:
    """Interactively edit an endpoint list, in try order.

    Existing endpoints are reviewed one by one (Enter keeps the
    current value, typing 'remove' drops the endpoint), then new
    endpoints can be appended. Order given here is the order the
    endpoints are tried in.

    `ask`/`say` are injectable for the same reason as prompt()'s own
    `ask` - see its docstring.
    """

    endpoints: list[Endpoint] = []

    for number, endpoint in enumerate(existing, start=1):
        say(f"Endpoint {number} (currently {endpoint.name}, {endpoint.address}):")

        address = prompt("  Address (or 'remove')", default=endpoint.address, ask=ask)

        if address.strip().lower() == "remove":
            continue

        name = prompt("  Name", default=endpoint.name, ask=ask)

        endpoints.append(Endpoint(name=name, address=address))

    say("Add another endpoint? Leave the address blank to stop.")

    while True:
        number = len(endpoints) + 1

        address = ask("  New endpoint address: ").strip()

        if not address:
            break

        name = prompt("  Name", default=f"EP{number}", ask=ask)

        endpoints.append(Endpoint(name=name, address=address))

    return endpoints


def run_wizard(
    id_: str, existing: CameraConfig | None, *, ask=input, say=print
) -> CameraConfig:
    """Run the interactive question-and-answer wizard.

    `ask`/`say` are injectable for the same reason as prompt()'s own
    `ask` - see its docstring.
    """

    default_name = existing.name if existing else id_
    default_target = str(existing.target) if existing else ""
    existing_endpoints = existing.endpoints if existing else []

    while True:
        name = prompt("Name", default=default_name, ask=ask)
        try:
            validate_name(name)
            break
        except CameraConfigError as exc:
            say(f"  {exc}")

    while True:
        target = prompt("Target (download path)", default=default_target, ask=ask)
        if target:
            break
        say("  Target must not be empty.")

    endpoints = edit_endpoints(existing_endpoints, ask=ask, say=say)

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
        # See bv_export.py's own ArgumentParser for why: argparse's
        # default prefix-abbreviation matching silently breaks the
        # moment a sibling flag sharing a prefix gets added later.
        allow_abbrev=False,
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


def _default_warn(message: str) -> None:
    """`_run()`'s default `warn` - real stderr, the CLI's normal
    error-output contract. Kept as its own top-level function (rather
    than a lambda) so it's easy to point at from a test or from
    bv-web's job runner if a caller ever wants "real stderr" as an
    explicit choice rather than just the default."""

    print(message, file=sys.stderr)


def _run(
    args: argparse.Namespace, *, ask=input, say=print, warn=_default_warn
) -> int:
    """Run bv-config for already-parsed arguments.

    `ask`/`say` are threaded straight through to run_wizard() - see
    its docstring. `warn` is the equivalent for this function's own
    error-path messages, kept separate from `say` (rather than one
    combined callable) so real terminal use keeps writing errors to
    actual stderr by default, exactly as before this parameter
    existed; bv-web's job runner passes its own `warn` (routed into
    the same job output the browser sees) explicitly instead of
    relying on this default.
    """

    try:
        validate_id(args.id)
    except CameraConfigError as exc:
        warn(f"bv-config: {exc}")
        return EXIT_INVALID_ID

    path = config_path(args.config_dir, args.id)

    existing: CameraConfig | None = None

    if path.exists():
        try:
            existing = load_camera_config(path)
        except CameraConfigError as exc:
            warn(f"bv-config: {exc}")
            return EXIT_CONFIG_ERROR

        say(f"Editing existing config: {path}")
    else:
        say(f"Creating new config: {path}")

    config = run_wizard(args.id, existing, ask=ask, say=say)

    save_camera_config(path, config)

    say(f"Saved {path}")

    return EXIT_OK


def main(argv: list[str] | None = None) -> int:
    """Run bv-config."""

    args = parse_args(argv)
    return run_cli("bv-config", lambda: _run(args))


if __name__ == "__main__":
    raise SystemExit(main())
