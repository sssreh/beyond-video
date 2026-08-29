"""
Tests for web/voice_llm.py's pure functions - prompt building and
model-output parsing/validation. See voice_llm.py's own module
docstring: this is an experimental local-LLM structured-extraction
parser run *in parallel* with voice_query.py/voice_time.py's regex
parsers, not a replacement for them. No fixtures needed - the
functions under test are pure, so these are plain assert-based tests
(no pytest.fixture()), matching test_voice_query.py/test_voice_time.py's
own style. The model-loading/generation code
(_generate_via_scene_model()/_generate_via_small_text_model()) is
deliberately untested here - no GPU/transformers/network in this
sandbox, same reasoning test_scene.py already documents for scene.py's
own model-loading path.

Copyright (C) 2026 Christer R. (sssreh)

SPDX-License-Identifier: GPL-3.0-or-later
"""

from __future__ import annotations

from datetime import date

from blackvue.web.voice_llm import VALID_MODEL_CHOICES
from blackvue.web.voice_llm import MODEL_SCENE
from blackvue.web.voice_llm import MODEL_SMALL
from blackvue.web.voice_llm import _build_parsed_result
from blackvue.web.voice_llm import _build_prompt
from blackvue.web.voice_llm import _extract_json_blob
from blackvue.web.voice_llm import _parse_llm_json_response
from blackvue.web.voice_llm import _validate_ymd


# ---------------------------------------------------------------------------
# _build_prompt()
# ---------------------------------------------------------------------------


def test_build_prompt_includes_transcript_and_todays_date():
    prompt = _build_prompt("videos near Slussen", date(2026, 8, 29))
    assert "videos near Slussen" in prompt
    assert "2026-08-29" in prompt
    # Weekday name grounding, per the module docstring's "resolve
    # relative dates against a fact, not a guess" reasoning.
    assert "Saturday" in prompt


def test_build_prompt_names_all_six_expected_keys():
    prompt = _build_prompt("anything", date(2026, 1, 1))
    for key in ("text", "place", "radius_meters", "timestamp", "from_", "until"):
        assert f'"{key}"' in prompt


# ---------------------------------------------------------------------------
# _extract_json_blob() / _parse_llm_json_response()
# ---------------------------------------------------------------------------


def test_extract_json_blob_plain():
    assert _extract_json_blob('{"a": 1}') == '{"a": 1}'


def test_extract_json_blob_strips_code_fence():
    raw = '```json\n{"a": 1}\n```'
    assert _extract_json_blob(raw) == '{"a": 1}'


def test_extract_json_blob_ignores_surrounding_prose():
    raw = 'Sure, here is the JSON:\n{"a": 1}\nHope that helps!'
    assert _extract_json_blob(raw) == '{"a": 1}'


def test_extract_json_blob_returns_none_when_no_object_present():
    assert _extract_json_blob("no json here at all") is None


def test_parse_llm_json_response_valid():
    data = _parse_llm_json_response('{"place": "Slussen", "radius_meters": 500}')
    assert data == {"place": "Slussen", "radius_meters": 500}


def test_parse_llm_json_response_raises_value_error_on_malformed_json():
    try:
        _parse_llm_json_response("{not: valid json,,,}")
    except ValueError:
        pass
    else:
        raise AssertionError("expected ValueError for malformed JSON")


def test_parse_llm_json_response_raises_value_error_when_no_object_found():
    try:
        _parse_llm_json_response("I cannot help with that.")
    except ValueError:
        pass
    else:
        raise AssertionError("expected ValueError when no JSON object is present")


def test_parse_llm_json_response_raises_value_error_for_non_object_json():
    try:
        _parse_llm_json_response("[1, 2, 3]")
    except ValueError:
        pass
    else:
        raise AssertionError("expected ValueError for a JSON array, not object")


# ---------------------------------------------------------------------------
# _validate_ymd()
# ---------------------------------------------------------------------------


def test_validate_ymd_accepts_real_date():
    assert _validate_ymd("20260715") == "20260715"


def test_validate_ymd_rejects_none():
    assert _validate_ymd(None) is None


def test_validate_ymd_rejects_wrong_length():
    assert _validate_ymd("202607") is None


def test_validate_ymd_rejects_non_digit_string():
    assert _validate_ymd("2026071X") is None


def test_validate_ymd_rejects_impossible_calendar_date():
    # February 30th - right shape (8 digits), not a real date.
    assert _validate_ymd("20260230") is None


def test_validate_ymd_rejects_non_string_types():
    assert _validate_ymd(20260715) is None
    assert _validate_ymd(20260715.0) is None


# ---------------------------------------------------------------------------
# _build_parsed_result() - the AND-conflict-avoidance + validation logic
# ---------------------------------------------------------------------------


def test_build_parsed_result_place_and_radius_clear_text():
    raw = (
        '{"text": "roundabout", "place": "Slussen", "radius_meters": 500, '
        '"timestamp": null, "from_": null, "until": null}'
    )
    result = _build_parsed_result(raw, transcript="find roundabout near Slussen")
    assert result.place == "Slussen"
    assert result.radius_meters == 500.0
    # AND-conflict-avoidance rule (mirrors voice_query.py's own): text
    # must be cleared, not left as the model's own "roundabout" value,
    # even though the model didn't null it out like the prompt asked.
    assert result.text == ""


