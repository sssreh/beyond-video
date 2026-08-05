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

Copyright (C) 2026 Christer R. (sssreh)

SPDX-License-Identifier: GPL-3.0-or-later
"""

from __future__ import annotations

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


class JobStatus(str, Enum):
    RUNNING = "running"
    WAITING_FOR_INPUT = "waiting_for_input"
    SUCCEEDED = "succeeded"
    FAILED = "failed"

    @property
    def is_finished(self) -> bool:
        return self in (JobStatus.SUCCEEDED, JobStatus.FAILED)


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
        id_: str | None,
        host: str | None,
        no_address: bool,
        username: str,
    ) -> Job:
        """Start bv-gps as a job. Exactly one of id_/host, matching
        bv-gps's own mutually-exclusive-required CLI arguments."""

        from ..cli import bv_gps

        argv: list[str] = [id_] if id_ else ["--host", host]
        if no_address:
            argv.append("--no-address")
        args = bv_gps.parse_args(argv)
        job = self._new_job(command=f"bv-gps {id_ or host}", username=username)

        def run() -> int:
            say = job.append_output
            return bv_gps._run(args, say=say, warn=say)

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
            except Exception as exc:  # noqa: BLE001 - report, never crash silently
                job.append_output(f"Error: {exc}")
                job.set_status(JobStatus.FAILED)
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
            job.set_status(JobStatus.RUNNING)
            job.append_output(f"> {answer}")
            return answer

        return ask
