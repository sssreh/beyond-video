"""
Recording.

Copyright (C) 2026 Christer R. (sssreh)

SPDX-License-Identifier: GPL-3.0-or-later
"""

from __future__ import annotations

from dataclasses import dataclass

from .vod_entry import VodEntry


@dataclass
class Recording:
    """BlackVue recording."""

    id: str
    entries: list[VodEntry]

    @property
    def kind(self) -> str:
        """Return the recording kind."""

        return self.id.rsplit("_", 1)[1]

    @property
    def is_normal(self) -> bool:
        return self.kind == "N"

    @property
    def is_event(self) -> bool:
        return self.kind == "E"

    @property
    def is_manual(self) -> bool:
        return self.kind == "M"

    @property
    def is_parking(self) -> bool:
        return self.kind == "P"

    @property
    def front(self) -> VodEntry | None:
        """Return the front entry."""

        for entry in self.entries:
            if entry.is_front:
                return entry
        return None

    @property
    def rear(self) -> VodEntry | None:
        """Return the rear entry."""

        for entry in self.entries:
            if entry.is_rear:
                return entry
        return None
    