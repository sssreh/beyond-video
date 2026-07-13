"""
VOD entry domain model.

Copyright (C) 2026 Christer R. (sssreh)

SPDX-License-Identifier: GPL-3.0-or-later
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import PurePosixPath


@dataclass(slots=True)
class VodEntry:
    """One entry from a BlackVue VOD response."""

    timestamp: datetime
    path: PurePosixPath
    fields: dict[str, str]

    @property
    def recording(self) -> str:
        """Return the recording identifier."""

        stem = self.path.stem

        return stem[:-1]
    