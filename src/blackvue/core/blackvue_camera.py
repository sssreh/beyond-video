"""
BlackVue camera.

Copyright (C) 2026 Christer R. (sssreh)

SPDX-License-Identifier: GPL-3.0-or-later
"""

from __future__ import annotations

from pathlib import Path

from .blackvue_client import BlackVueClient
from ..domain.recording import Recording
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
    ) -> bool:
        """Download a recording.

        Returns True if any file was downloaded or resumed.
        """

        destination.mkdir(
            parents=True,
            exist_ok=True,
        )

        changed = False

        for entry in recording.entries:
            filename = destination / entry.path.name

            if self._client.download(
                entry,
                filename,
            ):
                changed = True

        return changed
    