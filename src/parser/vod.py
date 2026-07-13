"""
BlackVue VOD parser.

Copyright (C) 2026 Christer R. (sssreh)

SPDX-License-Identifier: GPL-3.0-or-later
"""

from __future__ import annotations

from datetime import datetime

from ..domain.asset import Asset
from ..domain.recording import Recording


def parse_fields(line: str) -> dict[str, str]:
    """Parse a comma-separated list of key:value pairs."""

    fields: dict[str, str] = {}

    for part in line.split(","):
        key, value = part.split(":", 1)
        fields[key] = value

    return fields


def parse_timestamp(stem: str) -> datetime:
    """Parse a timestamp from the start of a BlackVue filename."""

    return datetime.strptime(stem[:15], "%Y%m%d_%H%M%S")


def parse_asset(line: str) -> Asset:
    """Parse one line from a BlackVue VOD response."""

    fields = parse_fields(line)

    raise NotImplementedError


def parse_vod(text: str) -> list[Recording]:
    """Parse a BlackVue VOD response."""

    raise NotImplementedError
