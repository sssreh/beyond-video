"""
Tests for blackvue/history.py - the filtering/numbering library behind
bv-history and bv-web's own /history page.

Copyright (C) 2026 Christer R. (sssreh)

SPDX-License-Identifier: GPL-3.0-or-later
"""

from __future__ import annotations

from datetime import datetime
from datetime import timedelta
from datetime import timezone

from blackvue import history as bv_history
from blackvue.core import history as core_history
from blackvue.core import joblog


def _record(
    command="bv-ls",
    command_line=None,
    source="cli",
    username=None,
    minutes_ago=10,
    duration=1.0,
    status="succeeded",
):
    started_at = (
        datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)
    ).isoformat()
    core_history.record(
        core_history.HistoryEntry(
            command=command,
            command_line=command_line or f"{command} Kirby",
            source=source,
            username=username,
            started_at=started_at,
            duration_seconds=duration,
            status=status,
        )
    )


def test_all_entries_numbers_from_1_oldest_first(tmp_path, monkeypatch):
    monkeypatch.setenv("BEYOND_VIDEO_LOGS_DIR", str(tmp_path))

    _record(command="bv-ls", minutes_ago=30)
    _record(command="bv-gps", minutes_ago=20)
    _record(command="bv-scribe", minutes_ago=10)

    numbered = bv_history.all_entries()

    assert [n.number for n in numbered] == [1, 2, 3]
    assert [n.entry.command for n in numbered] == ["bv-ls", "bv-gps", "bv-scribe"]


def test_filtered_entries_by_command_preserves_original_numbers(tmp_path, monkeypatch):
    monkeypatch.setenv("BEYOND_VIDEO_LOGS_DIR", str(tmp_path))

    _record(command="bv-ls", minutes_ago=30)
    _record(command="bv-gps", minutes_ago=20)
    _record(command="bv-ls", minutes_ago=10)

    matches = bv_history.filtered_entries(
        bv_history.HistoryFilter(command="bv-ls")
    )

    assert [m.number for m in matches] == [1, 3]


def test_filtered_entries_command_match_is_case_insensitive(tmp_path, monkeypatch):
    monkeypatch.setenv("BEYOND_VIDEO_LOGS_DIR", str(tmp_path))

    _record(command="bv-ls")

    matches = bv_history.filtered_entries(bv_history.HistoryFilter(command="BV-LS"))

    assert len(matches) == 1


def test_filtered_entries_by_camera_substring(tmp_path, monkeypatch):
    monkeypatch.setenv("BEYOND_VIDEO_LOGS_DIR", str(tmp_path))

    _record(command="bv-ls", command_line="bv-ls Kirby --all")
    _record(command="bv-ls", command_line="bv-ls Rex --all")

    matches = bv_history.filtered_entries(bv_history.HistoryFilter(camera="kirby"))

    assert len(matches) == 1
    assert "Kirby" in matches[0].entry.command_line


def test_filtered_entries_failed_only_includes_interrupted(tmp_path, monkeypatch):
    monkeypatch.setenv("BEYOND_VIDEO_LOGS_DIR", str(tmp_path))

    _record(command="bv-ls", status="succeeded")
    _record(command="bv-export", status="failed")
    _record(command="bv-scribe", status="interrupted")

    matches = bv_history.filtered_entries(bv_history.HistoryFilter(failed_only=True))

    assert [m.entry.command for m in matches] == ["bv-export", "bv-scribe"]


def test_filtered_entries_by_search_text(tmp_path, monkeypatch):
    monkeypatch.setenv("BEYOND_VIDEO_LOGS_DIR", str(tmp_path))

    _record(command="bv-search", command_line="bv-search --place Slussen")
    _record(command="bv-search", command_line="bv-search --place Vasastan")

    matches = bv_history.filtered_entries(
        bv_history.HistoryFilter(search="slussen")
    )

    assert len(matches) == 1


def test_filtered_entries_by_source(tmp_path, monkeypatch):
    monkeypatch.setenv("BEYOND_VIDEO_LOGS_DIR", str(tmp_path))

    _record(command="bv-ls", source="cli")
    _record(command="bv-ls", source="bv-web", username="christer")

    matches = bv_history.filtered_entries(bv_history.HistoryFilter(source="bv-web"))

    assert len(matches) == 1
    assert matches[0].entry.username == "christer"


