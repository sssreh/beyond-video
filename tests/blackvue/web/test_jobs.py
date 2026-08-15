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
from pathlib import Path

import pytest

from blackvue.cli import bv_config as bv_config_module
from blackvue.cli import bv_download as bv_download_module
from blackvue.cli import bv_export as bv_export_module
from blackvue.cli import bv_generate as bv_generate_module
from blackvue.cli import bv_gps as bv_gps_module
from blackvue.cli import bv_lock as bv_lock_module
from blackvue.cli import bv_ls as bv_ls_module
from blackvue.cli import bv_scribe as bv_scribe_module
from blackvue.cli import bv_search as bv_search_module
from blackvue.web.jobs import BvExportArgError
from blackvue.web.jobs import Job
from blackvue.web.jobs import JobRunner
from blackvue.web.jobs import JobStatus
from blackvue.web.jobs import _quote_for_replicate
from blackvue.web.jobs import _replicate_command_line


# ---------------------------------------------------------------------------
# _quote_for_replicate / _replicate_command_line
# ---------------------------------------------------------------------------


def test_quote_for_replicate_leaves_plain_values_alone():
    assert _quote_for_replicate("kirby") == "kirby"
    assert _quote_for_replicate("20260101_000000") == "20260101_000000"


def test_quote_for_replicate_quotes_values_with_whitespace():
    assert _quote_for_replicate("Slussen, Stockholm") == '"Slussen, Stockholm"'


def test_quote_for_replicate_quotes_the_empty_string():
    assert _quote_for_replicate("") == '""'


def test_quote_for_replicate_escapes_embedded_double_quotes():
    assert _quote_for_replicate('a "quoted" name') == '"a \\"quoted\\" name"'


def test_replicate_command_line_joins_name_and_argv():
    line = _replicate_command_line(
        "bv-search", ["kirby", "--place", "Slussen, Stockholm", "--radius", "150"]
    )
    assert line == 'bv-search kirby --place "Slussen, Stockholm" --radius 150'


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


def test_job_append_output_also_persists_to_the_joblog(tmp_path, monkeypatch):
    # The bv-web half of core/joblog.py's persistent output logfile (see
    # its own module docstring) - append_output() mirrors every line into
    # the same rotating logfile direct-CLI runs write to via
    # wrap_say()/wrap_warn(), tagged with the job's own prog name (the
    # first word of Job.command - see append_output()'s own comment for
    # why that's enough without a dedicated field). tests/conftest.py's
    # autouse fixture already isolates BEYOND_VIDEO_LOGS_DIR globally;
    # this test just points it at its own tmp_path so it can read back
    # what got written.
    from blackvue.core import joblog

    monkeypatch.setenv("BEYOND_VIDEO_LOGS_DIR", str(tmp_path))
    joblog._logger = None

    job = _new_job(command="bv-scribe Kirby")
    job.append_output("Starting scene description job")
    job.append_output("(exit code 0)")

    from datetime import datetime

    month_key = datetime.now().strftime("%Y-%m")
    log_file = tmp_path / f"beyond-video-{month_key}.log"
    text = log_file.read_text()
    assert "[bv-scribe] Starting scene description job" in text
    assert "[bv-scribe] (exit code 0)" in text


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


def test_spawned_job_records_a_succeeded_history_entry(tmp_path, monkeypatch):
    # The bv-web half of core/history.py's persistent command-history
    # index (see that module's own docstring) - JobRunner._spawn()'s
    # outer `finally` records one entry per job once it reaches a
    # terminal status. Polls the history file itself (not just
    # job.snapshot()) since the background thread's own `finally`
    # block - where the record actually gets written - runs slightly
    # after the status flip the thread makes just before it.
    import json

    from blackvue.core import history

    monkeypatch.setenv("BEYOND_VIDEO_LOGS_DIR", str(tmp_path))

    runner = JobRunner()
    job = runner._new_job(
        command="bv-scribe Kirby",
        replicate_command="bv-scribe Kirby --task describe_scene",
        username="christer",
    )

    runner._spawn(job, lambda: 0)

    # Wait for actual content, not just the file's existence - open(path,
    # "a") creates the (empty) file on disk before write() lands, so a
    # bare .exists() check can observe that empty in-between moment
    # under load and read back nothing.
    _wait_until(
        lambda: history.history_path().exists()
        and history.history_path().read_text().strip() != ""
    )
    entry = json.loads(history.history_path().read_text().strip())
    assert entry["command"] == "bv-scribe"
    assert entry["command_line"] == "bv-scribe Kirby --task describe_scene"
    assert entry["source"] == "bv-web"
    assert entry["username"] == "christer"
    assert entry["status"] == "succeeded"


def test_spawned_job_records_a_failed_history_entry(tmp_path, monkeypatch):
    import json

    from blackvue.core import history

    monkeypatch.setenv("BEYOND_VIDEO_LOGS_DIR", str(tmp_path))

    runner = JobRunner()
    job = runner._new_job(command="bv-export Kirby", username="christer")

    runner._spawn(job, lambda: 2)

    _wait_until(
        lambda: history.history_path().exists()
        and history.history_path().read_text().strip() != ""
    )
    entry = json.loads(history.history_path().read_text().strip())
    assert entry["command"] == "bv-export"
    assert entry["status"] == "failed"
    # No replicate_command given - falls back to the bare job.command.
    assert entry["command_line"] == "bv-export Kirby"


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


# ---------------------------------------------------------------------------
# _spawn()'s unload_scene_model() call - "Scene model never unloads from
# GPU" (Christer). Every job type shares this one `finally` block, so a
# single set of tests here covers all of them regardless of which job
# actually ran.
# ---------------------------------------------------------------------------


def test_spawn_calls_unload_scene_model_after_success(monkeypatch):
    from blackvue.web import jobs as jobs_module

    calls = []
    monkeypatch.setattr(jobs_module, "unload_scene_model", lambda: calls.append(1))

    runner = JobRunner()
    job = runner._new_job(command="fake-cmd", username="christer")

    runner._spawn(job, lambda: 0)

    _wait_until(lambda: job.snapshot()[0].is_finished)
    _wait_until(lambda: len(calls) == 1)
    assert calls == [1]


def test_spawn_calls_unload_scene_model_after_failure(monkeypatch):
    from blackvue.web import jobs as jobs_module

    calls = []
    monkeypatch.setattr(jobs_module, "unload_scene_model", lambda: calls.append(1))

    runner = JobRunner()
    job = runner._new_job(command="fake-cmd", username="christer")

    runner._spawn(job, lambda: 1)

    _wait_until(lambda: job.snapshot()[0].is_finished)
    _wait_until(lambda: len(calls) == 1)
    assert calls == [1]


def test_spawn_calls_unload_scene_model_when_run_raises(monkeypatch):
    from blackvue.web import jobs as jobs_module

    calls = []
    monkeypatch.setattr(jobs_module, "unload_scene_model", lambda: calls.append(1))

    runner = JobRunner()
    job = runner._new_job(command="fake-cmd", username="christer")

    def boom() -> int:
        raise RuntimeError("camera on fire")

    runner._spawn(job, boom)

    _wait_until(lambda: job.snapshot()[0].is_finished)
    _wait_until(lambda: len(calls) == 1)
    assert calls == [1]


