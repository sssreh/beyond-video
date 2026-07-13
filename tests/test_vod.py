"""
Tests for the BlackVue VOD parser.

Copyright (C) 2026 Christer R. (sssreh)

SPDX-License-Identifier: GPL-3.0-or-later
"""

from pathlib import PurePosixPath

from src.parser.vod import parse_vod_entries, parse_vod_entry


def test_parse_vod_entry() -> None:
    """Parse one VOD entry from a BlackVue VOD response."""

    entry = parse_vod_entry(
        "n:/Record/20260711_121334_ER.mp4,s:1000000"
    )

    assert entry.path == PurePosixPath(
        "/Record/20260711_121334_ER.mp4"
    )

    assert entry.timestamp.strftime(
        "%Y%m%d_%H%M%S"
    ) == "20260711_121334"

    assert entry.recording == "20260711_121334_E"

    assert entry.fields == {
        "n": "/Record/20260711_121334_ER.mp4",
        "s": "1000000",
    }


def test_parse_vod_entries() -> None:
    """Parse multiple VOD entries from a BlackVue VOD response."""

    text = (
        "n:/Record/20260711_121334_EF.mp4,s:1000000\n"
        "n:/Record/20260711_121334_ER.mp4,s:1000000"
    )

    entries = parse_vod_entries(text)

    assert len(entries) == 2

    assert entries[0].recording == "20260711_121334_E"
    assert entries[1].recording == "20260711_121334_E"