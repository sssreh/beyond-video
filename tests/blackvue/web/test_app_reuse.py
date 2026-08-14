"""
Tests for web/app.py's "reuse a previous run's parameters" helpers -
_recent_web_runs() and _reuse_defaults() (see their own docstrings) -
the bv-scribe pilot of the feature Christer asked for: "i would like
to have a button or something like in bv-web to get the latest run
parameters filled in for bv-web or maybe a list of the latest."

Deliberately its own file rather than folded into test_jobs.py:
web/app.py imports fastapi (unlike web/jobs.py), so this module can
only be collected in an environment with the `web` extra installed
(CI installs it - see .github/workflows/*.yml - but this repo's own
day-to-day sandbox often doesn't have fastapi available, hence the
separate file rather than mixing collection-import requirements
inside test_jobs.py).

Copyright (C) 2026 Christer R. (sssreh)

SPDX-License-Identifier: GPL-3.0-or-later
"""

from __future__ import annotations

from datetime import datetime
from datetime import timezone

import pytest
from fastapi import HTTPException

from blackvue.core import history
from blackvue.web.app import _authorize_job_view
from blackvue.web.app import _job_camera_id
from blackvue.web.app import _recent_web_runs
from blackvue.web.app import _reuse_defaults
from blackvue.web.app import _sliced_job_output
from blackvue.web.app import TAIL_LINE_COUNT
from blackvue.web.jobs import Job
from blackvue.web.jobs import JobStatus
from blackvue.web.users import User


def _record(tmp_path, monkeypatch, **overrides):
    monkeypatch.setenv("BEYOND_VIDEO_LOGS_DIR", str(tmp_path))
    fields = dict(
        command="bv-scribe",
        command_line="bv-scribe kirby --task both",
        source="bv-web",
        username="christer",
        started_at="2026-08-11T12:00:00+00:00",
        duration_seconds=5.0,
        status="succeeded",
        params={"id": "kirby", "task": "both"},
    )
    fields.update(overrides)
    history.record(history.HistoryEntry(**fields))


# ---------------------------------------------------------------------------
# _recent_web_runs()
# ---------------------------------------------------------------------------


def test_recent_web_runs_empty_when_no_history(tmp_path, monkeypatch):
    monkeypatch.setenv("BEYOND_VIDEO_LOGS_DIR", str(tmp_path))

    assert _recent_web_runs("bv-scribe") == []


def test_recent_web_runs_excludes_cli_entries(tmp_path, monkeypatch):
    _record(tmp_path, monkeypatch, source="cli", username=None)

    assert _recent_web_runs("bv-scribe") == []


def test_recent_web_runs_excludes_entries_without_params(tmp_path, monkeypatch):
    # A bv-web entry recorded before HistoryEntry.params existed (or
    # a real one that somehow captured no fields) has params=None -
    # nothing to reuse from, so it must not show up here.
    _record(tmp_path, monkeypatch, params=None)

    assert _recent_web_runs("bv-scribe") == []


def test_recent_web_runs_excludes_other_commands(tmp_path, monkeypatch):
    _record(tmp_path, monkeypatch, command="bv-search", command_line="bv-search kirby")

    assert _recent_web_runs("bv-scribe") == []


def test_recent_web_runs_returns_matches_newest_first(tmp_path, monkeypatch):
    _record(tmp_path, monkeypatch, started_at="2026-08-09T10:00:00+00:00")
    _record(tmp_path, monkeypatch, started_at="2026-08-10T10:00:00+00:00")
    _record(tmp_path, monkeypatch, started_at="2026-08-11T10:00:00+00:00")

    runs = _recent_web_runs("bv-scribe")

    assert [n.entry.started_at for n in runs] == [
        "2026-08-11T10:00:00+00:00",
        "2026-08-10T10:00:00+00:00",
        "2026-08-09T10:00:00+00:00",
    ]
    # Numbering is still the real, absolute oldest-first position -
    # newest entry (recorded 3rd) is entry #3, not #1.
    assert [n.number for n in runs] == [3, 2, 1]


def test_recent_web_runs_skips_non_matching_entries_in_between(tmp_path, monkeypatch):
    _record(tmp_path, monkeypatch, started_at="2026-08-09T10:00:00+00:00")
    _record(
        tmp_path,
        monkeypatch,
        command="bv-search",
        command_line="bv-search kirby",
        started_at="2026-08-10T10:00:00+00:00",
        params={"id": "kirby"},
    )
    _record(tmp_path, monkeypatch, started_at="2026-08-11T10:00:00+00:00")

    runs = _recent_web_runs("bv-scribe")

    assert [n.number for n in runs] == [3, 1]


# ---------------------------------------------------------------------------
# _reuse_defaults()
# ---------------------------------------------------------------------------


def test_reuse_defaults_empty_with_no_recent_runs():
    assert _reuse_defaults([], None) == ({}, None)


def test_reuse_defaults_empty_with_no_recent_runs_even_with_reuse_param():
    assert _reuse_defaults([], "1") == ({}, None)


def test_reuse_defaults_defaults_to_latest_run_when_no_reuse_param(tmp_path, monkeypatch):
    _record(tmp_path, monkeypatch, started_at="2026-08-10T10:00:00+00:00", params={"id": "old"})
    _record(tmp_path, monkeypatch, started_at="2026-08-11T10:00:00+00:00", params={"id": "new"})
    runs = _recent_web_runs("bv-scribe")

    defaults, active_number = _reuse_defaults(runs, None)

    assert defaults == {"id": "new"}
    assert active_number == runs[0].number