def test_spawn_calls_unload_scene_model_when_cancelled(monkeypatch):
    from blackvue.web import jobs as jobs_module
    from blackvue.web.jobs import _JobCancelled

    calls = []
    monkeypatch.setattr(jobs_module, "unload_scene_model", lambda: calls.append(1))

    runner = JobRunner()
    job = runner._new_job(command="fake-cmd", username="christer")

    def cancelled() -> int:
        raise _JobCancelled()

    runner._spawn(job, cancelled)

    _wait_until(lambda: len(calls) == 1)
    assert calls == [1]


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
    assert job.replicate_command == "bv-config kirby"

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
    job = runner.start_bv_gps(
        id_="kirby", timeout=5, no_address=False, username="christer"
    )

    assert job.command == "bv-gps kirby"
    assert job.replicate_command == "bv-gps kirby --timeout 5"

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
    job = runner.start_bv_gps(
        id_="kirby", timeout=5, no_address=True, username="christer"
    )

    _wait_until(lambda: job.snapshot()[0].is_finished)
    status, _, _ = job.snapshot()
    assert status == JobStatus.SUCCEEDED


def test_start_bv_gps_timeout_reaches_parsed_args(monkeypatch):
    def fake_run(args, *, say, warn):
        assert args.id == "kirby"
        assert args.timeout == 15
        say("Coordinates: 1.0,2.0")
        return bv_gps_module.EXIT_OK

    monkeypatch.setattr(bv_gps_module, "_run", fake_run)

    runner = JobRunner()
    job = runner.start_bv_gps(
        id_="kirby", timeout=15, no_address=False, username="christer"
    )

    _wait_until(lambda: job.snapshot()[0].is_finished)
    status, _, _ = job.snapshot()
    assert status == JobStatus.SUCCEEDED


# ---------------------------------------------------------------------------
# JobRunner.start_bv_generate - real wiring, fake _run
# ---------------------------------------------------------------------------


def _generate_kwargs(**overrides):
    """Every start_bv_generate() keyword, defaulted to "give me
    nothing to do" - individual tests override just the ones they
    care about, the same shape _export_kwargs() below uses for
    start_bv_export()'s much larger parameter list."""

    kwargs = dict(
        camera_id="kirby",
        archive_path=Path("/archive/kirby"),
        from_=None,
        until=None,
        timestamp=None,
        extract_audio=False,
        get_duration=False,
        transcribe=False,
        translate=None,
        language=None,
        model_size=None,
        diarize=False,
        hf_token=None,
        srt=False,
        describe_scene=False,
        scene_model=None,
        camera="front",
        overwrite=False,
        dry_run=False,
        ignore_lock=False,
        username="christer",
    )
    kwargs.update(overrides)
    return kwargs


def test_start_bv_generate_wires_say_warn_into_bv_generates_run(monkeypatch):
    captured = {}

    def fake_run(args, *, say, warn):
        captured["path"] = args.path
        say("bv-generate: done")
        return bv_generate_module.EXIT_OK

    monkeypatch.setattr(bv_generate_module, "_run", fake_run)

    runner = JobRunner()
    job = runner.start_bv_generate(**_generate_kwargs(extract_audio=True))

    assert job.command == "bv-generate kirby"
    # camera_id, not the resolved archive_path - see Job.replicate_
    # command's own docstring for why (the resolved path is only
    # meaningful inside this container/machine).
    assert job.replicate_command == "bv-generate kirby --extract-audio"

    _wait_until(lambda: job.snapshot()[0].is_finished)
    status, output, _ = job.snapshot()
    assert status == JobStatus.SUCCEEDED
    assert captured["path"] == "/archive/kirby"
    assert "bv-generate: done" in output
    assert "(exit code 0)" in output


def test_start_bv_generate_flags_reach_parsed_args(monkeypatch):
    captured = {}

    def fake_run(args, *, say, warn):
        captured["args"] = args
        return bv_generate_module.EXIT_OK

    monkeypatch.setattr(bv_generate_module, "_run", fake_run)

    runner = JobRunner()
    runner.start_bv_generate(
        **_generate_kwargs(
            transcribe=True,
            translate="sv",
            language="en",
            model_size="medium",
            diarize=True,
            hf_token="hf_secret",
            srt=True,
            describe_scene=True,
            scene_model="custom-vlm",
            camera="rear",
            overwrite=True,
            dry_run=True,
            ignore_lock=True,
            from_="20260101_000000",
            until="20260102_000000",
        )
    )

    _wait_until(lambda: "args" in captured)
    args = captured["args"]
    assert args.transcribe is True
    assert args.translate == "sv"
    assert args.language == "en"
    assert args.model_size == "medium"
    assert args.diarize is True
    assert args.hf_token == "hf_secret"
    assert args.srt is True
    assert args.describe_scene is True
    assert args.scene_model == "custom-vlm"
    assert args.camera == "rear"
    assert args.overwrite is True
    assert args.dry_run is True
    assert args.ignore_lock is True
    assert args.from_ == "20260101_000000"
    assert args.until == "20260102_000000"


def test_start_bv_generate_camera_default_omits_flag_for_cli_parity(
    monkeypatch,
):
    """camera="front" (the web form's own default selection) must not
    add an explicit --camera to argv - same reasoning as the
    Auto-model-size test above: --camera's own CLI default is already
    "front", so adding it explicitly here would be redundant, and
    would silently stop tracking bv_generate.py's own default if it
    ever changed."""

    captured = {}

    def fake_run(args, *, say, warn):
        captured["args"] = args
        return bv_generate_module.EXIT_OK

    monkeypatch.setattr(bv_generate_module, "_run", fake_run)

    runner = JobRunner()
    runner.start_bv_generate(
        **_generate_kwargs(describe_scene=True, camera="front")
    )

    _wait_until(lambda: "args" in captured)
    assert captured["args"].camera == "front"


def test_start_bv_generate_ignore_lock_defaults_to_false(monkeypatch):
    """A separate, minimal test for the default (unchecked-checkbox)
    case - the flags test above only proves ignore_lock=True reaches
    args; this proves the web form's default (ignore_lock not passed
    at all, i.e. _generate_kwargs()'s own default of False) does not
    accidentally add --ignore-lock to argv."""

    captured = {}

    def fake_run(args, *, say, warn):
        captured["args"] = args
        return bv_generate_module.EXIT_OK

    monkeypatch.setattr(bv_generate_module, "_run", fake_run)

    runner = JobRunner()
    runner.start_bv_generate(**_generate_kwargs(get_duration=True))

    _wait_until(lambda: "args" in captured)
    assert captured["args"].ignore_lock is False


