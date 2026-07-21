"""
Endpoint.

Copyright (C) 2026 Christer R. (sssreh)

SPDX-License-Identifier: GPL-3.0-or-later
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Endpoint:
    """One way to reach a camera.

    Endpoints are tried in priority order (cheapest and fastest
    first), for example home WiFi before a cellular router fallback.
    """

    name: str
    address: str
