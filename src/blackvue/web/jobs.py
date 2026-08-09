"""
Background job runner for bv-web: lets the browser trigger bv-config's
interactive wizard and bv-gps, watch their output, and answer
bv-config's prompts as they come up.

Design: each job runs the target CLI module's own already-tested
_run() function directly, in a background thread, in-process -
deliberately not a subprocess. bv-config's _run()/run_wizard() (see
cli/bv_config.py) and bv-gps's _run() (see cli/bv_gps.py) both accept
injectable ask/say/warn callables (default input/print/stderr-print,
for real terminal use unchanged) - the job runner supplies its own,
which write into a per-job Job.output list and, for ask, block on a
per-job queue.Queue until the browser POSTs an answer.

That sidesteps a real problem a subprocess approach would have: a
real subprocess's stdout can't reliably tell "a prompt is waiting for
input" apart from "more output is still coming", since input()'s own
prompt text has no trailing newline - scraping raw bytes for that
would need fragile heuristics. Calling the real Python function
directly means the job runner controls exactly when ask()/say() are
invoked, no scraping needed. Running in-process also means each job's
own ask/say closures only ever touch that one job's own Job.output
list - nothing needs to touch the real process-wide sys.stdout at
all, so jobs are safe to run concurrently without stepping on each
other's output (a real hazard with the alternative of redirecting
sys.stdout globally for the duration of a job).

Cancellation (Job.cancel()/JobRunner.cancel()): Python can't
force-kill a thread, so this is honest, not absolute. A job currently
WAITING_FOR_INPUT is unblocked immediately via a sentinel pushed onto
its own answer queue - ask() recognizes it and raises _JobCancelled,
which unwinds the wizard right away, same as any other exception
escaping _run(). A job that's RUNNING with no prompt open (e.g.
bv-gps blocked inside a socket call) can't be interrupted mid-call -
cancel() instead flips its status to CANCELLED immediately so the
browser stops trusting its output, while the background thread itself
may keep running invisibly (daemon=True below means it can't block
process shutdown either way) until whatever it's blocked on returns
or times out on its own.

Copyright (C) 2026 Christer R. (sssreh)

SPDX-License-Identifier: GPL-3.0-or-later
"""

from __future__ import annotations

import contextlib
import io
import queue
import threading
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from dataclasses import field
from datetime import datetime
from datetime import timezone
from enum import Enum
from pathlib import Path


class BvExportArgError(Exception):
    """Raised by JobRunner.start_bv_export() when bv-export's own
    parse_args() rejects the argv built from the web form - a bad
    numeric value out of range, an invalid --stitch-resolution, both
    --stitch-gsensor-pos and --stitch-gsensor-xy given, and so on.

    argparse only reports this as a raised SystemExit plus a message
    printed straight to stderr (parser.error() -> print_usage() +
    exit(2)) - neither is something app.py's route can turn into a
    re-rendered form on its own, so start_bv_export() catches the
    SystemExit around its own parse_args() call, captures what
    argparse printed, and re-raises as this instead. See
    start_bv_export()'s own docstring for why that one capture is safe
    despite this module's usual rule against redirecting real
    stdout/stderr for a job."""

# Sentinel pushed onto a job's answer queue by Job.cancel() to unblock
# an ask() that's currently waiting - a plain object() rather than a
# string so it can never collide with a real (even empty-string)
# browser-submitted answer.
_CANCEL_SENTINEL = object()


class _JobCancelled(Exception):
    """Raised inside a job's own ask() when Job.cancel() unblocks it
    while it's waiting for an answer that will now never come. Caught
    by JobRunner._spawn()'s target() wrapper - by the time this is
    raised, cancel() has already set the job's status to CANCELLED and
    recorded the "Cancelled." output line, so the wrapper has nothing
    further to do beyond letting the thread end."""


class JobStatus(str, Enum):
    RUNNING = "running"
    WAITING_FOR_INPUT = "waiting_for_input"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"

    @property
    def is_finished(self) -> bool:
        return self in (
            JobStatus.SUCCEEDED,
            JobStatus.FAILED,
            JobStatus.CANCELLED,
        )