def test_build_parsed_result_single_timestamp_match_clears_text():
    raw = (
        '{"text": null, "place": null, "radius_meters": null, '
        '"timestamp": "20260715", "from_": null, "until": null}'
    )
    result = _build_parsed_result(raw, transcript="videos from July 15th")
    assert result.timestamp == "20260715"
    assert result.from_ is None
    assert result.until is None
    assert result.text == ""


def test_build_parsed_result_date_range_clears_text():
    raw = (
        '{"text": null, "place": null, "radius_meters": null, '
        '"timestamp": null, "from_": "20260701", "until": "20260710"}'
    )
    result = _build_parsed_result(raw, transcript="videos from July 1st to July 10th")
    assert result.from_ == "20260701"
    assert result.until == "20260710"
    assert result.timestamp is None
    assert result.text == ""


def test_build_parsed_result_no_match_falls_back_to_transcript():
    raw = (
        '{"text": null, "place": null, "radius_meters": null, '
        '"timestamp": null, "from_": null, "until": null}'
    )
    result = _build_parsed_result(raw, transcript="show me roundabouts")
    assert result.place is None
    assert result.timestamp is None
    assert result.from_ is None
    assert result.until is None
    # Nothing structured matched - falls back to the literal transcript,
    # matching parse_spoken_query()'s own no-match behavior.
    assert result.text == "show me roundabouts"


def test_build_parsed_result_text_field_used_when_nothing_else_matched():
    raw = (
        '{"text": "roundabout", "place": null, "radius_meters": null, '
        '"timestamp": null, "from_": null, "until": null}'
    )
    result = _build_parsed_result(raw, transcript="search for roundabout")
    assert result.text == "roundabout"


def test_build_parsed_result_partial_range_is_dropped():
    # Only "from_" present, no "until" - not a usable range, mirrors
    # voice_time.py's own all-or-nothing range handling.
    raw = (
        '{"text": null, "place": null, "radius_meters": null, '
        '"timestamp": null, "from_": "20260701", "until": null}'
    )
    result = _build_parsed_result(raw, transcript="videos from July 1st")
    assert result.from_ is None
    assert result.until is None
    # Nothing usable matched, so falls back to the transcript.
    assert result.text == "videos from July 1st"


def test_build_parsed_result_range_wins_over_conflicting_timestamp():
    # Model shouldn't emit both per the prompt, but don't trust that -
    # a full range is strictly more informative if it does.
    raw = (
        '{"text": null, "place": null, "radius_meters": null, '
        '"timestamp": "20260705", "from_": "20260701", "until": "20260710"}'
    )
    result = _build_parsed_result(raw, transcript="videos from July 1st to July 10th")
    assert result.timestamp is None
    assert result.from_ == "20260701"
    assert result.until == "20260710"


def test_build_parsed_result_drops_invalid_calendar_dates():
    raw = (
        '{"text": null, "place": null, "radius_meters": null, '
        '"timestamp": "20260230", "from_": null, "until": null}'
    )
    result = _build_parsed_result(raw, transcript="videos from that day")
    assert result.timestamp is None
    assert result.text == "videos from that day"


def test_build_parsed_result_radius_without_place_is_dropped():
    # radius_meters is only meaningful paired with place - an orphaned
    # radius shouldn't silently reach the form.
    raw = (
        '{"text": null, "place": null, "radius_meters": 500, '
        '"timestamp": null, "from_": null, "until": null}'
    )
    result = _build_parsed_result(raw, transcript="within 500 meters")
    assert result.place is None
    assert result.radius_meters is None


def test_build_parsed_result_blank_place_string_treated_as_none():
    raw = (
        '{"text": null, "place": "   ", "radius_meters": 500, '
        '"timestamp": null, "from_": null, "until": null}'
    )
    result = _build_parsed_result(raw, transcript="anything")
    assert result.place is None
    assert result.radius_meters is None


def test_build_parsed_result_wraps_malformed_json_as_value_error():
    try:
        _build_parsed_result("not json at all, sorry", transcript="anything")
    except ValueError:
        pass
    else:
        raise AssertionError("expected ValueError for malformed model output")


def test_build_parsed_result_tolerates_code_fenced_json_with_prose():
    raw = (
        "Sure! Here's the extracted JSON:\n"
        "```json\n"
        '{"text": null, "place": "Nacka", "radius_meters": 1000, '
        '"timestamp": null, "from_": null, "until": null}\n'
        "```\n"
        "Let me know if you need anything else."
    )
    result = _build_parsed_result(raw, transcript="near Nacka within 1 km")
    assert result.place == "Nacka"
    assert result.radius_meters == 1000.0
    assert result.text == ""


def test_build_parsed_result_ignores_boolean_radius():
    # bool is a subclass of int in Python - guard against True/False
    # slipping through isinstance(x, (int, float)) as a bogus radius.
    raw = (
        '{"text": null, "place": "Slussen", "radius_meters": true, '
        '"timestamp": null, "from_": null, "until": null}'
    )
    result = _build_parsed_result(raw, transcript="near Slussen")
    assert result.radius_meters is None


# ---------------------------------------------------------------------------
# Model-choice constants
# ---------------------------------------------------------------------------


def test_model_choice_constants():
    assert MODEL_SCENE == "scene"
    assert MODEL_SMALL == "small"
    assert VALID_MODEL_CHOICES == (MODEL_SCENE, MODEL_SMALL)