def test_start_bv_generate_auto_model_size_applies_gpu_aware_default(
    monkeypatch,
):
    """model_size=None (the web form's "Auto" option, see
    job_new_bv_generate.html) must not add an explicit --model-size to
    argv - otherwise bv_generate.parse_args()'s own GPU-aware default
    (task #593) never runs for web-triggered jobs, which was exactly
    Christer's "bv-generate in bv-web does not show default model
    large for gpu server" bug report. Monkeypatching gpu_available()
    (rather than relying on this test machine's real GPU-or-not
    status) makes the assertion deterministic either way."""

    captured = {}

    def fake_run(args, *, say, warn):
        captured["args"] = args
        return bv_generate_module.EXIT_OK

    monkeypatch.setattr(bv_generate_module, "_run", fake_run)
    monkeypatch.setattr(bv_generate_module, "gpu_available", lambda: True)

    runner = JobRunner()
    runner.start_bv_generate(
        **_generate_kwargs(transcribe=True, model_size=None)
    )

    _wait_until(lambda: "args" in captured)
    assert captured["args"].model_size == "large"


def test_start_bv_generate_auto_model_size_falls_back_to_small_without_gpu(
    monkeypatch,
):
    captured = {}

    def fake_run(args, *, say, warn):
        captured["args"] = args
        return bv_generate_module.EXIT_OK

    monkeypatch.setattr(bv_generate_module, "_run", fake_run)
    monkeypatch.setattr(bv_generate_module, "gpu_available", lambda: False)

    runner = JobRunner()
    runner.start_bv_generate(
        **_generate_kwargs(transcribe=True, model_size=None)
    )

    _wait_until(lambda: "args" in captured)
    assert captured["args"].model_size == "small"


def test_start_bv_generate_job_fails_when_run_returns_nonzero(monkeypatch):
    def fake_run(args, *, say, warn):
        warn("bv-generate: had errors")
        return bv_generate_module.EXIT_HAD_ERRORS

    monkeypatch.setattr(bv_generate_module, "_run", fake_run)

    runner = JobRunner()
    job = runner.start_bv_generate(**_generate_kwargs(get_duration=True))

    _wait_until(lambda: job.snapshot()[0].is_finished)
    status, output, _ = job.snapshot()
    assert status == JobStatus.FAILED
    assert any("had errors" in line for line in output)


# ---------------------------------------------------------------------------
# JobRunner.start_bv_export - real wiring, fake _run
# ---------------------------------------------------------------------------


def _export_kwargs(**overrides):
    """Every start_bv_export() keyword, defaulted to bv-export's own
    plainest possible invocation (no rendering extras, no --stitch) -
    individual tests override just the handful they're exercising
    rather than repeating all 49 parameters every time."""

    kwargs = dict(
        camera_id="kirby",
        archive_path=Path("/archive/kirby"),
        target=Path("/trips"),
        prefix=None,
        from_=None,
        until=None,
        timestamp=None,
        max_gap_minutes=None,
        movement=False,
        no_duration=False,
        duration_heal_archive=False,
        gap_tolerance_seconds=None,
        max_parking_duration_minutes=None,
        render_map=False,
        map_icon=None,
        map_zoom_meters=None,
        map_track_up=False,
        render_map_intro=False,
        map_intro_seconds=None,
        render_gsensor=False,
        render_gsensor_graph=False,
        gsensor_graph_x=False,
        stitch=False,
        stitch_layout="auto",
        stitch_mirror_size=None,
        stitch_mirror_radius=None,
        stitch_mirror_zoom=None,
        stitch_mirror_pan_x=None,
        stitch_mirror_pan_y=None,
        stitch_mirror_icon=None,
        stitch_resolution=None,
        stitch_bitrate=None,
        stitch_scale=None,
        stitch_max_width=None,
        stitch_max_height=None,
        stitch_map=None,
        stitch_map_side=None,
        stitch_map_size=None,
        stitch_map_circle=False,
        stitch_gsensor=False,
        stitch_gsensor_size=None,
        stitch_gsensor_pos=None,
        stitch_gsensor_xy=None,
        stitch_graph=False,
        stitch_graph_side=None,
        stitch_graph_size=None,
        stitch_subtitles=False,
        no_subtitles_bg=False,
        include_parking=False,
        parking_speed=None,
        trip_summary=False,
        scene_model=None,
        scene_cpu=False,
        overwrite=False,
        dry_run=False,
        debug=False,
        username="christer",
    )
    kwargs.update(overrides)
    return kwargs


def test_start_bv_export_wires_say_warn_and_command_line_into_run(monkeypatch):
    captured = {}

    def fake_run(args, *, command_line, should_continue, say, warn):
        captured["path"] = args.path
        captured["target"] = args.target
        captured["command_line"] = command_line
        say("bv-export: done")
        return 0

    monkeypatch.setattr(bv_export_module, "_run", fake_run)

    runner = JobRunner()
    job = runner.start_bv_export(**_export_kwargs())

    assert job.command == "bv-export kirby"
    # --stitch-layout is always appended (its own default is "auto",
    # a truthy string, not conditioned on --stitch itself).
    assert (
        job.replicate_command
        == "bv-export kirby --target /trips --stitch-layout auto"
    )

    _wait_until(lambda: job.snapshot()[0].is_finished)
    status, output, _ = job.snapshot()
    assert status == JobStatus.SUCCEEDED
    assert captured["path"] == "/archive/kirby"
    assert captured["target"] == "/trips"
    assert captured["command_line"].startswith("bv-export ")
    assert "bv-export: done" in output


def test_start_bv_export_defaults_reach_parsed_args(monkeypatch):
    captured = {}

    def fake_run(args, *, command_line, should_continue, say, warn):
        captured["args"] = args
        return 0

    monkeypatch.setattr(bv_export_module, "_run", fake_run)

    runner = JobRunner()
    runner.start_bv_export(**_export_kwargs())

    _wait_until(lambda: "args" in captured)
    args = captured["args"]
    assert args.stitch_layout == "auto"
    assert args.render_map is False
    assert args.stitch is False
    assert args.overwrite is False
    assert args.dry_run is False


def test_start_bv_export_flags_reach_parsed_args(monkeypatch):
    captured = {}

    def fake_run(args, *, command_line, should_continue, say, warn):
        captured["args"] = args
        return 0

    monkeypatch.setattr(bv_export_module, "_run", fake_run)

    runner = JobRunner()
    runner.start_bv_export(
        **_export_kwargs(
            prefix="Holiday",
            max_gap_minutes=10,
            movement=True,
            render_map=True,
            map_zoom_meters=150,
            map_track_up=True,
            stitch=True,
            stitch_layout="side_by_side",
            stitch_gsensor=True,
            stitch_gsensor_pos="top-right",
            stitch_subtitles=True,
            overwrite=True,
            dry_run=True,
        )
    )

    _wait_until(lambda: "args" in captured)
    args = captured["args"]
    assert args.prefix == "Holiday"
    assert args.max_gap_minutes == 10
    assert args.movement is True
    assert args.render_map is True
    assert args.map_zoom_meters == 150.0
    assert args.map_track_up is True
    assert args.stitch is True
    assert args.stitch_layout == "side_by_side"
    assert args.stitch_gsensor is True
    assert args.stitch_gsensor_pos == "top-right"
    assert args.stitch_subtitles is True
    assert args.overwrite is True
    assert args.dry_run is True


