"""
BlackVue camera.

Copyright (C) 2026 Christer R. (sssreh)

SPDX-License-Identifier: GPL-3.0-or-later
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from pathlib import PurePosixPath

from .blackvue_client import BlackVueClient
from ..domain.recording import Recording
from ..domain.vod_entry import VodEntry
from ..parser.vod import parse_timestamp
from ..parser.vod import parse_vod

# Metadata sidecar suffixes some camera models don't list in their own
# blackvue_vod.cgi response, even though the files exist and download
# fine via a direct GET at the expected path - confirmed on a real
# BlackVue Elite 10 (see WORKING_CONTEXT.md). blackvue_vod.cgi is
# therefore treated as a hint for these two specific extensions, not
# the sole source of truth - probe_missing_sidecars() fills the gap
# when needed, and is a no-op (zero extra network calls) for every
# camera confirmed so far except the Elite 10, where the listing
# already includes them.
_PROBEABLE_SIDECAR_SUFFIXES = (".gps", ".3gf")


class BlackVueCamera:
    """BlackVue camera."""

    def __init__(self, client: BlackVueClient) -> None:
        """Initialize a BlackVue camera."""

        self._client = client

    def recordings(self) -> list[Recording]:
        """Return the camera recordings."""

        return parse_vod(self._client.vod())

    def probe_missing_sidecars(self, recording: Recording) -> list[VodEntry]:
        """Opportunistically add .gps/.3gf entries this recording's
        camera-reported listing doesn't include, but which the camera
        still serves directly at the expected `/Record/<id><suffix>`
        path.

        A no-op - zero extra network calls - for a recording that
        already has these entries listed, which is the normal case on
        every model confirmed so far except the Elite 10. Mutates
        recording.entries in place (appending any entry found) and
        also returns whatever new entries were found, for a caller
        that wants to report on it (e.g. bv-download's --verbose
        output).
        """

        existing_suffixes = {
            entry.path.suffix.lower() for entry in recording.entries
        }

        found: list[VodEntry] = []

        for suffix in _PROBEABLE_SIDECAR_SUFFIXES:
            if suffix in existing_suffixes:
                continue

            path = f"/Record/{recording.id}{suffix}"

            if not self._client.probe(path):
                continue

            entry = VodEntry(
                timestamp=parse_timestamp(recording.id),
                path=PurePosixPath(path),
                fields={},
            )
            recording.entries.append(entry)
            found.append(entry)

        return found

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
    