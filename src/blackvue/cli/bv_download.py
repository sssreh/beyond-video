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
from ..adapters.registry import AdapterNotFoundError
from ..adapters.registry import load_adapter_manifest
from ..core.blackvue_camera import BlackVueCamera
from ..core.blackvue_client import BlackVueClient
from ..core.camera_config import DEFAULT_ADAPTER_ID
from ..core.camera_config import CameraConfigError
from ..core.camera_config import config_path
from ..core.camera_config import default_config_dir
from ..core.camera_config import load_camera_config
from ..core.joblog import wrap_say
from ..core.joblog import wrap_warn
from ..core.connection import CameraUnreachableError
from ..core.connection import connect
from ..core.endpoint import Endpoint
from ..core.media_camera import MediaCamera
from ..domain.recording import Recording
from ..domain.vod_entry import VodEntry
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
EXIT_PARTIAL_FAILURE = 4

ALL_KINDS = frozenset({"N", "E", "M", "P", "A"})

TRACE_INTERVAL_BYTES = 10 * 1024 * 1024

# Camera direction letter -> full word, for the per-recording download
# report below (Christer's worked example spelled these out in full:
# "Metadata"/"Front"/"Rear" rather than "sidecars"/"F"/"R"). An
# unrecognized letter (see _on_entry's own fallback for a direction
# BlackVue hasn't shipped yet) just prints as-is via .get()'s default.
_DIRECTION_LABELS = {"F": "Front", "R": "Rear", "I": "Interior"}


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


