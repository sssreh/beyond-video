"""
Tests for core/resume.py - the per-archive "how far did the last
--resume run get" cursor bv-generate writes and reads for itself (see
that module's own docstring for why this is a high-water mark per
exact asset-name combination, not a per-recording completeness check).

Copyright (C) 2026 Christer R. (sssreh)

SPDX-License-Identifier: GPL-3.0-or-later
"""

from __future__ import annotations

import json

import pytest

from blackvue.core.resume import ResumeError
from blackvue.core.resume import ResumeState
from blackvue.core.resume import advance_resume_point
from blackvue.core.resume import load_resume_state
from blackvue.core.resume import resume_point
from blackvue.core.resume import resume_state_path
from blackvue.core.resume import save_resume_state


# ---------------------------------------------------------------------------
# resume_state_path / load_resume_state / save_resume_state
# ---------------------------------------------------------------------------


def test_resume_state_path_is_a_sibling_of_the_recordings(tmp_path):
    assert resume_state_path(tmp_path) == tmp_path / ".bv-generate-resume.json"


def test_load_resume_state_returns_empty_when_no_file_exists(tmp_path):
    state = load_resume_state(tmp_path)

    assert state.entries == []


def test_save_then_load_round_trips_entries(tmp_path):
    state = advance_resume_point(
        ResumeState(), {"extract-audio", "transcribe"}, "20260801_000000"
    )

    save_resume_state(tmp_path, state)
    reloaded = load_resume_state(tmp_path)

    assert len(reloaded.entries) == 1
    assert reloaded.entries[0].assets == {"extract-audio", "transcribe"}
    assert reloaded.entries[0].last_seen == "20260801_000000"


def test_save_resume_state_creates_the_archive_directory_if_missing(tmp_path):
    archive = tmp_path / "not-yet-created"
    state = advance_resume_point(ResumeState(), {"transcribe"}, "20260801_000000")

    save_resume_state(archive, state)

    assert archive.exists()
    assert (archive / ".bv-generate-resume.json").exists()


def test_save_resume_state_writes_readable_json(tmp_path):
    state = advance_resume_point(ResumeState(), {"describe-scene"}, "20260801_000000")

    save_resume_state(tmp_path, state)

    data = json.loads((tmp_path / ".bv-generate-resume.json").read_text())
    assert data["entries"][0]["assets"] == ["describe-scene"]
    assert data["entries"][0]["last_seen"] == "20260801_000000"


def test_load_resume_state_raises_resume_error_for_invalid_json(tmp_path):
    (tmp_path / ".bv-generate-resume.json").write_text("not json{{{")

    with pytest.raises(ResumeError):
        load_resume_state(tmp_path)


def test_load_resume_state_raises_resume_error_for_missing_fields(tmp_path):
    (tmp_path / ".bv-generate-resume.json").write_text(
        json.dumps({"entries": [{"assets": ["transcribe"]}]})
    )

    with pytest.raises(ResumeError):
        load_resume_state(tmp_path)


# ---------------------------------------------------------------------------
# resume_point
# ---------------------------------------------------------------------------


def test_resume_point_returns_none_when_never_run_before():
    assert resume_point(ResumeState(), {"transcribe"}) is None


def test_resume_point_returns_the_cursor_for_an_exact_match():
    state = advance_resume_point(ResumeState(), {"transcribe"}, "20260801_000000")

    assert resume_point(state, {"transcribe"}) == "20260801_000000"


def test_resume_point_ignores_a_different_asset_combination():
    state = advance_resume_point(ResumeState(), {"transcribe"}, "20260801_000000")

    assert resume_point(state, {"transcribe", "describe-scene"}) is None
    assert resume_point(state, {"extract-audio"}) is None


def test_resume_point_treats_asset_order_as_irrelevant():
    state = advance_resume_point(
        ResumeState(), ["transcribe", "extract-audio"], "20260801_000000"
    )

    assert resume_point(state, {"extract-audio", "transcribe"}) == "20260801_000000"


# ---------------------------------------------------------------------------
# advance_resume_point
# ---------------------------------------------------------------------------


def test_advance_resume_point_creates_a_new_entry():
    state = advance_resume_point(ResumeState(), {"transcribe"}, "20260801_000000")

    assert len(state.entries) == 1
    assert state.entries[0].assets == {"transcribe"}
    assert state.entries[0].last_seen == "20260801_000000"


def test_advance_resume_point_replaces_the_existing_cursor_for_the_same_combination():
    state = advance_resume_point(ResumeState(), {"transcribe"}, "20260801_000000")
    state = advance_resume_point(state, {"transcribe"}, "20260815_000000")

    assert len(state.entries) == 1
    assert state.entries[0].last_seen == "20260815_000000"


def test_advance_resume_point_keeps_different_combinations_independent():
    state = advance_resume_point(ResumeState(), {"transcribe"}, "20260801_000000")
    state = advance_resume_point(state, {"describe-scene"}, "20260601_000000")

    assert len(state.entries) == 2
    assert resume_point(state, {"transcribe"}) == "20260801_000000"
    assert resume_point(state, {"describe-scene"}) == "20260601_000000"


def test_advance_resume_point_can_move_the_cursor_backwards():
    # Not something bv_generate.py's own --resume wiring ever does in
    # practice (it always advances to the newest recording just
    # walked), but the function itself is a plain replace, not a
    # max()-guarded one - defensive callers, not this module, own that
    # invariant, same as core/lock.py's own add_lock_assets().
    state = advance_resume_point(ResumeState(), {"transcribe"}, "20260815_000000")
    state = advance_resume_point(state, {"transcribe"}, "20260801_000000")

    assert resume_point(state, {"transcribe"}) == "20260801_000000"
