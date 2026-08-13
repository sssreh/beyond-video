"""
Tests for cli/bv_lock.py.

Copyright (C) 2026 Christer R. (sssreh)

SPDX-License-Identifier: GPL-3.0-or-later
"""

from __future__ import annotations

import pytest

from blackvue.cli import bv_lock
from blackvue.core.lock import load_lock_manifest


# ---------------------------------------------------------------------------
# parse_args
# ---------------------------------------------------------------------------


def test_parse_args_defaults_path_to_current_directory():
    args = bv_lock.parse_args(["--list"])

    assert args.path == "."


def test_parse_args_requires_exactly_one_mode():
    with pytest.raises(SystemExit):
        bv_lock.parse_args(["archive"])


def test_parse_args_rejects_lock_and_unlock_together():
    with pytest.raises(SystemExit):
        bv_lock.parse_args(
            [
                "archive",
                "--timestamp",
                "2019",
                "--lock-assets",
                "transcribe",
                "--unlock-assets",
                "transcribe",
            ]
        )


def test_parse_args_splits_comma_separated_asset_names():
    args = bv_lock.parse_args(
        [
            "archive",
            "--timestamp",
            "2019",
            "--lock-assets",
            "transcribe,get-duration, describe-scene",
        ]
    )

    assert args.lock_assets == ["transcribe", "get-duration", "describe-scene"]


def test_parse_args_rejects_an_unknown_asset_name():
    with pytest.raises(SystemExit):
        bv_lock.parse_args(
            ["archive", "--timestamp", "2019", "--lock-assets", "bogus"]
        )


def test_parse_args_all_expands_to_every_lockable_asset():
    from blackvue.core.lock import LOCKABLE_ASSETS

    args = bv_lock.parse_args(
        ["archive", "--timestamp", "2019", "--lock-assets", "all"]
    )

    assert args.lock_assets == sorted(LOCKABLE_ASSETS)


def test_parse_args_all_wins_even_alongside_other_names():
    from blackvue.core.lock import LOCKABLE_ASSETS

    args = bv_lock.parse_args(
        [
            "archive",
            "--timestamp",
            "2019",
            "--lock-assets",
            "transcribe,all",
        ]
    )

    assert args.lock_assets == sorted(LOCKABLE_ASSETS)


def test_parse_args_unlock_assets_also_accepts_all():
    from blackvue.core.lock import LOCKABLE_ASSETS

    args = bv_lock.parse_args(
        ["archive", "--timestamp", "2019", "--unlock-assets", "all"]
    )

    assert args.unlock_assets == sorted(LOCKABLE_ASSETS)


# ---------------------------------------------------------------------------
# _run
# ---------------------------------------------------------------------------


def test_run_lock_all_locks_every_lockable_asset(tmp_path):
    from blackvue.core.lock import LOCKABLE_ASSETS

    args = bv_lock.parse_args(
        [str(tmp_path), "--timestamp", "2019", "--lock-assets", "all"]
    )

    exit_code = bv_lock._run(args)

    assert exit_code == bv_lock.EXIT_OK
    manifest = load_lock_manifest(tmp_path)
    assert manifest.entries[0].assets == LOCKABLE_ASSETS


def test_run_unlock_all_clears_every_lockable_asset(tmp_path):
    lock_args = bv_lock.parse_args(
        [str(tmp_path), "--timestamp", "2019", "--lock-assets", "all"]
    )
    bv_lock._run(lock_args)

    unlock_args = bv_lock.parse_args(
        [str(tmp_path), "--timestamp", "2019", "--unlock-assets", "all"]
    )
    exit_code = bv_lock._run(unlock_args)

    assert exit_code == bv_lock.EXIT_OK
    manifest = load_lock_manifest(tmp_path)
    assert manifest.entries == []


def test_run_lock_writes_a_manifest_entry(tmp_path):
    args = bv_lock.parse_args(
        [str(tmp_path), "--timestamp", "2019", "--lock-assets", "get-duration"]
    )
    messages = []

    exit_code = bv_lock._run(args, say=messages.append)

    assert exit_code == bv_lock.EXIT_OK
    manifest = load_lock_manifest(tmp_path)
    assert len(manifest.entries) == 1
    assert manifest.entries[0].assets == {"get-duration"}
    assert any("locked" in m for m in messages)


def test_run_unlock_removes_the_asset(tmp_path):
    lock_args = bv_lock.parse_args(
        [
            str(tmp_path),
            "--timestamp",
            "2019",
            "--lock-assets",
            "get-duration,transcribe",
        ]
    )
    bv_lock._run(lock_args)

    unlock_args = bv_lock.parse_args(
        [str(tmp_path), "--timestamp", "2019", "--unlock-assets", "transcribe"]
    )
    exit_code = bv_lock._run(unlock_args)

    assert exit_code == bv_lock.EXIT_OK
    manifest = load_lock_manifest(tmp_path)
    assert manifest.entries[0].assets == {"get-duration"}


def test_run_list_reports_no_locks_for_a_fresh_archive(tmp_path):
    args = bv_lock.parse_args([str(tmp_path), "--list"])
    messages = []

    exit_code = bv_lock._run(args, say=messages.append)

    assert exit_code == bv_lock.EXIT_OK
    assert any("no locks" in m for m in messages)


def test_run_list_shows_an_existing_lock(tmp_path):
    lock_args = bv_lock.parse_args(
        [str(tmp_path), "--timestamp", "2019", "--lock-assets", "get-duration"]
    )
    bv_lock._run(lock_args)

    list_args = bv_lock.parse_args([str(tmp_path), "--list"])
    messages = []
    bv_lock._run(list_args, say=messages.append)

    assert any("get-duration" in m for m in messages)


def test_run_rejects_from_and_timestamp_together(tmp_path):
    args = bv_lock.parse_args(
        [
            str(tmp_path),
            "--from",
            "20190101",
            "--timestamp",
            "20190201",
            "--lock-assets",
            "get-duration",
        ]
    )
    warnings = []

    exit_code = bv_lock._run(args, warn=warnings.append)

    assert exit_code == bv_lock.EXIT_ARGS_ERROR
    assert warnings


def test_run_reports_a_clean_error_for_a_corrupt_manifest(tmp_path):
    (tmp_path / ".bv-lock.json").write_text("not json{{{")

    args = bv_lock.parse_args(
        [str(tmp_path), "--timestamp", "2019", "--lock-assets", "get-duration"]
    )
    warnings = []

    exit_code = bv_lock._run(args, warn=warnings.append)

    assert exit_code == bv_lock.EXIT_ARGS_ERROR
    assert any("bv-lock:" in w for w in warnings)


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------


def test_main_lock_then_list_end_to_end(tmp_path, capsys):
    exit_code = bv_lock.main(
        [str(tmp_path), "--timestamp", "2019", "--lock-assets", "get-duration"]
    )
    assert exit_code == 0
    capsys.readouterr()

    exit_code = bv_lock.main([str(tmp_path), "--list"])
    out = capsys.readouterr().out

    assert exit_code == 0
    assert "get-duration" in out
