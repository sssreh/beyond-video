"""
Tests for core/history.py - the persistent command-history index (see
that module's own docstring for how it relates to core/joblog.py's raw
output transcript).

Copyright (C) 2026 Christer R. (sssreh)

SPDX-License-Identifier: GPL-3.0-or-later
"""

from __future__ import annotations

import json

from blackvue.core import history
from blackvue.core.history import HistoryEntry


def _entry(**overrides) -> HistoryEntry:
    fields = dict(
        command="bv-ls",
        command_line="bv-ls /data/archive/Kirby --all",
        source="cli",
        username=None,
        started_at="2026-08-11T12:00:00+00:00",
        duration_seconds=1.5,
        status="succeeded",
    )
    fields.update(overrides)
    return HistoryEntry(**fields)


def test_history_path_is_a_sibling_of_the_output_logs(tmp_path, monkeypatch):
    monkeypatch.setenv("BEYOND_VIDEO_LOGS_DIR", str(tmp_path))

    assert history.history_path() == tmp_path / "history.jsonl"


def test_record_appends_a_single_json_line(tmp_path, monkeypatch):
    monkeypatch.setenv("BEYOND_VIDEO_LOGS_DIR", str(tmp_path))

    history.record(_entry())

    text = history.history_path().read_text()
    lines = text.strip().split("\n")
    assert len(lines) == 1
    parsed = json.loads(lines[0])
    assert parsed["command"] == "bv-ls"
    assert parsed["status"] == "succeeded"


def test_record_appends_multiple_entries_in_order(tmp_path, monkeypatch):
    monkeypatch.setenv("BEYOND_VIDEO_LOGS_DIR", str(tmp_path))

    history.record(_entry(command="bv-ls"))
    history.record(_entry(command="bv-gps"))
    history.record(_entry(command="bv-scribe"))

    lines = history.history_path().read_text().strip().split("\n")
    commands = [json.loads(line)["command"] for line in lines]
    assert commands == ["bv-ls", "bv-gps", "bv-scribe"]


def test_record_creates_the_logs_directory_if_missing(tmp_path, monkeypatch):
    target = tmp_path / "nested" / "logs"
    monkeypatch.setenv("BEYOND_VIDEO_LOGS_DIR", str(target))

    history.record(_entry())

    assert target.exists()
    assert (target / "history.jsonl").exists()


def test_record_never_raises_even_if_writing_fails(monkeypatch):
    def boom():
        raise OSError("disk full")

    monkeypatch.setattr(history, "history_path", boom)

    # Should not raise - see record()'s own docstring.
    history.record(_entry())


def test_record_captures_username_for_bv_web_source(tmp_path, monkeypatch):
    monkeypatch.setenv("BEYOND_VIDEO_LOGS_DIR", str(tmp_path))

    history.record(
        _entry(source="bv-web", username="christer", command="bv-export")
    )

    parsed = json.loads(history.history_path().read_text().strip())
    assert parsed["source"] == "bv-web"
    assert parsed["username"] == "christer"


def test_quote_for_display_leaves_plain_values_alone():
    assert history.quote_for_display("Kirby") == "Kirby"
    assert history.quote_for_display("20260101_000000") == "20260101_000000"


def test_quote_for_display_quotes_values_with_whitespace():
    assert history.quote_for_display("Slussen, Stockholm") == '"Slussen, Stockholm"'


def test_quote_for_display_quotes_the_empty_string():
    assert history.quote_for_display("") == '""'


def test_quote_for_display_escapes_embedded_double_quotes():
    assert history.quote_for_display('say "hi"') == '"say \\"hi\\""'


def test_command_line_from_argv_joins_prog_and_args():
    result = history.command_line_from_argv(
        "bv-search", ["--place", "Slussen, Stockholm", "--radius", "50"]
    )
    assert result == 'bv-search --place "Slussen, Stockholm" --radius 50'


def test_command_line_from_argv_with_no_args():
    assert history.command_line_from_argv("bv-ls", []) == "bv-ls"
