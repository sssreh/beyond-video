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

bv-export is the one exception to "keeps running invisibly": its own
_run()/bv_export() accept a `should_continue` callable (see
export/trip_export.py's own docstring for the checkpoint mechanism),
and start_bv_export() below wires that to
`job.snapshot()[0] != JobStatus.CANCELLED` - so a cancelled export job
actually stops starting new work, checked at phase boundaries and
every _FRAME_CHECKPOINT_INTERVAL frames inside the slower per-frame
render loops (map/intro/g-sensor-graph), typically within a few
seconds. Still not instant, and still doesn't interrupt a single
in-flight ffmpeg subprocess call already running (concatenation,
stitch.mp4, the dot-gauge gsensor.mp4 render) - those finish that one
call before the next checkpoint is reached, same "honest, not
absolute" limitation as everywhere else in this module, just with a
much shorter real-world window than "keeps running until the whole
job ends."

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

from ..core.joblog import log_line
from ..generate import unload_scene_model


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


def _quote_for_replicate(value: str) -> str:
    """Minimal quoting for Job.replicate_command - good enough to
    copy/paste into either bash or PowerShell for the values these
    arguments actually take (camera ids, dates, numbers, --place names
    with a space or comma, tokens). Only quotes when the value is
    empty or contains whitespace; embedded double quotes are escaped
    with a backslash, which both shells accept for the realistic case
    here (an unescaped one inside a value would be unusual). Not using
    shlex.quote() - its single-quote style is bash-only and would
    confuse a PowerShell user copying the same line, defeating the
    "works in either" goal this exists for."""

    if value == "" or any(character.isspace() for character in value):
        return '"' + value.replace('"', '\\"') + '"'
    return value


def _replicate_command_line(name: str, argv: list[str]) -> str:
    """Build the human-facing "here's how to run this yourself"
    command line shown on the job detail page - see Job.replicate_
    command's own docstring for why this exists and what it
    deliberately doesn't guarantee."""

    return " ".join([name, *(_quote_for_replicate(a) for a in argv)])


def _record_job_history(job: Job) -> None:
    """Append one core/history.py entry for a just-finished job - see
    JobRunner._spawn()'s own comment for why this lives in a single
    outer `finally` rather than at each individual exit path.

    `command` is recovered from `job.command`'s own first word (every
    start_bv_*() method sets it as `f"bv-... {...}"` - see
    Job.append_output()'s own comment for the identical trick, used
    there for core/joblog.py's output transcript instead). `command_
    line` prefers `job.replicate_command` - the real paste-able
    invocation, options included - falling back to the bare `job.
    command` only for the hypothetical case a caller left it unset.
    """

    from ..core import history

    status, _, _ = job.snapshot()
    duration_seconds = (
        datetime.now(timezone.utc) - job.created_at
    ).total_seconds()
    history.record(
        history.HistoryEntry(
            command=job.command.split(maxsplit=1)[0],
            command_line=job.replicate_command or job.command,
            source="bv-web",
            username=job.username,
            started_at=job.created_at.isoformat(),
            duration_seconds=duration_seconds,
            status=status.value,
            params=job.params or None,
        )
    )


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
    replicate_command: str = ""
    """The equivalent real bv-* CLI invocation for this job, using the
    camera id (not this job's resolved-on-the-server archive path) as
    the positional PATH argument - a job started from the web UI runs
    inside bv-web's own process/container, so the literal path it used
    (e.g. /data/archive/Kirby) usually isn't a real path on whatever
    machine someone reads this to replicate the job on; the camera id
    resolves the same way there too as long as that machine also has
    the camera configured (bv-config), which is Christer's actual dual
    -machine setup (NAS + PC, see docs/DEPLOY.md). Includes whatever
    flags the job runner itself always adds (e.g. bv-download's forced
    --yes) - that's what actually ran, even if a from-scratch terminal
    run wouldn't need it. Set by every start_bv_*() method, including
    bv-config's wizard trigger (its own replicate command just starts
    the same interactive wizard for real, in a real terminal)."""
    params: dict = field(default_factory=dict)
    """The raw web-form field values this job was triggered with, keyed
    by the same `name=` attributes the trigger form's own inputs use
    (e.g. `{"id": "Kirby", "from_": "", "task": "both", "cpu": False,
    ...}`) - captured once, in app.py's own POST route, before any
    cleaning/type-conversion happens. Exists for the "reuse a previous
    run's parameters" feature (Christer: "i would like to have a
    button ... to get the latest run parameters filled in"):
    _record_job_history() persists this onto the job's HistoryEntry,
    and the trigger form's own GET route reads it back to prefill the
    form. Empty for job types that don't build one yet (see each
    start_bv_*() method - only ones that opted in set this), and
    always empty for bv-config's wizard trigger (there's no ordinary
    field-form to prefill there)."""
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
        # Mirror into the persistent rotating logfile too (see
        # core/joblog.py) - the bv-web half of "I would also want a
        # logfile of all the output" (WORKING_CONTEXT.md); direct-CLI
        # runs get the same coverage via wrap_say()/wrap_warn() in each
        # bv-*.py's own main(). `self.command` always starts with
        # "bv-<name> ..." (every start_bv_*() method sets it that way -
        # see each one's own `command=f"bv-... {...}"` above), so
        # splitting on the first space recovers the same `prog` string
        # wrap_say()/wrap_warn() use, without needing a dedicated field
        # threaded through all eight start_bv_*() methods just for this.
        log_line(self.command.split(maxsplit=1)[0], line)

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
        job = self._new_job(
            command=f"bv-config {id_}",
            replicate_command=_replicate_command_line("bv-config", [id_]),
            username=username,
        )

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
        job = self._new_job(
            command=f"bv-gps {id_}",
            replicate_command=_replicate_command_line("bv-gps", argv),
            username=username,
        )

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
        full: bool,
        from_: str | None,
        until: str | None,
        timestamp: str | None,
        source: str | None,
        trips: bool,
        max_gap_minutes: int | None,
        movement: bool,
        gps_split: bool,
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
        diarize/srt rules) and nothing destructive or slow, so
        there was no reason to curate a subset the way
        bv-config/bv-gps's id-only triggers do.

        `archive_path` is resolved by the caller (app.py's route) the
        same way start_bv_generate()'s own docstring explains.
        """

        from ..cli import bv_ls as bv_ls_cli

        argv: list[str] = [str(archive_path)]

        if all:
            argv.append("--all")
        if full:
            argv.append("--full")
        if from_:
            argv += ["--from", from_]
        if until:
            argv += ["--until", until]
        if timestamp:
            argv += ["--timestamp", timestamp]
        if source:
            argv += ["--source", source]
        if trips:
            argv.append("--trips")
        if max_gap_minutes is not None:
            argv += ["--max-gap", str(max_gap_minutes)]
        if movement:
            argv.append("--movement")
        if gps_split:
            argv.append("--gps-split")
        if not duration:
            argv.append("--no-duration")
        if gap_tolerance_seconds is not None:
            argv += ["--gap-tolerance", str(gap_tolerance_seconds)]

        args = bv_ls_cli.parse_args(argv)
        job = self._new_job(
            command=f"bv-ls {camera_id}",
            replicate_command=_replicate_command_line(
                "bv-ls", [camera_id, *argv[1:]]
            ),
            username=username,
        )

        def run() -> int:
            say = job.append_output
            return bv_ls_cli._run(args, say=say)

        self._spawn(job, run)
        return job

    def start_bv_lock(
        self,
        *,
        camera_id: str,
        archive_path: Path,
        mode: str,
        from_: str | None,
        until: str | None,
        timestamp: str | None,
        assets: list[str],
        username: str,
    ) -> Job:
        """Start bv-lock as a job against one already-configured
        camera's archive - same treatment bv-ls got (full parity,
        nothing curated away, nothing slow or destructive enough to
        need special handling).

        `mode` is one of "lock", "unlock", "list" - app.py's route
        picks the CLI flag this maps to (--lock-assets/--unlock-assets
        /--list) and, for "list", drops --from/--until/--timestamp
        the same way bv-lock's own CLI ignores them for --list (see
        cli/bv_lock.py's parse_args() - --list "always shows every
        lock entry, since a lock's own range is exactly what's being
        listed"). `assets` is the already-validated, non-empty (for
        "lock"/"unlock") list of asset names or `["all"]` - app.py's
        route is responsible for that validation before calling this,
        same as it validates numeric fields before calling
        start_bv_ls()."""

        from ..cli import bv_lock as bv_lock_cli

        argv: list[str] = [str(archive_path)]

        if mode == "list":
            argv.append("--list")
        else:
            if from_:
                argv += ["--from", from_]
            if until:
                argv += ["--until", until]
            if timestamp:
                argv += ["--timestamp", timestamp]
            flag = "--lock-assets" if mode == "lock" else "--unlock-assets"
            argv += [flag, ",".join(assets)]

        args = bv_lock_cli.parse_args(argv)
        job = self._new_job(
            command=f"bv-lock {camera_id}",
            replicate_command=_replicate_command_line(
                "bv-lock", [camera_id, *argv[1:]]
            ),
            username=username,
        )

        def run() -> int:
            say = job.append_output
            return bv_lock_cli._run(args, say=say, warn=say)

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
        thumbnail: bool,
        transcribe: bool,
        translate: str | None,
        language: str | None,
        model_size: str | None,
        diarize: bool,
        hf_token: str | None,
        srt: bool,
        describe_scene: bool,
        scene_model: str | None,
        camera: str,
        overwrite: bool,
        dry_run: bool,
        ignore_lock: bool,
        username: str,
        params: dict | None = None,
    ) -> Job:
        """Start bv-generate as a job against one already-configured
        camera's archive - full flag parity with the CLI (Christer's
        own choice when asked how much of bv-generate/bv-export's
        surface to expose: "full parity but grouped by required,
        default and the rest", see job_new_bv_generate.html's own
        Required/Defaults/Optional groups), unlike bv-config/bv-gps's
        deliberately curated subset above.

        `params`, if given, is the raw web-form field dict app.py's
        own POST route captured before cleaning - see start_bv_scribe's
        own docstring for the full "reuse a previous run" explanation;
        this method just threads it through the same way.

        `archive_path` is resolved by the caller (app.py's route, via
        the same `_find_camera_archive()` the archive browser already
        uses) rather than here - camera-id-to-archive-path resolution
        (including the untrusted-camera_id-in-URL guard) is already an
        app.py concern for every other archive route, so this method
        takes the already-resolved Path rather than duplicating that
        lookup.

        argparse's own cross-field validation (at least one action;
        --diarize/--srt require --transcribe or --translate) is
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
        if thumbnail:
            argv.append("--thumbnail")
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
        if describe_scene:
            argv.append("--describe-scene")
        if scene_model:
            argv += ["--scene-model", scene_model]
        if camera and camera != "front":
            argv += ["--camera", camera]
        if overwrite:
            argv.append("--overwrite")
        if dry_run:
            argv.append("--dry-run")
        if ignore_lock:
            argv.append("--ignore-lock")

        args = bv_generate.parse_args(argv)
        job = self._new_job(
            command=f"bv-generate {camera_id}",
            replicate_command=_replicate_command_line(
                "bv-generate", [camera_id, *argv[1:]]
            ),
            username=username,
            params=params,
        )

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
        gps_split: bool,
        no_duration: bool,
        duration_heal_archive: bool,
        gap_tolerance_seconds: int | None,
        max_parking_duration_minutes: int | None,
        render_map: bool,
        map_icon: str | None,
        map_zoom_meters: float | None,
        map_track_up: bool,
        render_map_intro: bool,
        map_intro_seconds: float | None,
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
        stitch_map_circle: bool,
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
        parking_speed: float | None,
        trip_summary: bool,
        scene_model: str | None,
        scene_cpu: bool,
        overwrite: bool,
        dry_run: bool,
        debug: bool,
        username: str,
        params: dict | None = None,
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
        if gps_split:
            argv.append("--gps-split")
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
        if render_map_intro:
            argv.append("--map-intro")
        if map_intro_seconds is not None:
            argv += ["--map-intro-seconds", str(map_intro_seconds)]
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
        if stitch_map_circle:
            argv.append("--stitch-map-circle")
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
        if parking_speed is not None:
            argv += ["--parking-speed", str(parking_speed)]
        if trip_summary:
            argv.append("--trip-summary")
        if scene_model:
            argv += ["--scene-model", scene_model]
        if scene_cpu:
            argv.append("--scene-cpu")
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
        job = self._new_job(
            command=f"bv-export {camera_id}",
            replicate_command=_replicate_command_line(
                "bv-export", [camera_id, *argv[1:]]
            ),
            username=username,
            params=params,
        )

        def run() -> int:
            say = job.append_output
            return bv_export._run(
                args,
                command_line=command_line,
                should_continue=lambda: job.snapshot()[0] != JobStatus.CANCELLED,
                say=say,
                warn=say,
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
        job = self._new_job(
            command=f"bv-download {id_}",
            replicate_command=_replicate_command_line("bv-download", argv),
            username=username,
        )

        def run() -> int:
            say = job.append_output
            return bv_download._run(args, say=say, warn=say)

        self._spawn(job, run)
        return job

    def start_bv_scribe(
        self,
        *,
        camera_id: str,
        archive_path: Path,
        from_: str | None,
        until: str | None,
        timestamp: str | None,
        task: str,
        camera: str,
        model: str | None,
        fps: float | None,
        max_frames: int | None,
        max_pixels: int | None,
        resized_width: int | None,
        resized_height: int | None,
        crop_top: float | None,
        crop_bottom: float | None,
        max_new_tokens: int | None,
        repetition_penalty: float | None,
        no_repeat_ngram_size: int | None,
        do_sample: bool,
        temperature: float | None,
        top_p: float | None,
        top_k: int | None,
        zoom_signs: bool,
        zoom_frames: int | None,
        zoom_detect_width: int | None,
        zoom_padding: float | None,
        zoom_ocr_width: int | None,
        zoom_max_new_tokens: int | None,
        zoom_detect_max_new_tokens: int | None,
        zoom_repetition_penalty: float | None,
        zoom_no_repeat_ngram_size: int | None,
        zoom_plate_confidence_check: bool,
        cpu: bool,
        overwrite: bool,
        dry_run: bool,
        verbose: bool,
        username: str,
        params: dict | None = None,
    ) -> Job:
        """Start bv-scribe as a job against one already-configured
        camera's archive - full flag parity with the CLI (unlike the
        original "curated subset" version of this method), just like
        start_bv_generate()/start_bv_export()/start_bv_ls() above.
        Christer's actual ask, on reflection, wasn't "leave these
        flags off the form" - it was "hide them the way bv-export
        hides its own advanced flags" (see job_new_bv_export.html's
        progressive-disclosure `<details>` sections, and the
        "Progressive disclosure for the bv-export web form" entry in
        WORKING_CONTEXT.md). So every flag gets a real keyword here;
        job_new_bv_scribe.html is what actually keeps the zoom-
        detection/sampling knobs out of sight by default, not this
        method.

        Every `None` here means "don't pass the flag, let bv-scribe's
        own parse_args() default apply" - the same optional-numeric-
        field convention start_bv_export()'s own fields already use.
        `do_sample`/`zoom_signs`/`zoom_plate_confidence_check` are
        plain bools (not Optional) because their CLI defaults are a
        fixed True/False `parse_args()` already bakes in via
        BooleanOptionalAction - `zoom_signs`/`zoom_plate_confidence_
        check` default True and only ever need a `--no-...` flag
        appended when explicitly turned off, the exact same "default-
        true-omits-the-negative-flag" pattern start_bv_ls()'s own
        `duration` parameter already established; `do_sample` defaults
        False and only ever needs `--do-sample` appended when
        explicitly turned on.

        `--raw` and `--config-dir` are still not exposed - unlike the
        advanced tuning flags above, these aren't about clutter, they
        're an escape hatch for non-archive footage with no camera id
        at all (orthogonal to this curated-by-camera-id trigger, the
        same way bv-gps's own --host isn't exposed either).
        `--zoom-debug-dir` is also not exposed, for the same "no
        arbitrary filesystem path as a form field" reason bv-export's
        own `--target` was never a field either (see "Curated subset
        vs. full parity" in docs/WEB_ARCHITECTURE.md) - it's a real
        server-filesystem path, not curated data.

        `archive_path` is resolved by the caller (app.py's route, via
        `_find_camera_archive()`) the same way start_bv_generate()'s
        own docstring explains.

        `params`, if given, is the raw web-form field dict app.py's
        own POST route captured before cleaning - stored on the
        returned Job (see Job.params's own docstring) so a later
        history-driven "reuse this run's parameters" form load can
        read it back. Optional and otherwise ignored by this method -
        the actual job still runs from `argv` above, built from this
        method's own typed kwargs, not from `params`.
        """

        from ..cli import bv_scribe

        argv: list[str] = [str(archive_path)]

        if from_:
            argv += ["--from", from_]
        if until:
            argv += ["--until", until]
        if timestamp:
            argv += ["--timestamp", timestamp]
        if task:
            argv += ["--task", task]
        if camera:
            argv += ["--camera", camera]
        if model:
            argv += ["--model", model]

        if fps is not None:
            argv += ["--fps", str(fps)]
        if max_frames is not None:
            argv += ["--max-frames", str(max_frames)]
        if max_pixels is not None:
            argv += ["--max-pixels", str(max_pixels)]
        if resized_width is not None:
            argv += ["--resized-width", str(resized_width)]
        if resized_height is not None:
            argv += ["--resized-height", str(resized_height)]
        if crop_top is not None:
            argv += ["--crop-top", str(crop_top)]
        if crop_bottom is not None:
            argv += ["--crop-bottom", str(crop_bottom)]
        if max_new_tokens is not None:
            argv += ["--max-new-tokens", str(max_new_tokens)]
        if repetition_penalty is not None:
            argv += ["--repetition-penalty", str(repetition_penalty)]
        if no_repeat_ngram_size is not None:
            argv += ["--no-repeat-ngram-size", str(no_repeat_ngram_size)]
        if do_sample:
            argv.append("--do-sample")
        if temperature is not None:
            argv += ["--temperature", str(temperature)]
        if top_p is not None:
            argv += ["--top-p", str(top_p)]
        if top_k is not None:
            argv += ["--top-k", str(top_k)]

        if not zoom_signs:
            argv.append("--no-zoom-signs")
        if zoom_frames is not None:
            argv += ["--zoom-frames", str(zoom_frames)]
        if zoom_detect_width is not None:
            argv += ["--zoom-detect-width", str(zoom_detect_width)]
        if zoom_padding is not None:
            argv += ["--zoom-padding", str(zoom_padding)]
        if zoom_ocr_width is not None:
            argv += ["--zoom-ocr-width", str(zoom_ocr_width)]
        if zoom_max_new_tokens is not None:
            argv += ["--zoom-max-new-tokens", str(zoom_max_new_tokens)]
        if zoom_detect_max_new_tokens is not None:
            argv += ["--zoom-detect-max-new-tokens", str(zoom_detect_max_new_tokens)]
        if zoom_repetition_penalty is not None:
            argv += ["--zoom-repetition-penalty", str(zoom_repetition_penalty)]
        if zoom_no_repeat_ngram_size is not None:
            argv += ["--zoom-no-repeat-ngram-size", str(zoom_no_repeat_ngram_size)]
        if not zoom_plate_confidence_check:
            argv.append("--no-zoom-plate-confidence-check")

        if cpu:
            argv.append("--cpu")
        if overwrite:
            argv.append("--overwrite")
        if dry_run:
            argv.append("--dry-run")
        if verbose:
            argv.append("--verbose")

        args = bv_scribe.parse_args(argv)
        job = self._new_job(
            command=f"bv-scribe {camera_id}",
            replicate_command=_replicate_command_line(
                "bv-scribe", [camera_id, *argv[1:]]
            ),
            username=username,
            params=params,
        )

        def run() -> int:
            say = job.append_output
            return bv_scribe._run(args, say=say, warn=say)

        self._spawn(job, run)
        return job

    def start_bv_search(
        self,
        *,
        camera_id: str,
        archive_path: Path,
        from_: str | None,
        until: str | None,
        timestamp: str | None,
        text: str | None,
        asset: str,
        regex: bool,
        case_sensitive: bool,
        near: str | None,
        place: str | None,
        radius: float | None,
        trace: bool,
        username: str,
        params: dict | None = None,
    ) -> Job:
        """Start bv-search as a job against one already-configured
        camera's archive - full flag parity with the CLI (bv-search's
        surface is small enough that "full parity but grouped" makes
        sense here the same way it did for bv-generate/bv-export,
        unlike bv-scribe's own curated subset above).

        `near` is the raw "LAT,LON" text bv-search's own --near takes
        (e.g. "59.3293,18.0686"), not two separate lat/lon fields -
        the web form's text input matches the CLI's own syntax
        directly rather than inventing a different shape only to
        rejoin it into the same string here. `near`/`place` are
        mutually exclusive and at least one of `text`/`near`/`place`
        is required, same as the CLI's own `_run()` - app.py's route
        re-checks both before ever calling this, so a bad web form
        re-renders with a friendly error instead of parse_args()
        raising SystemExit(2) inside this method, the same "small
        number of conditions -> a plain pre-check" approach
        start_bv_ls()/start_bv_download()'s own routes already use
        (bv-search has nowhere near bv-export's dozens of validators).

        `archive_path` is resolved by the caller (app.py's route, via
        `_find_camera_archive()`) the same way start_bv_generate()'s
        own docstring explains.
        """

        from ..cli import bv_search as bv_search_cli

        argv: list[str] = [str(archive_path)]

        if from_:
            argv += ["--from", from_]
        if until:
            argv += ["--until", until]
        if timestamp:
            argv += ["--timestamp", timestamp]
        if text:
            argv += ["--text", text]
        if asset:
            argv += ["--asset", asset]
        if regex:
            argv.append("--regex")
        if case_sensitive:
            argv.append("--case-sensitive")
        if near:
            argv += ["--near", near]
        if place:
            argv += ["--place", place]
        if radius is not None:
            argv += ["--radius", str(radius)]
        if trace:
            argv.append("--trace")

        args = bv_search_cli.parse_args(argv)
        job = self._new_job(
            command=f"bv-search {camera_id}",
            replicate_command=_replicate_command_line(
                "bv-search", [camera_id, *argv[1:]]
            ),
            username=username,
            params=params,
        )

        def run() -> int:
            say = job.append_output
            return bv_search_cli._run(args, say=say, warn=say)

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

    def _new_job(
        self,
        *,
        command: str,
        replicate_command: str = "",
        username: str,
        params: dict | None = None,
    ) -> Job:
        job = Job(
            id=uuid.uuid4().hex,
            command=command,
            replicate_command=replicate_command,
            username=username,
            created_at=datetime.now(timezone.utc),
            params=params or {},
        )
        self._jobs[job.id] = job
        return job

    @staticmethod
    def _spawn(job: Job, run: Callable[[], int]) -> None:
        def target() -> None:
            try:
                try:
                    code = run()
                except _JobCancelled:
                    # cancel() already set CANCELLED and appended
                    # "Cancelled." before unblocking ask() - nothing
                    # left to record.
                    return
                except (Exception, SystemExit) as exc:  # noqa: BLE001 - report, never crash silently
                    # SystemExit alongside the usual Exception: none
                    # of this project's own _run()s should ever let
                    # one escape here (bv_export._run() in particular
                    # converts its own internal SystemExit into a
                    # normal return before this point - see that
                    # function's own docstring for why), but
                    # SystemExit subclasses BaseException, not
                    # Exception, so a bare `except Exception` would
                    # silently miss it entirely - the background
                    # thread would just end with this job stuck
                    # showing RUNNING forever, since nothing would
                    # ever set its status. Defense in depth against
                    # exactly that, not a substitute for handling it
                    # properly at the source.
                    job.append_output(f"Error: {exc}")
                    job.set_status(JobStatus.FAILED)
                    return
                if job.snapshot()[0] == JobStatus.CANCELLED:
                    # cancel() was called while this job was RUNNING
                    # with no prompt open (so there was nothing to
                    # unblock), and run() has now returned on its own
                    # - don't overwrite the cancellation with a stale
                    # success/failure status.
                    return
                job.append_output(f"(exit code {code})")
                job.set_status(
                    JobStatus.SUCCEEDED if code == 0 else JobStatus.FAILED
                )
            finally:
                # core/history.py's own persistent command-history
                # index - the bv-web half of the same "direct CLI
                # calls too, not just bv-web jobs" scope
                # cli/errors.py's run_cli() already covers on the
                # direct-CLI side. A single outer `finally` wrapping
                # every exit path above (including both early
                # `return`s) means exactly one entry is recorded per
                # job regardless of which path it took, with the
                # job's own now-final status already settled by the
                # time this runs.
                _record_job_history(job)

                # "Scene model never unloads from GPU" (Christer).
                # bv-generate --describe-scene, bv-scribe, and
                # bv-export --trip-summary all load the ~16GB
                # Qwen3-VL-8B-Instruct model into generate/scene.py's
                # module-level _SCENE_MODEL_CACHE. A one-shot CLI
                # process doesn't care - the OS reclaims everything on
                # exit - but this server process is long-running and
                # may run any of those job types back to back, so
                # nothing was ever releasing that memory. Called here,
                # unconditionally, for every job type: cheap/no-op if
                # the job never touched the scene model (cache is
                # already empty), and releases the GPU as soon as a
                # job that may have loaded it finishes, regardless of
                # success/failure/cancellation.
                unload_scene_model()

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