def _summarize_found_kinds(found: list[VodEntry]) -> str:
    """Summarize probe_missing_sidecars()'s found entries into short,
    kind-level labels ("gps", "3gf", "thumbnails") for --verbose,
    instead of listing every individual filename. A recording with
    all three sidecar suffixes plus F/R/I thumbnails used to print
    five full filenames on one line; this collapses that down to
    three words. Order is fixed (gps, 3gf, thumbnails), not whatever
    order probe_missing_sidecars() happened to find them in.
    """

    suffixes = {entry.path.suffix.lower() for entry in found}
    kinds = []

    if ".gps" in suffixes:
        kinds.append("gps")
    if ".3gf" in suffixes:
        kinds.append("3gf")
    if ".thm" in suffixes:
        kinds.append("thumbnails")

    if len(kinds) <= 1:
        return "".join(kinds)

    return ", ".join(kinds[:-1]) + " and " + kinds[-1]


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
            "--target; cannot be combined with ID or --media."
        ),
    )

    parser.add_argument(
        "--media",
        type=Path,
        metavar="DIR",
        help=(
            "Import recordings directly from a mounted SD card, USB-"
            "connected camera, or other removable media at DIR instead "
            "of connecting over the network - no CGI protocol, just a "
            "plain filesystem copy. Combine with ID to import into "
            "that camera's configured archive, or with --target for a "
            "one-off import with no config. Cannot be combined with "
            "--host."
        ),
    )

    # Deprecated name for --media, kept working silently - --sdcard was
    # the documented, released (v1.0.0) flag name before Christer
    # pointed out that many cameras (GoPro included) are imported over
    # USB rather than via a card reader, so "sdcard" undersold what
    # this actually covers. help=SUPPRESS keeps it out of --help/usage
    # without breaking any existing script or muscle memory that still
    # types --sdcard.
    parser.add_argument(
        "--sdcard",
        dest="media",
        type=Path,
        metavar="DIR",
        help=argparse.SUPPRESS,
    )

    parser.add_argument(
        "--target",
        type=Path,
        metavar="DIR",
        help="Directory to download into. Requires --host or --media.",
    )

    parser.add_argument(
        "--config-dir",
        type=Path,
        default=default_config_dir(),
        help="Directory camera configs live in (default: %(default)s).",
    )

    # 30s, not 5s: this single value covers every request made with the
    # resulting client - endpoint connect, size/probe/sidecar checks,
    # and the download itself (see core/connection.py's connect()) -
    # and a real request over the "Internet" WAN-relay endpoint can
    # comfortably take longer than 5s to answer, especially the several
    # sequential sidecar-probe GETs that happen before a single byte of
    # video is even requested. Christer hit exactly this from a
    # terminal: a slow-but-working Internet-relay probe tripped the old
    # 5s default and printed "couldn't check for sidecar files (timed
    # out)" even though the recording downloaded fine right after.
    # bv-web's own job form (job_new_bv_download.html /
    # web/app.py's start_bv_download route) hardcodes its own,
    # separate 5s default rather than reading this one - deliberately
    # left alone, since a stuck background job ties up a job slot in a
    # way a person watching a terminal and free to Ctrl-C isn't
    # affected by.
    parser.add_argument(
        "--timeout",
        type=int,
        default=30,
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

    if args.id is None and args.host is None and args.media is None:
        parser.error("either ID, --host, or --media is required")

    if args.id is not None and args.host is not None:
        parser.error("--host cannot be combined with ID")

    if args.host is not None and args.media is not None:
        parser.error("--host cannot be combined with --media")

    if args.id is not None and args.target is not None:
        parser.error("--target cannot be combined with ID")

    if args.host is not None and args.target is None:
        parser.error("--host requires --target")

    if args.media is not None and args.id is None and args.target is None:
        parser.error("--media requires ID or --target")

    if args.target is not None and args.host is None and args.media is None:
        parser.error("--target requires --host or --media")

    return args


def _default_warn(message: str) -> None:
    """`_run()`'s default `warn` - real stderr, the CLI's normal
    error-output contract. See bv_config.py's own `_default_warn` for
    why this is a named function rather than a lambda."""

    print(message, file=sys.stderr)


def _write_record_time_snapshot_if_needed(
    destination: Path,
    recording_id: str,
    record_time_seconds: int,
    *,
    verbose: bool,
    say=print,
    warn=_default_warn,
) -> None:
    """Write a new RecordTime snapshot (see archive/configuration.py)
    into `destination` if either the value has changed since the most
    recently recorded one, or this run's earliest recording isn't
    already covered by an existing snapshot - a no-op only when
    neither is true, so a normal run doesn't grow the archive with a
    new file every time.

    Shared by both `_capture_record_time()` (the network path - reads
    config.ini over the wire) and `_capture_record_time_from_media()`
    (the --media path - reads it directly off the mounted card/drive):
    once a raw RecordTime integer has been obtained, from either
    source, what to do with it is identical - only how it's obtained
    differs.

    `recording_id` anchors the snapshot to the earliest recording this
    run is considering (see each call site above) - since changing a
    setting on the camera reformats/wipes the SD card (see
    WORKING_CONTEXT.md), every recording still on the card at the time
    config.ini is read was necessarily made under this same
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

    `say`/`warn` are injectable (default: real stdout/stderr via
    print/`_default_warn`) so bv-web's job runner (see web/jobs.py)
    can capture this function's own --verbose output into a job's
    transcript the same way `_run()` below does for the rest of this
    module.
    """

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
            say(
                f"bv-download: recorded RecordTime={record_time_seconds}s "
                f"as of {recording_id}"
            )
        else:
            say(
                f"bv-download: extended RecordTime={record_time_seconds}s "
                f"coverage back to {recording_id} (this run's earliest "
                "recording predates the archive's existing snapshot)"
            )


def _capture_record_time(
    client: BlackVueClient,
    destination: Path,
    recording_id: str,
    *,
    verbose: bool,
    say=print,
    warn=_default_warn,
) -> None:
    """Fetch the camera's current config.ini over the network, extract
    RecordTime, and hand off to _write_record_time_snapshot_if_needed()
    - see that function's own docstring for the write logic shared
    with the --media path.

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
            warn(f"bv-download: couldn't read RecordTime from config.ini: {exc}")
        return

    _write_record_time_snapshot_if_needed(
        destination,
        recording_id,
        record_time_seconds,
        verbose=verbose,
        say=say,
        warn=warn,
    )


def _capture_record_time_from_media(
    camera: MediaCamera,
    destination: Path,
    recording_id: str,
    *,
    verbose: bool,
    say=print,
    warn=_default_warn,
) -> None:
    """Read config.ini directly off the mounted card/drive (see
    MediaCamera.read_config_text()'s own docstring for the candidate
    paths tried), extract RecordTime, and hand off to
    _write_record_time_snapshot_if_needed() - the --media counterpart
    to _capture_record_time()'s network version.

    Best-effort, same as the network version: media with no readable
    config.ini (its real on-disk location isn't confirmed yet - see
    docs/CAMERA_ADAPTERS.md) only means bv-export's own --max-gap
    default goes unset, never a reason to fail an import run.
    """

    text = camera.read_config_text()

    if text is None:
        if verbose:
            warn(
                "bv-download: no config.ini found on the imported "
                "media - skipping RecordTime capture"
            )
        return

    try:
        record_time_seconds = parse_record_time_seconds(text)
    except Exception as exc:
        if verbose:
            warn(
                "bv-download: couldn't read RecordTime from the "
                f"imported media's config.ini: {exc}"
            )
        return

    _write_record_time_snapshot_if_needed(
        destination,
        recording_id,
        record_time_seconds,
        verbose=verbose,
        say=say,
        warn=warn,
    )


def _destination_message(
    display_name: str, destination: Path, *, dry_run: bool
) -> str:
    """The one-line "here's where this run is downloading into" message
    printed unconditionally near the start of every run - not gated
    behind --verbose, since the target folder is basic context for
    what's about to happen, not extra diagnostic detail. Matches
    bv-export's own "always state the destination folder" convention
    (its dry-run preview and its real per-trip write-confirmation both
    print the folder path unconditionally too - see bv_export.py).
    """

    verb = "would download into" if dry_run else "downloading into"
    return f"bv-download: {display_name}: {verb} {destination}"


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


def _run(
    args: argparse.Namespace, *, say=print, warn=_default_warn
) -> int:
    """Run bv-download for already-parsed arguments.

    `say`/`warn` are injectable (default: real stdout/stderr via
    print/`_default_warn`) so bv-web's job runner (see web/jobs.py)
    can capture this command's output into a job's transcript instead
    of the real terminal - same pattern as bv_generate.py/bv_gps.py's
    own `_run()`.

    Unlike those two, this module does have one interactive prompt -
    confirm(), gated on `interactive = sys.stdin.isatty() and
    sys.stdout.isatty()` below - but it's never reached from the job
    runner regardless: web/jobs.py's start_bv_download() always
    passes --yes, since confirm()'s own input() call reads the real
    process stdin, which the job runner has no way to answer through
    its own ask()/Job.submit_answer() prompt mechanism (unlike
    bv-config's wizard, which is built around exactly that) - a
    real-stdin block here would hang the job forever with no way for
    the browser to unblock it. See start_bv_download()'s own
    docstring for the forced --yes.
    """

    # Three ways to say where recordings come from and where they go:
    # --host/--target (one-off network connection, no config), --media
    # alone/with --target (one-off local import, no config), or ID
    # (either over the network or, combined with --media, from a
    # mounted card/drive) using a saved camera config for the
    # destination. `endpoints`/`client` stay None on the --media path -
    # there's no network connection to make at all (see the `camera`
    # construction a little further down).
    endpoints: list[Endpoint] | None = None
    client: BlackVueClient | None = None
    # Non-None only for an ID-backed --media import whose camera config
    # picked a non-BlackVue adapter (e.g. "gopro") - drives both
    # MediaCamera's recognizer (see the construction below) and the
    # mode-default decision further down, since a manifest-driven
    # camera has no event/manual/parking kind vocabulary at all for
    # select_by_context()'s own BlackVue-specific heuristic to work
    # with.
    media_adapter_id: str | None = None

    if args.media is not None:
        #
        # --media: import recordings directly from a mounted SD card,
        # USB-connected camera, or other removable media - no CGI wire
        # protocol, just the camera's own file layout on disk (see
        # MediaCamera's own docstring and docs/CAMERA_ADAPTERS.md's
        # "Add SD-card import" step - the flag itself was renamed from
        # --sdcard, kept working as a hidden alias, once it became
        # clear this covers USB-connected cameras too, not just card
        # readers).
        #
        if args.id is not None:
            path = config_path(args.config_dir, args.id)

            try:
                config = load_camera_config(path)
            except CameraConfigError as exc:
                warn(f"bv-download: {exc}")
                return EXIT_CONFIG_ERROR

            destination = config.archive
            display_name = config.name
            media_adapter_id = config.adapter
        else:
            destination = args.target
            display_name = str(args.media)

        if media_adapter_id is None or media_adapter_id == DEFAULT_ADAPTER_ID:
            # BlackVue (or a bare --media with no config at all, which
            # has no adapter to consult) - the original, strict
            # filename-convention recognizer, byte-for-byte unchanged.
            camera = MediaCamera(args.media)
        else:
            # A non-BlackVue adapter (GoPro today) - its own manifest
            # drives recognition instead (extension match, mtime
            # timestamp - see MediaCamera/_scan()'s own docstring).
            try:
                manifest = load_adapter_manifest(media_adapter_id)
            except AdapterNotFoundError as exc:
                warn(f"bv-download: {exc}")
                return EXIT_CONFIG_ERROR
            camera = MediaCamera(args.media, manifest=manifest)
    elif args.host is not None:
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
            warn(f"bv-download: {exc}")
            return EXIT_CONFIG_ERROR

        if not config.endpoints:
            warn(f"bv-download: {path}: no [[endpoint]] entries found")
            return EXIT_CONFIG_ERROR

        endpoints = config.endpoints
        destination = config.archive
        display_name = config.name

    say(_destination_message(display_name, destination, dry_run=args.dry_run))

    try:
        interval = LexicalTimeParser(
            timestamp=args.timestamp,
            from_=args.from_,
            until=args.until,
        ).parse()
    except ValueError as exc:
        warn(f"bv-download: {exc}")
        return EXIT_CONFIG_ERROR

    has_range = (
        args.from_ is not None
        or args.until is not None
        or args.timestamp is not None
    )

    #
    # A specific range was asked for explicitly - default to
    # fetching everything in it, unless --mode said otherwise. (A
    # non-BlackVue adapter's --media import - e.g. GoPro - is handled
    # separately below, bypassing mode/kind selection entirely: see
    # is_generic_media.)
    #
    mode = args.mode
    if mode is None and has_range:
        mode = ALL_KINDS

    # A manifest-driven camera (e.g. GoPro) has no BlackVue-style kind
    # letter at all - domain.Recording.kind is "" for it (see its own
    # docstring), which never matches ALL_KINDS = {"N","E","M","P","A"}
    # and would silently download nothing. select_by_context()'s
    # event/manual-plus-context heuristic is equally meaningless for
    # it. So bypass both: every matched recording just downloads,
    # matching gopro/manifest.json's own "no --mode filtering... every
    # file is just 'a video'" contract.
    is_generic_media = (
        media_adapter_id is not None and media_adapter_id != DEFAULT_ADAPTER_ID
    )

    if args.media is not None:
        # `camera` (a MediaCamera) was already constructed above,
        # eagerly scanning the card/drive - see the source-setup
        # block. Nothing to connect to.
        scan = camera.scan_summary()
        recognized_label = (
            "recordings" if is_generic_media else "BlackVue-named recordings"
        )

        if scan.recognized_file_count == 0:
            say(
                f"bv-download: {args.media}: no {recognized_label} "
                f"found ({scan.total_files_seen} file(s) "
                "scanned)"
            )
        elif args.verbose:
            say(
                f"bv-download: {args.media}: found "
                f"{len(scan.recordings)} recording(s) across "
                f"{scan.recognized_file_count} recognized file(s) "
                f"(of {scan.total_files_seen} scanned)"
            )
    else:
        try:
            endpoint, client = connect(endpoints, timeout=args.timeout)
        except CameraUnreachableError as exc:
            warn(f"bv-download: {exc}")
            return EXIT_UNREACHABLE

        if args.verbose:
            say(
                f"bv-download: connected to {display_name} "
                f"via {endpoint.name} ({endpoint.address})"
            )

        camera = BlackVueCamera(client)

    if is_generic_media:
        # A manifest-driven camera's recording id is just the source
        # file's own stem (e.g. GoPro's "GH010123" - see
        # MediaCamera/_scan()'s manifest-driven path) with no
        # timestamp in it at all, unlike BlackVue's YYYYMMDD_HHMMSS
        # convention. TimeInterval.__contains__() does a lexical
        # string comparison assuming that convention - id shapes like
        # "GH010123" sort lexically *after* the default full-range
        # upper bound ("99991231_235959", since 'G' > '9'), so the
        # interval filter would silently exclude every recording
        # instead of erroring. --from/--until/--timestamp aren't
        # meaningful for this adapter shape (see gopro/manifest.json's
        # own "no --mode filtering" note) - every recognized file is
        # included, unfiltered.
        recordings = list(camera.recordings())
    else:
        recordings = [
            recording
            for recording in camera.recordings()
            if recording.id in interval
        ]

    # Recordings already fully present at the destination are dropped
    # here, before the "Matching recordings" listing/confirmation
    # prompt even builds - Christer: "ignore files already fully
    # downloaded". download() already skipped re-copying their bytes
    # (see MediaCamera.download()'s own docstring), but that's a
    # separate concern from the UX one: without this, the same
    # recording kept showing up in the listing and needing
    # re-confirming on every re-run even though it needed nothing
    # further. Only MediaCamera implements is_fully_downloaded() - a
    # network listing has no local "already there" concept the way a
    # filesystem scan does - so this is a no-op (hasattr() is False)
    # for a --host/ID network download.
    if hasattr(camera, "is_fully_downloaded"):
        already_downloaded = {
            recording.id
            for recording in recordings
            if camera.is_fully_downloaded(recording, destination)
        }

        if already_downloaded:
            say(
                f"bv-download: {len(already_downloaded)} recording(s) "
                "already fully downloaded, skipping"
            )
            recordings = [
                recording
                for recording in recordings
                if recording.id not in already_downloaded
            ]

    # Sidecar probing (see the loop below, right before each recording
    # is actually listed/downloaded) is deliberately NOT done here, up
    # front for every matching recording - it used to be, but that ran
    # before the confirmation prompt even appeared, for every recording
    # in the whole range regardless of whether the user went on to
    # download any of it. On a camera/firmware combination whose
    # listing doesn't already include sidecars (see
    # probe_missing_sidecars()'s own docstring), that's several extra
    # HTTP round-trips per recording - Christer: "it takes a long time
    # and nothing is happening" - and it was also printing "found
    # ..." lines under --verbose before he'd been asked "Proceed with
    # download?" at all, misleadingly suggesting a download was already
    # underway.

    interactive = sys.stdin.isatty() and sys.stdout.isatty()

    if interactive and not args.dry_run and not args.yes:
        if not confirm(recordings, interval):
            say("bv-download: aborted")
            return EXIT_ABORTED

    #
    # RecordTime snapshot capture is a beyond-video-specific
    # bookkeeping step (see _write_record_time_snapshot_if_needed()'s
    # own docstring) - skipped for a bare one-off run with no archive
    # conventions imposed (--host, or --media used without ID). If
    # this same directory is later downloaded/imported into via a real
    # bv-config id, the snapshot just gets written on that first run
    # instead.
    bare_run = args.host is not None or (
        args.media is not None and args.id is None
    )

    if not args.dry_run and recordings and not bare_run:
        if args.media is not None:
            _capture_record_time_from_media(
                camera,
                destination,
                recordings[0].id,
                verbose=args.verbose,
                say=say,
                warn=warn,
            )
        else:
            _capture_record_time(
                client,
                destination,
                recordings[0].id,
                verbose=args.verbose,
                say=say,
                warn=warn,
            )

    if is_generic_media:
        selection = ((recording, True) for recording in recordings)
    elif mode is not None:
        selection = select_by_mode(recordings, mode)
    else:
        selection = select_by_context(recordings)

    # DotProgress prints its dots straight to real stdout, not through
    # `say` - deliberately not made injectable. It's a partial-line,
    # no-trailing-newline "still alive" stream (see its own docstring)
    # meant for someone watching a real terminal; Job.output (see
    # web/jobs.py) is a list of complete lines, so there's no sensible
    # way to represent an in-progress dot-stream there anyway. A
    # --trace job from the browser still runs fine - the dots just go
    # to bv-web's own process console/log instead of the job's own
    # output, the same as any other --trace run whose stdout happens
    # to be redirected somewhere other than a live terminal.
    progress = DotProgress() if args.trace else None

    #
    # Each recording's network calls (sidecar probing, then the
    # actual download) are wrapped individually below so that one
    # recording's network hiccup - a dropped WiFi frame, a timeout
    # mid-download - doesn't abort the rest of an otherwise-healthy
    # batch. Before this, any OSError/TimeoutError from inside this
    # loop propagated straight up to run_cli()'s generic catch-all
    # (see errors.py), which prints a single bare "bv-download: timed
    # out" with no indication of which recording or which step failed,
    # and gives up on every recording still left in the batch - even
    # ones a retry moments later would have fetched fine. Christer hit
    # exactly this after confirming a 10-recording range: one bare
    # timeout, nothing downloaded, no clue which of the 10 it was.
    #
    failed_ids: list[str] = []

    #
    # Christer: "a total duration for all the sidecars and ... a
    # duration each video per recordingid" - meaning download *time*,
    # not video content length. Later: "an average download speed in
    # parantheses for video files." Later still: "I want separate
    # download time for all sidecars together, then for each video
    # file i want download time and speed" - a talkative, per-
    # recording breakdown (id header line, then indented detail
    # lines). And finally: "when every task is done, the [ids] where
    # already downloaded" plus a worked example - every recording in
    # the range gets its own id line printed unconditionally (not
    # gated behind --verbose, and no `(kind)` annotation cluttering
    # it), and only a recording that actually transferred something
    # gets indented detail lines under its id; one already fully
    # present just shows its bare id with nothing under it. Christer's
    # example also spelled out "Metadata"/"Front"/"Rear" in full
    # rather than "sidecars"/"F"/"R" - see _DIRECTION_LABELS below.
    # sidecar_seconds/sidecar_files are reset per recording (see the
    # `.clear()`/reassignment right before camera.download() in the
    # loop below) - "all sidecars together" means every .gps/.3gf/.thm
    # file *for that one recording* combined into one figure, not a
    # single total across the whole run. video_stats is likewise
    # rebuilt fresh per recording, keyed by camera direction letter
    # (F/R/I, mapped to a full word only at print time - see
    # _DIRECTION_LABELS) since that's the actual file-level
    # granularity for BlackVue (one video file per direction) - each
    # value is (elapsed_seconds, bytes_transferred) so the line can
    # show both the duration and a derived MB/s figure. Speed is
    # video-only, per Christer's request; sidecars never get one.
    # Both are populated via camera.download()'s on_entry hook (see
    # core/blackvue_camera.py / core/media_camera.py), which only
    # fires for an entry that actually transferred bytes - an
    # already-up-to-date recording contributes nothing to either
    # rather than polluting them with a near-zero/undefined-speed
    # measurement (and, per the above, ends up with no detail lines
    # under its id at all).
    #
    sidecar_seconds = 0.0
    sidecar_files = 0
    video_stats: dict[str, tuple[float, int]] = {}

    def _on_entry(
        entry: VodEntry, elapsed_seconds: float, bytes_transferred: int
    ) -> None:
        nonlocal sidecar_seconds, sidecar_files

        if entry.is_video:
            if entry.is_front:
                direction = "F"
            elif entry.is_rear:
                direction = "R"
            elif entry.is_interior:
                direction = "I"
            else:
                direction = entry.path.stem[-1]

            video_stats[direction] = (elapsed_seconds, bytes_transferred)
        else:
            sidecar_seconds += elapsed_seconds
            sidecar_files += 1

    try:
        for recording, want_video in selection:
            #
            # blackvue_vod.cgi's own recording listing has consistently
            # only ever contained video files across the camera models
            # confirmed so far (see WORKING_CONTEXT.md) - .gps/.3gf/.thm
            # sidecars exist and download fine directly even though the
            # listing never mentions them. A no-op - zero extra network
            # calls - on any camera/firmware combination that does list
            # them, so this costs nothing for that case. Probed here,
            # one recording at a time right before it's actually listed
            # (--dry-run --files, which needs entries populated to
            # describe them) or downloaded - not eagerly for the whole
            # matching range before the user has even been asked
            # "Proceed with download?" (see the comment above this
            # loop's own recordings/selection setup for why that used
            # to be a problem).
            #
            try:
                found = camera.probe_missing_sidecars(recording)
            except OSError as exc:
                # A failed probe only means the opportunistic sidecar
                # check itself didn't complete - it says nothing about
                # whether the recording's actual video is reachable.
                # Falling through to the download step below (instead
                # of `continue`-ing past it) matters most for a
                # recording with a partially-downloaded video already
                # on disk from an earlier run: camera.download()'s own
                # size-comparison/resume logic is what would have
                # fixed that, and skipping the recording entirely here
                # meant it never got the chance to run. Christer hit
                # this: a partial video stopped getting repaired once
                # its sidecar probe started hitting a transient
                # WiFi/timeout error on every run.
                found = []
                warn(
                    f"bv-download: {recording.id}: couldn't check for "
                    f"sidecar files ({exc}) - continuing without it"
                )

            if found and args.verbose:
                summary = _summarize_found_kinds(found)
                say(
                    f"bv-download: {recording.id}: found {summary} "
                    "for downloading"
                )

            if args.dry_run:
                if args.files:
                    say(f"{recording.id}:")
                    for filename, would_download in describe_recording_files(
                        recording, want_video
                    ):
                        marker = "download" if would_download else "skip"
                        say(f"  {filename}: {marker}")
                else:
                    kind = "video+metadata" if want_video else "metadata only"
                    say(f"{recording.id}: {kind}")
                continue

            select = None if want_video else (lambda entry: not entry.is_video)
            video_stats.clear()
            sidecar_seconds = 0.0
            sidecar_files = 0

            try:
                camera.download(
                    recording,
                    destination,
                    select=select,
                    on_bytes=progress,
                    on_entry=_on_entry,
                )
            except OSError as exc:
                failed_ids.append(recording.id)
                warn(
                    f"bv-download: {recording.id}: download failed "
                    f"({exc}) - skipping to the next recording"
                )
                continue

            # Christer's worked example ("when every task is done, the
            # [ids] where already downloaded", with every id in the
            # range listed and only some carrying indented detail
            # lines underneath) prints every recording's bare id
            # unconditionally - no `(kind)` annotation, no trailing
            # colon - whether it needed a download or was already
            # fully present. Detail lines (Metadata/Front/Rear/
            # Interior) only appear underneath a recording that
            # actually transferred something, via the same
            # sidecar_files/video_stats populated by _on_entry above.
            # This replaces the old changed/--verbose gating: there's
            # no separate "already up to date" message anymore, since
            # a bare id with nothing indented under it already says
            # that.
            say(recording.id)

            if sidecar_files:
                say(f"  Metadata: {sidecar_seconds:.1f}s")

            for direction, (seconds, transferred) in sorted(
                video_stats.items()
            ):
                label = _DIRECTION_LABELS.get(direction, direction)
                line = f"  {label}: {seconds:.1f}s"

                if seconds > 0:
                    mb_per_s = transferred / (1024 * 1024) / seconds
                    line += f" ({mb_per_s:.1f} MB/s)"

                say(line)
    finally:
        if progress is not None:
            progress.finish()

    if failed_ids:
        warn(
            f"bv-download: {len(failed_ids)} of "
            f"{len(recordings)} recording(s) failed and were skipped: "
            f"{', '.join(failed_ids)}"
        )
        return EXIT_PARTIAL_FAILURE

    return EXIT_OK


def main(argv: list[str] | None = None) -> int:
    """Run bv-download."""

    args = parse_args(argv)
    # See bv_scribe.py's own main() for why - wrap_say()/wrap_warn()
    # (core/joblog.py) mirror every printed line into the persistent
    # output log alongside the real terminal output.
    say = wrap_say("bv-download")
    warn = wrap_warn("bv-download", _default_warn)
    return run_cli(
        "bv-download", lambda: _run(args, say=say, warn=warn), argv=argv
    )


if __name__ == "__main__":
    raise SystemExit(main())
