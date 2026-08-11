"""
Tests for cli/bv_history.py.

Copyright (C) 2026 Christer R. (sssreh)

SPDX-License-Identifier: GPL-3.0-or-later
"""

from __future__ import annotations

from datetime import datetime
from datetime import timedelta
from datetime import timezone

from blackvue.cli import bv_history
from blackvue.cli.bv_history import main
from blackvue.cli.bv_history import parse_list_args
from blackvue.cli.bv_history import parse_show_args
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


def test_parse_list_args_defaults():
    args = parse_list_args([])

    assert args.last == bv_history.DEFAULT_TAIL_COUNT
    assert args.all is False
    assert args.command is None
    assert args.failed_only is False


def test_parse_show_args_requires_an_id():
    args = parse_show_args(["7"])

    assert args.id == 7


def test_main_list_prints_nothing_matching_message_when_empty(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("BEYOND_VIDEO_LOGS_DIR", str(tmp_path))

    exit_code = main([])

    out = capsys.readouterr().out
    assert exit_code == 0
    assert "no matching entries" in out


def test_main_list_shows_entries_oldest_first_numbered(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("BEYOND_VIDEO_LOGS_DIR", str(tmp_path))

    _record(command="bv-ls", minutes_ago=30, command_line="bv-ls Kirby --all")
    _record(command="bv-gps", minutes_ago=20, command_line="bv-gps Kirby")
    _record(command="bv-scribe", minutes_ago=10, command_line="bv-scribe Kirby")

    exit_code = main([])
    out = capsys.readouterr().out
    lines = [l for l in out.strip().split("\n") if l.strip()]

    assert exit_code == 0
    assert len(lines) == 3
    assert lines[0].strip().startswith("1")
    assert "bv-ls Kirby --all" in lines[0]
    assert lines[2].strip().startswith("3")
    assert "bv-scribe Kirby" in lines[2]


def test_main_list_respects_last_default_and_shows_truncation_note(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("BEYOND_VIDEO_LOGS_DIR", str(tmp_path))

    for i in range(bv_history.DEFAULT_TAIL_COUNT + 5):
        _record(command="bv-ls", minutes_ago=100 - i, command_line=f"bv-ls run{i}")

    exit_code = main([])
    out = capsys.readouterr().out

    assert exit_code == 0
    assert "most recent" in out
    # 20 entries recorded (i=0 oldest .. i=19 newest, since minutes_ago
    # decreases as i increases) - only the DEFAULT_TAIL_COUNT (15) most
    # recent should show, i.e. i=5..19, not i=0..4.
    assert "run0\n" not in out
    assert "run4\n" not in out
    assert "run5\n" in out
    assert f"run{bv_history.DEFAULT_TAIL_COUNT + 4}\n" in out


def test_main_list_all_shows_everything(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("BEYOND_VIDEO_LOGS_DIR", str(tmp_path))

    for i in range(bv_history.DEFAULT_TAIL_COUNT + 5):
        _record(command="bv-ls", minutes_ago=100 - i, command_line=f"bv-ls run{i}")

    exit_code = main(["--all"])
    out = capsys.readouterr().out

    assert exit_code == 0
    assert "run0" in out
    assert "most recent" not in out


def test_main_list_filters_by_command(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("BEYOND_VIDEO_LOGS_DIR", str(tmp_path))

    _record(command="bv-ls", command_line="bv-ls Kirby")
    _record(command="bv-export", command_line="bv-export Kirby")

    exit_code = main(["--command", "bv-export"])
    out = capsys.readouterr().out

    assert exit_code == 0
    assert "bv-export Kirby" in out
    assert "bv-ls Kirby" not in out


def test_main_list_failed_only(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("BEYOND_VIDEO_LOGS_DIR", str(tmp_path))

    _record(command="bv-ls", status="succeeded", command_line="bv-ls good")
    _record(command="bv-export", status="failed", command_line="bv-export bad")

    exit_code = main(["--failed-only"])
    out = capsys.readouterr().out

    assert exit_code == 0
    assert "bv-export bad" in out
    assert "bv-ls good" not in out


def test_main_list_reports_bad_timestamp_filter(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("BEYOND_VIDEO_LOGS_DIR", str(tmp_path))

    exit_code = main(["--from", "not-a-real-timestamp!!"])
    err = capsys.readouterr().err

    assert exit_code == bv_history.EXIT_ARGS_ERROR
    assert "bv-history" in err


def test_main_show_reports_unknown_id(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("BEYOND_VIDEO_LOGS_DIR", str(tmp_path))

    exit_code = main(["show", "42"])
    err = capsys.readouterr().err

    assert exit_code == bv_history.EXIT_NOT_FOUND
    assert "42" in err


def test_main_show_dumps_matching_output(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("BEYOND_VIDEO_LOGS_DIR", str(tmp_path))
    joblog._logger = None

    started = datetime.now(timezone.utc)
    joblog.log_line("bv-scribe", "wrote thing.scene.txt")

    core_history.record(
        core_history.HistoryEntry(
            command="bv-scribe",
            command_line="bv-scribe Kirby --task describe_scene",
            source="cli",
            username=None,
            started_at=started.isoformat(),
            duration_seconds=0.05,
            status="succeeded",
        )
    )

    exit_code = main(["show", "1"])
    out = capsys.readouterr().out

    assert exit_code == 0
    assert "bv-scribe Kirby --task describe_scene" in out
    assert "wrote thing.scene.txt" in out


def test_main_show_reports_no_output_found(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("BEYOND_VIDEO_LOGS_DIR", str(tmp_path))

    _record(command="bv-ls")

    exit_code = main(["show", "1"])
    out = capsys.readouterr().out

    assert exit_code == 0
    assert "no logged output found" in out


def test_main_records_its_own_history_entry(tmp_path, monkeypatch):
    # bv-history itself goes through run_cli() like every other bv-*
    # command, so running it is itself a recordable event.
    monkeypatch.setenv("BEYOND_VIDEO_LOGS_DIR", str(tmp_path))

    main([])

    entries = core_history.read_entries()
    assert any(e.command == "bv-history" for e in entries)
