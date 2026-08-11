"""
Tests for core/joblog.py - the persistent output log for bv-* commands
(both direct-CLI wrap_say()/wrap_warn() use and bv-web's Job.append_output()
use, see that module's own docstring).

Copyright (C) 2026 Christer R. (sssreh)

SPDX-License-Identifier: GPL-3.0-or-later
"""

from __future__ import annotations

import gzip
from datetime import datetime
from pathlib import Path

from blackvue.core import joblog
from blackvue.core.joblog import MonthlyRotatingFileHandler
from blackvue.core.joblog import log_line
from blackvue.core.joblog import wrap_say
from blackvue.core.joblog import wrap_warn


# ---------------------------------------------------------------------------
# MonthlyRotatingFileHandler - direct unit tests, no logging module glue.
# ---------------------------------------------------------------------------


def test_handler_writes_a_prefix_year_month_log_file(tmp_path):
    handler = MonthlyRotatingFileHandler(tmp_path, prefix="beyond-video")
    handler.setFormatter(__import__("logging").Formatter("%(message)s"))

    import logging

    record = logging.LogRecord(
        name="test", level=logging.INFO, pathname="", lineno=0,
        msg="hello", args=(), exc_info=None,
    )
    handler.emit(record)
    handler.close()

    month_key = datetime.now().strftime("%Y-%m")
    expected = tmp_path / f"beyond-video-{month_key}.log"
    assert expected.exists()
    assert expected.read_text().strip() == "hello"


def test_handler_creates_its_directory_if_missing(tmp_path):
    target = tmp_path / "nested" / "logs"
    handler = MonthlyRotatingFileHandler(target)
    import logging

    handler.setFormatter(logging.Formatter("%(message)s"))
    handler.emit(
        logging.LogRecord(
            name="t", level=logging.INFO, pathname="", lineno=0,
            msg="x", args=(), exc_info=None,
        )
    )
    handler.close()

    assert target.exists()


def test_handler_rotation_gzips_the_previous_month(tmp_path):
    import logging

    handler = MonthlyRotatingFileHandler(tmp_path, prefix="bv")
    handler.setFormatter(logging.Formatter("%(message)s"))

    def emit_at(when: datetime, message: str) -> None:
        handler._rotate_if_needed = lambda: _rotate_at(handler, when)
        handler.emit(
            logging.LogRecord(
                name="t", level=logging.INFO, pathname="", lineno=0,
                msg=message, args=(), exc_info=None,
            )
        )

    def _rotate_at(handler: MonthlyRotatingFileHandler, when: datetime) -> None:
        month_key = handler._month_key(when)
        if month_key == handler._current_month:
            return
        previous_month = handler._current_month
        if handler._stream is not None:
            handler._stream.close()
            handler._stream = None
        if previous_month is not None:
            handler._gzip_and_remove(handler._path_for(previous_month))
            handler._prune_old_backups()
        handler._open_for(month_key)

    emit_at(datetime(2026, 1, 15), "january line")
    emit_at(datetime(2026, 2, 1), "february line")
    handler.close()

    jan_gz = tmp_path / "bv-2026-01.log.gz"
    feb_log = tmp_path / "bv-2026-02.log"
    assert jan_gz.exists()
    assert not (tmp_path / "bv-2026-01.log").exists()
    assert feb_log.exists()

    with gzip.open(jan_gz, "rt") as fh:
        assert fh.read().strip() == "january line"


def test_handler_prunes_backups_beyond_backup_months(tmp_path):
    handler = MonthlyRotatingFileHandler(tmp_path, prefix="bv", backup_months=2)

    # Fabricate 4 already-gzipped monthly backups directly, oldest-named
    # first - _prune_old_backups() sorts by filename (which sorts
    # chronologically for YYYY-MM), so this doesn't need real rotation.
    for month in ("2025-10", "2025-11", "2025-12", "2026-01"):
        with gzip.open(tmp_path / f"bv-{month}.log.gz", "wt") as fh:
            fh.write("x")

    handler._prune_old_backups()

    remaining = sorted(p.name for p in tmp_path.glob("bv-*.log.gz"))
    assert remaining == ["bv-2025-12.log.gz", "bv-2026-01.log.gz"]


