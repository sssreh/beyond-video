"""
Asset domain model.

Copyright (C) 2026 Christer R. (sssreh)

SPDX-License-Identifier: GPL-3.0-or-later
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import PurePosixPath


@dataclass(slots=True)
class Asset:
    """One physical file stored on a dashcam."""

    timestamp: datetime
    path: PurePosixPath
    size: int