def test_reuse_defaults_honors_a_valid_reuse_param(tmp_path, monkeypatch):
    _record(tmp_path, monkeypatch, started_at="2026-08-10T10:00:00+00:00", params={"id": "old"})
    _record(tmp_path, monkeypatch, started_at="2026-08-11T10:00:00+00:00", params={"id": "new"})
    runs = _recent_web_runs("bv-scribe")
    older_number = runs[1].number

    defaults, active_number = _reuse_defaults(runs, str(older_number))

    assert defaults == {"id": "old"}
    assert active_number == older_number


def test_reuse_defaults_falls_back_to_latest_for_unknown_reuse_number(tmp_path, monkeypatch):
    _record(tmp_path, monkeypatch, started_at="2026-08-11T10:00:00+00:00", params={"id": "new"})
    runs = _recent_web_runs("bv-scribe")

    defaults, active_number = _reuse_defaults(runs, "999999")

    assert defaults == {"id": "new"}
    assert active_number == runs[0].number


def test_reuse_defaults_falls_back_to_latest_for_non_numeric_reuse_param(tmp_path, monkeypatch):
    _record(tmp_path, monkeypatch, started_at="2026-08-11T10:00:00+00:00", params={"id": "new"})
    runs = _recent_web_runs("bv-scribe")

    defaults, active_number = _reuse_defaults(runs, "not-a-number")

    assert defaults == {"id": "new"}
    assert active_number == runs[0].number


# ---------------------------------------------------------------------------
# _authorize_job_view() / _sliced_job_output() / _job_camera_id() - shared
# by job_detail() and its /jobs/{job_id}/poll AJAX sibling (task #772-776,
# WORKING_CONTEXT.md). Testing the helpers directly, rather than the two
# routes through a TestClient, matches this file's own established
# approach above (see the module docstring) and this repo's convention of
# not depending on httpx/TestClient anywhere in the suite.
# ---------------------------------------------------------------------------


def _job(command: str = "bv-generate kirby") -> Job:
    return Job(
        id="test-job-id",
        command=command,
        username="christer",
        created_at=datetime.now(timezone.utc),
    )


def _user(role: str) -> User:
    return User(username="christer", password_hash="x", role=role)


def test_authorize_job_view_allows_owner_for_any_command():
    _authorize_job_view(_job("bv-export kirby"), _user("owner"))


def test_authorize_job_view_allows_viewer_for_bv_search():
    _authorize_job_view(_job("bv-search kirby"), _user("viewer"))


def test_authorize_job_view_rejects_viewer_for_other_commands():
    with pytest.raises(HTTPException) as exc_info:
        _authorize_job_view(_job("bv-export kirby"), _user("viewer"))

    assert exc_info.value.status_code == 403


def test_authorize_job_view_rejects_viewer_for_a_command_merely_starting_with_bv_search_prefix_mismatch():
    # "bv-search-ish kirby" must not slip through a naive startswith("bv-search")
    # check without the trailing space _authorize_job_view() actually uses.
    with pytest.raises(HTTPException):
        _authorize_job_view(_job("bv-search-ish kirby"), _user("viewer"))


def test_sliced_job_output_returns_full_output_when_tail_not_requested():
    output = [f"line {i}" for i in range(TAIL_LINE_COUNT + 10)]

    tail_active, displayed, truncated_count = _sliced_job_output(
        JobStatus.RUNNING, output, tail_requested=False
    )

    assert tail_active is False
    assert displayed == output
    assert truncated_count == 0


def test_sliced_job_output_truncates_when_tail_requested_and_running():
    output = [f"line {i}" for i in range(TAIL_LINE_COUNT + 10)]

    tail_active, displayed, truncated_count = _sliced_job_output(
        JobStatus.RUNNING, output, tail_requested=True
    )

    assert tail_active is True
    assert displayed == output[-TAIL_LINE_COUNT:]
    assert truncated_count == 10


def test_sliced_job_output_tail_requested_but_short_output_is_not_truncated():
    output = ["line 1", "line 2"]

    tail_active, displayed, truncated_count = _sliced_job_output(
        JobStatus.RUNNING, output, tail_requested=True
    )

    assert tail_active is True
    assert displayed == output
    assert truncated_count == 0


def test_sliced_job_output_tail_requested_but_finished_job_shows_full_output():
    # Tailing is offered only while running (see the route's own
    # comment) - a finished job's output is cheap to keep around/
    # re-render since no more poll ticks are coming.
    output = [f"line {i}" for i in range(TAIL_LINE_COUNT + 10)]

    tail_active, displayed, truncated_count = _sliced_job_output(
        JobStatus.SUCCEEDED, output, tail_requested=True
    )

    assert tail_active is False
    assert displayed == output
    assert truncated_count == 0


def test_job_camera_id_extracts_id_for_bv_search():
    assert _job_camera_id(_job("bv-search kirby")) == "kirby"


def test_job_camera_id_is_none_for_other_commands():
    assert _job_camera_id(_job("bv-export kirby")) is None
    assert _job_camera_id(_job("bv-generate kirby")) is None


def test_job_camera_id_is_none_when_bv_search_command_has_no_id():
    # Shouldn't happen in practice (start_bv_search() always sets
    # job.command to "bv-search {camera_id}"), but the split() guard
    # should not raise on malformed input.
    assert _job_camera_id(_job("bv-search")) is None
