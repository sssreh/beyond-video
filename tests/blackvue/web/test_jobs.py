"""
Tests for the bv-web background job runner (web/jobs.py).

Job/JobStatus's own state-machine methods (snapshot/append_output/
set_status/submit_answer) are tested directly, with no threading
involved - they're plain synchronous methods guarded by a lock, not
inherently concurrent themselves. start_bv_config()/start_bv_gps() are
tested with the target CLI module's real _run() monkeypatched out to a
fake, so these tests never touch a real camera, the network, or the
real default_config_dir() (~/.config/beyond-video) - only the
job-runner plumbing (ask/say/warn wiring, thread spawn, status
transitions, the answer hand-off queue) is under test here.

Copyright (C) 2026 Christer R. (sssreh)

SPDX-License-Identifier: GPL-3.0-or-later
"""

from __future__ import annotations

import threading
import time

from blackvue.cli import bv_config as bv_config_module
from blackvue.cli import bv_gps as bv_gps_module
from blackvue.web.jobs import Job
from blackvue.web.jobs import JobRunner
from blackvue.web.jobs import JobStatus


def _wait_until(predicate, *, timeout: float = 2.0) -> None:
    """Poll `predicate()` until it's truthy or `timeout` seconds pass.

    A real background thread is involved in most of these tests (see
    JobRunner._spawn) - a fixed sleep would be both flaky (too short)
    and slow (too long), so every wait here is a short poll loop
    instead, the same shape a real browser's own 2s-refresh polling
    loop takes against a live job.
    """

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.01)
    raise AssertionError(f"condition not met within {timeout}s")


# ---------------------------------------------------------------------------
# JobStatus
# ---------------------------------------------------------------------------


def test_job_status_is_finished_only_for_terminal_states():
    assert JobStatus.RUNNING.is_finished is False
    assert JobStatus.WAITING_FOR_INPUT.is_finished is False
    assert JobStatus.SUCCEEDED.is_finished is True
    assert JobStatus.FAILED.is_finished is True
    assert JobStatus.CANCELLED.is_finished is True


def test_job_status_value_is_the_plain_lowercase_string():
    # job_detail's template renders {{ status }} against this .value,
    # not the raw enum member (see app.py's job_detail route comment
    # on why - str(JobStatus.RUNNING) is "JobStatus.RUNNING", not
    # "running").
    assert JobStatus.RUNNING.value == "running"
    assert JobStatus.WAITING_FOR_INPUT.value == "waiting_for_input"
    assert JobStatus.SUCCEEDED.value == "succeeded"
    assert JobStatus.FAILED.value == "failed"
    assert JobStatus.CANCELLED.value == "cancelled"


# ---------------------------------------------------------------------------
# Job
# ---------------------------------------------------------------------------


def _new_job(command: str = "bv-config kirby") -> Job:
    from datetime import datetime, timezone

    return Job(
        id="test-job-id",
        command=command,
        username="christer",
        created_at=datetime.now(timezone.utc),
    )


def test_job_starts_running_with_empty_output_and_no_prompt():
    job = _new_job()

    status, output, prompt = job.snapshot()

    assert status == JobStatus.RUNNING
    assert output == []
    assert prompt is None


def test_job_append_output_accumulates_in_order():
    job = _new_job()

    job.append_output("first line")
    job.append_output("second line")

    _, output, _ = job.snapshot()
    assert output == ["first line", "second line"]


def test_job_set_status_updates_status_and_prompt_together():
    job = _new_job()

    job.set_status(JobStatus.WAITING_FOR_INPUT, prompt="Name [kirby]: ")

    status, _, prompt = job.snapshot()
    assert status == JobStatus.WAITING_FOR_INPUT
    assert prompt == "Name [kirby]: "


def test_job_set_status_without_prompt_clears_it():
    job = _new_job()
    job.set_status(JobStatus.WAITING_FOR_INPUT, prompt="Name [kirby]: ")

    job.set_status(JobStatus.RUNNING)

    status, _, prompt = job.snapshot()
    assert status == JobStatus.RUNNING
    assert prompt is None


def test_job_submit_answer_succeeds_only_while_waiting_for_input():
    job = _new_job()

    # Not waiting yet - a submit here is a no-op, not an error (e.g. a
    # stale/double form submit hitting a job that already moved on).
    assert job.submit_answer("too early") is False

    job.set_status(JobStatus.WAITING_FOR_INPUT, prompt="Name [kirby]: ")
    assert job.submit_answer("Kirby") is True

    # get() should now return exactly what was submitted.
    assert job._answer_queue.get(timeout=1) == "Kirby"


def test_job_submit_answer_rejected_once_finished():
    job = _new_job()
    job.set_status(JobStatus.WAITING_FOR_INPUT, prompt="Name [kirby]: ")
    job.set_status(JobStatus.SUCCEEDED)

    assert job.submit_answer("too late") is False


