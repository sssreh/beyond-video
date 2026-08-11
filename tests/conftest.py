"""
Shared pytest fixtures.

Copyright (C) 2026 Christer R. (sssreh)

SPDX-License-Identifier: GPL-3.0-or-later
"""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _isolate_persistent_joblog(tmp_path, monkeypatch):
    """Point core/joblog.py's persistent output logfile (see its own
    module docstring - the "I would also want a logfile of all the
    output" feature) at a per-test tmp_path instead of the real
    ~/beyond-video-data/logs, for every test in the whole suite.

    Without this, any test that exercises a bv-* CLI's main() (see
    each cli/bv_*.py's wrap_say()/wrap_warn() wiring) or bv-web's
    Job.append_output() (web/jobs.py) - and there are many, since
    almost every job-runner test spawns and runs a real job - would
    silently write real log lines onto whatever machine runs the test
    suite. BEYOND_VIDEO_LOGS_DIR is the same override default_logs_dir()
    (core/camera_config.py) already understands, so this needs no extra
    plumbing on the production side.

    joblog.py caches its logger (and the directory its
    MonthlyRotatingFileHandler was opened against) in a module-level
    singleton the first time get_logger() runs - resetting that
    singleton before and after every test is what makes each test's
    isolation actually take effect, instead of every test after the
    first reusing whichever tmp_path got there first.
    """

    from blackvue.core import joblog

    monkeypatch.setenv("BEYOND_VIDEO_LOGS_DIR", str(tmp_path / "logs"))

    joblog._logger = None
    yield
    if joblog._logger is not None:
        for handler in joblog._logger.handlers:
            handler.close()
    joblog._logger = None
