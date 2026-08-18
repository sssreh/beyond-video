"""
Tests for web/voice_query.py's parse_spoken_query() heuristic parser.

See voice_query.py's own module docstring for why this exists: raw
Whisper transcripts of natural-language distance+place queries (e.g.
Christer's real report, "Vissa alla videos som ar mindre an 1000 meter
ifran varby gard nagon gang.") are not themselves valid bv-search Text
queries - this module recognizes that one shape and routes it into
bv-search's own Place/Radius fields instead. No fixtures needed - the
function under test is pure, so these are plain assert-based tests
(no pytest.fixture()), unlike most of the adapter/archive test suite.

Copyright (C) 2026 Christer R. (sssreh)

SPDX-License-Identifier: GPL-3.0-or-later
"""

from __future__ import annotations

from blackvue.web.voice_query import parse_spoken_query


def test_christers_reported_swedish_sentence_parses_to_place_and_radius():
    # The exact real-world report that prompted this module: dictating
    # this sentence and dropping it straight into Text (the original
    # task #1007-1010 implementation) searched for that literal
    # sentence and found nothing.
    result = parse_spoken_query(
        "Vissa alla videos som är mindre än 1000 meter ifrån vårby gård "
        "någon gång."
    )
    assert result.place == "vårby gård"
    assert result.radius_meters == 1000.0
    # Text must be empty, not the leftover "Vissa alla videos som är" -
    # bv-search ANDs Text and Place/Radius together (cli/bv_search.py's
    # _run()), so leftover command words there would zero out every
    # result even though the place/radius half parsed correctly.
    assert result.text == ""


def test_english_within_meters_of_place():
    result = parse_spoken_query("Show all videos within 500 meters of Slussen.")
    assert result.place == "Slussen"
    assert result.radius_meters == 500.0
    assert result.text == ""


def test_kilometers_are_converted_to_meters():
    result = parse_spoken_query(
        "Find clips less than 2 km from Stockholm central station"
    )
    assert result.place == "Stockholm central station"
    assert result.radius_meters == 2000.0


def test_swedish_decimal_comma_kilometers():
    result = parse_spoken_query("mindre än 1,5 km från Nacka")
    assert result.place == "Nacka"
    assert result.radius_meters == 1500.0


def test_plain_text_query_falls_back_to_untouched_transcript():
    result = parse_spoken_query("roundabout")
    assert result.text == "roundabout"
    assert result.place is None
    assert result.radius_meters is None


def test_trailing_swedish_filler_is_stripped_from_place():
    result = parse_spoken_query("inom 200 meter från Slussen ibland")
    assert result.place == "Slussen"
    assert result.radius_meters == 200.0


def test_empty_transcript_returns_empty_text_and_no_place():
    result = parse_spoken_query("")
    assert result.text == ""
    assert result.place is None
    assert result.radius_meters is None
