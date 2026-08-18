"""
BlackVue camera.

Copyright (C) 2026 Christer R. (sssreh)

SPDX-License-Identifier: GPL-3.0-or-later
"""

from __future__ import annotations

import time
from collections.abc import Callable
from pathlib import Path
from pathlib import PurePosixPath

from .blackvue_client import BlackVueClient
from ..domain.recording import Recording
from ..domain.vod_entry import VodEntry
from ..parser.vod import parse_timestamp
from ..parser.vod import parse_vod

# Metadata sidecar suffixes that blackvue_vod.cgi never lists, even
# though the camera exists and serves them fine via a direct GET at
# the expected path - confirmed across multiple of Christer's real
# camera models, not just the Elite 10 (see WORKING_CONTEXT.md):
# blackvue_vod.cgi's own listing has consistently only ever contained
# video files. blackvue_vod.cgi is therefore treated as a hint for
# these, not the sole source of truth - probe_missing_sidecars() fills
# the gap, and is a no-op (zero extra network calls) for any suffix a
# given camera/firmware combination *does* happen to list.
_PROBEABLE_SIDECAR_SUFFIXES = (".gps", ".3gf")

# Thumbnail files are one-per-camera-direction (e.g. "..._NF.thm" for
# the front camera), unlike the suffixes above which are one-per-
# recording with no direction letter - so they can't share that flat
# suffix table and need their own direction-aware probe. Probed only
# for directions the recording actually has a video for (there's no
# way to know in advance whether a recording has a rear/interior
# camera at all otherwise).
_THUMBNAIL_SUFFIX = ".thm"
_DIRECTION_LETTERS = ("F", "R", "I")


class BlackVueCamera:
    """BlackVue camera."""

    def __init__(self, client: BlackVueClient) -> None:
        """Initialize a BlackVue camera."""

        self._client = client

    def recordings(self) -> list[Recording]:
        """Return the camera recordings."""

        return parse_vod(self._client.vod())

    def probe_missing_sidecars(self, recording: Recording) -> list[VodEntry]:
        """Opportunistically add .gps/.3gf/.thm entries this
        recording's camera-reported listing doesn't include, but which
        the camera still serves directly at the expected path.

        A no-op - zero extra network calls - for a recording that
        already has these entries listed, which some camera/firmware
        combinations do even though the ones confirmed so far never
        have. Mutates recording.entries in place (appending any entry
        found) and also returns whatever new entries were found, for a
        caller that wants to report on it (e.g. bv-download's
        --verbose output).
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

        found.extend(self._probe_missing_thumbnails(recording))

        return found

    def _probe_missing_thumbnails(self, recording: Recording) -> list[VodEntry]:
        """Opportunistically add a .thm entry for any camera direction
        (front/rear/interior) this recording has a video for but no
        thumbnail listed - see _THUMBNAIL_SUFFIX's own comment for why
        this can't share the flat-suffix probe above."""

        directions_with_video = {
            letter
            for letter in _DIRECTION_LETTERS
            for entry in recording.entries
            if entry.is_video and entry.path.stem.endswith(letter)
        }
        directions_with_thumbnail = {
            letter
            for letter in _DIRECTION_LETTERS
            for entry in recording.entries
            if entry.path.suffix.lower() == _THUMBNAIL_SUFFIX
            and entry.path.stem.endswith(letter)
        }

        found: list[VodEntry] = []

        for letter in sorted(directions_with_video - directions_with_thumbnail):
            path = f"/Record/{recording.id}{letter}{_THUMBNAIL_SUFFIX}"

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
        on_entry: Callable[[VodEntry, float], None] | None = None,
    ) -> bool:
        """Download a recording.

        If select is given, only entries for which it returns True are
        downloaded (e.g. ``lambda entry: entry.is_video``). By default
        every entry is downloaded.

        If on_bytes is given, it's passed straight through to
        BlackVueClient.download() for every entry - see its docstring.

        If on_entry is given, it's called once per entry that was
        actually downloaded (not one already present at `destination`
        with nothing to transfer - see BlackVueClient.download()'s own
        return value) with the entry itself and how many seconds that
        entry's own download() call took. Christer: "duration for
        downloading all of [the sidecars] ... and a duration each
        video per recordingid" - bv-download's _run() uses this to
        report per-video download time per recording plus a running
        total across every sidecar (.gps/.3gf/.thm) file downloaded in
        the run (see cli/bv_download.py). Only entries that actually
        transferred bytes fire this - an already-up-to-date recording
        (nothing to resume) doesn't add a near-zero measurement to
        either total.

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
            started = time.monotonic()

            if self._client.download(
                entry,
                filename,
                on_bytes=on_bytes,
            ):
                changed = True

                if on_entry is not None:
                    on_entry(entry, time.monotonic() - started)

        return changed
    