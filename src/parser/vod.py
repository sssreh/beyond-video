"""
BlackVue VOD parser.

Copyright (C) 2026 Christer R. (sssreh)

SPDX-License-Identifier: GPL-3.0-or-later
"""

from __future__ import annotations

from ..domain.recording import Recording


def parse_vod(text: str) -> list[Recording]:
    """Parse a BlackVue VOD response."""

    raise NotImplementedError
