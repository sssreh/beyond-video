"""
Scan a bv-export --target directory for trip folders, for bv-web's
trip-list/trip-detail pages.

Deliberately doesn't touch blackvue.archive/blackvue.trip at all -
those model the *source* recordings bv-export reads from, before a
trip even exists as a folder. This module only ever looks at
bv-export's own *output*: the folders it already wrote under
--target, one per trip, each holding whatever combination of
front.mp4/rear.mp4/stitch.mp4/map.mp4/... a given `bv-export` run
happened to produce (see export/trip_export.py). A trip folder is
identified by containing trip.log - the one file export_trip() always
writes first (see export/trip_log.py) - so this can't mistake an
unrelated directory (e.g. the --map-cache-dir's .osm_cache) for a
trip.

Copyright (C) 2026 Christer R. (sssreh)

SPDX-License-Identifier: GPL-3.0-or-later
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from dataclasses import field
from pathlib import Path

TRIP_LOG_FILENAME = "trip.log"

# Matches TripLog's own header line, e.g.
# "=== bv-export trip log: trip_20260715_133458_20260715_141235 ==="
# - see export/trip_log.py's __init__(). Read straight from the log
# rather than re-derived from the folder name, since the folder name
# may carry an extra --prefix (folder_name_for_trip()) that isn't
# part of the trip's own label.
_TRIP_LABEL_RE = re.compile(r"^=== bv-export trip log: (.+) ===$")

# The video files export_trip() may have written, in the order
# they're preferred for playback: stitch.mp4 already has everything
# composited together, so it wins over the raw front/rear feeds when
# it exists.
VIDEO_FILENAMES = ("stitch.mp4", "front.mp4", "rear.mp4")
MAP_FILENAME = "map.mp4"
GSENSOR_VIDEO_FILENAME = "gsensor.mp4"
GPX_FILENAME = "trip.gpx"
SRT_FILENAME = "trip.srt"
LRC_FILENAME = "trip.lrc"


@dataclass(frozen=True)
class TripAssets:
    """Which of a trip folder's possible files actually exist on
    disk - a real bv-export run only produces the subset its flags
    asked for (e.g. no map.mp4 without --map, no gsensor.mp4 without
    --gsensor-video), so every field here can legitimately be empty/
    None."""

    folder: Path
    label: str
    videos: tuple[str, ...] = field(default_factory=tuple)
    map_video: str | None = None
    map_zoom_videos: tuple[str, ...] = field(default_factory=tuple)
    gsensor_video: str | None = None
    gpx: bool = False
    srt: bool = False
    lrc: bool = False

    @property
    def id(self) -> str:
        """The URL-safe identifier bv-web uses for this trip - just
        the folder name, since that's already filesystem-safe and
        already unique within --target."""

        return self.folder.name

    @property
    def primary_video(self) -> str | None:
        """The single best video to play for this trip, or None if
        the trip has no video at all (e.g. a GPS-only export with
        neither --stitch nor raw video kept)."""

        return self.videos[0] if self.videos else None

    @property
    def known_filenames(self) -> frozenset[str]:
        """Every filename trip_file() is allowed to serve for this
        trip - the web app's file-serving route checks against this
        rather than trusting a filename straight from the URL, so a
        request can't walk outside what was actually found on disk
        here."""

        names = set(self.videos) | set(self.map_zoom_videos)
        if self.map_video:
            names.add(self.map_video)
        if self.gsensor_video:
            names.add(self.gsensor_video)
        if self.gpx:
            names.add(GPX_FILENAME)
        if self.srt:
            names.add(SRT_FILENAME)
        if self.lrc:
            names.add(LRC_FILENAME)
        return frozenset(names)


def _read_trip_label(trip_log_path: Path) -> str | None:
    try:
        with trip_log_path.open("r", encoding="utf-8") as file:
            first_line = file.readline().strip()
    except OSError:
        return None

    match = _TRIP_LABEL_RE.match(first_line)
    return match.group(1) if match else None


def scan_trip(folder: Path) -> TripAssets | None:
    """Return the TripAssets for `folder`, or None if it doesn't look
    like a trip folder (no trip.log)."""

    trip_log_path = folder / TRIP_LOG_FILENAME
    if not trip_log_path.is_file():
        return None

    label = _read_trip_label(trip_log_path) or folder.name

    videos = tuple(
        name for name in VIDEO_FILENAMES if (folder / name).is_file()
    )
    map_zoom_videos = tuple(
        sorted(path.name for path in folder.glob("map_zoom_*.mp4"))
    )

    return TripAssets(
        folder=folder,
        label=label,
        videos=videos,
        map_video=MAP_FILENAME if (folder / MAP_FILENAME).is_file() else None,
        map_zoom_videos=map_zoom_videos,
        gsensor_video=(
            GSENSOR_VIDEO_FILENAME
            if (folder / GSENSOR_VIDEO_FILENAME).is_file()
            else None
        ),
        gpx=(folder / GPX_FILENAME).is_file(),
        srt=(folder / SRT_FILENAME).is_file(),
        lrc=(folder / LRC_FILENAME).is_file(),
    )


def scan_trips(target: Path) -> list[TripAssets]:
    """Scan every immediate subfolder of `target` (a bv-export
    --target directory) for trips, newest first. A `target` that
    doesn't exist yet (e.g. bv-export hasn't been run) is read as
    zero trips rather than an error - bv-web's trip list should just
    look empty, not crash."""

    if not target.is_dir():
        return []

    trips = [
        trip
        for entry in sorted(target.iterdir())
        if entry.is_dir() and (trip := scan_trip(entry)) is not None
    ]
    trips.sort(key=lambda trip: trip.label, reverse=True)
    return trips
