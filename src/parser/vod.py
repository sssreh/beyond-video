"""
BlackVue VOD parser.

Copyright (C) 2026 Christer R. (sssreh)

SPDX-License-Identifier: GPL-3.0-or-later
"""

from __future__ import annotations

from datetime import datetime

from ..domain.asset import Asset
from ..domain.recording import Recording


def parse_timestamp(stem: str) -> datetime:
    """Parse a timestamp from the start of a BlackVue filename."""

    return datetime.strptime(stem[:15], "%Y%m%d_%H%M%S")


def parse_asset(line: str) -> Asset:
    """Parse one line from a BlackVue VOD response."""

    filename_part, size_part = line.split(",")
    _, path = filename_part.split(":", 1)
    _, size_text = size_part.split(":", 1)

    raise NotImplementedError


def parse_vod(text: str) -> list[Recording]:
    """Parse a BlackVue VOD response."""

    raise NotImplementedError
