"""
Raw archive browsing for bv-web: lists a camera's raw recordings -
what bv-download actually writes to disk, before any trip-grouping or
bv-export processing - with each recording's downloaded thumbnail(s)
so a long archive is easier to scan visually, without needing
bv-export to have run first.

Deliberately thin, the same way trips.py is thin relative to what it
wraps: this goes through a camera's own CameraAdapter (see
adapters/registry.py and docs/CAMERA_ADAPTERS.md) - the same adapter
bv-ls already resolves via CameraConfig.adapter - rather than adding
any new disk-scanning logic of its own, just a browsing-friendly
wrapper around Recording plus the day-grouping this page's UI needs.
The one exception is find_recording(), which calls
CameraAdapter.find_recording() - a targeted single-recording lookup
(BlackVueAdapter delegates to ArchiveReader.read_recording(); see that
method's own docstring) specifically because the thumbnail grid and
the video player's range requests each resolve one recording per HTTP
request, and a full archive scan on every one of those would be far
too slow on a large archive.

Copyright (C) 2026 Christer R. (sssreh)

SPDX-License-Identifier: GPL-3.0-or-later
"""

from __future__ import annotations

import itertools
import time
from collections.abc import Collection
from dataclasses import dataclass
from datetime import date
from datetime import datetime
from pathlib import Path

from ..adapters import registry
from ..adapters.base import CameraAdapter
from ..adapters.telemetry_bridge import read_recording_gps
from ..archive import Asset
from ..archive import Recording
from ..archive import RecordingId
from ..archive import recording_is_photo
from ..core.camera_config import DEFAULT_ADAPTER_ID
from ..generate.scene import extract_description_section
from ..lexicaltimeparser import TimeInterval
from ..telemetry.gps_reader import GpsFix

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

# (display label, asset), for the detail page's scene/OCR text panel
# (task #681) - the two Asset types bv-generate --describe-scene/
# bv-scribe write, same pair blackvue/search.py's own "scene" group
# already searches (see TEXT_SEARCH_ASSETS there). No diarized
# equivalent exists for scene text the way transcript/translation
# have one, so unlike TEXT_SEARCH_ASSETS this is just the two.
_SCENE_TEXTS = (
    ("Front", Asset.SCENE_DESCRIPTION),
    ("Rear", Asset.SCENE_DESCRIPTION_REAR),
)

# Substring a "## Zoomed sign reads" bullet line's read text is
# checked against (case-insensitively) to drop it from scene_summary
# below - see that property's own docstring for why "not legible" is
# noise for a human-readable summary even though it's worth keeping in
# the raw file scene_texts still shows in full.
_NOT_LEGIBLE = "not legible"


