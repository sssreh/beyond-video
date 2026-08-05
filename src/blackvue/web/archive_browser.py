"""
Raw archive browsing for bv-web: lists a camera's raw recordings -
what bv-download actually writes to disk, before any trip-grouping or
bv-export processing - with each recording's downloaded thumbnail(s)
so a long archive is easier to scan visually, without needing
bv-export to have run first.

Deliberately thin, the same way trips.py is thin relative to what it
wraps: this reuses blackvue.archive.Archive (the exact same reader
bv-ls/bv-export already use to enumerate recordings) rather than
adding any new disk-scanning logic - just a browsing-friendly wrapper
around Recording plus the day-grouping this page's UI needs.

Copyright (C) 2026 Christer R. (sssreh)

SPDX-License-Identifier: GPL-3.0-or-later
"""

from __future__ import annotations

import itertools
from dataclasses import dataclass
from datetime import date
from datetime import datetime
from pathlib import Path

from ..archive import Archive
from ..archive import Asset
from ..archive import Recording

# (display label, video asset, thumbnail asset), in the order the
# detail page's video tabs and the list page's per-direction badges
# should show them.
_DIRECTIONS = (
    ("Front", Asset.FRONT, Asset.FRONT_THUMBNAIL),
    ("Rear", Asset.REAR, Asset.REAR_THUMBNAIL),
    ("Interior", Asset.INTERIOR, Asset.INTERIOR_THUMBNAIL),
)

_THUMBNAIL_ASSET_BY_DIRECTION = {
    "front": Asset.FRONT_THUMBNAIL,
    "rear": Asset.REAR_THUMBNAIL,
    "interior": Asset.INTERIOR_THUMBNAIL,
}

# (display label, asset), for the detail page's non-video sidecar
# download links - GPS/G-sensor logs, not video and not a thumbnail.
_SIDECARS = (
    ("GPS log", Asset.GPS),
    ("G-sensor log", Asset.GSENSOR),
)

# RecordingId.kind's single-letter codes - see recording_id.py's own
# docstring on "A" (observed on real hardware, meaning unconfirmed).
_KIND_LABELS = {
    "N": "Normal",
    "E": "Event",
    "M": "Manual",
    "P": "Parking",
    "A": "Unknown",
}


