"""
Recording domain model.

Copyright (C) 2026 Christer R. (sssreh)

SPDX-License-Identifier: GPL-3.0-or-later
"""

from __future__ import annotations

from dataclasses import dataclass

from .vod_entry import VodEntry


@dataclass(slots=True)
class Recording:
    """One logical recording consisting of one or more VOD entries."""

    recording: str
    entries: list[VodEntry]
    