"""
Per-trip media assembly for bv-export - the "hard work" step:
concatenating video/audio/text assets across a trip's recordings, and
generating a merged GPX track and g-sensor log covering the whole
trip.

Copyright (C) 2026 Christer R. (sssreh)

SPDX-License-Identifier: GPL-3.0-or-later
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from ..archive.asset import Asset
from ..generate.media import MediaToolError
from ..generate.media import probe
from ..telemetry.gps_reader import read_gps
from ..telemetry.gsensor_reader import GSensorSample
from ..telemetry.gsensor_reader import read_gsensor
from ..telemetry.gsensor_reader import write_gsensor
from ..trip.trip import Trip
from .gpx_writer import write_gpx
from .gsensor_video import render_gsensor_video
from .map_video import render_map_video
from .media import concatenate_media
from .osm_roads import bounding_box_for_fixes
from .osm_roads import load_or_fetch_roads
from .subtitles import merge_lrc
from .subtitles import merge_srt
from .text import merge_text_assets

# (asset, output filename) pairs for every text asset bv-export knows
# how to merge. Only assets that at least one recording in the trip
# actually has produce an output file.
TEXT_ASSETS = (
    (Asset.TRANSCRIPT, "transcript.txt"),
    (Asset.TRANSCRIPT_DIARIZED, "transcript.diarized.txt"),
    (Asset.TRANSLATION, "translation.txt"),
    (Asset.TRANSLATION_DIARIZED, "translation.diarized.txt"),
)


@dataclass(frozen=True)
class ExportResult:
    """Which files export_trip() actually wrote for one trip."""

    front_video: Path | None = None
    rear_video: Path | None = None
    audio: Path | None = None
    gpx: Path | None = None
    gsensor: Path | None = None
    map: Path | None = None
    gsensor_video: Path | None = None
    srt: Path | None = None
    lrc: Path | None = None
    text: tuple[Path, ...] = field(default_factory=tuple)
    warnings: tuple[str, ...] = field(default_factory=tuple)


def folder_name_for_trip(trip: Trip, prefix: str | None) -> str:
    """Return the subfolder name bv-export uses for a trip, e.g.
    'Holiday_trip_20260715_133458_20260715_141235' when prefix is
    'Holiday', or just 'trip_20260715_133458_20260715_141235' with no
    prefix."""

    if prefix:
        return f"{prefix}_{trip.label}"
    return trip.label


def _concatenate_asset(
    trip: Trip,
    asset: Asset,
    filename: str,
    destination: Path,
    warnings: list[str],
) -> Path | None:
    sources = [
        recording.file(asset).path for recording in trip if recording.has(asset)
    ]
    if not sources:
        return None

    out = destination / filename
    try:
        concatenate_media(sources, out)
    except MediaToolError as exc:
        warnings.append(str(exc))
        return None

    return out


def _merge_gps(trip: Trip) -> tuple:
    fixes = []

    for recording in trip:
        gps_file = recording.file(Asset.GPS)
        if gps_file is None:
            continue
        try:
            fixes.extend(read_gps(gps_file.path))
        except MediaToolError:
            continue

    return tuple(sorted(fixes, key=lambda fix: fix.timestamp))


def _merge_gsensor(trip: Trip) -> tuple[GSensorSample, ...]:
    """Merge every recording's g-sensor samples into one trip-relative
    stream: each recording's own offsets (relative to its own start)
    are rebased by how far that recording started after the trip's
    first recording."""

    samples: list[GSensorSample] = []
    trip_start = trip.start_timestamp

    for recording in trip:
        gsensor_file = recording.file(Asset.GSENSOR)
        if gsensor_file is None:
            continue
        try:
            recording_samples = read_gsensor(gsensor_file.path)
        except MediaToolError:
            continue

        rebase = recording.id.timestamp - trip_start
        samples.extend(
            GSensorSample(offset=rebase + sample.offset, x=sample.x, y=sample.y, z=sample.z)
            for sample in recording_samples
        )

    return tuple(sorted(samples, key=lambda sample: sample.offset))


def _render_map(
    fixes: tuple,
    destination: Path,
    map_cache_dir: Path,
    warnings: list[str],
) -> Path | None:
    """Render map.mp4 for a trip's merged GPS fixes, degrading to a
    warning (not a failed export) on any network or ffmpeg problem -
    the rest of the trip's export is still worth having even if the
    map couldn't be built.
    """

    bbox = bounding_box_for_fixes(fixes)
    if bbox is None:
        return None

    try:
        roads = load_or_fetch_roads(bbox, map_cache_dir)
    except MediaToolError as exc:
        warnings.append(f"map: {exc}")
        return None

    try:
        return render_map_video(fixes, roads, bbox, destination / "map.mp4")
    except MediaToolError as exc:
        warnings.append(f"map: {exc}")
        return None


def export_trip(
    trip: Trip,
    destination: Path,
    *,
    render_map: bool = False,
    map_cache_dir: Path | None = None,
    render_gsensor: bool = False,
) -> ExportResult:
    """Assemble one trip's concatenated video/audio/text, GPX track,
    and g-sensor log into `destination`.

    `destination` is created if missing. bv-export's CLI is
    responsible for the create/overwrite-existing-folder policy
    before calling this - export_trip just writes into whatever
    directory it's given.

    `render_map=True` additionally renders map.mp4 - a route/position/
    speed overlay on an OSM-road basemap (see osm_roads.py/map_video.py
    for why this uses Overpass data rather than live map tiles).
    `map_cache_dir` is where fetched OSM road data is cached between
    trips/runs (defaults to a `.osm_cache` folder next to `destination`
    - bv-export's CLI points this at --target so it's shared across
    every trip in one export run, not wiped when a trip folder is
    refreshed). Off by default: it needs network the first time a
    region is exported, and adds real render time.

    `render_gsensor=True` additionally renders gsensor.mp4 - a dot
    moving around a gauge, tracking the trip's g-sensor (x, y)
    readings with a short fading trail, on a flat chroma-key green
    background meant to be composited over the front/rear footage
    later (see gsensor_render.py/gsensor_video.py). No network
    involved, but off by default since it's extra render time most
    exports won't want.
    """

    destination.mkdir(parents=True, exist_ok=True)
    warnings: list[str] = []

    front_video = _concatenate_asset(
        trip, Asset.FRONT, "front.mp4", destination, warnings
    )
    rear_video = _concatenate_asset(
        trip, Asset.REAR, "rear.mp4", destination, warnings
    )
    audio = _concatenate_asset(
        trip, Asset.AUDIO, "audio.aac", destination, warnings
    )

    text_paths = []
    for asset, filename in TEXT_ASSETS:
        merged = merge_text_assets(trip, asset)
        if merged is None:
            continue
        out = destination / filename
        out.write_text(merged, encoding="utf-8")
        text_paths.append(out)

    # Whisper only emits segments for actual speech, so a trip with a
    # quiet stretch at the end (nobody talking for the last couple of
    # minutes, say) produces a merged subtitle file that stops well
    # before the video does. Probing the actual concatenated video
    # bv-export just wrote - not summing recordings' own .duration.txt
    # files, which may not all exist - gives merge_srt()/merge_lrc()
    # the real length to pad the trailing cue out to.
    video_duration_seconds = None
    video_for_duration = front_video or rear_video
    if video_for_duration is not None:
        try:
            video_duration_seconds = probe(video_for_duration).duration_seconds
        except MediaToolError as exc:
            warnings.append(f"subtitle padding: {exc}")

    srt_path = None
    merged_srt = merge_srt(trip, total_duration_seconds=video_duration_seconds)
    if merged_srt is not None:
        srt_path = destination / "trip.srt"
        srt_path.write_text(merged_srt + "\n", encoding="utf-8")

    lrc_path = None
    merged_lrc = merge_lrc(trip, total_duration_seconds=video_duration_seconds)
    if merged_lrc is not None:
        lrc_path = destination / "trip.lrc"
        lrc_path.write_text(merged_lrc + "\n", encoding="utf-8")

    gpx_path = None
    fixes = _merge_gps(trip)
    if fixes:
        gpx_path = destination / "trip.gpx"
        write_gpx(fixes, gpx_path, name=trip.label)

    map_path = None
    if render_map and fixes:
        cache_dir = map_cache_dir or (destination.parent / ".osm_cache")
        map_path = _render_map(fixes, destination, cache_dir, warnings)

    gsensor_path = None
    samples = _merge_gsensor(trip)
    if samples:
        gsensor_path = destination / "trip.3gf"
        write_gsensor(samples, gsensor_path)

    gsensor_video_path = None
    if render_gsensor and samples:
        try:
            gsensor_video_path = render_gsensor_video(
                samples, destination / "gsensor.mp4"
            )
        except MediaToolError as exc:
            warnings.append(f"gsensor video: {exc}")

    return ExportResult(
        front_video=front_video,
        rear_video=rear_video,
        audio=audio,
        gpx=gpx_path,
        gsensor=gsensor_path,
        map=map_path,
        gsensor_video=gsensor_video_path,
        srt=srt_path,
        lrc=lrc_path,
        text=tuple(text_paths),
        warnings=tuple(warnings),
    )