@dataclass(eq=False)
class Job:
    """One background job's full state.

    `output`/`status`/`prompt` are read by the job-detail page on
    every poll and written by the job's own background thread - `_lock`
    guards all three together so a reader never sees them
    mid-update (e.g. status already flipped to WAITING_FOR_INPUT but
    prompt not set yet).
    """

    id: str
    command: str
    username: str
    created_at: datetime
    status: JobStatus = JobStatus.RUNNING
    output: list[str] = field(default_factory=list)
    prompt: str | None = None
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)
    _answer_queue: "queue.Queue[str]" = field(
        default_factory=queue.Queue, repr=False
    )

    def snapshot(self) -> tuple[JobStatus, list[str], str | None]:
        """Consistent (status, output, prompt) triple for rendering -
        without this, the detail page could read output/prompt from
        either side of a status change mid-render."""

        with self._lock:
            return self.status, list(self.output), self.prompt

    def append_output(self, line: str) -> None:
        with self._lock:
            self.output.append(line)

    def set_status(
        self, status: JobStatus, *, prompt: str | None = None
    ) -> None:
        with self._lock:
            self.status = status
            self.prompt = prompt

    def submit_answer(self, text: str) -> bool:
        """Feed a browser-submitted answer to whatever `ask()` call is
        currently blocked waiting for one. Returns False (and does
        nothing) if this job isn't actually waiting for input right
        now - e.g. a stale/double form submission - rather than
        silently queuing an answer nothing will ever read."""

        with self._lock:
            if self.status != JobStatus.WAITING_FOR_INPUT:
                return False
        self._answer_queue.put(text)
        return True

    def cancel(self) -> bool:
        """Request cancellation. Returns False if the job is already
        finished (succeeded/failed/already-cancelled) - nothing to do.

        See this module's own docstring for what cancellation can and
        can't guarantee: immediate for a job waiting on an answer,
        best-effort (status only) for a job that's actively running
        with no prompt open.
        """

        with self._lock:
            if self.status.is_finished:
                return False
            was_waiting = self.status == JobStatus.WAITING_FOR_INPUT
            self.status = JobStatus.CANCELLED
            self.prompt = None
            self.output.append("Cancelled.")
        if was_waiting:
            self._answer_queue.put(_CANCEL_SENTINEL)
        return True


