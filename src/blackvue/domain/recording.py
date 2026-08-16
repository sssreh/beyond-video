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
        """Return the recording kind - the letter after the id's last
        underscore in BlackVue's own YYYYMMDD_HHMMSS_K shape.

        A generic (adapter-driven, e.g. GoPro) SD-card recording's id
        has no such letter at all - see SdCardCamera's own manifest-
        driven scan path (core/sdcard_camera.py), which keeps a
        matched file's own stem as the recording id verbatim, with no
        BlackVue-shaped suffix appended. Returns "" for that case
        rather than raising, so is_normal/is_event/is_manual/
        is_parking/is_a all correctly read False for it (never
        matching any of BlackVue's known kind letters) instead of
        crashing bv-download's own mode-selection (select_by_mode()/
        select_by_context()) for a camera whose manifest declares it
        has no kind distinction at all."""

        if "_" not in self.id:
            return ""

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
    def is_a(self) -> bool:
        """Return True for the "A" recording kind - meaning unknown,
        see RecordingId.kind's docstring in archive/recording_id.py
        for the same note (this class and that one both parse the
        kind letter independently)."""

        return self.kind == "A"

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

    @property
    def interior(self) -> VodEntry | None:
        """Return the interior (cabin-facing) camera entry, if any."""

        for entry in self.entries:
            if entry.is_interior:
                return entry
        return None
    