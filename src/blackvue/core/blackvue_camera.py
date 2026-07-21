"""
BlackVue camera.

Copyright (C) 2026 Christer R. (sssreh)

SPDX-License-Identifier: GPL-3.0-or-later
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from .blackvue_client import BlackVueClient
from ..domain.recording import Recording
from ..domain.vod_entry import VodEntry
from ..parser.vod import parse_vod


class BlackVueCamera:
    """BlackVue camera."""

    def __init__(self, client: BlackVueClient) -> None:
        """Initialize a BlackVue camera."""

        self._client = client

    def recordings(self) -> list[Recording]:
        """Return the camera recordings."""

        return parse_vod(self._client.vod())

    def download(
        self,
        recording: Recording,
        destination: Path,
        *,
        select: Callable[[VodEntry], bool] | None = None,
        on_bytes: Callable[[int], None] | None = None,
    ) -> bool:
        """Download a recording.

        If select is given, only entries for which it returns True are
        downloaded (e.g. ``lambda entry: entry.is_video``). By default
        every entry is downloaded.

        If on_bytes is given, it's passed straight through to
        BlackVueClient.download() for every entry - see its docstring.

        Returns True if any file was downloaded or resumed.
        """

        destination.mkdir(
            parents=True,
            exist_ok=True,
        )

        changed = False

        for entry in recording.entries:
            if select is not None and not select(entry):
                continue

            filename = destination / entry.path.name

            if self._client.download(
                entry,
                filename,
                on_bytes=on_bytes,
            ):
                changed = True

        return changed
    