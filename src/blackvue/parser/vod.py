"""
BlackVue VOD parser.

Copyright (C) 2026 Christer R. (sssreh)

SPDX-License-Identifier: GPL-3.0-or-later
"""

from __future__ import annotations

from datetime import datetime
from pathlib import PurePosixPath

from ..domain.recording import Recording
from ..domain.vod_entry import VodEntry


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


def parse_vod_entry(line: str) -> VodEntry:
    """Parse one line from a BlackVue VOD response."""

    fields = parse_fields(line)

    path = PurePosixPath(fields["n"])
    timestamp = parse_timestamp(path.name)

    return VodEntry(
        timestamp=timestamp,
        path=path,
        fields=fields,
    )


def parse_vod_entries(text: str) -> list[VodEntry]:
    """Parse a BlackVue VOD response into VOD entries."""

    entries: list[VodEntry] = []

    for line in text.splitlines():
        if not line or line.startswith("v:"):
            continue

        entries.append(parse_vod_entry(line))

    return entries


def parse_vod(text: str) -> list[Recording]:
    """Parse a BlackVue VOD response."""

    recordings: dict[str, Recording] = {}

    for entry in parse_vod_entries(text):
        recording = recordings.setdefault(
            entry.recording,
            Recording(
                id=entry.recording,
                entries=[],
            ),
        )

        recording.entries.append(entry)

    return sorted(
        recordings.values(),
        key=lambda recording: recording.id,
        reverse=True,
    )
