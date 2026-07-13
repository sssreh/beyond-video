"""
Tests for the BlackVue VOD parser.

Copyright (C) 2026 Christer R. (sssreh)

SPDX-License-Identifier: GPL-3.0-or-later
"""

from pathlib import PurePosixPath

from src.parser.vod import parse_vod_entry


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

    assert entry.fields == {
        "n": "/Record/20260711_121334_ER.mp4",
        "s": "1000000",
    }
    