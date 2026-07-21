"""
bv-download.

Copyright (C) 2026 Christer R. (sssreh)

SPDX-License-Identifier: GPL-3.0-or-later
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Iterable
from collections.abc import Iterator
from pathlib import Path

from .errors import run_cli
from ..core.blackvue_camera import BlackVueCamera
from ..core.camera_config import CameraConfigError
from ..core.camera_config import config_path
from ..core.camera_config import default_config_dir
from ..core.camera_config import load_camera_config
from ..core.connection import CameraUnreachableError
from ..core.connection import connect
from ..domain.recording import Recording
from ..humantimeformatter import HumanTimeFormatter
from ..lexicaltimeparser import LexicalTimeParser
from ..lexicaltimeparser import TimeInterval

#
# Exit codes.
#
# A cron job triggers this hourly, and most runs will find the
# camera unreachable (car away from every known endpoint). That is
# an expected outcome, not a failure worth alerting on - it gets its
# own exit code so a scheduler can tell it apart from a real error.
#
EXIT_OK = 0
EXIT_CONFIG_ERROR = 1
EXIT_UNREACHABLE = 2
EXIT_ABORTED = 3

ALL_KINDS = frozenset({"N", "E", "M", "P"})

TRACE_INTERVAL_BYTES = 50 * 1024 * 1024


class DotProgress:
    """A --trace progress indicator: print '.' to stdout every
    TRACE_INTERVAL_BYTES downloaded, across the whole run (not reset
    per file) - a simple "still alive" signal for long downloads, not
    a percentage (the total size across a run isn't known upfront).

    Call instances directly as the on_bytes callback passed to
    BlackVueCamera.download() / BlackVueClient.download(); call
    finish() once at the end of the run to close the line with a
    trailing newline - but only if at least one dot was ever printed,
    so a --trace run that downloads nothing doesn't print a stray
    blank line.
    """

    def __init__(self, interval_bytes: int = TRACE_INTERVAL_BYTES) -> None:
        self._interval_bytes = interval_bytes
        self._accumulated_bytes = 0
        self._dots_printed = 0

    def __call__(self, byte_count: int) -> None:
        self._accumulated_bytes += byte_count
        dots_due = self._accumulated_bytes // self._interval_bytes

        while self._dots_printed < dots_due:
            print(".", end="", flush=True)
            self._dots_printed += 1

    def finish(self) -> None:
        if self._dots_printed:
            print()


def parse_mode(value: str) -> frozenset[str]:
    """Parse a --mode value into a set of recording kind letters."""

    if value.strip().lower() == "all":
        return ALL_KINDS

    kinds = frozenset(
        part.strip().upper()
        for part in value.split(",")
        if part.strip()
    )

    invalid = kinds - ALL_KINDS

    if invalid or not kinds:
        raise argparse.ArgumentTypeError(
            f"invalid --mode value {value!r} "
            f"(expected a comma-separated list of E, M, N, P, or 'all')"
        )

    return kinds


def select_by_mode(
    recordings: Iterable[Recording],
    mode: frozenset[str],
) -> Iterator[tuple[Recording, bool]]:
    """Select recordings by kind only.

    Video is downloaded for every recording whose kind is in mode.
    There is no context/previous-recording logic here - mode fully
    determines what is downloaded.
    """

    for recording in recordings:
        yield recording, recording.kind in mode


def select_by_context(
    recordings: Iterable[Recording],
) -> Iterator[tuple[Recording, bool]]:
    """Select recordings using the default event/manual policy.

    Video is downloaded for every event and manual recording, plus
    the one recording immediately before each (of any kind), for
    pre-event context. Every other recording is metadata-only.
    """

    pending: Recording | None = None

    for recording in recordings:
        if recording.is_event or recording.is_manual:
            if pending is not None:
                yield pending, True
                pending = None

            yield recording, True
        else:
            if pending is not None:
                yield pending, False

            pending = recording

    if pending is not None:
        yield pending, False


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Parse command line arguments."""

    parser = argparse.ArgumentParser(
        prog="bv-download",
        description=(
            "Download recordings from a BlackVue camera. By default, "
            "downloads video for event and manual recordings plus the "
            "recording immediately before each, for context. Metadata "
            "(thumbnails, GPS, gsensor) is always downloaded for every "
            "recording, regardless of mode."
        ),
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
        "--mode",
        type=parse_mode,
        default=None,
        metavar="{E,M,N,P,all}[,...]",
        help=(
            "Recording kinds to download video for (comma-separated, "
            "case-insensitive), or 'all'. Default: event/manual "
            "recordings plus the recording before each, for context. "
            "If --from/--until/--timestamp is given without --mode, "
            "the default becomes 'all', since a specific range was "
            "requested explicitly."
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
        "--dry-run",
        action="store_true",
        help="List what would be downloaded without downloading it.",
    )

    parser.add_argument(
        "--yes",
        action="store_true",
        help="Skip the interactive range confirmation.",
    )

    parser.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="Print each file as it is downloaded.",
    )

    parser.add_argument(
        "--trace",
        action="store_true",
        help=(
            "Print a '.' for every "
            f"{TRACE_INTERVAL_BYTES // (1024 * 1024)}MB downloaded, as "
            "a simple progress indicator across the whole run."
        ),
    )

    return parser.parse_args(argv)


def confirm(
    recordings: list[Recording],
    interval: TimeInterval,
) -> bool:
    """Show the resolved range and ask the user to confirm it.

    Only called when running interactively (a real terminal), so
    this never blocks an unattended/cron run.
    """

    human = HumanTimeFormatter(interval)

    print(f"Range: {human.first} to {human.last}")
    print(f"Matching recordings ({len(recordings)}):")

    for recording in recordings:
        print(f"  {recording.id}")

    answer = input("Proceed with download? [y/N] ").strip().lower()

    return answer in ("y", "yes")


def _run(args: argparse.Namespace) -> int:
    """Run bv-download for already-parsed arguments."""

    path = config_path(args.config_dir, args.id)

    try:
        config = load_camera_config(path)
    except CameraConfigError as exc:
        print(f"bv-download: {exc}", file=sys.stderr)
        return EXIT_CONFIG_ERROR

    if not config.endpoints:
        print(
            f"bv-download: {path}: no [[endpoint]] entries found",
            file=sys.stderr,
        )
        return EXIT_CONFIG_ERROR

    try:
        interval = LexicalTimeParser(
            timestamp=args.timestamp,
            from_=args.from_,
            until=args.until,
        ).parse()
    except ValueError as exc:
        print(f"bv-download: {exc}", file=sys.stderr)
        return EXIT_CONFIG_ERROR

    has_range = (
        args.from_ is not None
        or args.until is not None
        or args.timestamp is not None
    )

    #
    # A specific range was asked for explicitly - default to
    # fetching everything in it, unless --mode said otherwise.
    #
    mode = args.mode
    if mode is None and has_range:
        mode = ALL_KINDS

    try:
        endpoint, client = connect(config.endpoints, timeout=args.timeout)
    except CameraUnreachableError as exc:
        print(f"bv-download: {exc}", file=sys.stderr)
        return EXIT_UNREACHABLE

    if args.verbose:
        print(
            f"bv-download: connected to {config.name} "
            f"via {endpoint.name} ({endpoint.address})"
        )

    destination = config.target

    camera = BlackVueCamera(client)

    recordings = [
        recording
        for recording in camera.recordings()
        if recording.id in interval
    ]

    interactive = sys.stdin.isatty() and sys.stdout.isatty()

    if interactive and not args.dry_run and not args.yes:
        if not confirm(recordings, interval):
            print("bv-download: aborted")
            return EXIT_ABORTED

    if mode is not None:
        selection = select_by_mode(recordings, mode)
    else:
        selection = select_by_context(recordings)

    progress = DotProgress() if args.trace else None

    try:
        for recording, want_video in selection:
            if args.dry_run:
                kind = "video+metadata" if want_video else "metadata only"
                print(f"{recording.id}: {kind}")
                continue

            select = None if want_video else (lambda entry: not entry.is_video)

            changed = camera.download(
                recording,
                destination,
                select=select,
                on_bytes=progress,
            )

            if args.verbose and changed:
                print(f"{recording.id}: downloaded")
    finally:
        if progress is not None:
            progress.finish()

    return EXIT_OK


def main(argv: list[str] | None = None) -> int:
    """Run bv-download."""

    args = parse_args(argv)
    return run_cli("bv-download", lambda: _run(args))


if __name__ == "__main__":
    raise SystemExit(main())