def _extract_legible_sign_reads(text: str) -> list[str]:
    """Pull the '## Zoomed sign reads' bullet lines (see
    generate/scene.py's zoom_into_signs()) out of a scene.txt/
    rear.scene.txt body, keeping only the ones that actually read
    something - a "not legible" line means the detection pipeline
    found a real sign/plate but couldn't read it, which is exactly the
    kind of line scene_summary wants to drop. Returns [] if the
    section isn't present at all (task="ocr"-without-zoom-signs, or
    zoom_signs never found anything to crop).

    A single bullet's read text can itself span multiple raw lines -
    zoom_into_signs() writes `f"- [t=...] {label}: {read_text}"` with
    read_text taken verbatim from the model, and a multi-row sign (e.g.
    several destinations stacked on one board) comes back as a read
    with embedded newlines, e.g.:

        - [t=40.6s] blue road sign with white text: 227 DALARO
        259 HUDDINGE
        JORDBRO
        500

    An earlier version of this function only kept the "- [t=...]" line
    itself and silently dropped every continuation line below it -
    Christer caught this from a real scene.txt where "259 HUDDINGE" /
    "JORDBRO" / "500" vanished from the summary entirely (see
    WORKING_CONTEXT.md). Any non-bullet, non-blank line encountered
    while inside the section is now folded into the read currently
    being built, joined with a space, until the next "- " bullet (or
    the section ends) closes it off.

    Stops at the next '#' heading or the disclaimer footer's '---'
    divider, whichever comes first - describe_scene() always appends
    DISCLAIMER right after this section, and it doesn't start with '#'
    so a naive "stop at the next heading" scan would otherwise swallow
    it as if it were more bullet lines.
    """

    lines = text.splitlines()
    in_section = False
    reads: list[str] = []
    current: str | None = None

    def _flush() -> None:
        nonlocal current
        if current is not None:
            content = current.strip()
            if content and _NOT_LEGIBLE not in content.lower():
                reads.append(content)
            current = None

    for line in lines:
        stripped = line.strip()
        if stripped.startswith("#") and "zoomed sign reads" in stripped.lower():
            in_section = True
            continue
        if in_section and (stripped.startswith("#") or stripped.startswith("---")):
            _flush()
            break
        if not in_section:
            continue
        if stripped.startswith("- "):
            _flush()
            current = stripped[2:].strip()
        elif stripped and current is not None:
            current += " " + stripped
    else:
        _flush()

    return reads

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
    def gps_path(self) -> Path | None:
        """Path to this recording's .gps sidecar file, or None if it
        doesn't have one (see has_gps) - what the archive detail
        page's "Show start location" link reads via
        first_valid_gps_fix() below."""

        asset_file = self.recording.file(Asset.GPS)
        return asset_file.path if asset_file else None

    @property
    def has_gsensor(self) -> bool:
        return self.recording.has(Asset.GSENSOR)

    @property
    def scene_texts(self) -> list[tuple[str, str]]:
        """(direction label, text) pairs for whichever scene/OCR
        description(s) this recording actually has (task #681 - "the
        only way to read a scene description is opening the file
        directly on disk"). Empty list if neither exists - the vast
        majority of recordings, unless bv-generate --describe-scene or
        bv-scribe has run against this camera's archive.

        A read failure (permissions, a file that vanished between the
        directory scan and this read, a mounted archive going away
        mid-request - the same real failure modes ArchiveRecording's
        other read paths already tolerate) is surfaced as a bracketed
        placeholder message rather than raising and taking down the
        whole detail page over one unreadable text file - the video/
        GPS/other panels on this page are independently useful even if
        this one can't render.
        """

        result = []
        for label, asset in _SCENE_TEXTS:
            asset_file = self.recording.file(asset)
            if asset_file is None:
                continue
            try:
                text = asset_file.path.read_text(encoding="utf-8")
            except OSError as exc:
                text = f"[could not read {asset_file.name}: {exc}]"
            result.append((label, text))
        return result

    @property
    def scene_summary(self) -> list[tuple[str, str, list[str]]]:
        """(direction label, description, legible sign reads) triples -
        a cleaner read of whatever scene_texts already has, for
        someone who just wants "what happened + what signs said"
        without wading through the raw on-screen-text dump or the
        "not legible" clutter in the zoomed-sign-reads section.
        Christer, after seeing how much of a real scene.txt is "not
        legible" noise for his bv-search use case: "maybe i just want
        a report on the scene files for human reading" -> "like a
        trip-summary but per recording, could be shown when you look
        at a video... only freshly generated and not a new file" (see
        WORKING_CONTEXT.md). Computed live from the same files
        scene_texts reads on every call - no new asset file is ever
        written, and unlike --trip-summary this doesn't call the
        vision model again, it just re-parses text already on disk.

        Skips any direction where neither a description nor a single
        legible sign read was found - e.g. a rear file generated
        alongside --camera both, whose forced OCR-only pass has no
        '## Description' section at all and may have nothing legible
        in it either, so there'd be nothing worth showing.
        """

        result = []
        for label, text in self.scene_texts:
            description = extract_description_section(text)
            legible_reads = _extract_legible_sign_reads(text)
            if description or legible_reads:
                result.append((label, description, legible_reads))
        return result

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
        thumbnail for that direction.

        For "front", falls back to the recording's own FRONT file when
        it's a photo (see archive/photo.py) and no *_THUMBNAIL sidecar
        exists for it - true for every photo recording today, since
        nothing generates thumbnail sidecars for stills (folder/gopro's
        own manifests document `thumbnails: "generated"` as an unbuilt
        hook). The photo itself already *is* a small, real preview
        image, so serving it directly is a real thumbnail, not a
        placeholder - no ffmpeg frame-extraction needed the way a real
        video would require."""

        asset = _THUMBNAIL_ASSET_BY_DIRECTION.get(direction)
        if asset is None:
            return None
        asset_file = self.recording.file(asset)
        if asset_file is not None:
            return asset_file.path
        if direction == "front" and recording_is_photo(self.recording):
            front = self.recording.file(Asset.FRONT)
            return front.path if front is not None else None
        return None


def first_valid_gps_fix(adapter: CameraAdapter, recording: Recording) -> GpsFix | None:
    """Return the first fix for `recording` (read via `adapter`, per
    its manifest's gps_source_asset - see adapters/telemetry_bridge.py)
    that has a real position - the location "at the start" of the
    recording, for the archive detail page's "Show start and stop
    location" link. None if there's no valid fix at all (e.g. the
    camera hadn't acquired a GPS signal yet when the clip started -
    common for the first recording after the car's been parked
    somewhere without sky view); a GPS source existing at all (see
    ArchiveRecording.has_gps or telemetry_bridge.recording_has_gps())
    is a separate, weaker guarantee than this actually finding a fix.

    "Valid" matches GpsFix.valid's own definition (a real position
    per the sentence's mode indicator) plus a defensive check that
    latitude/longitude both parsed - read_gps() already skips
    malformed sentences entirely, but a $GPRMC sentence can in
    principle report a valid mode with an unparsed coordinate field,
    so this doesn't assume the two always travel together.
    """

    for fix in read_recording_gps(adapter, recording):
        if fix.valid and fix.latitude is not None and fix.longitude is not None:
            return fix
    return None


def last_valid_gps_fix(adapter: CameraAdapter, recording: Recording) -> GpsFix | None:
    """Return the last fix for `recording` that has a real position -
    the location "at the end" of the recording, for the archive
    detail page's "Show start and stop location" link. Mirrors
    first_valid_gps_fix() exactly, just walking the fixes in reverse -
    see its own docstring for what "valid" means. read_recording_gps()
    already returns fixes in chronological order, so reversed() alone
    is enough - no separate sort needed.
    """

    for fix in reversed(read_recording_gps(adapter, recording)):
        if fix.valid and fix.latitude is not None and fix.longitude is not None:
            return fix
    return None


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


def scan_archive(
    archive_path: Path, camera_id: str, adapter_id: str = DEFAULT_ADAPTER_ID
) -> list[ArchiveRecording]:
    """Return every recording in a camera's raw archive, newest
    first.

    `adapter_id` selects which CameraAdapter scans `archive_path` (see
    adapters/registry.py) - defaults to "blackvue"
    (DEFAULT_ADAPTER_ID), same as bv-ls. Callers pass the resolved
    camera's own CameraConfig.adapter (see app.py's
    archive_recording_list() route).

    A missing archive directory (e.g. bv-download has never run for
    this camera yet) is treated as zero recordings, not an error -
    the same convention trips.py's scan_trips() uses for a missing
    --target.
    """

    if not archive_path.is_dir():
        return []

    archive = registry.get_adapter(adapter_id).open_archive(archive_path)

    return sorted(
        (
            ArchiveRecording(camera_id=camera_id, recording=recording)
            for recording in archive.recordings
        ),
        key=lambda item: item.recording.id,
        reverse=True,
    )


def find_recording(
    archive_path: Path,
    camera_id: str,
    recording_id: str,
    adapter_id: str = DEFAULT_ADAPTER_ID,
) -> ArchiveRecording | None:
    """Resolve a single recording id within a camera's archive, or
    None if it doesn't exist.

    Uses CameraAdapter.find_recording() - a targeted lookup for just
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
    CameraAdapter.find_recording()'s own docstring (base.py) for how
    each adapter meets that bar - or doesn't; FolderAdapter's own
    find_recording() falls back to a full rescan, which is a real,
    accepted cost for that kind of archive today, not a regression
    from before this adapter existed (nothing served folder-adapter
    cameras through bv-web at all until this wiring).
    """

    parsed_id = RecordingId.parse(recording_id)
    if parsed_id is None or parsed_id.value != recording_id:
        return None

    if not archive_path.is_dir():
        return None

    recording = registry.get_adapter(adapter_id).find_recording(archive_path, parsed_id)
    if recording is None:
        return None

    return ArchiveRecording(camera_id=camera_id, recording=recording)


class ArchiveRecordingCache:
    """Caches find_recording() results briefly, per (camera_id,
    recording_id) pair - mirrors trips.py's TripCache (see its own
    docstring for the full reasoning) for the same underlying problem
    on the archive-browser side.

    find_recording() is already a targeted single-recording lookup,
    not a full scan_archive() (see its own docstring), but a single
    page view still fires it several times in a burst: once for the
    detail page itself, once for its thumbnail, and then again for
    every HTTP range request while a video plays. Each of those redoes
    the same handful of filesystem calls against the same recording.
    On a LAN where bv-web's Docker host is the NAS rather than the
    machine actually watching the video, that repeated per-request
    cost is what shows up as felt lag on playback and thumbnail loads
    - the same story that motivated TripCache for trip playback.

    A short TTL (default 2 seconds, same as TripCache) keeps this from
    masking a recording bv-download is still in the middle of writing
    - a request just outside the window re-checks the real filesystem.
    Misses are deliberately NOT cached, matching TripCache, so a bad
    id or a recording that hasn't finished downloading yet is
    re-checked on the very next request rather than held as "not
    found" for the TTL.
    """

    def __init__(self, ttl_seconds: float = 2.0) -> None:
        self._ttl_seconds = ttl_seconds
        self._entries: dict[tuple[str, str], tuple[ArchiveRecording, float]] = {}

    def get(
        self,
        archive_path: Path,
        camera_id: str,
        recording_id: str,
        adapter_id: str = DEFAULT_ADAPTER_ID,
    ) -> ArchiveRecording | None:
        now = time.monotonic()
        key = (camera_id, recording_id)

        cached = self._entries.get(key)
        if cached is not None:
            recording, expires_at = cached
            if now < expires_at:
                return recording

        recording = find_recording(archive_path, camera_id, recording_id, adapter_id)
        if recording is not None:
            self._entries[key] = (recording, now + self._ttl_seconds)
        else:
            self._entries.pop(key, None)
        return recording


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
