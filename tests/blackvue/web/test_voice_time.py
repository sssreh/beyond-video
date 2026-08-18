"""
Tests for web/voice_time.py's parse_spoken_timerange() heuristic
parser.

See voice_time.py's own module docstring: this extracts spoken date/
date-range references ("yesterday", "last week", "from July 15th to
July 20th", ...) into bv-search's Timestamp (single day) or From/Until
(range) fields. A fixed reference date (2026-08-18, a Tuesday) is used
throughout instead of date.today() so the expected outputs are stable.
No fixtures needed - parse_spoken_timerange() is pure.

Copyright (C) 2026 Christer R. (sssreh)

SPDX-License-Identifier: GPL-3.0-or-later
"""

from __future__ import annotations

from datetime import date

from blackvue.web.voice_time import parse_spoken_timerange

TODAY = date(2026, 8, 18)  # a Tuesday


def test_yesterday_resolves_to_a_single_timestamp():
    result = parse_spoken_timerange("Show all videos from yesterday", TODAY)
    assert result.matched is True
    assert result.timestamp == "20260817"
    assert result.from_ is None
    assert result.until is None


def test_today_and_tomorrow():
    assert parse_spoken_timerange("today", TODAY).timestamp == "20260818"
    assert parse_spoken_timerange("tomorrow", TODAY).timestamp == "20260819"


def test_swedish_yesterday_with_and_without_diacritics():
    assert parse_spoken_timerange("igar", TODAY).timestamp == "20260817"
    assert parse_spoken_timerange("igår", TODAY).timestamp == "20260817"


def test_last_weekday_english():
    # 2026-08-18 is a Tuesday; "last Tuesday" must not be today - it
    # should go back a full week, not zero days.
    result = parse_spoken_timerange("videos from last Tuesday", TODAY)
    assert result.timestamp == "20260811"


def test_swedish_i_weekday_s_idiom():
    result = parse_spoken_timerange("i tisdags", TODAY)
    assert result.timestamp == "20260811"


def test_this_week_and_last_week_are_monday_to_sunday():
    this_week = parse_spoken_timerange("clips from this week", TODAY)
    assert this_week.from_ == "20260817"  # Monday of this week
    assert this_week.until == "20260823"  # Sunday of this week

    last_week = parse_spoken_timerange("clips from last week", TODAY)
    assert last_week.from_ == "20260810"
    assert last_week.until == "20260816"


def test_swedish_this_month_and_last_month():
    this_month = parse_spoken_timerange("denna manad", TODAY)
    assert this_month.from_ == "20260801"
    assert this_month.until == "20260831"

    last_month = parse_spoken_timerange("forra manaden", TODAY)
    assert last_month.from_ == "20260701"
    assert last_month.until == "20260731"


def test_explicit_date_english_and_swedish_orders():
    en = parse_spoken_timerange("clips from July 15th, 2026", TODAY)
    assert en.timestamp == "20260715"

    sv = parse_spoken_timerange("15 juli 2026", TODAY)
    assert sv.timestamp == "20260715"


def test_iso_date():
    result = parse_spoken_timerange("clips from 2026-07-15", TODAY)
    assert result.timestamp == "20260715"


def test_year_omitted_defaults_to_current_year():
    result = parse_spoken_timerange("15 juli", TODAY)
    assert result.timestamp == "20260715"


def test_explicit_range_from_to():
    result = parse_spoken_timerange(
        "show clips from July 15th to July 20th", TODAY
    )
    assert result.timestamp is None
    assert result.from_ == "20260715"
    assert result.until == "20260720"


def test_explicit_range_shares_year_stated_on_either_side():
    # A spoken year is normally said once and applies to the whole
    # range - regardless of which side states it.
    year_on_end = parse_spoken_timerange(
        "between July 15 and July 20 2025", TODAY
    )
    assert year_on_end.from_ == "20250715"
    assert year_on_end.until == "20250720"

    year_on_start = parse_spoken_timerange(
        "from July 15 2025 to July 20", TODAY
    )
    assert year_on_start.from_ == "20250715"
    assert year_on_start.until == "20250720"


def test_swedish_range_connector():
    result = parse_spoken_timerange(
        "fran 15 juli till 20 juli 2026", TODAY
    )
    assert result.from_ == "20260715"
    assert result.until == "20260720"


def test_plain_text_does_not_match():
    result = parse_spoken_timerange("roundabout", TODAY)
    assert result.matched is False
    assert result.timestamp is None
    assert result.from_ is None
    assert result.until is None
    assert result.remainder == "roundabout"


def test_stray_to_elsewhere_in_sentence_does_not_false_positive():
    # "within 500 meters of Slussen going to work" contains "to" but
    # is not a date range - both sides of any "X to Y" match here fail
    # to resolve as dates, so the connector attempt must be rejected.
    result = parse_spoken_timerange(
        "within 500 meters of Slussen going to work", TODAY
    )
    assert result.matched is False


def test_empty_transcript():
    result = parse_spoken_timerange("", TODAY)
    assert result.matched is False
    assert result.remainder == ""