@dataclass(frozen=True)
class ArchiveRecording:
    """One raw archive recording, wrapped for the browsing UI."""

    camera_id: str
    recording: Recording

    @property
    def id(self) -> str:
        """The recording's own id string (e.g. "20260715_140212_N") -
        already a fixed, filesystem-safe, URL-safe format, same as
        RecordingId.parse() requires on the way in."""

        return self.recording.id.value

    @property
    def timestamp(self) -> datetime:
        return self.recording.id.timestamp

    @property
    def kind_label(self) -> str:
        return _KIND_LABELS.get(self.recording.id.kind, self.recording.id.kind)

    @property
    def size(self) -> int:
        return self.recording.size

    @property
    def size_label(self) -> str:
        """Human-readable size (e.g. "482M") - a small self-contained
        formatter rather than importing cli/bv_ls.py's format_size()
        into web/, which would pull a CLI module into bv-web for one
        function."""

        value = float(self.size)
        for unit in ("B", "K", "M", "G", "T"):
            if value < 1024 or unit == "T":
                return f"{int(value)}{unit}" if unit == "B" else f"{value:.1f}{unit}"
            value /= 1024
        raise AssertionError("unreachable")  # loop always returns on "T"

    @property
    def thumbnail_direction(self) -> str | None:
        """Lowercase direction name ("front"/"rear"/"interior")
        matching the first available thumbnail (front preferred, then
        rear, then interior) - what the thumbnail-serving route's URL
        expects. None if this recording has no thumbnail at all -
        e.g. an older archive predating the thumbnail sidecar-probing
        fix (see core/blackvue_camera.py's
        _probe_missing_thumbnails()), or a camera/firmware that
        doesn't serve .thm files."""

        for label, _, thumbnail_asset in _DIRECTIONS:
            if self.recording.has(thumbnail_asset):
                return label.lower()
        return None

    @property
    def videos(self) -> list[tuple[str, str]]:
        """(direction label, filename) pairs for every video this
        recording actually has, front/rear/interior order - what the
        detail page's video tabs/download links and the list page's
        per-direction badges are built from."""

        result = []
        for label, video_asset, _ in _DIRECTIONS:
            asset_file = self.recording.file(video_asset)
            if asset_file is not None:
                result.append((label, asset_file.name))
        return result

    @property
    def sidecars(self) -> list[tuple[str, str]]:
        """(display label, filename) pairs for this recording's
        non-video, non-thumbnail sidecar files (GPS/g-sensor logs) -
        the detail page's other download links."""

        result = []
        for label, asset in _SIDECARS:
            asset_file = self.recording.file(asset)
            if asset_file is not None:
                result.append((label, asset_file.name))
        return result

    @property
    def has_gps(self) -> bool:
        return self.recording.has(Asset.GPS)

    @property
    def has_gsensor(self) -> bool:
        return self.recording.has(Asset.GSENSOR)

    @property
    def known_filenames(self) -> frozenset[str]:
        """Every real filename this recording actually owns - the
        allow-list the file-serving/thumbnail routes check a
        requested filename against before ever touching the
        filesystem, same pattern as trips.py's
        TripAssets.known_filenames."""

        return frozenset(
            asset_file.name for asset_file in self.recording.assets.values()
        )

    def file_path(self, filename: str) -> Path | None:
        """Resolve an already-allow-listed filename (see
        known_filenames) to its real path, or None if it isn't
        actually one of this recording's own files."""

        for asset_file in self.recording.assets.values():
            if asset_file.name == filename:
                return asset_file.path
        return None

    def thumbnail_path(self, direction: str) -> Path | None:
        """Resolve a direction name ("front"/"rear"/"interior") to
        its thumbnail file's path, or None if this recording has no
        thumbnail for that direction."""

        asset = _THUMBNAIL_ASSET_BY_DIRECTION.get(direction)
        if asset is None:
            return None
        asset_file = self.recording.file(asset)
        return asset_file.path if asset_file else None


def scan_archive(archive_path: Path, camera_id: str) -> list[ArchiveRecording]:
    """Return every recording in a camera's raw archive, newest
    first.

    A missing archive directory (e.g. bv-download has never run for
    this camera yet) is treated as zero recordings, not an error -
    the same convention trips.py's scan_trips() uses for a missing
    --target.
    """

    if not archive_path.is_dir():
        return []

    archive = Archive(archive_path)

    return sorted(
        (
            ArchiveRecording(camera_id=camera_id, recording=recording)
            for recording in archive.recordings
        ),
        key=lambda item: item.recording.id,
        reverse=True,
    )


def find_recording(
    archive_path: Path, camera_id: str, recording_id: str
) -> ArchiveRecording | None:
    """Resolve a single recording id within a camera's archive, or
    None if it doesn't exist. There's no cheaper way to read a single
    recording than scanning the whole archive (ArchiveReader.read()
    always reads the full directory) - same one-scan-per-request
    trade-off scan_archive() itself already accepts."""

    for recording in scan_archive(archive_path, camera_id):
        if recording.id == recording_id:
            return recording
    return None


def group_by_day(
    recordings: list[ArchiveRecording],
) -> list[tuple[date, list[ArchiveRecording]]]:
    """Group already newest-first recordings into (day, recordings)
    pairs, still newest-day-first. Relies on the input already being
    sorted by timestamp descending (scan_archive()'s own contract) -
    itertools.groupby only groups consecutive equal keys, which is
    exactly what a sorted list gives us for free."""

    return [
        (day, list(group))
        for day, group in itertools.groupby(
            recordings, key=lambda item: item.timestamp.date()
        )
    ]