def test_job_cancel_while_running_marks_cancelled_immediately():
    job = _new_job()

    assert job.cancel() is True

    status, output, prompt = job.snapshot()
    assert status == JobStatus.CANCELLED
    assert prompt is None
    assert "Cancelled." in output


def test_job_cancel_while_waiting_for_input_unblocks_the_answer_queue():
    job = _new_job()
    job.set_status(JobStatus.WAITING_FOR_INPUT, prompt="Name [kirby]: ")

    assert job.cancel() is True

    status, output, prompt = job.snapshot()
    assert status == JobStatus.CANCELLED
    assert prompt is None
    assert "Cancelled." in output

    from blackvue.web.jobs import _CANCEL_SENTINEL

    assert job._answer_queue.get(timeout=1) is _CANCEL_SENTINEL


def test_job_cancel_is_a_noop_once_already_finished():
    job = _new_job()
    job.set_status(JobStatus.SUCCEEDED)

    assert job.cancel() is False

    status, output, _ = job.snapshot()
    assert status == JobStatus.SUCCEEDED
    assert "Cancelled." not in output


def test_job_cancel_is_a_noop_once_already_cancelled():
    job = _new_job()
    job.cancel()

    assert job.cancel() is False


# ---------------------------------------------------------------------------
# JobRunner - generic spawn/answer plumbing (no real CLI module involved)
# ---------------------------------------------------------------------------


def test_runner_get_returns_none_for_an_unknown_job_id():
    runner = JobRunner()

    assert runner.get("does-not-exist") is None


def test_spawned_job_reaches_succeeded_on_exit_code_zero():
    runner = JobRunner()
    job = runner._new_job(command="fake-cmd", username="christer")

    runner._spawn(job, lambda: 0)

    _wait_until(lambda: job.snapshot()[0].is_finished)
    status, output, _ = job.snapshot()
    assert status == JobStatus.SUCCEEDED
    assert "(exit code 0)" in output


def test_spawned_job_reaches_failed_on_nonzero_exit_code():
    runner = JobRunner()
    job = runner._new_job(command="fake-cmd", username="christer")

    runner._spawn(job, lambda: 1)

    _wait_until(lambda: job.snapshot()[0].is_finished)
    status, output, _ = job.snapshot()
    assert status == JobStatus.FAILED
    assert "(exit code 1)" in output


def test_spawned_job_reaches_failed_when_run_raises():
    runner = JobRunner()
    job = runner._new_job(command="fake-cmd", username="christer")

    def boom() -> int:
        raise RuntimeError("camera on fire")

    runner._spawn(job, boom)

    _wait_until(lambda: job.snapshot()[0].is_finished)
    status, output, _ = job.snapshot()
    assert status == JobStatus.FAILED
    assert any("camera on fire" in line for line in output)


def test_make_ask_blocks_until_an_answer_is_submitted_then_echoes_it():
    runner = JobRunner()
    job = runner._new_job(command="fake-cmd", username="christer")
    ask = runner._make_ask(job)

    answers: list[str] = []

    def run() -> int:
        answers.append(ask("Name [kirby]: "))
        return 0

    runner._spawn(job, run)

    _wait_until(lambda: job.snapshot()[0] == JobStatus.WAITING_FOR_INPUT)
    status, output, prompt = job.snapshot()
    assert prompt == "Name [kirby]: "
    assert output[-1] == "Name [kirby]: "

    assert runner.answer(job.id, "Kirby") is True

    _wait_until(lambda: job.snapshot()[0].is_finished)
    assert answers == ["Kirby"]
    _, output, _ = job.snapshot()
    # The echoed "> <answer>" line lets the browser see what it just
    # submitted once the page reloads, same as a real terminal
    # transcript would show what was typed.
    assert "> Kirby" in output


def test_answer_returns_false_for_an_unknown_job_id():
    runner = JobRunner()

    assert runner.answer("does-not-exist", "anything") is False


def test_answer_returns_false_when_job_is_not_waiting():
    runner = JobRunner()
    job = runner._new_job(command="fake-cmd", username="christer")
    runner._spawn(job, lambda: 0)

    _wait_until(lambda: job.snapshot()[0].is_finished)

    assert runner.answer(job.id, "too late") is False


def test_cancel_returns_false_for_an_unknown_job_id():
    runner = JobRunner()

    assert runner.cancel("does-not-exist") is False


