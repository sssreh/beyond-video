"""
Tests for web/voice_asr.py's pure functions - known-place extraction from
past bv-search history and Qwen3-ASR context-bias string building. See
voice_asr.py's own module docstring for the full story (a real failed
search traced back to Whisper mis-transcribing a Swedish place name,
Qwen3-ASR-1.7B replacing Whisper for bv-search's voice-search route
only). No fixtures needed - the functions under test are pure, so these
are plain assert-based tests (no pytest.fixture()), matching
test_voice_llm.py's own style. The model-loading/transcription code
(_get_asr_model()/transcribe_voice_query()) is deliberately untested
here - no GPU/qwen_asr/network in this sandbox, same reasoning
test_scene.py already documents for scene.py's own model-loading path.

Copyright (C) 2026 Christer R. (sssreh)

SPDX-License-Identifier: GPL-3.0-or-later
"""

from __future__ import annotations

from blackvue.web.voice_asr import DEFAULT_ASR_MODEL
from blackvue.web.voice_asr import _build_context
from blackvue.web.voice_asr import known_places_from_config
from blackvue.web.voice_asr import known_places_from_params


# ---------------------------------------------------------------------------
# known_places_from_params()
# ---------------------------------------------------------------------------


def test_known_places_from_params_empty_input():
    assert known_places_from_params([]) == []


def test_known_places_from_params_pulls_place_field_only():
    params_list = [
        {"place": "Vårbygård", "text": "roundabout", "radius": "500"},
    ]
    assert known_places_from_params(params_list) == ["Vårbygård"]


def test_known_places_from_params_skips_missing_or_non_string_place():
    params_list = [
        {"text": "no place key here"},
        {"place": None},
        {"place": 500},
        {"place": "Slussen"},
    ]
    assert known_places_from_params(params_list) == ["Slussen"]


def test_known_places_from_params_skips_blank_place():
    params_list = [
        {"place": ""},
        {"place": "   "},
        {"place": "Nacka"},
    ]
    assert known_places_from_params(params_list) == ["Nacka"]


def test_known_places_from_params_strips_whitespace():
    params_list = [{"place": "  Slussen  "}]
    assert known_places_from_params(params_list) == ["Slussen"]


def test_known_places_from_params_dedupes_case_insensitively():
    params_list = [
        {"place": "Vårby gård"},
        {"place": "vårby gård"},
        {"place": "VÅRBY GÅRD"},
        {"place": "Slussen"},
    ]
    # First-seen spelling wins, later case-variant duplicates dropped -
    # newest-first order (callers pass _recent_web_runs()'s own
    # newest-first sequence) is preserved as-is.
    assert known_places_from_params(params_list) == ["Vårby gård", "Slussen"]


def test_known_places_from_params_preserves_newest_first_order():
    params_list = [
        {"place": "Nacka"},
        {"place": "Slussen"},
        {"place": "Vårbygård"},
    ]
    assert known_places_from_params(params_list) == ["Nacka", "Slussen", "Vårbygård"]


def test_known_places_from_params_respects_limit():
    params_list = [{"place": f"Place{i}"} for i in range(30)]
    result = known_places_from_params(params_list, limit=5)
    assert result == [f"Place{i}" for i in range(5)]


def test_known_places_from_params_default_limit_is_20():
    params_list = [{"place": f"Place{i}"} for i in range(25)]
    result = known_places_from_params(params_list)
    assert len(result) == 20
    assert result == [f"Place{i}" for i in range(20)]


def test_known_places_from_params_ignores_mapping_without_get_failures():
    # A dict missing "place" entirely (not even a None value) is just
    # skipped, not an error.
    params_list = [{}, {"place": "Slussen"}]
    assert known_places_from_params(params_list) == ["Slussen"]


# ---------------------------------------------------------------------------
# known_places_from_config() - the config-file bias source added after
# Christer hit the same "Vårby gård" mis-transcription again even with
# known_places_from_params()'s history-based bias in place (see that
# function's own docstring for the bootstrap gap this closes). Needs
# real file I/O (impure), so unlike the rest of this file these use
# pytest's built-in tmp_path fixture rather than being plain asserts.
# ---------------------------------------------------------------------------


def test_known_places_from_config_missing_file_returns_empty_list(tmp_path):
    assert known_places_from_config(tmp_path) == []


def test_known_places_from_config_reads_one_place_per_line(tmp_path):
    (tmp_path / "known_places.txt").write_text(
        "Vårby gård\nSlussen\nNacka\n", encoding="utf-8"
    )
    assert known_places_from_config(tmp_path) == ["Vårby gård", "Slussen", "Nacka"]


def test_known_places_from_config_skips_blank_and_comment_lines(tmp_path):
    (tmp_path / "known_places.txt").write_text(
        "Vårby gård\n\n# a comment\n   \nSlussen\n", encoding="utf-8"
    )
    assert known_places_from_config(tmp_path) == ["Vårby gård", "Slussen"]


def test_known_places_from_config_strips_whitespace(tmp_path):
    (tmp_path / "known_places.txt").write_text(
        "  Vårby gård  \n", encoding="utf-8"
    )
    assert known_places_from_config(tmp_path) == ["Vårby gård"]


# ---------------------------------------------------------------------------
# _build_context()
# ---------------------------------------------------------------------------


def test_build_context_empty_tuple_returns_empty_string():
    assert _build_context(()) == ""


def test_build_context_empty_list_returns_empty_string():
    assert _build_context([]) == ""


def test_build_context_single_place():
    assert _build_context(["Vårbygård"]) == "May mention these place names: Vårbygård."


def test_build_context_multiple_places_comma_joined():
    result = _build_context(["Slussen", "Nacka", "Vårbygård"])
    assert result == "May mention these place names: Slussen, Nacka, Vårbygård."


# ---------------------------------------------------------------------------
# Model-choice constant
# ---------------------------------------------------------------------------


def test_default_asr_model_constant():
    assert DEFAULT_ASR_MODEL == "Qwen/Qwen3-ASR-1.7B"
