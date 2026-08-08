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
from blackvue.cli import bv_ls as bv_ls_module
from blackvue.web.jobs import BvExportArgError
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
    job = runner.start_bv_gps(
        id_="kirby", timeout=5, no_address=False, username="christer"
    )

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
        model_size="small",
        diarize=False,
        hf_token=None,
        srt=False,
        lrc=False,
        overwrite=False,
        dry_run=False,
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
            lrc=True,
            overwrite=True,
            dry_run=True,
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
    assert args.lrc is True
    assert args.overwrite is True
    assert args.dry_run is True
    assert args.from_ == "20260101_000000"
    assert args.until == "20260102_000000"


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
        overwrite=False,
        dry_run=False,
        debug=False,
        username="christer",
    )
    kwargs.update(overrides)
    return kwargs


def test_start_bv_export_wires_say_warn_and_command_line_into_run(monkeypatch):
    captured = {}

    def fake_run(args, *, command_line, say, warn):
        captured["path"] = args.path
        captured["target"] = args.target
        captured["command_line"] = command_line
        say("bv-export: done")
        return 0

    monkeypatch.setattr(bv_export_module, "_run", fake_run)

    runner = JobRunner()
    job = runner.start_bv_export(**_export_kwargs())

    assert job.command == "bv-export kirby"

    _wait_until(lambda: job.snapshot()[0].is_finished)
    status, output, _ = job.snapshot()
    assert status == JobStatus.SUCCEEDED
    assert captured["path"] == "/archive/kirby"
    assert captured["target"] == "/trips"
    assert captured["command_line"].startswith("bv-export ")
    assert "bv-export: done" in output


def test_start_bv_export_defaults_reach_parsed_args(monkeypatch):
    captured = {}

    def fake_run(args, *, command_line, say, warn):
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

    def fake_run(args, *, command_line, say, warn):
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
    def fake_run(args, *, command_line, say, warn):
        warn("bv-export: something went wrong")
        return 1

    monkeypatch.setattr(bv_export_module, "_run", fake_run)

    runner = JobRunner()
    job = runner.start_bv_export(**_export_kwargs())

    _wait_until(lambda: job.snapshot()[0].is_finished)
    status, output, _ = job.snapshot()
    assert status == JobStatus.FAILED
    assert any("something went wrong" in line for line in output)


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