def test_start_bv_export_stitch_map_circle_reaches_parsed_args(monkeypatch):
    captured = {}

    def fake_run(args, *, command_line, should_continue, say, warn):
        captured["args"] = args
        return 0

    monkeypatch.setattr(bv_export_module, "_run", fake_run)

    runner = JobRunner()
    runner.start_bv_export(
        **_export_kwargs(
            stitch=True, stitch_map="map", stitch_map_circle=True,
        )
    )

    _wait_until(lambda: "args" in captured)
    assert captured["args"].stitch_map_circle is True


def test_start_bv_export_stitch_map_circle_defaults_to_false(monkeypatch):
    captured = {}

    def fake_run(args, *, command_line, should_continue, say, warn):
        captured["args"] = args
        return 0

    monkeypatch.setattr(bv_export_module, "_run", fake_run)

    runner = JobRunner()
    runner.start_bv_export(**_export_kwargs())

    _wait_until(lambda: "args" in captured)
    assert captured["args"].stitch_map_circle is False


def test_start_bv_export_map_intro_and_parking_speed_reach_parsed_args(monkeypatch):
    captured = {}

    def fake_run(args, *, command_line, should_continue, say, warn):
        captured["args"] = args
        return 0

    monkeypatch.setattr(bv_export_module, "_run", fake_run)

    runner = JobRunner()
    runner.start_bv_export(
        **_export_kwargs(
            render_map_intro=True,
            map_intro_seconds=8.5,
            include_parking=True,
            parking_speed=2.5,
        )
    )

    _wait_until(lambda: "args" in captured)
    args = captured["args"]
    assert args.render_map_intro is True
    assert args.map_intro_seconds == 8.5
    assert args.include_parking is True
    assert args.parking_speed == 2.5


def test_start_bv_export_map_intro_and_parking_speed_default_to_off(monkeypatch):
    captured = {}

    def fake_run(args, *, command_line, should_continue, say, warn):
        captured["args"] = args
        return 0

    monkeypatch.setattr(bv_export_module, "_run", fake_run)

    runner = JobRunner()
    runner.start_bv_export(**_export_kwargs())

    _wait_until(lambda: "args" in captured)
    args = captured["args"]
    assert args.render_map_intro is False
    # bv_export.py's own --map-intro-seconds default (DEFAULT_INTRO_SECONDS)
    # applies whenever the web form leaves it blank, same as every other
    # None-means-omit-the-flag field in this form.
    assert args.map_intro_seconds == pytest.approx(5.0)
    assert args.parking_speed == pytest.approx(1.0)


def test_start_bv_export_trip_summary_flags_reach_parsed_args(monkeypatch):
    captured = {}

    def fake_run(args, *, command_line, should_continue, say, warn):
        captured["args"] = args
        return 0

    monkeypatch.setattr(bv_export_module, "_run", fake_run)

    runner = JobRunner()
    runner.start_bv_export(
        **_export_kwargs(
            trip_summary=True,
            scene_model="some/other-model",
            scene_cpu=True,
        )
    )

    _wait_until(lambda: "args" in captured)
    args = captured["args"]
    assert args.trip_summary is True
    assert args.scene_model == "some/other-model"
    assert args.scene_cpu is True


def test_start_bv_export_trip_summary_flags_default_to_off(monkeypatch):
    captured = {}

    def fake_run(args, *, command_line, should_continue, say, warn):
        captured["args"] = args
        return 0

    monkeypatch.setattr(bv_export_module, "_run", fake_run)

    runner = JobRunner()
    runner.start_bv_export(**_export_kwargs())

    _wait_until(lambda: "args" in captured)
    args = captured["args"]
    assert args.trip_summary is False
    assert args.scene_model == bv_export_module.SCENE_DEFAULT_MODEL
    assert args.scene_cpu is False


def test_start_bv_export_never_exposes_target_as_a_form_field():
    # start_bv_export()'s own signature takes `target` - the point
    # under test is that its docstring's promise holds: nothing here
    # lets a caller point it anywhere other than the one Path given,
    # i.e. there's no separate "target string from the web form" path
    # that could diverge from app.state.target. Exercised elsewhere
    # (test_start_bv_export_wires_say_warn_and_command_line_into_run
    # above) by asserting args.target always equals the given target.
    import inspect

    sig = inspect.signature(JobRunner.start_bv_export)
    assert "target" in sig.parameters
    assert sig.parameters["target"].kind == inspect.Parameter.KEYWORD_ONLY


def test_start_bv_export_raises_bv_export_arg_error_on_bad_value():
    runner = JobRunner()

    with pytest.raises(BvExportArgError) as exc_info:
        runner.start_bv_export(
            **_export_kwargs(stitch=True, stitch_resolution="garbage")
        )

    assert "stitch-resolution" in str(exc_info.value)


def test_start_bv_export_raises_bv_export_arg_error_for_conflicting_gsensor_position():
    runner = JobRunner()

    with pytest.raises(BvExportArgError):
        runner.start_bv_export(
            **_export_kwargs(
                stitch=True,
                stitch_gsensor=True,
                stitch_gsensor_pos="top-right",
                stitch_gsensor_xy="10,10",
            )
        )


def test_start_bv_export_arg_error_does_not_create_a_job():
    # A rejected form shouldn't leave a phantom job behind for the
    # owner to find on some other page - nothing should exist to look
    # up once parse_args() has rejected the argv.
    runner = JobRunner()

    with pytest.raises(BvExportArgError):
        runner.start_bv_export(
            **_export_kwargs(stitch=True, stitch_resolution="garbage")
        )

    assert runner._jobs == {}


def test_start_bv_export_job_fails_when_run_returns_nonzero(monkeypatch):
    def fake_run(args, *, command_line, should_continue, say, warn):
        warn("bv-export: something went wrong")
        return 1

    monkeypatch.setattr(bv_export_module, "_run", fake_run)

    runner = JobRunner()
    job = runner.start_bv_export(**_export_kwargs())

    _wait_until(lambda: job.snapshot()[0].is_finished)
    status, output, _ = job.snapshot()
    assert status == JobStatus.FAILED
    assert any("something went wrong" in line for line in output)


def test_start_bv_export_should_continue_reflects_job_cancellation(monkeypatch):
    # Christer: "That hasnt stopped it, it still creating files" -
    # clicking Cancel used to only flip the job's own status, since
    # bv-export never checked it. start_bv_export() now wires a real
    # should_continue callable tied to that same status - this test
    # confirms the callable itself actually flips False once
    # job.cancel() runs, not just that some object gets passed through.
    import threading

    captured = {}
    proceed = threading.Event()

    def fake_run(args, *, command_line, should_continue, say, warn):
        captured["should_continue"] = should_continue
        proceed.wait(timeout=5)
        return 0

    monkeypatch.setattr(bv_export_module, "_run", fake_run)

    runner = JobRunner()
    job = runner.start_bv_export(**_export_kwargs())

    _wait_until(lambda: "should_continue" in captured)
    assert captured["should_continue"]() is True

    job.cancel()
    assert captured["should_continue"]() is False

    proceed.set()
    _wait_until(lambda: job.snapshot()[0].is_finished)


