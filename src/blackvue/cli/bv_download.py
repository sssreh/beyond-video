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
from ..archive.configuration import RECORD_TIME_SUFFIX
from ..archive.configuration import parse_record_time_seconds
from ..archive.configuration import read_record_time_snapshot
from ..archive.configuration import write_record_time_snapshot
from ..core.blackvue_camera import BlackVueCamera
from ..core.blackvue_client import BlackVueClient
from ..core.camera_config import CameraConfigError
from ..core.camera_config import config_path
from ..core.camera_config import default_config_dir
from ..core.camera_config import load_camera_config
from ..core.connection import CameraUnreachableError
from ..core.connection import connect
from ..core.endpoint import Endpoint
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

ALL_KINDS = frozenset({"N", "E", "M", "P", "A"})

TRACE_INTERVAL_BYTES = 10 * 1024 * 1024


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
            f"(expected a comma-separated list of A, E, M, N, P, or 'all')"
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


def describe_recording_files(
    recording: Recording,
    want_video: bool,
) -> Iterator[tuple[str, bool]]:
    """Yield (filename, would_download) for every entry in a
    recording, under --dry-run --files.

    Mirrors the exact select= logic BlackVueCamera.download() is
    actually given at the real download call site below (select=None
    downloads everything when want_video is True; the metadata-only
    branch keeps only non-video entries otherwise) - kept as its own
    function so this listing can never silently drift from what a
    real run would do, and so it's testable without a live camera.
    """

    for entry in recording.entries:
        would_download = want_video or not entry.is_video
        yield entry.path.name, would_download


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
            "recording, regardless of mode. Either ID (a camera set up "
            "with bv-config) or --host/--target (a direct one-off "
            "connection, no config needed) is required."
        ),
        # See bv_export.py's own ArgumentParser for why: argparse's
        # default prefix-abbreviation matching silently breaks the
        # moment a sibling flag sharing a prefix gets added later.
        allow_abbrev=False,
    )

    parser.add_argument(
        "id",
        nargs="?",
        default=None,
        help=(
            "Camera system id (see bv-config). Omit this and use "
            "--host/--target instead to download without setting up a "
            "config first."
        ),
    )

    parser.add_argument(
        "--host",
        metavar="HOST",
        help=(
            "Connect directly to this camera address instead of "
            "looking up a configured id - e.g. its WiFi IP. Requires "
            "--target; cannot be combined with ID."
        ),
    )

    parser.add_argument(
        "--target",
        type=Path,
        metavar="DIR",
        help="Directory to download into. Requires --host.",
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
        metavar="{A,E,M,N,P,all}[,...]",
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
        "--files",
        action="store_true",
        help=(
            "With --dry-run, list every individual file (video, "
            "thumbnail, GPS, gsensor, etc.) for each matching "
            "recording, and whether it would be downloaded, instead "
            "of one summary line per recording id."
        ),
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

    args = parser.parse_args(argv)

    if args.files and not args.dry_run:
        parser.error("--files requires --dry-run")

    if args.id is None and args.host is None:
        parser.error("either ID or --host is required")

    if args.id is not None and args.host is not None:
        parser.error("--host cannot be combined with ID")

    if args.host is not None and args.target is None:
        parser.error("--host requires --target")

    if args.target is not None and args.host is None:
        parser.error("--target requires --host")

    return args


def _capture_record_time(
    client: BlackVueClient,
    destination: Path,
    recording_id: str,
    *,
    verbose: bool,
) -> None:
    """Fetch the camera's current config.ini, extract RecordTime, and
    write a new snapshot (see archive/configuration.py) into
    `destination` if either the value has changed since the most
    recently recorded one, or this run's earliest recording isn't
    already covered by an existing snapshot - a no-op only when
    neither is true, so a normal run doesn't grow the archive with a
    new file every time.

    `recording_id` anchors the snapshot to the earliest recording this
    run is considering (see this function's own call site) - since
    changing a setting on the camera reformats/wipes the SD card (see
    WORKING_CONTEXT.md), every recording still on the card at the time
    config.ini is fetched was necessarily made under this same
    RecordTime, so anchoring to the earliest one is always correct
    provenance, not just a guess.

    The "already covered" check matters because Archive.configuration()
    only ever applies a snapshot to recordings at or after its own
    anchor - never retroactively. Downloading a later batch first and
    an earlier batch afterwards (backfilling) means this run's anchor
    can predate every existing snapshot; without this check, an
    unchanged RecordTime value would skip the write entirely (the
    plain "did the value change" comparison this used to be), leaving
    that earlier recording - and its own configuration() lookups -
    uncovered even though nothing was actually lost, just never
    recorded from that far back. See WORKING_CONTEXT.md for the real
    case this was found from.

    Only the derived RecordTime integer is ever written - never the
    raw config.ini text, which also carries Wi-Fi/cloud credentials
    (see configuration.py's own module docstring).

    Best-effort: any failure here (endpoint unavailable on this
    firmware, unexpected config.ini shape, a transient network error)
    is reported to stderr if --verbose and otherwise silently ignored -
    this only ever informs bv-export's own --max-gap default, so it
    must never be allowed to fail a download run.
    """

    try:
        record_time_seconds = parse_record_time_seconds(client.config())
    except Exception as exc:
        if verbose:
            print(
                f"bv-download: couldn't read RecordTime from config.ini: {exc}",
                file=sys.stderr,
            )
        return

    destination.mkdir(parents=True, exist_ok=True)

    existing = sorted(destination.glob(f"*{RECORD_TIME_SUFFIX}"))

    if existing:
        earliest_id = existing[0].name.removesuffix(RECORD_TIME_SUFFIX)
        last_value = read_record_time_snapshot(existing[-1])
    else:
        earliest_id = None
        last_value = None

    already_covered = earliest_id is not None and recording_id >= earliest_id
    value_changed = last_value != record_time_seconds

    if already_covered and not value_changed:
        return

    write_record_time_snapshot(destination, recording_id, record_time_seconds)

    if verbose:
        if value_changed:
            print(
                f"bv-download: recorded RecordTime={record_time_seconds}s "
                f"as of {recording_id}"
            )
        else:
            print(
                f"bv-download: extended RecordTime={record_time_seconds}s "
                f"coverage back to {recording_id} (this run's earliest "
                "recording predates the archive's existing snapshot)"
            )


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

    if args.host is not None:
        #
        # --host/--target: a one-off connection with no saved config,
        # for people who just want to grab recordings and don't care
        # about the rest of the toolkit's setup. Single endpoint,
        # tried as-is - no bv-config wizard, no fallback endpoints.
        #
        endpoints = [Endpoint(name="host", address=args.host)]
        destination = args.target
        display_name = args.host
    else:
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

        endpoints = config.endpoints
        destination = config.target
        display_name = config.name

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
        endpoint, client = connect(endpoints, timeout=args.timeout)
    except CameraUnreachableError as exc:
        print(f"bv-download: {exc}", file=sys.stderr)
        return EXIT_UNREACHABLE

    if args.verbose:
        print(
            f"bv-download: connected to {display_name} "
            f"via {endpoint.name} ({endpoint.address})"
        )

    camera = BlackVueCamera(client)

    recordings = [
        recording
        for recording in camera.recordings()
        if recording.id in interval
    ]

    #
    # Some camera models (confirmed: Elite 10 - see WORKING_CONTEXT.md)
    # don't list .gps/.3gf sidecar files in their own recording
    # listing even though the files exist and download fine directly.
    # A no-op - zero extra network calls - on every model that already
    # lists them, so this costs nothing for the common case.
    #
    for recording in recordings:
        found = camera.probe_missing_sidecars(recording)

        if found and args.verbose:
            names = ", ".join(entry.path.name for entry in found)
            print(
                f"bv-download: {recording.id}: found {names} "
                "(not listed by the camera's own recording listing)"
            )

    interactive = sys.stdin.isatty() and sys.stdout.isatty()

    if interactive and not args.dry_run and not args.yes:
        if not confirm(recordings, interval):
            print("bv-download: aborted")
            return EXIT_ABORTED

    #
    # RecordTime snapshot capture is a beyond-video-specific
    # bookkeeping step (see _capture_record_time's own docstring) -
    # skipped in --host mode, which is meant to be a bare download
    # with no archive conventions imposed. If this same directory is
    # later downloaded into via a real bv-config id, the snapshot
    # just gets written on that first run instead.
    if not args.dry_run and recordings and args.host is None:
        _capture_record_time(
            client,
            destination,
            recordings[0].id,
            verbose=args.verbose,
        )

    if mode is not None:
        selection = select_by_mode(recordings, mode)
    else:
        selection = select_by_context(recordings)

    progress = DotProgress() if args.trace else None

    try:
        for recording, want_video in selection:
            if args.dry_run:
                if args.files:
                    print(f"{recording.id}:")
                    for filename, would_download in describe_recording_files(
                        recording, want_video
                    ):
                        marker = "download" if would_download else "skip"
                        print(f"  {filename}: {marker}")
                else:
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
