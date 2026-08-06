"""
Raw archive browsing for bv-web: lists a camera's raw recordings -
what bv-download actually writes to disk, before any trip-grouping or
bv-export processing - with each recording's downloaded thumbnail(s)
so a long archive is easier to scan visually, without needing
bv-export to have run first.

Deliberately thin, the same way trips.py is thin relative to what it
wraps: this reuses blackvue.archive.Archive/ArchiveReader (the exact
same reader bv-ls/bv-export already use to enumerate recordings)
rather than adding any new disk-scanning logic - just a
browsing-friendly wrapper around Recording plus the day-grouping this
page's UI needs. The one exception is find_recording(), which calls
ArchiveReader.read_recording() - a targeted single-recording lookup
added to the reader itself (not duplicated here) specifically because
the thumbnail grid and the video player's range requests each resolve
one recording per HTTP request, and a full archive scan on every one
of those would be far too slow on a large archive.

Copyright (C) 2026 Christer R. (sssreh)

SPDX-License-Identifier: GPL-3.0-or-later
"""

from __future__ import annotations

import itertools
from collections.abc import Collection
from dataclasses import dataclass
from datetime import date
from datetime import datetime
from pathlib import Path

from ..archive import Archive
from ..archive import ArchiveReader
from ..archive import Asset
from ..archive import Recording
from ..archive import RecordingId
from ..lexicaltimeparser import TimeInterval

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
    def has_video(self) -> bool:
        """False if this recording has no video at all - possible
        even with a thumbnail present, since the two download
        separately (the thumbnail is small and downloads fast; the
        video is much bigger and can fail/lag behind, or the camera
        may have rotated the video off its SD card via loop recording
        before bv-download ever got to it - see WORKING_CONTEXT.md).
        The grid still shows the thumbnail in this case (it's useful
        information on its own), but archive_recording_list.html
        overlays a red cross on it using this flag, since a thumbnail
        alone isn't something the detail page can actually play."""

        return bool(self.videos)

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


def kind_options() -> list[tuple[str, str]]:
    """(kind letter, display label) pairs in canonical N/E/M/P/A order
    - what the archive browser's mode-filter checkboxes are built
    from."""

    return [(letter, _KIND_LABELS[letter]) for letter in ("N", "E", "M", "P", "A")]


def filter_recordings(
    recordings: list[ArchiveRecording],
    *,
    modes: Collection[str] | None = None,
    time_interval: TimeInterval | None = None,
    videos_only: bool = False,
) -> list[ArchiveRecording]:
    """Filter an already-scanned recording list by kind letter(s),
    a lexical timestamp range, and/or whether a video actually
    downloaded - the same TimeInterval bv-ls/bv-export/bv-download/
    bv-generate already filter recordings with (see
    lexicaltimeparser.py's LexicalTimeParser), applied here for the
    archive browser's own filter bar instead of a CLI flag.

    `modes=None` means "no mode filter" (every kind shows), not "show
    nothing" - the same convention an unchecked-by-default checkbox
    row implies. `time_interval=None` likewise means no time filter.
    `videos_only=True` hides recordings with no video at all (see
    ArchiveRecording.has_video's docstring on why a recording can have
    a thumbnail but no video) - the "Show only with videos" checkbox
    Christer asked for after the red-cross overlay made those
    recordings visible but still cluttering the grid. Order is
    preserved from the input list (already newest-first from
    scan_archive()), so this can run before or after group_by_day()
    depending on what a caller needs.
    """

    def matches(recording: ArchiveRecording) -> bool:
        if modes is not None and recording.recording.id.kind not in modes:
            return False
        if time_interval is not None and recording.id not in time_interval:
            return False
        if videos_only and not recording.has_video:
            return False
        return True

    return [recording for recording in recordings if matches(recording)]


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
    None if it doesn't exist.

    Uses ArchiveReader.read_recording() - a targeted lookup for just
    this one recording's own files - rather than scan_archive()'s
    full-archive read. This matters a lot here specifically: the
    thumbnail grid calls this once per recording shown on the page,
    and the video player's file-serving route calls it again for
    every HTTP range request while a browser seeks/buffers. Doing
    either of those via a full scan_archive() (which stat()s every
    file across the whole archive on every single call) would make an
    N-recording page load O(N^2), and would make video playback feel
    like it hangs - dozens of range requests, each re-scanning a
    potentially large archive from scratch. See
    ArchiveReader.read_recording()'s own docstring for the same
    reasoning from the reader's side.
    """

    parsed_id = RecordingId.parse(recording_id)
    if parsed_id is None or parsed_id.value != recording_id:
        return None

    if not archive_path.is_dir():
        return None

    recording = ArchiveReader(archive_path).read_recording(parsed_id)
    if recording is None:
        return None

    return ArchiveRecording(camera_id=camera_id, recording=recording)


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