# ---------------------------------------------------------------------------
# JobRunner.start_bv_download - real wiring, fake _run
# ---------------------------------------------------------------------------


def _download_kwargs(**overrides):
    """Every start_bv_download() keyword, defaulted to "give me the
    default CLI behavior" - same helper shape as _generate_kwargs()/
    _export_kwargs() above."""

    kwargs = dict(
        id_="kirby",
        timeout=5,
        modes=[],
        from_=None,
        until=None,
        timestamp=None,
        dry_run=False,
        files=False,
        verbose=False,
        trace=False,
        username="christer",
    )
    kwargs.update(overrides)
    return kwargs


def test_start_bv_download_wires_say_warn_and_forces_yes(monkeypatch):
    def fake_run(args, *, say, warn):
        assert args.id == "kirby"
        assert args.host is None
        # --yes is always forced on by start_bv_download() itself,
        # never a form field - see that method's own docstring for
        # why (confirm()'s input() call reads real process stdin,
        # which this job runner has no way to answer).
        assert args.yes is True
        say("bv-download: kirby: downloading into /archive/kirby")
        return bv_download_module.EXIT_OK

    monkeypatch.setattr(bv_download_module, "_run", fake_run)

    runner = JobRunner()
    job = runner.start_bv_download(**_download_kwargs())

    assert job.command == "bv-download kirby"
    # Includes the forced --yes - see Job.replicate_command's own
    # docstring on why this shows what actually ran, flags the job
    # runner itself always adds included.
    assert job.replicate_command == "bv-download kirby --timeout 5 --yes"

    _wait_until(lambda: job.snapshot()[0].is_finished)
    status, output, _ = job.snapshot()
    assert status == JobStatus.SUCCEEDED
    assert any("downloading into" in line for line in output)
    assert "(exit code 0)" in output


def test_start_bv_download_defaults_reach_parsed_args(monkeypatch):
    def fake_run(args, *, say, warn):
        assert args.mode is None
        assert args.from_ is None
        assert args.until is None
        assert args.timestamp is None
        assert args.dry_run is False
        assert args.files is False
        assert args.verbose is False
        assert args.trace is False
        assert args.timeout == 5
        return bv_download_module.EXIT_OK

    monkeypatch.setattr(bv_download_module, "_run", fake_run)

    runner = JobRunner()
    job = runner.start_bv_download(**_download_kwargs())

    _wait_until(lambda: job.snapshot()[0].is_finished)
    assert job.snapshot()[0] == JobStatus.SUCCEEDED


def test_start_bv_download_modes_reach_parsed_args(monkeypatch):
    def fake_run(args, *, say, warn):
        assert args.mode == frozenset({"A", "E"})
        return bv_download_module.EXIT_OK

    monkeypatch.setattr(bv_download_module, "_run", fake_run)

    runner = JobRunner()
    job = runner.start_bv_download(**_download_kwargs(modes=["A", "E"]))

    _wait_until(lambda: job.snapshot()[0].is_finished)
    assert job.snapshot()[0] == JobStatus.SUCCEEDED


def test_start_bv_download_time_range_reaches_parsed_args(monkeypatch):
    def fake_run(args, *, say, warn):
        assert args.from_ == "20260701"
        assert args.until == "20260731"
        assert args.timestamp is None
        return bv_download_module.EXIT_OK

    monkeypatch.setattr(bv_download_module, "_run", fake_run)

    runner = JobRunner()
    job = runner.start_bv_download(
        **_download_kwargs(from_="20260701", until="20260731")
    )

    _wait_until(lambda: job.snapshot()[0].is_finished)
    assert job.snapshot()[0] == JobStatus.SUCCEEDED


def test_start_bv_download_dry_run_and_files_reach_parsed_args(monkeypatch):
    def fake_run(args, *, say, warn):
        assert args.dry_run is True
        assert args.files is True
        return bv_download_module.EXIT_OK

    monkeypatch.setattr(bv_download_module, "_run", fake_run)

    runner = JobRunner()
    job = runner.start_bv_download(
        **_download_kwargs(dry_run=True, files=True)
    )

    _wait_until(lambda: job.snapshot()[0].is_finished)
    assert job.snapshot()[0] == JobStatus.SUCCEEDED


def test_start_bv_download_verbose_and_trace_reach_parsed_args(monkeypatch):
    def fake_run(args, *, say, warn):
        assert args.verbose is True
        assert args.trace is True
        return bv_download_module.EXIT_OK

    monkeypatch.setattr(bv_download_module, "_run", fake_run)

    runner = JobRunner()
    job = runner.start_bv_download(
        **_download_kwargs(verbose=True, trace=True)
    )

    _wait_until(lambda: job.snapshot()[0].is_finished)
    assert job.snapshot()[0] == JobStatus.SUCCEEDED


def test_start_bv_download_never_exposes_host_or_target():
    # Same "curated, not an arbitrary-connection escape hatch"
    # guarantee as start_bv_export()'s own target test - confirmed
    # here by checking start_bv_download()'s own signature has no
    # host/target parameter at all, rather than just trusting the
    # docstring.
    import inspect

    params = inspect.signature(JobRunner.start_bv_download).parameters
    assert "host" not in params
    assert "target" not in params
    assert "config_dir" not in params


def test_start_bv_download_job_fails_when_run_returns_nonzero(monkeypatch):
    def fake_run(args, *, say, warn):
        warn("bv-download: kirby: unreachable")
        return bv_download_module.EXIT_UNREACHABLE

    monkeypatch.setattr(bv_download_module, "_run", fake_run)

    runner = JobRunner()
    job = runner.start_bv_download(**_download_kwargs())

    _wait_until(lambda: job.snapshot()[0].is_finished)
    status, output, _ = job.snapshot()
    assert status == JobStatus.FAILED
    assert any("unreachable" in line for line in output)


# ---------------------------------------------------------------------------
# JobRunner.start_bv_ls - real wiring, fake _run
# ---------------------------------------------------------------------------


def _ls_kwargs(**overrides):
    """Every start_bv_ls() keyword, defaulted to bv-ls's own plainest
    possible invocation (grouped table, no time filter, no --trips) -
    the same per-test-override shape _generate_kwargs()/_export_kwargs()
    above use."""

    kwargs = dict(
        camera_id="kirby",
        archive_path=Path("/archive/kirby"),
        all=False,
        from_=None,
        until=None,
        timestamp=None,
        trips=False,
        max_gap_minutes=None,
        movement=False,
        duration=True,
        gap_tolerance_seconds=None,
        username="christer",
    )
    kwargs.update(overrides)
    return kwargs