def test_handler_keeps_everything_when_backup_months_is_none(tmp_path):
    handler = MonthlyRotatingFileHandler(tmp_path, prefix="bv", backup_months=None)

    for month in ("2020-01", "2020-02", "2020-03"):
        with gzip.open(tmp_path / f"bv-{month}.log.gz", "wt") as fh:
            fh.write("x")

    handler._prune_old_backups()

    assert len(list(tmp_path.glob("bv-*.log.gz"))) == 3


def test_handler_emit_failure_is_swallowed_not_raised(tmp_path):
    # A directory that can't be created (parent is actually a file, not a
    # dir) makes _rotate_if_needed()'s mkdir raise inside emit() - emit()
    # is expected to route that through handleError() rather than let it
    # escape, same contract as any other logging.Handler.
    blocker = tmp_path / "blocker"
    blocker.write_text("not a directory")
    handler = MonthlyRotatingFileHandler(blocker / "logs")

    import logging

    handler.setFormatter(logging.Formatter("%(message)s"))
    # Should not raise.
    handler.emit(
        logging.LogRecord(
            name="t", level=logging.INFO, pathname="", lineno=0,
            msg="x", args=(), exc_info=None,
        )
    )


# ---------------------------------------------------------------------------
# get_logger() / log_line() - the shared singleton both direct-CLI and
# bv-web code paths write through.
# ---------------------------------------------------------------------------


def test_log_line_writes_a_tagged_line_to_the_configured_logs_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("BEYOND_VIDEO_LOGS_DIR", str(tmp_path))
    joblog._logger = None

    log_line("bv-ls", "42 recordings found")

    month_key = datetime.now().strftime("%Y-%m")
    log_file = tmp_path / f"beyond-video-{month_key}.log"
    assert log_file.exists()
    assert "[bv-ls] 42 recordings found" in log_file.read_text()


def test_log_line_never_raises_even_if_logging_itself_fails(monkeypatch):
    def _boom():
        raise OSError("disk full")

    monkeypatch.setattr(joblog, "get_logger", _boom)

    # Should not raise - see log_line()'s own docstring.
    log_line("bv-ls", "whatever")


def test_get_logger_returns_the_same_instance_on_repeat_calls(tmp_path, monkeypatch):
    monkeypatch.setenv("BEYOND_VIDEO_LOGS_DIR", str(tmp_path))
    joblog._logger = None

    first = joblog.get_logger()
    second = joblog.get_logger()

    assert first is second


# ---------------------------------------------------------------------------
# wrap_say() / wrap_warn() - the direct-CLI main() wiring (see each
# cli/bv_*.py's own main()).
# ---------------------------------------------------------------------------


def test_wrap_say_calls_the_original_say_unchanged(tmp_path, monkeypatch):
    monkeypatch.setenv("BEYOND_VIDEO_LOGS_DIR", str(tmp_path))
    joblog._logger = None

    captured = []
    say = wrap_say("bv-ls", say=captured.append)
    say("Archive: /data/archive/Kirby")

    assert captured == ["Archive: /data/archive/Kirby"]


def test_wrap_say_also_persists_the_line(tmp_path, monkeypatch):
    monkeypatch.setenv("BEYOND_VIDEO_LOGS_DIR", str(tmp_path))
    joblog._logger = None

    say = wrap_say("bv-ls", say=lambda message="": None)
    say("Archive: /data/archive/Kirby")

    month_key = datetime.now().strftime("%Y-%m")
    log_file = tmp_path / f"beyond-video-{month_key}.log"
    assert "[bv-ls] Archive: /data/archive/Kirby" in log_file.read_text()


def test_wrap_warn_calls_the_original_warn_and_persists(tmp_path, monkeypatch):
    monkeypatch.setenv("BEYOND_VIDEO_LOGS_DIR", str(tmp_path))
    joblog._logger = None

    captured = []
    warn = wrap_warn("bv-ls", captured.append)
    warn("bv-ls: something went wrong")

    assert captured == ["bv-ls: something went wrong"]

    month_key = datetime.now().strftime("%Y-%m")
    log_file = tmp_path / f"beyond-video-{month_key}.log"
    assert "[bv-ls] bv-ls: something went wrong" in log_file.read_text()


# ---------------------------------------------------------------------------
# read_lines() - the read side, added for bv-history show <id>
# (blackvue.history.matching_log_lines()).
# ---------------------------------------------------------------------------


