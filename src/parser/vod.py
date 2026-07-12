"""
BlackVue VOD parser.

Copyright (C) 2026 Christer R. (sssreh)

SPDX-License-Identifier: GPL-3.0-or-later
"""

from __future__ import annotations

from ..domain.asset import Asset
from ..domain.recording import Recording


def parse_asset(line: str) -> Asset:
    """Parse one line from a BlackVue VOD response."""

    raise NotImplementedError


def parse_vod(text: str) -> list[Recording]:
    """Parse a BlackVue VOD response."""

    raise NotImplementedError