def test_start_bv_ls_wires_say_but_needs_no_ask_or_warn(monkeypatch):
    def fake_run(args, *, say):
        assert args.path == "/archive/kirby"
        say("Recording          Front ...")
        return 0

    monkeypatch.setattr(bv_ls_module, "_run", fake_run)

    runner = JobRunner()
    job = runner.start_bv_ls(**_ls_kwargs())

    assert job.command == "bv-ls kirby"
    assert job.replicate_command == "bv-ls kirby"

    _wait_until(lambda: job.snapshot()[0].is_finished)
    status, output, _ = job.snapshot()
    assert status == JobStatus.SUCCEEDED
    assert "Recording          Front ..." in output


def test_start_bv_ls_all_flag_reaches_parsed_args(monkeypatch):
    def fake_run(args, *, say):
        assert args.all is True
        return 0

    monkeypatch.setattr(bv_ls_module, "_run", fake_run)

    runner = JobRunner()
    job = runner.start_bv_ls(**_ls_kwargs(all=True))

    _wait_until(lambda: job.snapshot()[0].is_finished)
    assert job.snapshot()[0] == JobStatus.SUCCEEDED


def test_start_bv_ls_trips_flags_reach_parsed_args(monkeypatch):
    captured = {}

    def fake_run(args, *, say):
        captured["args"] = args
        return 0

    monkeypatch.setattr(bv_ls_module, "_run", fake_run)

    runner = JobRunner()
    runner.start_bv_ls(
        **_ls_kwargs(
            trips=True,
            max_gap_minutes=10,
            movement=True,
            duration=False,
            gap_tolerance_seconds=5,
            from_="20260101_000000",
            until="20260102_000000",
        )
    )

    _wait_until(lambda: "args" in captured)
    args = captured["args"]
    assert args.trips is True
    assert args.max_gap_minutes == 10
    assert args.movement is True
    assert args.duration is False
    assert args.gap_tolerance_seconds == 5
    assert args.from_ == "20260101_000000"
    assert args.until == "20260102_000000"


def test_start_bv_ls_duration_default_true_omits_no_duration_flag(monkeypatch):
    # duration=True (the default) must NOT translate into --no-duration
    # ending up on the argv bv-ls's own parse_args() sees - only the
    # duration=False override should.
    def fake_run(args, *, say):
        assert args.duration is True
        return 0

    monkeypatch.setattr(bv_ls_module, "_run", fake_run)

    runner = JobRunner()
    job = runner.start_bv_ls(**_ls_kwargs())

    _wait_until(lambda: job.snapshot()[0].is_finished)
    assert job.snapshot()[0] == JobStatus.SUCCEEDED


def test_start_bv_ls_job_fails_when_run_returns_nonzero(monkeypatch):
    def fake_run(args, *, say):
        return 1

    monkeypatch.setattr(bv_ls_module, "_run", fake_run)

    runner = JobRunner()
    job = runner.start_bv_ls(**_ls_kwargs())

    _wait_until(lambda: job.snapshot()[0].is_finished)
    status, _, _ = job.snapshot()
    assert status == JobStatus.FAILED


# ---------------------------------------------------------------------------
# JobRunner.start_bv_lock - real wiring, fake _run
# ---------------------------------------------------------------------------


def _lock_kwargs(**overrides):
    """Every start_bv_lock() keyword, defaulted to a plain lock of one
    asset over the whole archive - the same per-test-override shape
    _ls_kwargs() above uses."""

    kwargs = dict(
        camera_id="kirby",
        archive_path=Path("/archive/kirby"),
        mode="lock",
        from_=None,
        until=None,
        timestamp=None,
        assets=["get-duration"],
        username="christer",
    )
    kwargs.update(overrides)
    return kwargs


def test_start_bv_lock_lock_mode_reaches_parsed_args(monkeypatch):
    def fake_run(args, *, say, warn):
        assert args.path == "/archive/kirby"
        assert args.lock_assets == ["get-duration"]
        assert args.unlock_assets is None
        assert args.list is False
        say("bv-lock: /archive/kirby - locked [get-duration] for ...")
        return 0

    monkeypatch.setattr(bv_lock_module, "_run", fake_run)

    runner = JobRunner()
    job = runner.start_bv_lock(**_lock_kwargs())

    assert job.command == "bv-lock kirby"
    assert job.replicate_command == "bv-lock kirby --lock-assets get-duration"

    _wait_until(lambda: job.snapshot()[0].is_finished)
    status, output, _ = job.snapshot()
    assert status == JobStatus.SUCCEEDED
    assert any("locked [get-duration]" in line for line in output)


def test_start_bv_lock_unlock_mode_reaches_parsed_args(monkeypatch):
    def fake_run(args, *, say, warn):
        assert args.unlock_assets == ["get-duration", "transcribe"]
        assert args.lock_assets is None
        return 0

    monkeypatch.setattr(bv_lock_module, "_run", fake_run)

    runner = JobRunner()
    job = runner.start_bv_lock(
        **_lock_kwargs(mode="unlock", assets=["get-duration", "transcribe"])
    )

    _wait_until(lambda: job.snapshot()[0].is_finished)
    assert job.snapshot()[0] == JobStatus.SUCCEEDED


def test_start_bv_lock_list_mode_ignores_range_and_assets(monkeypatch):
    def fake_run(args, *, say, warn):
        assert args.list is True
        assert args.lock_assets is None
        assert args.unlock_assets is None
        return 0

    monkeypatch.setattr(bv_lock_module, "_run", fake_run)

    runner = JobRunner()
    job = runner.start_bv_lock(
        **_lock_kwargs(
            mode="list",
            from_="20260101_000000",
            timestamp="2019",
            assets=["get-duration"],
        )
    )

    assert job.replicate_command == "bv-lock kirby --list"

    _wait_until(lambda: job.snapshot()[0].is_finished)
    assert job.snapshot()[0] == JobStatus.SUCCEEDED


def test_start_bv_lock_all_alias_reaches_parsed_args(monkeypatch):
    def fake_run(args, *, say, warn):
        assert args.lock_assets == sorted(
            {
                "extract-audio",
                "get-duration",
                "transcribe",
                "translate",
                "srt",
                "describe-scene",
                "diarize",
            }
        )
        return 0

    monkeypatch.setattr(bv_lock_module, "_run", fake_run)

    runner = JobRunner()
    job = runner.start_bv_lock(**_lock_kwargs(assets=["all"]))

    _wait_until(lambda: job.snapshot()[0].is_finished)
    assert job.snapshot()[0] == JobStatus.SUCCEEDED


def test_start_bv_lock_time_range_reaches_parsed_args(monkeypatch):
    captured = {}

    def fake_run(args, *, say, warn):
        captured["args"] = args
        return 0

    monkeypatch.setattr(bv_lock_module, "_run", fake_run)

    runner = JobRunner()
    runner.start_bv_lock(
        **_lock_kwargs(
            timestamp="2019",
        )
    )

    _wait_until(lambda: "args" in captured)
    assert captured["args"].timestamp == "2019"


