from __future__ import annotations

import pytest

from blackvue.timeparser import parse_from, parse_until


@pytest.mark.parametrize(
    ("prefix", "expected"),
    [
        ("2025", "20250101_000000"),
        ("202506", "20250601_000000"),
        ("20250614", "20250614_000000"),
        ("20250614_08", "20250614_080000"),
        ("20250614_0830", "20250614_083000"),
        ("20250614_083015", "20250614_083015"),
    ],
)
def test_parse_from(prefix: str, expected: str) -> None:
    assert parse_from(prefix) == expected


@pytest.mark.parametrize(
    ("prefix", "expected"),
    [
        ("2025", "20251231_235959"),
        ("202506", "20250630_235959"),
        ("20250614", "20250614_235959"),
        ("20250614_08", "20250614_085959"),
        ("20250614_0830", "20250614_083059"),
        ("20250614_083015", "20250614_083015"),
    ],
)
def test_parse_until(prefix: str, expected: str) -> None:
    assert parse_until(prefix) == expected


def test_parse_until_leap_year() -> None:
    assert parse_until("202402") == "20240229_235959"


def test_parse_until_non_leap_year() -> None:
    assert parse_until("202502") == "20250228_235959"


@pytest.mark.parametrize(
    "prefix",
    [
        "202513",
        "202500",
        "20250230",
        "20250431",
        "20250614_24",
        "20250614_0860",
        "20250614_083060",
        "202506_14",
        "20250614__08",
        "20250614_08_30",
        "2025*",
        "2025*14",
        "",
        "abc",
    ],
)
def test_invalid_prefix(prefix: str) -> None:
    with pytest.raises(ValueError):
        parse_from(prefix)

    with pytest.raises(ValueError):
        parse_until(prefix)
