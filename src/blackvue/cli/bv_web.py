"""
bv-web: serve the Beyond Video web app, and manage its user accounts.

Two subcommands, mirroring bv-lang's list/install pattern:

    bv-web serve TARGET [--users-file PATH] [--host HOST] [--port PORT]
    bv-web adduser USERNAME --role {owner,viewer} [--users-file PATH]

`serve` needs fastapi/uvicorn installed (see pyproject.toml's
dependencies) - `adduser` doesn't, since it only touches the users
file (blackvue.web.users), so accounts can be provisioned even on a
machine that never runs the server itself.

Copyright (C) 2026 Christer R. (sssreh)

SPDX-License-Identifier: GPL-3.0-or-later
"""

from __future__ import annotations

import argparse
import getpass
import sys
from pathlib import Path

from .errors import run_cli
from ..web.users import ROLES
from ..web.users import UsersConfigError
from ..web.users import default_users_path
from ..web.users import load_users_config


def _serve(target: Path, users_file: Path, host: str, port: int) -> int:
    # Checked before the uvicorn import below: a missing/empty users
    # file is the more likely first-run problem, so it's worth
    # reporting on its own rather than only after also confirming
    # uvicorn is installed.
    try:
        users_config = load_users_config(users_file)
    except UsersConfigError as exc:
        print(f"bv-web: {exc}", file=sys.stderr)
        return 1

    if not users_config.users:
        print(
            f"bv-web: {users_file} has no users yet - create the owner "
            "account first, e.g.:\n"
            f"  bv-web adduser {getpass.getuser()} --role owner "
            f"--users-file {users_file}",
            file=sys.stderr,
        )
        return 1

    try:
        import uvicorn
    except ImportError as exc:
        print(
            f"bv-web: uvicorn is not installed ({exc}) - "
            "pip install uvicorn fastapi python-multipart",
            file=sys.stderr,
        )
        return 1

    # Imported here, not at module level - see web/__init__.py's
    # docstring: app.py pulls in fastapi, so it should only ever be
    # imported once bv-web itself actually runs.
    from ..web.app import create_app

    app = create_app(target, users_config)
    uvicorn.run(app, host=host, port=port)
    return 0


def _adduser(username: str, role: str, users_file: Path) -> int:
    try:
        users_config = load_users_config(users_file)
    except UsersConfigError as exc:
        print(f"bv-web: {exc}", file=sys.stderr)
        return 1

    password = getpass.getpass("Password: ")
    confirm = getpass.getpass("Confirm password: ")
    if password != confirm:
        print("bv-web: passwords did not match", file=sys.stderr)
        return 1

    try:
        users_config.add_user(username, password, role)
    except UsersConfigError as exc:
        print(f"bv-web: {exc}", file=sys.stderr)
        return 1

    users_config.save()
    print(f"Added {role} user {username!r} to {users_config.path}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="bv-web",
        description=(
            "Serve the Beyond Video web app, and manage its user accounts."
        ),
    )

    subparsers = parser.add_subparsers(dest="command", required=True)

    serve_parser = subparsers.add_parser("serve", help="Run the web server.")
    serve_parser.add_argument(
        "target",
        metavar="TARGET",
        type=Path,
        help=(
            "The bv-export --target directory to browse (the same "
            "directory bv-export writes trip folders into)."
        ),
    )
    serve_parser.add_argument(
        "--users-file",
        type=Path,
        default=default_users_path(),
        help=f"Accounts file (default: {default_users_path()}).",
    )
    serve_parser.add_argument(
        "--host",
        default="127.0.0.1",
        help=(
            "Address to listen on (default: 127.0.0.1 - put a reverse "
            "proxy in front for real access from other devices)."
        ),
    )
    serve_parser.add_argument(
        "--port", type=int, default=8000, help="Port to listen on (default: 8000)."
    )

    adduser_parser = subparsers.add_parser(
        "adduser", help="Create a user account (prompts for a password)."
    )
    adduser_parser.add_argument("username", metavar="USERNAME")
    adduser_parser.add_argument(
        "--role",
        choices=ROLES,
        required=True,
        help=(
            "owner can browse/watch, and (once built) trigger "
            "download/generate/export. viewer can only browse/watch."
        ),
    )
    adduser_parser.add_argument(
        "--users-file",
        type=Path,
        default=default_users_path(),
        help=f"Accounts file (default: {default_users_path()}).",
    )

    args = parser.parse_args(argv)

    if args.command == "serve":
        return run_cli(
            "bv-web",
            lambda: _serve(args.target, args.users_file, args.host, args.port),
        )
    if args.command == "adduser":
        return run_cli(
            "bv-web", lambda: _adduser(args.username, args.role, args.users_file)
        )

    # Unreachable in practice - add_subparsers(required=True) already
    # rejects anything but "serve"/"adduser" before this point.
    print(f"bv-web: unknown command: {args.command}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