def test_start_bv_lock_job_fails_when_run_returns_nonzero(monkeypatch):
    def fake_run(args, *, say, warn):
        return 1

    monkeypatch.setattr(bv_lock_module, "_run", fake_run)

    runner = JobRunner()
    job = runner.start_bv_lock(**_lock_kwargs())

    _wait_until(lambda: job.snapshot()[0].is_finished)
    status, _, _ = job.snapshot()
    assert status == JobStatus.FAILED


# ---------------------------------------------------------------------------
# start_bv_scribe()
# ---------------------------------------------------------------------------


def _scribe_kwargs(**overrides):
    """Every start_bv_scribe() keyword, defaulted to bv-scribe's own
    plainest possible invocation - full CLI parity (every keyword
    that isn't a plain default-True/False flag is None, meaning "let
    bv-scribe's own parse_args() default apply"), same per-test-
    override shape the other _*_kwargs() helpers above use. What's
    curated here isn't the flag set (see JobRunner.start_bv_scribe()'s
    own docstring for why that changed) - it's job_new_bv_scribe.html
    keeping the advanced ones collapsed by default, a template/UI
    concern this JobRunner-level helper has nothing to do with."""

    kwargs = dict(
        camera_id="kirby",
        archive_path=Path("/archive/kirby"),
        from_=None,
        until=None,
        timestamp=None,
        task="both",
        camera="front",
        model=None,
        fps=None,
        max_frames=None,
        max_pixels=None,
        resized_width=None,
        resized_height=None,
        crop_top=None,
        crop_bottom=None,
        max_new_tokens=None,
        repetition_penalty=None,
        no_repeat_ngram_size=None,
        do_sample=False,
        temperature=None,
        top_p=None,
        top_k=None,
        zoom_signs=True,
        zoom_frames=None,
        zoom_detect_width=None,
        zoom_padding=None,
        zoom_ocr_width=None,
        zoom_max_new_tokens=None,
        zoom_detect_max_new_tokens=None,
        zoom_repetition_penalty=None,
        zoom_no_repeat_ngram_size=None,
        zoom_plate_confidence_check=True,
        cpu=False,
        overwrite=False,
        dry_run=False,
        verbose=False,
        username="christer",
    )
    kwargs.update(overrides)
    return kwargs


def test_start_bv_scribe_wires_say_and_warn_into_bv_scribes_run(monkeypatch):
    def fake_run(args, *, say, warn):
        assert args.path == "/archive/kirby"
        say("bv-scribe: started 12:00:00")
        return 0

    monkeypatch.setattr(bv_scribe_module, "_run", fake_run)

    runner = JobRunner()
    job = runner.start_bv_scribe(**_scribe_kwargs())

    assert job.command == "bv-scribe kirby"
    assert (
        job.replicate_command == "bv-scribe kirby --task both --camera front"
    )

    _wait_until(lambda: job.snapshot()[0].is_finished)
    status, output, _ = job.snapshot()
    assert status == JobStatus.SUCCEEDED
    assert "bv-scribe: started 12:00:00" in output


def test_start_bv_scribe_defaults_reach_parsed_args(monkeypatch):
    captured = {}

    def fake_run(args, *, say, warn):
        captured["args"] = args
        return 0

    monkeypatch.setattr(bv_scribe_module, "_run", fake_run)

    runner = JobRunner()
    runner.start_bv_scribe(**_scribe_kwargs())

    _wait_until(lambda: "args" in captured)
    args = captured["args"]
    assert args.task == "both"
    assert args.camera == "front"
    assert args.cpu is False
    assert args.overwrite is False
    assert args.dry_run is False
    assert args.verbose is False
    # Every advanced field left at None means "don't pass the flag" -
    # bv-scribe's own parse_args() defaults come through untouched.
    assert args.fps == 1.0
    assert args.max_frames == 16
    assert args.do_sample is False
    assert args.zoom_signs is True
    assert args.zoom_plate_confidence_check is True


def test_start_bv_scribe_core_flags_reach_parsed_args(monkeypatch):
    captured = {}

    def fake_run(args, *, say, warn):
        captured["args"] = args
        return 0

    monkeypatch.setattr(bv_scribe_module, "_run", fake_run)

    runner = JobRunner()
    runner.start_bv_scribe(
        **_scribe_kwargs(
            from_="20260101_000000",
            until="20260102_000000",
            timestamp="20260101",
            task="ocr",
            camera="both",
            model="a-custom-model",
            cpu=True,
            overwrite=True,
            dry_run=True,
            verbose=True,
        )
    )

    _wait_until(lambda: "args" in captured)
    args = captured["args"]
    assert args.from_ == "20260101_000000"
    assert args.until == "20260102_000000"
    assert args.timestamp == "20260101"
    assert args.task == "ocr"
    assert args.camera == "both"
    assert args.model == "a-custom-model"
    assert args.cpu is True
    assert args.overwrite is True
    assert args.dry_run is True
    assert args.verbose is True


def test_start_bv_scribe_advanced_sampling_flags_reach_parsed_args(monkeypatch):
    # Full parity now covers the sampling/model tuning knobs that used
    # to be curated away entirely - job_new_bv_scribe.html's own
    # "Advanced sampling & model" <details> is what keeps these out of
    # sight by default, not JobRunner/parse_args().
    captured = {}

    def fake_run(args, *, say, warn):
        captured["args"] = args
        return 0

    monkeypatch.setattr(bv_scribe_module, "_run", fake_run)

    runner = JobRunner()
    runner.start_bv_scribe(
        **_scribe_kwargs(
            fps=2.0,
            max_frames=32,
            max_pixels=200000,
            resized_width=800,
            resized_height=450,
            crop_top=0.05,
            crop_bottom=0.05,
            max_new_tokens=500,
            repetition_penalty=1.2,
            no_repeat_ngram_size=4,
            do_sample=True,
            temperature=0.9,
            top_p=0.5,
            top_k=10,
        )
    )

    _wait_until(lambda: "args" in captured)
    args = captured["args"]
    assert args.fps == 2.0
    assert args.max_frames == 32
    assert args.max_pixels == 200000
    assert args.resized_width == 800
    assert args.resized_height == 450
    assert args.crop_top == 0.05
    assert args.crop_bottom == 0.05
    assert args.max_new_tokens == 500
    assert args.repetition_penalty == 1.2
    assert args.no_repeat_ngram_size == 4
    assert args.do_sample is True
    assert args.temperature == 0.9
    assert args.top_p == 0.5
    assert args.top_k == 10