def test_filtered_entries_by_since_until(tmp_path, monkeypatch):
    monkeypatch.setenv("BEYOND_VIDEO_LOGS_DIR", str(tmp_path))

    old = datetime.now(timezone.utc) - timedelta(days=400)
    recent = datetime.now(timezone.utc) - timedelta(minutes=5)

    core_history.record(
        core_history.HistoryEntry(
            command="bv-ls", command_line="bv-ls old", source="cli",
            username=None, started_at=old.isoformat(), duration_seconds=1.0,
            status="succeeded",
        )
    )
    core_history.record(
        core_history.HistoryEntry(
            command="bv-ls", command_line="bv-ls recent", source="cli",
            username=None, started_at=recent.isoformat(), duration_seconds=1.0,
            status="succeeded",
        )
    )

    since = (datetime.now(timezone.utc) - timedelta(days=1)).strftime("%Y%m%d")
    matches = bv_history.filtered_entries(bv_history.HistoryFilter(since=since))

    assert len(matches) == 1
    assert matches[0].entry.command_line == "bv-ls recent"


def test_filtered_entries_raises_valueerror_for_bad_timestamp(tmp_path, monkeypatch):
    monkeypatch.setenv("BEYOND_VIDEO_LOGS_DIR", str(tmp_path))

    raised = False
    try:
        bv_history.filtered_entries(bv_history.HistoryFilter(since="not-a-date"))
    except ValueError:
        raised = True

    assert raised is True


def test_tail_returns_last_n_still_oldest_first():
    numbered = [
        bv_history.NumberedEntry(
            number=i,
            entry=core_history.HistoryEntry(
                command="bv-ls", command_line="bv-ls", source="cli",
                username=None, started_at="2026-01-01T00:00:00+00:00",
                duration_seconds=1.0, status="succeeded",
            ),
        )
        for i in range(1, 21)
    ]

    result = bv_history.tail(numbered, 5)

    assert [n.number for n in result] == [16, 17, 18, 19, 20]


def test_tail_returns_everything_when_count_exceeds_length():
    numbered = [
        bv_history.NumberedEntry(
            number=1,
            entry=core_history.HistoryEntry(
                command="bv-ls", command_line="bv-ls", source="cli",
                username=None, started_at="2026-01-01T00:00:00+00:00",
                duration_seconds=1.0, status="succeeded",
            ),
        )
    ]

    assert bv_history.tail(numbered, 10) == numbered


def test_tail_returns_everything_when_count_is_none():
    numbered = [
        bv_history.NumberedEntry(
            number=i,
            entry=core_history.HistoryEntry(
                command="bv-ls", command_line="bv-ls", source="cli",
                username=None, started_at="2026-01-01T00:00:00+00:00",
                duration_seconds=1.0, status="succeeded",
            ),
        )
        for i in range(1, 4)
    ]

    assert bv_history.tail(numbered, None) == numbered


def test_matching_log_lines_finds_output_within_the_run_window(tmp_path, monkeypatch):
    monkeypatch.setenv("BEYOND_VIDEO_LOGS_DIR", str(tmp_path))
    joblog._logger = None

    started = datetime.now(timezone.utc)
    joblog.log_line("bv-scribe", "line one")
    joblog.log_line("bv-scribe", "line two")

    entry = core_history.HistoryEntry(
        command="bv-scribe",
        command_line="bv-scribe Kirby",
        source="cli",
        username=None,
        started_at=started.isoformat(),
        duration_seconds=0.05,
        status="succeeded",
    )

    lines = bv_history.matching_log_lines(entry)

    assert [l.message for l in lines] == ["line one", "line two"]


def test_matching_log_lines_excludes_a_different_sources_output(tmp_path, monkeypatch):
    monkeypatch.setenv("BEYOND_VIDEO_LOGS_DIR", str(tmp_path))
    joblog._logger = None

    started = datetime.now(timezone.utc)
    joblog.log_line("bv-ls", "unrelated output")

    entry = core_history.HistoryEntry(
        command="bv-scribe",
        command_line="bv-scribe Kirby",
        source="cli",
        username=None,
        started_at=started.isoformat(),
        duration_seconds=0.05,
        status="succeeded",
    )

    lines = bv_history.matching_log_lines(entry)

    assert lines == []


def test_matching_log_lines_returns_empty_when_nothing_was_logged(tmp_path, monkeypatch):
    monkeypatch.setenv("BEYOND_VIDEO_LOGS_DIR", str(tmp_path))

    entry = core_history.HistoryEntry(
        command="bv-scribe",
        command_line="bv-scribe Kirby",
        source="cli",
        username=None,
        started_at=datetime.now(timezone.utc).isoformat(),
        duration_seconds=1.0,
        status="succeeded",
    )

    assert bv_history.matching_log_lines(entry) == []
