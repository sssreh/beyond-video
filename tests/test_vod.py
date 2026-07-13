"""
Tests for the BlackVue VOD parser.

Copyright (C) 2026 Christer R. (sssreh)

SPDX-License-Identifier: GPL-3.0-or-later
"""

from pathlib import PurePosixPath

from src.parser.vod import parse_asset


def test_parse_asset() -> None:
    """Parse one asset from a BlackVue VOD response."""

    asset = parse_asset(
        "n:/Record/20260711_121334_ER.mp4,s:1000000"
    )

    assert asset.path == PurePosixPath(
        "/Record/20260711_121334_ER.mp4"
    )

    assert asset.timestamp.strftime(
        "%Y%m%d_%H%M%S"
    ) == "20260711_121334"

    assert asset.fields == {
        "n": "/Record/20260711_121334_ER.mp4",
        "s": "1000000",
    }
    