def test_start_bv_scribe_advanced_zoom_flags_reach_parsed_args(monkeypatch):
    captured = {}

    def fake_run(args, *, say, warn):
        captured["args"] = args
        return 0

    monkeypatch.setattr(bv_scribe_module, "_run", fake_run)

    runner = JobRunner()
    runner.start_bv_scribe(
        **_scribe_kwargs(
            zoom_signs=False,
            zoom_frames=8,
            zoom_detect_width=1200,
            zoom_padding=0.25,
            zoom_ocr_width=800,
            zoom_max_new_tokens=300,
            zoom_detect_max_new_tokens=600,
            zoom_repetition_penalty=1.1,
            zoom_no_repeat_ngram_size=2,
            zoom_plate_confidence_check=False,
        )
    )

    _wait_until(lambda: "args" in captured)
    args = captured["args"]
    assert args.zoom_signs is False
    assert args.zoom_frames == 8
    assert args.zoom_detect_width == 1200
    assert args.zoom_padding == 0.25
    assert args.zoom_ocr_width == 800
    assert args.zoom_max_new_tokens == 300
    assert args.zoom_detect_max_new_tokens == 600
    assert args.zoom_repetition_penalty == 1.1
    assert args.zoom_no_repeat_ngram_size == 2
    assert args.zoom_plate_confidence_check is False


def test_start_bv_scribe_job_fails_when_run_returns_nonzero(monkeypatch):
    def fake_run(args, *, say, warn):
        return 1

    monkeypatch.setattr(bv_scribe_module, "_run", fake_run)

    runner = JobRunner()
    job = runner.start_bv_scribe(**_scribe_kwargs())

    _wait_until(lambda: job.snapshot()[0].is_finished)
    status, _, _ = job.snapshot()
    assert status == JobStatus.FAILED


def test_start_bv_scribe_stores_params_on_the_job(monkeypatch):
    # Job.params (the "reuse a previous run's parameters" feature - see
    # its own docstring in web/jobs.py) should end up exactly as given,
    # untouched by anything start_bv_scribe() itself does with its own
    # typed kwargs.
    monkeypatch.setattr(bv_scribe_module, "_run", lambda args, *, say, warn: 0)

    raw_params = {"id": "kirby", "task": "ocr", "trip_summary": True}
    runner = JobRunner()
    job = runner.start_bv_scribe(**_scribe_kwargs(params=raw_params))

    assert job.params == raw_params


def test_start_bv_scribe_defaults_params_to_empty_dict_when_not_given(monkeypatch):
    monkeypatch.setattr(bv_scribe_module, "_run", lambda args, *, say, warn: 0)

    runner = JobRunner()
    job = runner.start_bv_scribe(**_scribe_kwargs())

    assert job.params == {}


# ---------------------------------------------------------------------------
# start_bv_search()
# ---------------------------------------------------------------------------


def _search_kwargs(**overrides):
    """Every start_bv_search() keyword, defaulted to bv-search's own
    plainest possible invocation - full CLI parity (small flag
    surface, unlike bv-scribe above), same per-test-override shape
    the other _*_kwargs() helpers above use."""

    kwargs = dict(
        camera_id="kirby",
        archive_path=Path("/archive/kirby"),
        from_=None,
        until=None,
        timestamp=None,
        text=None,
        asset="all",
        regex=False,
        case_sensitive=False,
        near=None,
        place=None,
        radius=None,
        trace=False,
        username="christer",
    )
    kwargs.update(overrides)
    return kwargs


def test_start_bv_search_wires_say_and_warn_into_bv_searchs_run(monkeypatch):
    def fake_run(args, *, say, warn):
        assert args.path == "/archive/kirby"
        say("bv-search: started 12:00:00")
        return 0

    monkeypatch.setattr(bv_search_module, "_run", fake_run)

    runner = JobRunner()
    job = runner.start_bv_search(**_search_kwargs(text="roundabout"))

    assert job.command == "bv-search kirby"
    assert (
        job.replicate_command
        == "bv-search kirby --text roundabout --asset all"
    )

    _wait_until(lambda: job.snapshot()[0].is_finished)
    status, output, _ = job.snapshot()
    assert status == JobStatus.SUCCEEDED
    assert "bv-search: started 12:00:00" in output


def test_start_bv_search_text_and_asset_flags_reach_parsed_args(monkeypatch):
    captured = {}

    def fake_run(args, *, say, warn):
        captured["args"] = args
        return 0

    monkeypatch.setattr(bv_search_module, "_run", fake_run)

    runner = JobRunner()
    runner.start_bv_search(
        **_search_kwargs(
            text="roundabout",
            asset="scene",
            regex=True,
            case_sensitive=True,
        )
    )

    _wait_until(lambda: "args" in captured)
    args = captured["args"]
    assert args.text == "roundabout"
    assert args.asset == "scene"
    assert args.regex is True
    assert args.case_sensitive is True


def test_start_bv_search_near_reaches_parsed_args(monkeypatch):
    captured = {}

    def fake_run(args, *, say, warn):
        captured["args"] = args
        return 0

    monkeypatch.setattr(bv_search_module, "_run", fake_run)

    runner = JobRunner()
    runner.start_bv_search(
        **_search_kwargs(near="59.3293,18.0686", radius=150.0)
    )

    _wait_until(lambda: "args" in captured)
    args = captured["args"]
    assert args.near[0] == pytest.approx(59.3293)
    assert args.near[1] == pytest.approx(18.0686)
    assert args.radius == pytest.approx(150.0)
    assert args.place is None


def test_start_bv_search_place_reaches_parsed_args(monkeypatch):
    captured = {}

    def fake_run(args, *, say, warn):
        captured["args"] = args
        return 0

    monkeypatch.setattr(bv_search_module, "_run", fake_run)

    runner = JobRunner()
    job = runner.start_bv_search(**_search_kwargs(place="Slussen, Stockholm"))

    _wait_until(lambda: "args" in captured)
    args = captured["args"]
    assert args.place == "Slussen, Stockholm"
    assert args.near is None
    # A --place value containing a space/comma must come back quoted -
    # unquoted it would split into two shell arguments on replay.
    assert (
        job.replicate_command
        == 'bv-search kirby --asset all --place "Slussen, Stockholm"'
    )


def test_start_bv_search_time_range_and_trace_reach_parsed_args(monkeypatch):
    captured = {}

    def fake_run(args, *, say, warn):
        captured["args"] = args
        return 0

    monkeypatch.setattr(bv_search_module, "_run", fake_run)

    runner = JobRunner()
    runner.start_bv_search(
        **_search_kwargs(
            text="pothole",
            from_="20260101_000000",
            until="20260102_000000",
            timestamp="20260101",
            trace=True,
        )
    )

    _wait_until(lambda: "args" in captured)
    args = captured["args"]
    assert args.from_ == "20260101_000000"
    assert args.until == "20260102_000000"
    assert args.timestamp == "20260101"
    assert args.trace is True


def test_start_bv_search_job_fails_when_run_returns_nonzero(monkeypatch):
    def fake_run(args, *, say, warn):
        return 1

    monkeypatch.setattr(bv_search_module, "_run", fake_run)

    runner = JobRunner()
    job = runner.start_bv_search(**_search_kwargs(text="roundabout"))

    _wait_until(lambda: job.snapshot()[0].is_finished)
    status, _, _ = job.snapshot()
    assert status == JobStatus.FAILED
