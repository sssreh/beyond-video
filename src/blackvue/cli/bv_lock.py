"""
bv-lock.

Copyright (C) 2026 Christer R. (sssreh)

SPDX-License-Identifier: GPL-3.0-or-later
"""

from __future__ import annotations

import argparse
from pathlib import Path

from .errors import run_cli
from ..core.camera_config import default_config_dir
from ..core.camera_config import resolve_archive_path
from ..core.lock import LOCKABLE_ASSETS
from ..core.lock import LockError
from ..core.lock import add_lock_assets
from ..core.lock import load_lock_manifest
from ..core.lock import remove_lock_assets
from ..core.lock import save_lock_manifest
from ..lexicaltimeparser import LexicalTimeParser

EXIT_OK = 0
EXIT_ARGS_ERROR = 1


def _split_assets(parser: argparse.ArgumentParser, raw: str) -> list[str]:
    """Split a comma-separated --lock-assets/--unlock-assets value and
    validate every name against LOCKABLE_ASSETS, using parser.error()
    for a clean CLI message (rather than a raw exception) on an
    unknown name - the same argparse-time-validation pattern
    bv-generate's own parse_args() uses for its cross-flag checks.

    "all" is a convenience alias for every name in LOCKABLE_ASSETS -
    if present anywhere in the comma-separated list, it wins outright
    (any other names alongside it are redundant, not an error) rather
    than being treated as an unknown asset name itself."""

    names = [part.strip() for part in raw.split(",") if part.strip()]
    if "all" in names:
        return sorted(LOCKABLE_ASSETS)

    unknown = set(names) - LOCKABLE_ASSETS
    if unknown:
        parser.error(
            f"unknown asset name(s): {', '.join(sorted(unknown))} - "
            f"valid names: {', '.join(sorted(LOCKABLE_ASSETS))}, or 'all'"
        )
    return names


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Parse command line arguments."""

    parser = argparse.ArgumentParser(
        prog="bv-lock",
        description=(
            "Mark asset types as already generated for a time range in "
            "a BlackVue archive, so bv-generate can skip that range "
            "entirely on future runs - no per-recording file checks, "
            "no --overwrite prompts - instead of walking every "
            "recording in it. A range stays skippable only for the "
            "asset flags it was locked with; a bv-generate run asking "
            "for a flag that isn't locked yet (a new asset type added "
            "later) is never blocked."
        ),
        # See bv_export.py's own ArgumentParser for why: argparse's
        # default prefix-abbreviation matching silently breaks the
        # moment a sibling flag sharing a prefix gets added later.
        allow_abbrev=False,
    )

    parser.add_argument(
        "path",
        nargs="?",
        default=".",
        help=(
            "Archive directory, or a configured camera system id (see "
            "bv-config) - resolved to that camera's own archive "
            "directory. A path containing a separator (e.g. ./Kirby) "
            "is always used literally, never as an id, so a real "
            "directory sharing a camera's name is never ambiguous."
        ),
    )

    parser.add_argument(
        "--config-dir",
        type=Path,
        default=default_config_dir(),
        help=(
            "Directory camera configs live in, for resolving `path` "
            "as a camera id (default: %(default)s)."
        ),
    )

    parser.add_argument(
        "--from",
        dest="from_",
        metavar="TIMESTAMP",
        help="Only consider recordings from this timestamp.",
    )

    parser.add_argument(
        "--until",
        metavar="TIMESTAMP",
        help="Only consider recordings up to this timestamp.",
    )

    parser.add_argument(
        "--timestamp",
        metavar="TIMESTAMP",
        help="Only consider recordings matching this timestamp or prefix.",
    )

    parser.add_argument(
        "--lock-assets",
        metavar="ASSET[,ASSET...]",
        default=None,
        help=(
            "Mark these asset types as done for the selected range - "
            "comma-separated, from: "
            f"{', '.join(sorted(LOCKABLE_ASSETS))}, or 'all' for every "
            "one of them. 'translate' covers every target language, "
            "not one lock per language."
        ),
    )

    parser.add_argument(
        "--unlock-assets",
        metavar="ASSET[,ASSET...]",
        default=None,
        help=(
            "Remove these asset types from the lock for the selected "
            "range (comma-separated, or 'all'). A name that was never "
            "locked for that exact range is silently ignored, not an "
            "error."
        ),
    )

    parser.add_argument(
        "--list",
        action="store_true",
        help=(
            "List this archive's current locks and exit. Ignores "
            "--from/--until/--timestamp - always shows every lock "
            "entry, since a lock's own range is exactly what's being "
            "listed."
        ),
    )

    args = parser.parse_args(argv)

    modes = (
        args.lock_assets is not None,
        args.unlock_assets is not None,
        args.list,
    )
    if sum(modes) != 1:
        parser.error(
            "specify exactly one of --lock-assets, --unlock-assets, "
            "or --list"
        )

    if args.lock_assets is not None:
        args.lock_assets = _split_assets(parser, args.lock_assets)

    if args.unlock_assets is not None:
        args.unlock_assets = _split_assets(parser, args.unlock_assets)

    return args


def _default_warn(message: str) -> None:
    import sys

    print(message, file=sys.stderr)


def _run(args: argparse.Namespace, *, say=print, warn=_default_warn) -> int:
    """Run bv-lock for already-parsed arguments.

    `say`/`warn` are injectable, matching every other bv-* command's
    own `_run()` - kept even though bv-lock has no bv-web job-runner
    wiring yet, so adding one later doesn't need a signature change.
    """

    archive_path, _camera_config = resolve_archive_path(
        args.path, args.config_dir
    )

    if args.list:
        manifest = load_lock_manifest(archive_path)
        if not manifest.entries:
            say(f"bv-lock: {archive_path} - no locks.")
            return EXIT_OK

        for entry in sorted(manifest.entries, key=lambda e: (e.first, e.last)):
            say(
                f"{entry.first}..{entry.last}: "
                f"{', '.join(sorted(entry.assets))} "
                f"(locked {entry.locked_at})"
            )
        return EXIT_OK

    try:
        interval = LexicalTimeParser(
            timestamp=args.timestamp, from_=args.from_, until=args.until
        ).parse()
    except ValueError as exc:
        warn(f"bv-lock: {exc}")
        return EXIT_ARGS_ERROR

    try:
        manifest = load_lock_manifest(archive_path)

        if args.lock_assets is not None:
            manifest = add_lock_assets(manifest, interval, args.lock_assets)
            save_lock_manifest(archive_path, manifest)
            say(
                f"bv-lock: {archive_path} - locked "
                f"[{', '.join(sorted(args.lock_assets))}] for "
                f"{interval.first}..{interval.last}"
            )
        else:
            manifest = remove_lock_assets(
                manifest, interval, args.unlock_assets
            )
            save_lock_manifest(archive_path, manifest)
            say(
                f"bv-lock: {archive_path} - unlocked "
                f"[{', '.join(sorted(args.unlock_assets))}] for "
                f"{interval.first}..{interval.last}"
            )
    except LockError as exc:
        warn(f"bv-lock: {exc}")
        return EXIT_ARGS_ERROR

    return EXIT_OK


def main(argv: list[str] | None = None) -> int:
    """Run bv-lock."""

    args = parse_args(argv)
    return run_cli("bv-lock", lambda: _run(args), argv=argv)


if __name__ == "__main__":
    raise SystemExit(main())