def test_read_lines_returns_empty_list_when_logs_dir_is_missing(tmp_path, monkeypatch):
    monkeypatch.setenv("BEYOND_VIDEO_LOGS_DIR", str(tmp_path / "does-not-exist"))

    assert joblog.read_lines() == []


def test_read_lines_parses_written_lines_back(tmp_path, monkeypatch):
    monkeypatch.setenv("BEYOND_VIDEO_LOGS_DIR", str(tmp_path))
    joblog._logger = None

    log_line("bv-ls", "42 recordings found")
    log_line("bv-gps", "connected")

    lines = joblog.read_lines()

    assert [(l.source, l.message) for l in lines] == [
        ("bv-ls", "42 recordings found"),
        ("bv-gps", "connected"),
    ]
    assert all(isinstance(l.timestamp, datetime) for l in lines)


def test_read_lines_filters_by_source(tmp_path, monkeypatch):
    monkeypatch.setenv("BEYOND_VIDEO_LOGS_DIR", str(tmp_path))
    joblog._logger = None

    log_line("bv-ls", "line one")
    log_line("bv-gps", "line two")
    log_line("bv-ls", "line three")

    lines = joblog.read_lines(source="bv-ls")

    assert [l.message for l in lines] == ["line one", "line three"]


def test_read_lines_filters_by_since_and_until(tmp_path, monkeypatch):
    monkeypatch.setenv("BEYOND_VIDEO_LOGS_DIR", str(tmp_path))

    month_key = datetime.now().strftime("%Y-%m")
    log_file = tmp_path / f"beyond-video-{month_key}.log"
    log_file.parent.mkdir(parents=True, exist_ok=True)
    log_file.write_text(
        "2026-01-01 10:00:00,000 [bv-ls] too early\n"
        "2026-01-01 12:00:00,000 [bv-ls] in range\n"
        "2026-01-01 14:00:00,000 [bv-ls] too late\n"
    )

    lines = joblog.read_lines(
        since=datetime(2026, 1, 1, 11, 0, 0),
        until=datetime(2026, 1, 1, 13, 0, 0),
    )

    assert [l.message for l in lines] == ["in range"]


def test_read_lines_treats_unmatched_lines_as_a_continuation(tmp_path, monkeypatch):
    monkeypatch.setenv("BEYOND_VIDEO_LOGS_DIR", str(tmp_path))

    month_key = datetime.now().strftime("%Y-%m")
    log_file = tmp_path / f"beyond-video-{month_key}.log"
    log_file.parent.mkdir(parents=True, exist_ok=True)
    log_file.write_text(
        "2026-01-01 10:00:00,000 [bv-scribe] multi-line message\n"
        "second physical line, no timestamp prefix\n"
    )

    lines = joblog.read_lines()

    assert len(lines) == 1
    assert lines[0].message == (
        "multi-line message\nsecond physical line, no timestamp prefix"
    )


def test_read_lines_reads_a_gzipped_older_month_too(tmp_path, monkeypatch):
    monkeypatch.setenv("BEYOND_VIDEO_LOGS_DIR", str(tmp_path))
    tmp_path.mkdir(parents=True, exist_ok=True)

    with gzip.open(tmp_path / "beyond-video-2025-06.log.gz", "wt") as fh:
        fh.write("2025-06-15 09:00:00,000 [bv-ls] old month line\n")

    lines = joblog.read_lines()

    assert [l.message for l in lines] == ["old month line"]


def test_read_lines_orders_months_chronologically_not_lexically(tmp_path, monkeypatch):
    # "...-9.log" would sort after "...-10.log" lexically if compared
    # as plain filenames without zero-padding - real filenames are
    # always zero-padded (YYYY-MM), so this just confirms ordering
    # follows the parsed month key, not raw glob() order.
    monkeypatch.setenv("BEYOND_VIDEO_LOGS_DIR", str(tmp_path))
    tmp_path.mkdir(parents=True, exist_ok=True)

    (tmp_path / "beyond-video-2026-02.log").write_text(
        "2026-02-01 09:00:00,000 [bv-ls] february\n"
    )
    (tmp_path / "beyond-video-2026-01.log").write_text(
        "2026-01-01 09:00:00,000 [bv-ls] january\n"
    )

    lines = joblog.read_lines()

    assert [l.message for l in lines] == ["january", "february"]
