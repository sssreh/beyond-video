"""
VOD entry.

Copyright (C) 2026 Christer R. (sssreh)

SPDX-License-Identifier: GPL-3.0-or-later
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import PurePosixPath


@dataclass(frozen=True)
class VodEntry:
    """One file in the BlackVue VOD list."""

    timestamp: datetime
    path: PurePosixPath
    fields: dict[str, str]

    @property
    def recording(self) -> str:
        """Return the recording identifier."""

        stem = self.path.stem

        if stem.endswith(("F", "R")):
            stem = stem[:-1]

        return stem

    @property
    def is_video(self) -> bool:
        """Return True if this entry is a video."""

        return self.path.suffix.lower() == ".mp4"

    @property
    def is_front(self) -> bool:
        """Return True if this is a front camera file."""

        return self.path.stem.endswith("F")

    @property
    def is_rear(self) -> bool:
        """Return True if this is a rear camera file."""

        return self.path.stem.endswith("R")
    