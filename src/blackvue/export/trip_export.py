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
from ..telemetry.gps_reader import read_gps
from ..telemetry.gsensor_reader import GSensorSample
from ..telemetry.gsensor_reader import read_gsensor
from ..telemetry.gsensor_reader import write_gsensor
from ..trip.trip import Trip
from .gpx_writer import write_gpx
from .media import concatenate_media
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


def export_trip(trip: Trip, destination: Path) -> ExportResult:
    """Assemble one trip's concatenated video/audio/text, GPX track,
    and g-sensor log into `destination`.

    `destination` is created if missing. bv-export's CLI is
    responsible for the create/overwrite-existing-folder policy
    before calling this - export_trip just writes into whatever
    directory it's given.
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

    gpx_path = None
    fixes = _merge_gps(trip)
    if fixes:
        gpx_path = destination / "trip.gpx"
        write_gpx(fixes, gpx_path, name=trip.label)

    gsensor_path = None
    samples = _merge_gsensor(trip)
    if samples:
        gsensor_path = destination / "trip.3gf"
        write_gsensor(samples, gsensor_path)

    return ExportResult(
        front_video=front_video,
        rear_video=rear_video,
        audio=audio,
        gpx=gpx_path,
        gsensor=gsensor_path,
        text=tuple(text_paths),
        warnings=tuple(warnings),
    )
