"""
Recording domain model.

Copyright (C) 2026 Christer R. (sssreh)

SPDX-License-Identifier: GPL-3.0-or-later
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from .asset import Asset


@dataclass(slots=True)
class Recording:
    """One logical recording consisting of one or more assets."""

    timestamp: datetime
    assets: list[Asset]