class JobRunner:
    """Holds every job for the life of the process.

    In-memory only - the same restart-loses-state trade-off
    web/auth.py's SessionStore already accepts (see its own
    docstring). A job that's mid-run when bv-web restarts is gone
    either way, since its background thread doesn't survive the
    restart regardless of whether it's tracked here - persisting just
    this dict wouldn't actually save anything in flight.
    """

    def __init__(self) -> None:
        self._jobs: dict[str, Job] = {}

    def get(self, job_id: str) -> Job | None:
        return self._jobs.get(job_id)

    def start_bv_config(self, *, id_: str, username: str) -> Job:
        """Start bv-config's wizard as a job. Only the camera id is
        taken as input for this first increment - --config-dir stays
        at bv_config's own default, matching the "curated subset, not
        every CLI flag" approach the rest of this job runner uses."""

        from ..cli import bv_config

        args = bv_config.parse_args([id_])
        job = self._new_job(command=f"bv-config {id_}", username=username)

        def run() -> int:
            ask = self._make_ask(job)
            say = job.append_output
            return bv_config._run(args, ask=ask, say=say, warn=say)

        self._spawn(job, run)
        return job

    def start_bv_gps(
        self,
        *,
        id_: str,
        timeout: int,
        no_address: bool,
        username: str,
    ) -> Job:
        """Start bv-gps as a job, against an already-configured camera
        id only. bv-gps's own CLI also accepts a bare --host, useful
        from a real terminal for probing a camera that hasn't been
        set up with bv-config yet - deliberately not exposed here:
        that's a "test/scan an arbitrary address" escape hatch, not
        something bv-web's job trigger should let anyone reach for.

        `timeout` is bv-gps's own --timeout (per-endpoint connection
        timeout in seconds) - unlike --host/--config-dir this one is
        exposed, since it's just a number with a sensible default
        (job_new_bv_gps.html's own "Defaults" group), not an escape
        hatch around the curated id-only design."""

        from ..cli import bv_gps

        argv: list[str] = [id_, "--timeout", str(timeout)]
        if no_address:
            argv.append("--no-address")
        args = bv_gps.parse_args(argv)
        job = self._new_job(command=f"bv-gps {id_}", username=username)

        def run() -> int:
            say = job.append_output
            return bv_gps._run(args, say=say, warn=say)

        self._spawn(job, run)
        return job

    def start_bv_ls(
        self,
        *,
        camera_id: str,
        archive_path: Path,
        all: bool,
        from_: str | None,
        until: str | None,
        timestamp: str | None,
        trips: bool,
        max_gap_minutes: int | None,
        movement: bool,
        duration: bool,
        gap_tolerance_seconds: int | None,
        username: str,
    ) -> Job:
        """Start bv-ls as a job against one already-configured camera's
        archive - the last of the six bv-* commands to get a browser
        trigger (bv-config/bv-gps/bv-download/bv-generate/bv-export
        already had one; bv-ls, the very first bv-* command this
        project ever had, was overlooked until Christer pointed it
        out). Full flag parity with the CLI, same as
        start_bv_generate()/start_bv_export() above - bv-ls has no
        cross-field validation to re-check (unlike bv-generate's
        diarize/srt/lrc rules) and nothing destructive or slow, so
        there was no reason to curate a subset the way
        bv-config/bv-gps's id-only triggers do.

        `archive_path` is resolved by the caller (app.py's route) the
        same way start_bv_generate()'s own docstring explains.
        """

        from ..cli import bv_ls as bv_ls_cli

        argv: list[str] = [str(archive_path)]

        if all:
            argv.append("--all")
        if from_:
            argv += ["--from", from_]
        if until:
            argv += ["--until", until]
        if timestamp:
            argv += ["--timestamp", timestamp]
        if trips:
            argv.append("--trips")
        if max_gap_minutes is not None:
            argv += ["--max-gap", str(max_gap_minutes)]
        if movement:
            argv.append("--movement")
        if not duration:
            argv.append("--no-duration")
        if gap_tolerance_seconds is not None:
            argv += ["--gap-tolerance", str(gap_tolerance_seconds)]

        args = bv_ls_cli.parse_args(argv)
        job = self._new_job(command=f"bv-ls {camera_id}", username=username)

        def run() -> int:
            say = job.append_output
            return bv_ls_cli._run(args, say=say)

        self._spawn(job, run)
        return job

    def start_bv_generate(
        self,
        *,
        camera_id: str,
        archive_path: Path,
        from_: str | None,
        until: str | None,
        timestamp: str | None,
        extract_audio: bool,
        get_duration: bool,
        transcribe: bool,
        translate: str | None,
        language: str | None,
        model_size: str | None,
        diarize: bool,
        hf_token: str | None,
        srt: bool,
        lrc: bool,
        overwrite: bool,
        dry_run: bool,
        username: str,
    ) -> Job:
        """Start bv-generate as a job against one already-configured
        camera's archive - full flag parity with the CLI (Christer's
        own choice when asked how much of bv-generate/bv-export's
        surface to expose: "full parity but grouped by required,
        default and the rest", see job_new_bv_generate.html's own
        Required/Defaults/Optional groups), unlike bv-config/bv-gps's
        deliberately curated subset above.

        `archive_path` is resolved by the caller (app.py's route, via
        the same `_find_camera_archive()` the archive browser already
        uses) rather than here - camera-id-to-archive-path resolution
        (including the untrusted-camera_id-in-URL guard) is already an
        app.py concern for every other archive route, so this method
        takes the already-resolved Path rather than duplicating that
        lookup.

        argparse's own cross-field validation (at least one action;
        --diarize/--srt/--lrc require --transcribe or --translate) is
        deliberately re-checked by app.py's route *before* this is
        ever called, so a bad web form re-renders with a friendly
        error instead of parse_args() raising SystemExit(2) - a
        subprocess-CLI concern, not something to let escape into a
        FastAPI route.
        """

        from ..cli import bv_generate

        argv: list[str] = [str(archive_path)]

        if from_:
            argv += ["--from", from_]
        if until:
            argv += ["--until", until]
        if timestamp:
            argv += ["--timestamp", timestamp]
        if extract_audio:
            argv.append("--extract-audio")
        if get_duration:
            argv.append("--get-duration")
        if transcribe:
            argv.append("--transcribe")
        if translate:
            argv += ["--translate", translate]
        if language:
            argv += ["--language", language]
        if model_size:
            argv += ["--model-size", model_size]
        if diarize:
            argv.append("--diarize")
        if hf_token:
            argv += ["--hf-token", hf_token]
        if srt:
            argv.append("--srt")
        if lrc:
            argv.append("--lrc")
        if overwrite:
            argv.append("--overwrite")
        if dry_run:
            argv.append("--dry-run")

        args = bv_generate.parse_args(argv)
        job = self._new_job(command=f"bv-generate {camera_id}", username=username)

        def run() -> int:
            say = job.append_output
            return bv_generate._run(args, say=say, warn=say)

        self._spawn(job, run)
        return job

    def start_bv_export(
        self,
        *,
        camera_id: str,
        archive_path: Path,
        target: Path,
        prefix: str | None,
        from_: str | None,
        until: str | None,
        timestamp: str | None,
        max_gap_minutes: int | None,
        movement: bool,
        no_duration: bool,
        duration_heal_archive: bool,
        gap_tolerance_seconds: int | None,
        max_parking_duration_minutes: int | None,
        render_map: bool,
        map_icon: str | None,
        map_zoom_meters: float | None,
        map_track_up: bool,
        render_gsensor: bool,
        render_gsensor_graph: bool,
        gsensor_graph_x: bool,
        stitch: bool,
        stitch_layout: str,
        stitch_mirror_size: float | None,
        stitch_mirror_radius: float | None,
        stitch_mirror_zoom: float | None,
        stitch_mirror_pan_x: float | None,
        stitch_mirror_pan_y: float | None,
        stitch_mirror_icon: str | None,
        stitch_resolution: str | None,
        stitch_bitrate: str | None,
        stitch_scale: float | None,
        stitch_max_width: int | None,
        stitch_max_height: int | None,
        stitch_map: str | None,
        stitch_map_side: str | None,
        stitch_map_size: float | None,
        stitch_gsensor: bool,
        stitch_gsensor_size: float | None,
        stitch_gsensor_pos: str | None,
        stitch_gsensor_xy: str | None,
        stitch_graph: bool,
        stitch_graph_side: str | None,
        stitch_graph_size: float | None,
        stitch_subtitles: bool,
        no_subtitles_bg: bool,
        include_parking: bool,
        overwrite: bool,
        dry_run: bool,
        debug: bool,
        username: str,
    ) -> Job:
        """Start bv-export as a job against one already-configured
        camera's archive - full CLI parity (every bv-export flag gets
        its own parameter here), per Christer's own answer when asked
        how much of bv-export's surface the web form should expose:
        "full parity but grouped by required, default and the rest".

        `archive_path` is resolved by the caller (app.py's route, via
        `_find_camera_archive()`) the same way start_bv_generate()
        already documents. `target` is NOT resolved from the web form
        at all - it's always `app.state.target`, the exact directory
        the Trips tab already scans (trips.py's scan_trips()), so a
        web-triggered export shows up there immediately. Exposing
        --target as its own field would let the web form write
        anywhere on the filesystem the bv-web process can reach - the
        one flag deliberately NOT given full parity, for the same
        "curated, not an arbitrary filesystem write" reasoning
        bv-gps's own --host omission already established.

        Building argv here and calling bv_export.parse_args() (like
        every other start_bv_*() method) means argparse's own `type=`
        validators (numeric ranges, --stitch-resolution's WIDTHxHEIGHT
        shape, the --stitch-gsensor-pos/--stitch-gsensor-xy mutually
        -exclusive group) all still run - bv-export has dozens of
        these, far more than bv-config/bv-gps/bv-generate combined,
        so hand-replicating each one as a separate app.py pre-check
        (the approach start_bv_generate's route uses for its own much
        smaller 3-condition check) isn't practical here. Instead,
        parse_args() itself runs under a *synchronous*,
        single-call-scoped `contextlib.redirect_stderr()` - safe
        despite this module's own docstring warning against
        redirecting real stdout/stderr for a job: that warning is
        about redirecting *for the duration of a background job* while
        other jobs may be running concurrently in their own threads;
        this redirect wraps one plain function call, entirely before
        any Job exists or any thread is spawned, so there is nothing
        else running that could be affected by it. A validation
        failure raises BvExportArgError with argparse's own message
        text (extracted from what would otherwise have only gone to
        the real terminal) instead of ever creating a Job - app.py's
        route catches it and re-renders the form, the same
        friendly-error pattern used for bv-generate's own required
        -action check.
        """

        from ..cli import bv_export

        argv: list[str] = [str(archive_path), "--target", str(target)]

        if prefix:
            argv += ["--prefix", prefix]
        if from_:
            argv += ["--from", from_]
        if until:
            argv += ["--until", until]
        if timestamp:
            argv += ["--timestamp", timestamp]
        if max_gap_minutes is not None:
            argv += ["--max-gap", str(max_gap_minutes)]
        if movement:
            argv.append("--movement")
        if no_duration:
            argv.append("--no-duration")
        if duration_heal_archive:
            argv.append("--duration-heal-archive")
        if gap_tolerance_seconds is not None:
            argv += ["--gap-tolerance", str(gap_tolerance_seconds)]
        if max_parking_duration_minutes is not None:
            argv += ["--max-parking-duration", str(max_parking_duration_minutes)]
        if render_map:
            argv.append("--map")
        if map_icon:
            argv += ["--map-icon", map_icon]
        if map_zoom_meters is not None:
            argv += ["--map-zoom", str(map_zoom_meters)]
        if map_track_up:
            argv.append("--map-track-up")
        if render_gsensor:
            argv.append("--gsensor-video")
        if render_gsensor_graph:
            argv.append("--gsensor-graph-video")
        if gsensor_graph_x:
            argv.append("--gsensor-graph-x")
        if stitch:
            argv.append("--stitch")
        if stitch_layout:
            argv += ["--stitch-layout", stitch_layout]
        if stitch_mirror_size is not None:
            argv += ["--stitch-mirror-size", str(stitch_mirror_size)]
        if stitch_mirror_radius is not None:
            argv += ["--stitch-mirror-radius", str(stitch_mirror_radius)]
        if stitch_mirror_zoom is not None:
            argv += ["--stitch-mirror-zoom", str(stitch_mirror_zoom)]
        if stitch_mirror_pan_x is not None:
            argv += ["--stitch-mirror-pan-x", str(stitch_mirror_pan_x)]
        if stitch_mirror_pan_y is not None:
            argv += ["--stitch-mirror-pan-y", str(stitch_mirror_pan_y)]
        if stitch_mirror_icon:
            argv += ["--stitch-mirror-icon", stitch_mirror_icon]
        if stitch_resolution:
            argv += ["--stitch-resolution", stitch_resolution]
        if stitch_bitrate:
            argv += ["--stitch-bitrate", stitch_bitrate]
        if stitch_scale is not None:
            argv += ["--stitch-scale", str(stitch_scale)]
        if stitch_max_width is not None:
            argv += ["--stitch-max-width", str(stitch_max_width)]
        if stitch_max_height is not None:
            argv += ["--stitch-max-height", str(stitch_max_height)]
        if stitch_map:
            argv += ["--stitch-map", stitch_map]
        if stitch_map_side:
            argv += ["--stitch-map-side", stitch_map_side]
        if stitch_map_size is not None:
            argv += ["--stitch-map-size", str(stitch_map_size)]
        if stitch_gsensor:
            argv.append("--stitch-gsensor")
        if stitch_gsensor_size is not None:
            argv += ["--stitch-gsensor-size", str(stitch_gsensor_size)]
        if stitch_gsensor_pos:
            argv += ["--stitch-gsensor-pos", stitch_gsensor_pos]
        if stitch_gsensor_xy:
            argv += ["--stitch-gsensor-xy", stitch_gsensor_xy]
        if stitch_graph:
            argv.append("--stitch-graph")
        if stitch_graph_side:
            argv += ["--stitch-graph-side", stitch_graph_side]
        if stitch_graph_size is not None:
            argv += ["--stitch-graph-size", str(stitch_graph_size)]
        if stitch_subtitles:
            argv.append("--stitch-subtitles")
        if no_subtitles_bg:
            argv.append("--no-subtitles-bg")
        if include_parking:
            argv.append("--include-parking")
        if overwrite:
            argv.append("--overwrite")
        if dry_run:
            argv.append("--dry-run")
        if debug:
            argv.append("--debug")

        stderr_capture = io.StringIO()
        try:
            with contextlib.redirect_stderr(stderr_capture):
                args = bv_export.parse_args(argv)
        except SystemExit:
            lines = [
                line for line in stderr_capture.getvalue().splitlines() if line.strip()
            ]
            raise BvExportArgError(
                lines[-1] if lines else "Invalid bv-export options."
            )

        command_line = "bv-export " + " ".join(argv)
        job = self._new_job(command=f"bv-export {camera_id}", username=username)

        def run() -> int:
            say = job.append_output
            return bv_export._run(
                args, command_line=command_line, say=say, warn=say
            )

        self._spawn(job, run)
        return job

    def start_bv_download(
        self,
        *,
        id_: str,
        timeout: int,
        modes: list[str],
        from_: str | None,
        until: str | None,
        timestamp: str | None,
        dry_run: bool,
        files: bool,
        verbose: bool,
        trace: bool,
        username: str,
    ) -> Job:
        """Start bv-download as a job, against an already-configured
        camera id only - --host/--target (a one-off connection with no
        saved config) and --config-dir are deliberately not exposed,
        same "curated, not an arbitrary-connection escape hatch"
        reasoning as start_bv_gps()'s own omitted --host, and the same
        default_config_dir() every other job-trigger method here uses.

        `--yes` is always forced on, never a form field: bv-download's
        own confirm() prompt (skipped already whenever stdin/stdout
        aren't a real terminal, which a background thread inside
        bv-web's own process already isn't) reads real process stdin
        if it ever were reached, which this job runner has no way to
        answer through its own ask()/Job.submit_answer() mechanism
        (unlike bv-config's wizard, built around exactly that) - a
        stray real-stdin block would hang the job forever with no way
        for the browser to unblock it. Forcing --yes here removes any
        dependence on bv-web's own process happening to have no
        attached tty; see bv_download._run()'s own docstring for the
        same reasoning from the CLI side.

        `modes` is a list of kind letters (any of A/E/M/N/P, from the
        web form's checkboxes) joined into a single --mode value, or
        omitted entirely when empty - matching bv-download's own
        default (no --mode) of the event/manual-plus-context selection
        policy rather than a blank/invalid --mode. Checking all five
        letters is equivalent to bv-download's own --mode all, since
        parse_mode() treats them the same (see cli/bv_download.py).

        `files` (--files, "list every individual file under --dry-run"
        - only meaningful combined with dry_run) is NOT cross-checked
        here the way bv-export's dozens of validators are - bv-download
        only has this one condition, so app.py's route pre-checks it
        directly, the same "small number of conditions -> a plain
        pre-check" approach start_bv_generate's own route already
        uses, rather than a BvExportArgError-style exception class
        built for a much larger validator surface.
        """

        from ..cli import bv_download

        argv: list[str] = [id_, "--timeout", str(timeout), "--yes"]

        if modes:
            argv += ["--mode", ",".join(modes)]
        if from_:
            argv += ["--from", from_]
        if until:
            argv += ["--until", until]
        if timestamp:
            argv += ["--timestamp", timestamp]
        if dry_run:
            argv.append("--dry-run")
        if files:
            argv.append("--files")
        if verbose:
            argv.append("--verbose")
        if trace:
            argv.append("--trace")

        args = bv_download.parse_args(argv)
        job = self._new_job(command=f"bv-download {id_}", username=username)

        def run() -> int:
            say = job.append_output
            return bv_download._run(args, say=say, warn=say)

        self._spawn(job, run)
        return job

    def answer(self, job_id: str, text: str) -> bool:
        """Feed an answer to a waiting job. Returns False if the job
        doesn't exist or isn't actually waiting (see
        Job.submit_answer) - callers (app.py's route) treat that as a
        no-op redirect back to the job page rather than an error, since
        it's a harmless race (e.g. a double form submit), not a real
        failure."""

        job = self._jobs.get(job_id)
        if job is None:
            return False
        return job.submit_answer(text)

    def cancel(self, job_id: str) -> bool:
        """Cancel a job. Returns False if the job doesn't exist or is
        already finished - callers (app.py's route) treat that as a
        harmless no-op redirect, same pattern as answer() above."""

        job = self._jobs.get(job_id)
        if job is None:
            return False
        return job.cancel()

    def _new_job(self, *, command: str, username: str) -> Job:
        job = Job(
            id=uuid.uuid4().hex,
            command=command,
            username=username,
            created_at=datetime.now(timezone.utc),
        )
        self._jobs[job.id] = job
        return job

    @staticmethod
    def _spawn(job: Job, run: Callable[[], int]) -> None:
        def target() -> None:
            try:
                code = run()
            except _JobCancelled:
                # cancel() already set CANCELLED and appended
                # "Cancelled." before unblocking ask() - nothing left
                # to record.
                return
            except (Exception, SystemExit) as exc:  # noqa: BLE001 - report, never crash silently
                # SystemExit alongside the usual Exception: none of
                # this project's own _run()s should ever let one
                # escape here (bv_export._run() in particular converts
                # its own internal SystemExit into a normal return
                # before this point - see that function's own
                # docstring for why), but SystemExit subclasses
                # BaseException, not Exception, so a bare `except
                # Exception` would silently miss it entirely - the
                # background thread would just end with this job
                # stuck showing RUNNING forever, since nothing would
                # ever set its status. Defense in depth against
                # exactly that, not a substitute for handling it
                # properly at the source.
                job.append_output(f"Error: {exc}")
                job.set_status(JobStatus.FAILED)
                return
            if job.snapshot()[0] == JobStatus.CANCELLED:
                # cancel() was called while this job was RUNNING with
                # no prompt open (so there was nothing to unblock),
                # and run() has now returned on its own - don't
                # overwrite the cancellation with a stale
                # success/failure status.
                return
            job.append_output(f"(exit code {code})")
            job.set_status(
                JobStatus.SUCCEEDED if code == 0 else JobStatus.FAILED
            )

        threading.Thread(target=target, daemon=True).start()

    @staticmethod
    def _make_ask(job: Job) -> Callable[[str], str]:
        def ask(prompt_text: str) -> str:
            job.append_output(prompt_text)
            job.set_status(JobStatus.WAITING_FOR_INPUT, prompt=prompt_text)
            answer = job._answer_queue.get()
            if answer is _CANCEL_SENTINEL:
                raise _JobCancelled()
            job.set_status(JobStatus.RUNNING)
            job.append_output(f"> {answer}")
            return answer

        return ask
