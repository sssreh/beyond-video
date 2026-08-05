"""
Tests for LexicalTimeParser/TimeInterval.

Copyright (C) 2026 Christer R. (sssreh)

SPDX-License-Identifier: GPL-3.0-or-later

Replaces the old tests/test_timeparser.py, which tested a
parse_from()/parse_until() API and calendar-aware validation
(rejecting month 13, Feb 30th, leap years, ...) that was never
actually built - see docs/design/time-parser.md for the real design.
The shipped LexicalTimeParser is purely lexical: it pads a digit
string to 14 characters and never interprets it as a real date, so
there is no calendar math and no calendar validation anywhere in it.
"""

from __future__ import annotations

import pytest

from blackvue.lexicaltimeparser import LexicalTimeParser
from blackvue.lexicaltimeparser import TimeInterval


@pytest.mark.parametrize(
    ("prefix", "expected"),
    [
        ("2025", "20250000_000000"),
        ("202506", "20250600_000000"),
        ("20250614", "20250614_000000"),
        ("20250614_08", "20250614_080000"),
        ("20250614_0830", "20250614_083000"),
        ("20250614_083015", "20250614_083015"),
        # Half-length prefixes pad mid-field rather than at a field
        # boundary - still deterministic, just not calendar-shaped.
        ("2026071", "20260710_000000"),
        ("20260", "20260000_000000"),
    ],
)
def test_from_pads_with_zeros(prefix: str, expected: str) -> None:
    interval = LexicalTimeParser(from_=prefix).parse()
    assert interval.first == expected


@pytest.mark.parametrize(
    ("prefix", "expected"),
    [
        ("2025", "20259999_999999"),
        ("202506", "20250699_999999"),
        ("20250614", "20250614_999999"),
        ("20250614_08", "20250614_089999"),
        ("20250614_0830", "20250614_083099"),
        ("20250614_083015", "20250614_083015"),
        ("2026071", "20260719_999999"),
        ("20260", "20260999_999999"),
    ],
)
def test_until_pads_with_nines(prefix: str, expected: str) -> None:
    interval = LexicalTimeParser(until=prefix).parse()
    assert interval.last == expected


def test_no_calendar_validation() -> None:
    """Month 13, day 99, hour 25 - none of this is checked. The
    parser only cares that the input is digits (plus one optional
    underscore at index 8), never whether it names a real date."""

    assert LexicalTimeParser(from_="202513").parse().first == (
        "20251300_000000"
    )
    assert LexicalTimeParser(until="20250230").parse().last == (
        "20250230_999999"
    )
    assert LexicalTimeParser(until="20250614_25").parse().last == (
        "20250614_259999"
    )


@pytest.mark.parametrize(
    "prefix",
    [
        "",
        "abc",
        "2025*",
        "202506_14_08",  # more than one underscore
        "2025_0614",  # underscore not after 8 digits
        "202506140830156",  # 15 digits, too long
    ],
)
def test_invalid_prefix_raises(prefix: str) -> None:
    with pytest.raises(ValueError):
        LexicalTimeParser(from_=prefix).parse()

    with pytest.raises(ValueError):
        LexicalTimeParser(until=prefix).parse()


def test_timestamp_defaults_first_and_last_to_the_same_prefix() -> None:
    interval = LexicalTimeParser(timestamp="20250614").parse()

    assert interval.first == "20250614_000000"
    assert interval.last == "20250614_999999"


def test_timestamp_cannot_combine_with_from_or_until() -> None:
    with pytest.raises(ValueError):
        LexicalTimeParser(timestamp="2025", from_="2025").parse()

    with pytest.raises(ValueError):
        LexicalTimeParser(timestamp="2025", until="2025").parse()


def test_empty_string_from_and_until_count_as_combined_not_unset() -> None:
    # Documents a real trap, not just a hypothetical one: this class
    # checks `is not None`, not truthiness, to detect a --timestamp/
    # --from(/--until) conflict - so an empty string "" (not None)
    # for from_/until is treated the same as a real value, i.e. as
    # "the caller set this too". A CLI caller never hits this because
    # argparse leaves an unset flag as a genuine None. bv-web's
    # archive-browser filter form did hit this though: an HTML GET
    # form submits every named field, even ones left blank, as
    # `name=` (empty string) rather than omitting it - so leaving
    # "From"/"Until" untouched while filling in "Exact" still sent
    # from_="" and until="" to this class, which read that as a
    # conflict and raised even though the user only meant to filter
    # by "Exact". app.py's archive_recording_list() route now
    # normalizes "" to None before constructing LexicalTimeParser for
    # exactly this reason - this test exists so nobody "simplifies"
    # that normalization away without re-introducing the bug.
    with pytest.raises(ValueError):
        LexicalTimeParser(timestamp="20260715", from_="", until="").parse()


def test_unset_from_and_until_default_to_the_full_range() -> None:
    interval = LexicalTimeParser().parse()

    assert interval.first == "00000000_000000"
    assert interval.last == "99999999_999999"


def test_from_alone_leaves_until_at_the_open_upper_bound() -> None:
    interval = LexicalTimeParser(from_="20250614").parse()

    assert interval.first == "20250614_000000"
    assert interval.last == "99999999_999999"


def test_until_alone_leaves_from_at_the_open_lower_bound() -> None:
    interval = LexicalTimeParser(until="20250614").parse()

    assert interval.first == "00000000_000000"
    assert interval.last == "20250614_999999"


def test_contains_checks_an_inclusive_lexical_range() -> None:
    # __contains__ is only ever called with real recording-id-shaped
    # strings ("YYYYMMDD_HHMMSS_<kind>", e.g. "..._N"/"..._E") - see
    # RecordingId usage in bv_download.py's `recording.id in interval`.
    # Its rsplit("_", 1) strips exactly that trailing kind suffix, so
    # every case here needs one to compare correctly (a bare
    # "YYYYMMDD_HHMMSS" with no kind suffix would have its time
    # portion stripped too, since that's also just "everything after
    # the last underscore" - not a shape this method is ever actually
    # called with).
    interval = TimeInterval(
        first="20250614_000000",
        last="20250614_999999",
    )

    assert "20250614_120000_N" in interval
    assert "20250614_000000_N" in interval  # inclusive lower bound
    assert "20250614_235959_N" in interval
    assert "20250613_235959_N" not in interval
    assert "20250615_000000_N" not in interval


def test_contains_strips_only_the_trailing_kind_suffix() -> None:
    interval = TimeInterval(
        first="20250614_000000",
        last="20250614_999999",
    )

    assert "20250614_120000_E" in interval
    assert "20250615_120000_E" not in interval