def test_cancel_unblocks_a_job_waiting_for_input_and_stops_it_immediately():
    runner = JobRunner()
    job = runner._new_job(command="fake-cmd", username="christer")
    ask = runner._make_ask(job)

    reached_after_ask = []

    def run() -> int:
        ask("Name [kirby]: ")
        # Only reached if ask() returns normally instead of raising -
        # cancellation should mean this line never executes.
        reached_after_ask.append(True)
        return 0

    runner._spawn(job, run)

    _wait_until(lambda: job.snapshot()[0] == JobStatus.WAITING_FOR_INPUT)

    assert runner.cancel(job.id) is True

    _wait_until(lambda: job.snapshot()[0].is_finished)
    status, output, prompt = job.snapshot()
    assert status == JobStatus.CANCELLED
    assert prompt is None
    assert "Cancelled." in output
    assert reached_after_ask == []
    # The thread's own target() wrapper must not overwrite CANCELLED
    # with a stale success/failure status after _JobCancelled unwinds.
    assert "(exit code" not in " ".join(output)


def test_cancel_a_running_job_with_no_open_prompt_marks_it_cancelled():
    runner = JobRunner()
    job = runner._new_job(command="fake-cmd", username="christer")

    release = threading.Event()

    def run() -> int:
        release.wait(timeout=2)
        return 0

    runner._spawn(job, run)

    _wait_until(lambda: job.snapshot()[0] == JobStatus.RUNNING)
    assert runner.cancel(job.id) is True

    status, output, _ = job.snapshot()
    assert status == JobStatus.CANCELLED
    assert "Cancelled." in output

    # Let the background thread actually finish (run() returns 0) and
    # confirm that late completion doesn't clobber the CANCELLED
    # status the browser has already been shown.
    release.set()
    time.sleep(0.1)
    status, output, _ = job.snapshot()
    assert status == JobStatus.CANCELLED
    assert "(exit code 0)" not in output


def test_cancel_returns_false_once_job_already_finished():
    runner = JobRunner()
    job = runner._new_job(command="fake-cmd", username="christer")
    runner._spawn(job, lambda: 0)

    _wait_until(lambda: job.snapshot()[0].is_finished)

    assert runner.cancel(job.id) is False


# ---------------------------------------------------------------------------
# JobRunner.start_bv_config / start_bv_gps - real wiring, fake _run
# ---------------------------------------------------------------------------


def test_start_bv_config_wires_ask_say_warn_into_bv_configs_run(monkeypatch):
    captured = {}

    def fake_run(args, *, ask, say, warn):
        captured["id"] = args.id
        say("Creating new config: /tmp/whatever")
        name = ask("Name [kirby]: ")
        say(f"Saved with name {name}")
        return bv_config_module.EXIT_OK

    monkeypatch.setattr(bv_config_module, "_run", fake_run)

    runner = JobRunner()
    job = runner.start_bv_config(id_="kirby", username="christer")

    assert job.command == "bv-config kirby"

    _wait_until(lambda: job.snapshot()[0] == JobStatus.WAITING_FOR_INPUT)
    assert runner.answer(job.id, "Kirby") is True

    _wait_until(lambda: job.snapshot()[0].is_finished)
    status, output, _ = job.snapshot()
    assert status == JobStatus.SUCCEEDED
    assert captured["id"] == "kirby"
    assert any("Saved with name Kirby" in line for line in output)
    assert "(exit code 0)" in output


def test_start_bv_config_job_fails_when_run_returns_nonzero(monkeypatch):
    def fake_run(args, *, ask, say, warn):
        warn(f"bv-config: {args.id}: invalid id")
        return bv_config_module.EXIT_INVALID_ID

    monkeypatch.setattr(bv_config_module, "_run", fake_run)

    runner = JobRunner()
    job = runner.start_bv_config(id_="bad id", username="christer")

    _wait_until(lambda: job.snapshot()[0].is_finished)
    status, output, _ = job.snapshot()
    assert status == JobStatus.FAILED
    assert any("invalid id" in line for line in output)


def test_start_bv_gps_wires_say_warn_but_needs_no_ask(monkeypatch):
    def fake_run(args, *, say, warn):
        assert args.id == "kirby"
        assert args.host is None
        say("Coordinates: 1.0,2.0")
        return bv_gps_module.EXIT_OK

    monkeypatch.setattr(bv_gps_module, "_run", fake_run)

    runner = JobRunner()
    job = runner.start_bv_gps(id_="kirby", no_address=False, username="christer")

    assert job.command == "bv-gps kirby"

    _wait_until(lambda: job.snapshot()[0].is_finished)
    status, output, _ = job.snapshot()
    assert status == JobStatus.SUCCEEDED
    assert "Coordinates: 1.0,2.0" in output


def test_start_bv_gps_no_address_flag_reaches_parsed_args(monkeypatch):
    def fake_run(args, *, say, warn):
        assert args.id == "kirby"
        assert args.no_address is True
        say("Coordinates: 1.0,2.0")
        return bv_gps_module.EXIT_OK

    monkeypatch.setattr(bv_gps_module, "_run", fake_run)

    runner = JobRunner()
    job = runner.start_bv_gps(id_="kirby", no_address=True, username="christer")

    _wait_until(lambda: job.snapshot()[0].is_finished)
    status, _, _ = job.snapshot()
    assert status == JobStatus.SUCCEEDED
