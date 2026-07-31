"""
Live GPS fix.

Copyright (C) 2026 Christer R. (sssreh)

SPDX-License-Identifier: GPL-3.0-or-later
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class LiveGpsFix:
    """One GPS reading pulled live from a camera's blackvue_livedata.cgi."""

    latitude: float
    longitude: float

    @property
    def has_fix(self) -> bool:
        """False for exactly (0.0, 0.0).

        blackvue_livedata.cgi's own JSON has no explicit valid/invalid
        flag on a GPS reading (see WORKING_CONTEXT.md's bv-gps entry) -
        (0.0, 0.0) ["null island", off the coast of west Africa] is
        the only practical signal available that the camera currently
        has no real fix to report, standing in for it here rather than
        letting a caller mistake it for an actual location.
        """

        return self.latitude != 0.0 or self.longitude != 0.0
