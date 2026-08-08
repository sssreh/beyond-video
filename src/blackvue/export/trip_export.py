"""
Per-trip media assembly for bv-export - the "hard work" step:
concatenating video/audio/text assets across a trip's recordings, and
generating a merged GPX track and g-sensor log covering the whole
trip.

Copyright (C) 2026 Christer R. (sssreh)

SPDX-License-Identifier: GPL-3.0-or-later
"""

from __future__ import annotations

import concurrent.futures
import shutil
import sys
import tempfile
import time
from dataclasses import dataclass, field
from datetime import datetime
from datetime import timedelta
from pathlib import Path

from ..archive.asset import Asset
from ..archive.asset_file import AssetFile
from ..archive.recording_id import RecordingId
from ..core.camera_config import default_config_dir
from ..generate.media import MediaToolError
from ..generate.media import extract_audio
from ..generate.media import probe
from ..generate.media import probe_audio_codec
from ..generate.media import probe_audio_format
from ..generate.media import select_source
from ..generate.mp4_repair import load_or_repair_parking_video
from ..telemetry.gps_reader import read_gps
from ..telemetry.gsensor_reader import GSensorSample
from ..telemetry.gsensor_reader import read_gsensor
from ..telemetry.gsensor_reader import trim_gsensor_head
from ..telemetry.gsensor_reader import write_gsensor
from ..trip.trip import Trip
from .geocoding import load_or_reverse_geocode
from .gpx_writer import write_gpx
from .gsensor_graph_video import render_gsensor_graph_video
from .gsensor_video import render_gsensor_video
from .map_video import render_map_video
from .media import change_playback_speed
from .media import check_readable
from .media import concatenate_media
from .media import generate_silence
from .media import mux_audio_track
from .media import trim_media
from .media import trim_media_head
from .osm_roads import bounding_box_for_fixes
from .osm_roads import load_or_fetch_areas
from .osm_roads import load_or_fetch_roads
from .prebuffer import detect_prebuffer_seconds
from .trip_info import write_trip_info
from .trip_stats import compute_trip_stats
from .stitch import AUTO_LAYOUT
from .stitch import DEFAULT_GSENSOR_SIZE_PERCENT
from .stitch import DEFAULT_MIRROR_RADIUS_PERCENT
from .stitch import DEFAULT_MIRROR_SIZE_PERCENT
from .stitch import DEFAULT_MIRROR_PAN_X_PERCENT
from .stitch import DEFAULT_MIRROR_PAN_Y_PERCENT
from .stitch import DEFAULT_MIRROR_ZOOM_PERCENT
from .stitch import map_zoom_dimensions
from .stitch import pick_stitch_layout
from .stitch import stitch_cameras
from .subtitles import merge_lrc
from .subtitles import merge_srt
from .text import merge_text_assets
from .trip_log import TripLog

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
    trip_info: Path | None = None
    gsensor: Path | None = None
    map: Path | None = None
    map_zoom: Path | None = None
    gsensor_video: Path | None = None
    gsensor_graph_video: Path | None = None
    stitch: Path | None = None
    srt: Path | None = None
    lrc: Path | None = None
    text: tuple[Path, ...] = field(default_factory=tuple)
    warnings: tuple[str, ...] = field(default_factory=tuple)


def _content_timestamps(
    trip: Trip, *, include_parking: bool,
) -> tuple[datetime, datetime]:
    """Return the (start, end) timestamps that actually bound this
    trip's exported front/rear video content.

    `trip.start_timestamp`/`trip.end_timestamp` themselves when
    `include_parking` is True - every recording, Parking or not, ends
    up in the video, so the trip's own real detected boundary is
    exactly right. But when `include_parking` is False (the default),
    a leading or trailing Parking recording is left out of
    front.mp4/rear.mp4 entirely (see `_concatenate_asset()`'s own
    docstring) - so this returns the first/last *non*-Parking
    recording's own boundary instead. Christer, on a real export: "the
    name of the trip includes the start of the parking video, but in
    the [stitch].mp4 the parking is not included unless we specify
    --include-parking" - the folder name (and trip_info.txt's own
    "Started"/"Ended" lines) otherwise claimed a wider time range than
    what the exported video actually covers.

    Deliberately doesn't touch `trip.label` itself (used by bv-ls's
    --trips listing) or trip.gpx's own name (`write_gpx(...,
    name=trip.label)`) - both describe the trip as *detected*, and
    trip.gpx's own content already covers every recording's GPS data
    regardless of `include_parking` (GPS/g-sensor were never gated
    behind that flag to begin with, only video/audio content was), so
    trip.gpx's full-boundary name is already consistent with what's
    actually inside it.

    Falls back to the full trip boundary when every recording in the
    trip is Parking-mode - this trip's own video would be empty
    either way, so there's no narrower boundary that means anything
    (a vanishingly rare case given trip detection is built around
    driving/front-video recordings).
    """

    if include_parking:
        return trip.start_timestamp, trip.end_timestamp

    non_parking = [r for r in trip.recordings if not r.id.is_parking]
    if not non_parking:
        return trip.start_timestamp, trip.end_timestamp

    start = non_parking[0].id.timestamp
    last = non_parking[-1]
    duration_seconds = (
        trip.recording_duration(last) if trip.recording_duration else None
    )
    if duration_seconds is not None:
        end = last.id.timestamp + timedelta(seconds=duration_seconds)
    else:
        end = last.id.timestamp
    return start, end


def folder_name_for_trip(
    trip: Trip, prefix: str | None, *, include_parking: bool = True,
) -> str:
    """Return the subfolder name bv-export uses for a trip, e.g.
    'Holiday_trip_20260715_133458_20260715_141235' when prefix is
    'Holiday', or just 'trip_20260715_133458_20260715_141235' with no
    prefix.

    `include_parking` defaults to True (trip.label's own full-boundary
    behavior, unchanged) - pass the export's real `include_parking`
    value to get a folder name that matches what actually ends up in
    front.mp4/rear.mp4 instead (see `_content_timestamps()`'s own
    docstring for why these can otherwise disagree).
    """

    start, end = _content_timestamps(trip, include_parking=include_parking)
    label = f"trip_{start:%Y%m%d_%H%M%S}_{end:%Y%m%d_%H%M%S}"
    if prefix:
        return f"{prefix}_{label}"
    return label


# Below this, a front/rear duration difference is treated as
# floating-point/rounding noise in ffprobe's own duration reporting,
# not a real difference - not a "tolerance" in the sense of ignoring
# small real mismatches. Christer's original design used a 5-second
# tolerance (deliberately ignoring anything smaller as ordinary
# per-camera jitter), but that let small per-recording differences -
# each individually under 5s, none ever triggering a trim - add up
# across a whole trip: a real export came back with front/rear 8s out
# of sync overall despite no single recording differing by anywhere
# near 5s, and trip.log showing no trim had happened anywhere. His
# call once that surfaced: "Best is to trim every recording" - so
# every recording's own front/rear pair is now aligned exactly,
# every export, with no headroom for drift to accumulate through.
FRONT_REAR_DURATION_EPSILON_SECONDS = 0.01


def _repair_parking_sources(
    trip: Trip,
) -> dict[tuple[RecordingId, Asset], Path]:
    """Return a `{(recording id, FRONT/REAR): repaired path}` map for
    every Parking-mode recording in this trip whose own video file
    matches the broken-empty-audio-track container pattern documented
    in `generate/mp4_repair.py` - every Parking (P) recording fails
    ffprobe/ffmpeg outright with "contradictionary STSC and STCO" /
    "error reading header" otherwise, which is exactly what made a
    real export of Christer's silently drop 20230728_105305_PF.mp4/
    _PR.mp4 entirely (`_concatenate_asset()`'s own check_readable()
    step, further down this file, treats an unreadable source no
    differently than a genuinely corrupted one - "could not be read
    and was left out ... likely an incomplete recording").

    This is the exact same repair already wired into bv-web's archive
    browser (`web/app.py`'s `/archive/.../files/{filename}` route) -
    reused here rather than re-derived, and cached in the same shared
    `default_config_dir() / ".parking_repair_cache"`, so a recording
    already repaired for browser playback doesn't need repairing again
    for an export, and vice versa.

    Only ever touches FRONT/REAR - a Parking recording's own raw video
    file - never AUDIO (`.aac`, not an MP4 container at all;
    `_has_empty_audio_track()`'s box-walk isn't meaningful there and
    Parking recordings have no real audio to extract regardless, see
    bv-generate's own `--extract-audio` skip for Parking) and never a
    non-Parking recording's video, matching `mp4_repair.py`'s own
    narrow, confirmed-pattern-only scope.

    Returns an empty dict, doing no work at all, for a trip with no
    Parking recordings. A recording whose video doesn't match the
    known broken pattern is simply absent from the returned map -
    `load_or_repair_parking_video()` itself returns the source path
    unchanged in that case (see its own docstring), so there's nothing
    useful to override with.
    """

    cache_dir = default_config_dir() / ".parking_repair_cache"
    overrides: dict[tuple[RecordingId, Asset], Path] = {}

    for recording in trip.recordings:
        if not recording.id.is_parking:
            continue
        for asset in (Asset.FRONT, Asset.REAR):
            if not recording.has(asset):
                continue
            source = recording.file(asset).path
            repaired = load_or_repair_parking_video(source, cache_dir)
            if repaired != source:
                overrides[(recording.id, asset)] = repaired

    return overrides


def _apply_parking_speed(
    trip: Trip,
    work_dir: Path,
    warnings: list[str],
    log: TripLog | None,
    *,
    speed: float,
    include_parking: bool,
    source_overrides: dict[tuple[RecordingId, Asset], Path] | None = None,
) -> dict[tuple[RecordingId, Asset], Path]:
    """Return a `{(recording id, FRONT/REAR): sped-up path}` map, one
    entry per Parking-mode recording in this trip whose video got
    re-encoded at `speed`x via `change_playback_speed()`
    (`export/media.py`).

    Christer's own framing for why this exists: Parking-mode footage
    is motion-triggered and sparse, so a long real-world span often
    compresses into a short, slow-to-watch clip in the final export -
    `--parking-speed` lets it play back faster (or, for `speed < 1.0`,
    slower) without touching the rest of the trip's own pace.

    Does no work at all - not even probing a single file - and
    returns an empty dict immediately whenever `speed == 1.0` (the
    default) or `include_parking` is False: a Parking recording left
    out of the video entirely has nothing here worth re-encoding, and
    1.0x is a strict no-op by definition, so this stays fully inert
    for every trip that never asked for the flag. This matters beyond
    just "don't waste time" - every override this function *does*
    produce triggers a real re-encode (see `change_playback_speed()`'s
    own docstring for why a stream copy can't do this), the slowest
    kind of operation anywhere in this per-recording override chain,
    so callers who never opted in should never pay for it.

    `source_overrides`, if given, is consulted first for each
    recording/asset - the same `{(recording id, asset): path}` shape
    every other override producer in this chain uses (see
    `_repair_parking_sources()`/`_trim_prebuffers()`'s own docstrings),
    so this speeds up whatever the *repaired* Parking source actually
    is, not the camera's original (frequently unreadable, see
    `_repair_parking_sources()`) container.

    Deliberately runs *after* `_repair_parking_sources()` in
    `export_trip()`'s own pipeline (see that call site) for exactly
    that reason - `change_playback_speed()` re-encodes via ffmpeg,
    which needs a container ffprobe can actually open, and a raw
    unrepaired Parking recording fails that outright. Runs *before*
    `_align_front_rear_durations()`: FRONT and REAR are each sped up
    independently here by the same factor, so their already-close
    durations (see that function's own docstring for why they can
    still differ slightly) simply scale down together rather than
    diverging - alignment then trims whichever of the two sped-up
    files is longer, exactly as it already does for any other
    recording, with no special case needed for this one.

    The returned map feeds into the same `duration_overrides` dict
    `_recording_video_offsets()`/`_concatenate_asset()` already
    consume - once a Parking recording's own FRONT/REAR here is
    registered under its sped-up path, every downstream consumer of
    real video duration (map.mp4/gsensor.mp4 sync via
    `_video_position_breakpoints()`, subtitle rebasing, and
    `_pad_missing_audio_with_silence()`'s own silence-fill length)
    automatically sees the *sped-up* duration instead of the
    original, with zero code changes of their own needed - this is
    the exact same architecture `_repair_parking_sources()`/
    `_trim_prebuffers()`/`_align_front_rear_durations()` already
    established for Parking's other duration-changing operations, see
    `_recording_video_offsets()`'s own docstring for the full
    reasoning behind why probing each override's own real file,
    rather than trusting any wall-clock assumption, is what keeps
    everything in sync.

    Also drops `Asset.AUDIO` (if present) from any recording actually
    sped up here, mutating `recording.assets` in place - the same
    self-healing convention `_ensure_recording_audio()`/
    `_pad_missing_audio_with_silence()` already use elsewhere in this
    module. Most Parking recordings have no audio at all, in which
    case this is a no-op; but a recording that *does* carry real audio
    (typically because it doubles as an Event/Manual-triggered clip)
    would otherwise leave that audio at its original, now-mismatched
    length in the trip's `audio.aac` - `change_playback_speed()` only
    speeds up video (`-an`, see its own docstring), so a Parking
    recording's own real audio is never itself sped up to match.
    Discovered the hard way: muxing a shorter (sped-up) front.mp4
    against a longer, unsped audio.aac via `mux_audio_track()` (no
    `-shortest`) doesn't trim or desync gracefully - the muxed
    container's own reported duration follows the *longer* stream, so
    front.mp4 comes back claiming the audio's stale, unsped length
    even though its video stream itself is genuinely shorter.
    Dropping the audio here instead lets `_pad_missing_audio_with_
    silence()` (which runs later, from this recording's own post-
    speed-change `video_offsets`) fill the gap with correctly *sped-
    up*-duration silence, the same treatment a Parking recording with
    no audio at all already gets.
    """

    if speed == 1.0 or not include_parking:
        return {}

    overrides: dict[tuple[RecordingId, Asset], Path] = {}

    for recording in trip.recordings:
        if not recording.id.is_parking:
            continue

        sped_up = False
        for asset in (Asset.FRONT, Asset.REAR):
            if not recording.has(asset):
                continue

            override_path = (
                source_overrides.get((recording.id, asset))
                if source_overrides is not None
                else None
            )
            source = override_path or recording.file(asset).path

            destination = (
                work_dir
                / f"{recording.id}_{asset.name.lower()}_speed{speed:g}{source.suffix}"
            )
            try:
                change_playback_speed(source, destination, speed)
            except MediaToolError as exc:
                message = (
                    f"{recording.id}: could not apply --parking-speed "
                    f"{speed:g}x to {asset.name.lower()}: {exc} - kept "
                    "at its original speed"
                )
                warnings.append(message)
                if log is not None:
                    log.warning(message)
                continue

            overrides[(recording.id, asset)] = destination
            sped_up = True

        if sped_up:
            recording.assets.pop(Asset.AUDIO, None)

    return overrides


def _trim_prebuffers(
    trip: Trip,
    work_dir: Path,
    warnings: list[str],
    log: TripLog | None,
) -> tuple[
    dict[tuple[RecordingId, Asset], Path],
    dict[RecordingId, tuple[GSensorSample, ...]],
    dict[RecordingId, float],
]:
    """Detect and trim a pre-record-buffer overlap off the front of
    every Event/Manual recording in the trip that isn't the trip's own
    first recording - see export/prebuffer.py's module docstring for
    what this overlap is and why it exists at all.

    Christer: "A trip that starts with an E or M mode should not be
    trimmed" - a trip's first recording has nothing before it *in this
    trip* to compare against, so there's no real reference to detect
    an overlap from; trimming without one would be guessing rather
    than measuring. (The recording immediately preceding it in the
    full archive, if any, is deliberately not reached for either -
    scoped to the trip's own recordings, the same boundary
    TripBuilder's own gap-based grouping already draws.)

    For every other Event/Manual recording, compares it against
    whichever recording immediately precedes it *in trip order* -
    any kind, not just Normal, and regardless of whether that
    recording will itself end up in the final concatenated video
    (e.g. a leading Parking recording being left out via
    `include_parking=False` - detection only ever reads its g-sensor
    data, never its video, so that exclusion doesn't matter here).
    Both recordings need a readable GSENSOR asset or the pair is
    skipped outright - detect_prebuffer_seconds() itself already
    refuses to guess without real data to compare (see its own
    docstring), this is just the "the data isn't even there" case one
    level up.

    detect_prebuffer_seconds() returning a confident offset (not None)
    is applied identically to every asset this recording actually has
    - FRONT, REAR, AUDIO (a plain stream-copy head trim via
    trim_media_head() for all three - see WORKING_CONTEXT.md for why
    AUDIO needs the same trim, not just video: it's concatenated
    independently of FRONT/REAR with no duration compensation of its
    own, so leaving it untouched would push audio.aac out of sync with
    the video from this recording onward) and GSENSOR (its own sample-
    level trim via trim_gsensor_head(), returned separately as
    `gsensor_overrides` since _merge_gsensor() works from in-memory
    samples, never a file on disk - no reason to round-trip through a
    temp .3gf file just to read it straight back). GPS is deliberately
    left untouched: gps_reader.py's fixes already carry real wall-
    clock timestamps rather than being positioned relative to the
    recording's own video (see _merge_gps()), so they're unaffected by
    wherever the video itself gets cut.

    Runs *before* _align_front_rear_durations() in export_trip()'s own
    pipeline (see that call site) - trimming the duplicate content off
    the front first means the alignment trim, which cuts whichever of
    FRONT/REAR's tail is longer, is comparing two already-de-
    duplicated files instead of fighting a mismatch this step itself
    would otherwise have introduced (trimming the same P seconds off
    both FRONT and REAR here keeps their own difference unchanged, so
    in practice this rarely changes what alignment finds - but the
    ordering matters in principle regardless).

    Returns `(media_overrides, gsensor_overrides, prebuffer_offsets)`:
    `media_overrides` is shaped exactly like
    _align_front_rear_durations()'s own return value (`{(recording id,
    asset): trimmed path}`, for FRONT/REAR/AUDIO), meant to be merged
    with that function's own result and fed into
    _concatenate_asset()/_recording_video_offsets(); the caller should
    also pass this same dict to _align_front_rear_durations() as
    `source_overrides` so it trims from the already-prebuffer-trimmed
    files, not the camera's original ones. `gsensor_overrides`
    (`{recording id: trimmed samples}`) is meant for _merge_gsensor()'s
    own `gsensor_overrides` parameter.

    `prebuffer_offsets` (`{recording id: detected offset_seconds}`) is
    meant for _video_position_breakpoints()'s own `prebuffer_offsets`
    parameter: a trimmed recording's video frame 0 no longer lines up
    with its own ID timestamp - it's been moved forward in wall-clock
    terms by however much got cut off the front - and
    _video_position_breakpoints() needs to know that offset to keep
    map.mp4/subtitle timing lined up with the trimmed video rather than
    the untrimmed original. Only present for a recording that was
    actually trimmed - see that function's own docstring.
    """

    media_overrides: dict[tuple[RecordingId, Asset], Path] = {}
    gsensor_overrides: dict[RecordingId, tuple[GSensorSample, ...]] = {}
    prebuffer_offsets: dict[RecordingId, float] = {}

    recordings = trip.recordings

    for index in range(1, len(recordings)):
        current = recordings[index]
        if not (current.id.is_event or current.id.is_manual):
            continue

        preceding = recordings[index - 1]

        preceding_gsensor = preceding.file(Asset.GSENSOR)
        current_gsensor = current.file(Asset.GSENSOR)
        if preceding_gsensor is None or current_gsensor is None:
            continue

        try:
            preceding_samples = read_gsensor(preceding_gsensor.path)
            current_samples = read_gsensor(current_gsensor.path)
        except MediaToolError:
            continue

        offset_seconds = detect_prebuffer_seconds(preceding_samples, current_samples)
        if offset_seconds is None:
            continue

        trimmed_assets: list[str] = []
        for asset in (Asset.FRONT, Asset.REAR, Asset.AUDIO):
            asset_file = current.file(asset)
            if asset_file is None:
                continue

            trimmed_path = (
                work_dir
                / f"{current.id}_{asset.name.lower()}_prebuffer{asset_file.path.suffix}"
            )
            try:
                trim_media_head(asset_file.path, trimmed_path, offset_seconds)
            except MediaToolError as exc:
                message = (
                    f"{current.id}: detected a {offset_seconds:.2f}s "
                    f"pre-record buffer overlap with {preceding.id} but "
                    f"could not trim {asset.name.lower()}: {exc}"
                )
                warnings.append(message)
                if log is not None:
                    log.warning(message)
                continue

            media_overrides[(current.id, asset)] = trimmed_path
            trimmed_assets.append(asset.name.lower())

        gsensor_overrides[current.id] = trim_gsensor_head(
            current_samples, offset_seconds
        )
        trimmed_assets.append("gsensor")
        prebuffer_offsets[current.id] = offset_seconds

        message = (
            f"{current.id}: detected a {offset_seconds:.2f}s pre-record "
            f"buffer overlap with the preceding recording {preceding.id} "
            f"- trimmed {', '.join(trimmed_assets)}"
        )
        warnings.append(message)
        if log is not None:
            log.warning(message)

    return media_overrides, gsensor_overrides, prebuffer_offsets


def _align_front_rear_durations(
    trip: Trip,
    work_dir: Path,
    warnings: list[str],
    log: TripLog | None,
    *,
    include_parking: bool,
    epsilon_seconds: float = FRONT_REAR_DURATION_EPSILON_SECONDS,
    source_overrides: dict[tuple[RecordingId, Asset], Path] | None = None,
) -> dict[tuple[RecordingId, Asset], Path]:
    """Trim every recording's own front/rear video pair to exactly
    match each other, whenever they differ at all (beyond
    `epsilon_seconds`' worth of floating-point noise) - always the
    *longer* side trimmed down to the shorter one, never the other
    way around. Padding the shorter side would mean splicing
    synthetically generated frames onto the end of a real camera
    file, the exact class of corruption the parking-placeholder
    feature was removed over this same session (see
    WORKING_CONTEXT.md) - a plain tail trim only ever removes real
    bytes from a file that's already one continuous, valid encoder
    session, so it carries none of that risk (see media.py's
    trim_media() docstring).

    Originally gated behind a 5-second tolerance (skip anything
    smaller as ordinary per-camera jitter) - found for real on
    Christer's own archive that this let per-recording drift, each
    individually under 5s, accumulate across a whole trip with
    nothing ever triggering a trim: one export came back 8s out of
    sync overall with trip.log showing no trim had fired anywhere.
    Trimming every recording's pair exactly, every time, closes that
    gap - there's no accumulation window left for drift to hide in.
    The first real case that motivated this feature at all was more
    dramatic still: a corrupted download left one recording's front
    video at 34.9s against a normal 179.8s rear, a 144.9s gap from a
    single file.

    Returns a `{(recording id, asset): trimmed path}` map -
    `_concatenate_asset()` substitutes the trimmed file in place of
    the recording's own real one for whichever side (FRONT or REAR)
    was too long; the shorter side is left completely untouched.

    A recording that will be dropped anyway (Parking-mode, when
    `include_parking` is False) is skipped without probing either
    side - no point trimming footage that never reaches
    front.mp4/rear.mp4 regardless. A recording missing either side,
    or one ffprobe can't read at all, is also left alone - this
    function only ever acts on two files it can both successfully
    probe; anything else surfaces through `_concatenate_asset()`'s
    own existing per-file error handling instead, same as before this
    existed.

    `source_overrides`, if given, is `_trim_prebuffers()`'s own
    `{(recording id, asset): trimmed path}` return value - whenever a
    (recording, FRONT/REAR) pair appears there, this function probes
    and (if needed) trims from that already-prebuffer-trimmed file
    instead of the recording's own real one, so a further trim here
    compounds onto the earlier one rather than starting over from the
    untrimmed original. See export_trip()'s own call site for why
    _trim_prebuffers() runs first.
    """

    overrides: dict[tuple[RecordingId, Asset], Path] = {}

    for recording in trip.recordings:
        if recording.id.is_parking and not include_parking:
            continue
        if not recording.has(Asset.FRONT) or not recording.has(Asset.REAR):
            continue

        front_path = (
            source_overrides.get((recording.id, Asset.FRONT))
            if source_overrides is not None
            else None
        ) or recording.file(Asset.FRONT).path
        rear_path = (
            source_overrides.get((recording.id, Asset.REAR))
            if source_overrides is not None
            else None
        ) or recording.file(Asset.REAR).path

        try:
            front_duration = probe(front_path).duration_seconds
            rear_duration = probe(rear_path).duration_seconds
        except MediaToolError:
            continue

        diff = front_duration - rear_duration
        if abs(diff) <= epsilon_seconds:
            continue

        if diff > 0:
            longer_asset, longer_path, longer_label = Asset.FRONT, front_path, "front"
            shorter_duration, shorter_label = rear_duration, "rear"
        else:
            longer_asset, longer_path, longer_label = Asset.REAR, rear_path, "rear"
            shorter_duration, shorter_label = front_duration, "front"

        trimmed_path = work_dir / f"{recording.id}_{longer_label}_aligned.mp4"
        try:
            trim_media(longer_path, trimmed_path, shorter_duration)
        except MediaToolError as exc:
            message = (
                f"{recording.id}: front/rear duration differs by "
                f"{abs(diff):.2f}s but could not be aligned: {exc}"
            )
            warnings.append(message)
            if log is not None:
                log.warning(message)
            continue

        overrides[(recording.id, longer_asset)] = trimmed_path

        # Logged for every trim regardless of size, not just the
        # dramatic ones - Christer, once small per-recording
        # differences turned out to add up across a whole trip
        # without any single one being large enough to look
        # suspicious on its own: "Log every trim, any size."
        #
        # Not a warning: a front/rear duration difference here is the
        # expected, routine case (every camera's own clock/frame timer
        # drifts a little independently) and this trim handles it
        # completely - nothing is degraded or left unresolved, unlike
        # the "could not be aligned" branch above, which stays a real
        # warning since the mismatch there is left unfixed. Christer:
        # "front/rear duration differs shouldn't be a warning, just an
        # info" - so this is only a trip.log step (via log.step(), not
        # log.warning()) and never added to `warnings`, so it neither
        # prints as a CLI "warning:" line (see bv_export.py's own
        # `result.warnings` loop) nor counts toward the export's
        # warning total.
        message = (
            f"{recording.id}: front/rear duration differs by "
            f"{abs(diff):.2f}s (front={front_duration:.2f}s, "
            f"rear={rear_duration:.2f}s) - trimmed {longer_label} to "
            f"match {shorter_label}"
        )
        if log is not None:
            log.step(message)

    return overrides


def _ensure_recording_audio(
    trip: Trip,
    warnings: list[str],
    log: TripLog | None,
    *,
    debug: bool = False,
) -> None:
    """Self-heal a missing `<recording>.aac` for any recording in this
    trip that doesn't already have one, extracting it from that
    recording's own video - the same thing `bv-generate
    --extract-audio` would have produced, had it been run first.

    Mirrors two existing self-healing conventions in this module/
    package rather than introducing a third shape: `--stitch-gsensor`
    already self-renders a missing gsensor.mp4 on demand (see
    export_trip()'s own comment above its gsensor block), and
    `load_or_compute_duration()` (generate/media.py) already self-
    heals a missing `.duration.txt`. Christer, asked whether bv-export
    should just always extract audio itself rather than requiring a
    separate `bv-generate --extract-audio` pass first: "Or should it
    be extracted by bv-export?" - yes, following the same "compose
    only what's already there, but make sure it's there" shape those
    two already established, rather than adding a new flag.

    Mutates each healed recording's `assets[Asset.AUDIO]` in place, so
    `_concatenate_asset(trip, Asset.AUDIO, ...)` right after this call
    picks the new file up exactly like a pre-existing one - no
    separate wiring needed there.

    Deliberately scoped to just this trip's own recordings (not the
    whole archive) - same scoping `load_or_compute_duration()`'s own
    self-healing settled on for duration (see task #156's "Scope
    duration self-healing to exported trip(s) only").

    Parking recordings are always skipped, regardless of
    `include_parking` - matching `bv-generate`'s own
    `_do_extract_audio()`, which already refuses to extract audio for
    them (parking footage is typically silent/uninteresting, and
    Parking recordings never got this treatment before). This is
    independent of (and stricter than) `_concatenate_asset()`'s own
    `include_parking` gate, which only decides whether an *already-
    extracted* Parking recording's audio is used in the trip - it
    can't be asked to self-heal one that this function refuses to
    create in the first place.

    A recording missing both front and rear video has nothing to
    extract from and is silently skipped (nothing downstream expects
    audio from a recording with no video either). A recording whose
    video genuinely has no audio stream at all is also silently
    skipped, checked via `probe_audio_codec()` before attempting
    anything - this is expected and common enough (not every
    recording/camera mode has audio) that it isn't a failure worth
    warning about; a warning is reserved for a real extraction failure
    (a corrupted source, ffmpeg itself failing) on a recording that
    *does* have an audio stream. Either way, the trip's `audio.aac`
    just won't include this one recording - the same "leave it out"
    behavior `_concatenate_asset()` already has for a recording
    that's missing any other asset.
    """

    for recording in trip.recordings:
        if recording.has(Asset.AUDIO) or recording.id.is_parking:
            continue

        source_file = select_source(recording)
        if source_file is None:
            continue

        try:
            has_audio_stream = probe_audio_codec(source_file.path) is not None
        except MediaToolError:
            # Can't even probe it - let extract_audio() below attempt
            # the real thing and report on whatever it runs into,
            # rather than silently giving up on a probe-only failure.
            has_audio_stream = True

        if not has_audio_stream:
            continue

        destination = source_file.path.parent / f"{recording.id}.aac"

        try:
            extract_audio(source_file.path, destination)
        except MediaToolError as exc:
            message = f"{recording.id}: could not self-heal missing audio - {exc}"
            warnings.append(message)
            if log is not None:
                log.warning(message)
            continue

        recording.assets[Asset.AUDIO] = AssetFile(Asset.AUDIO, destination)
        if debug:
            print(
                f"bv-export: {recording.id}: extracted {destination.name} "
                "(self-healed, missing from archive)",
                file=sys.stderr,
            )


def _recording_video_offsets(
    trip: Trip,
    *,
    include_parking: bool,
    duration_overrides: dict[tuple[RecordingId, Asset], Path] | None = None,
) -> tuple[dict[RecordingId, float], float | None]:
    """Return each recording's own real start position, in seconds,
    within the concatenated video `_concatenate_asset()` actually
    produces - the sum of every earlier included recording's own
    (possibly front/rear-trimmed) duration, NOT the gap between
    recording ID timestamps - alongside the trip's own real total
    video duration (the same running sum, carried one recording
    further, or `None` if no recording's video could be probed at
    all).

    That second value exists because Christer hit a real case where
    probing the *concatenated* front.mp4 for its own total duration -
    what this function's callers used to do separately, on the
    reasonable-sounding assumption that "ask the actual output file"
    beats re-deriving the number from its ingredients - was wrong by
    roughly 16x. A trip whose Parking recording (repaired via
    `_repair_parking_sources()`) got `-c copy` concatenated onto the
    front side only produced a front.mp4 whose own container metadata
    reported `avg_frame_rate=47375/25573` (~1.85fps) and
    `duration=3682s`, while the *same* underlying footage on the rear
    side - 6822 vs 6829 real frames, matching almost exactly - reported
    `avg_frame_rate=3414500/115109` (~29.66fps) and `duration=230s` for
    what both real frame counts agree is the same real ~230s of
    footage. ffmpeg's concat demuxer's own `-c copy` stream copy
    doesn't harmonize timescales across inputs that don't share one
    (plausibly the Parking recording's own repaired container, built
    by `mp4_repair.py` from a from-scratch rewritten `moov`, carrying a
    different timescale than the camera's normal recording pipeline
    produces) - the real packets and real frame count survive the
    concatenation fine, but the container-level *summary*
    duration/average-frame-rate metadata ffprobe reports for the whole
    file can end up describing something that never actually happened.
    Summing each source's own individually-probed duration - exactly
    what this function was already doing internally to build its
    offsets, just not exposing the running total - sidesteps the bad
    concatenated-container metadata entirely by never reading it, and
    was independently confirmed correct against Christer's real files
    (both front and rear repaired Parking sources probed identically:
    1530 frames, ~30.3fps, 50.457s each).

    This matters because `_concatenate_asset()` builds front.mp4/
    rear.mp4 by gluing each included recording's own video file
    straight onto the end of the previous one, with zero awareness of
    wall-clock time - a recording's real position in the video is
    "wherever the earlier recordings' own durations happen to add up
    to," full stop. That only agrees with "however many seconds after
    the trip's first recording its ID timestamp claims to be" when
    every consecutive pair has exactly zero gap/overlap AND no front/
    rear trim ever fires - which in practice is close to never: two
    recordings from the same camera, "back to back" by ID, commonly
    overlap or gap by several seconds (a Manual/Event recording's own
    pre-record buffer is one concrete, confirmed cause - see
    WORKING_CONTEXT.md), and `_align_front_rear_durations()` trims
    real content off the end of nearly every recording's longer side.
    Positioning g-sensor samples or GPS fixes by ID-timestamp gap
    instead of this real video position is exactly what made
    gsensor.mp4/map.mp4 drift out of sync with the actual footage on a
    real trip - confirmed directly: recording gap-by-ID-timestamp said
    32s, but the previous recording's own (rear-trimmed) video was
    36.73s long, a ~4.7s position error before any prebuffer effect
    even entered into it.

    Recordings left out of the video entirely (Parking-mode, when
    `include_parking` is False; or a recording whose FRONT/REAR can't
    be probed at all) are left out of the returned map too, mirroring
    `_concatenate_asset()`'s own inclusion rule as closely as
    practical without duplicating its full readable-source handling -
    a caller that finds a recording missing from this map has no real
    video position to rebase against and should fall back to its own
    best-effort behavior (see `_merge_gsensor()`).

    Uses FRONT's own (overridden/trimmed, if `duration_overrides` has
    an entry for it) duration as each recording's video-timeline
    length, falling back to REAR if a recording has no FRONT - the two
    should already match almost exactly post-
    `_align_front_rear_durations()`, so which one is probed only
    matters for a recording missing one side entirely.
    """

    offsets: dict[RecordingId, float] = {}
    elapsed = 0.0

    for recording in trip.recordings:
        if recording.id.is_parking and not include_parking:
            continue

        if recording.has(Asset.FRONT):
            asset = Asset.FRONT
        elif recording.has(Asset.REAR):
            asset = Asset.REAR
        else:
            continue

        override_path = (
            duration_overrides.get((recording.id, asset))
            if duration_overrides is not None
            else None
        )
        path = override_path or recording.file(asset).path

        try:
            duration = probe(path).duration_seconds
        except MediaToolError:
            continue

        offsets[recording.id] = elapsed
        elapsed += duration

    return offsets, (elapsed if offsets else None)


def _video_position_breakpoints(
    trip: Trip,
    video_offsets: dict[RecordingId, float],
    prebuffer_offsets: dict[RecordingId, float] | None = None,
) -> tuple[tuple[float, datetime], ...]:
    """Return `video_offsets` reshaped into a (video_position_seconds,
    wallclock_start) sequence, sorted by video position - what
    map_video.py's render_map_video() (`recording_breakpoints` param)
    needs to convert a video-elapsed position back into the real
    wall-clock instant it corresponds to, piecewise per recording
    instead of one global "trip start" anchor. See
    _recording_video_offsets()'s own docstring for why the two only
    agree by coincidence.

    `prebuffer_offsets`, if given, is _trim_prebuffers()'s own
    `{recording id: detected offset_seconds}` return value. A
    recording's own ID timestamp normally *is* the wall-clock instant
    its video's frame 0 was recorded at - but not anymore for a
    recording whose head got prebuffer-trimmed: frame 0 has been moved
    forward by however many seconds of duplicate content got cut, so
    the real wall-clock instant it now starts at is `id.timestamp +
    offset_seconds`, not `id.timestamp` on its own. Confirmed on a real
    export: without this adjustment, map.mp4's displayed position/
    timestamp for a trimmed recording visibly lagged the video's own
    burned-in camera timestamp by close to the trimmed amount - Christer
    caught this by comparing the two on-screen. Recordings absent from
    `prebuffer_offsets` (the overwhelming majority - only a trimmed
    Event/Manual recording is ever in it) are unaffected, same as
    before this parameter existed.
    """

    breakpoints = [
        (
            video_offsets[recording.id],
            recording.id.timestamp
            + timedelta(
                seconds=(
                    prebuffer_offsets.get(recording.id, 0.0)
                    if prebuffer_offsets is not None
                    else 0.0
                )
            ),
        )
        for recording in trip.recordings
        if recording.id in video_offsets
    ]
    return tuple(sorted(breakpoints, key=lambda item: item[0]))


def _pad_missing_audio_with_silence(
    trip: Trip,
    video_offsets: dict[RecordingId, float],
    total_video_duration_seconds: float | None,
    silence_dir: Path,
    warnings: list[str],
    log: TripLog | None,
    *,
    debug: bool = False,
) -> None:
    """Generate a silent `.aac` for any recording that's part of the
    actual video timeline (present in `video_offsets`) but has no
    `Asset.AUDIO` of its own - most commonly a Parking recording (see
    `_ensure_recording_audio()`'s own docstring: it always skips
    extracting audio for Parking recordings, on purpose), but also any
    other recording whose own video genuinely has no audio stream.

    Mutates each padded recording's `assets[Asset.AUDIO]` in place,
    the same self-healing convention `_ensure_recording_audio()`
    already established - `_concatenate_asset(trip, Asset.AUDIO, ...)`
    right after this call picks the new silent file up automatically,
    no separate wiring needed.

    Why this matters: front.mp4's video keeps every included
    recording's own real duration - Parking included, once
    concatenate_media()'s video_only fix stopped dropping the segment
    itself, only its audio. `_concatenate_asset(trip, Asset.AUDIO,
    ...)` builds audio.aac by simply leaving out any recording with no
    `Asset.AUDIO` - correct for audio.aac's own standalone timeline,
    but the moment it's muxed straight onto front.mp4 (`mux_audio_track()`)
    or into stitch.mp4 (`stitch.py`), a skipped recording's real video
    seconds have nothing under them - audio.aac is shorter than the
    video by exactly that recording's duration, and everything *after*
    the gap plays back against audio recorded for an entirely
    different moment in the trip. Christer, on a real export: "audio
    it not in sync width front, its synching with the parking file."
    Filling the gap with real silence of the *exact* right duration
    keeps audio.aac the same length as the video, so everything after
    it stays lined up - the same "pad rather than leave a hole" idea
    `_pad_to_duration()` (subtitles.py) already uses for a trip whose
    transcript runs out early, just applied to keeping two *tracks* in
    sync instead of one track reaching the video's own length.

    A recording's own duration is derived from `video_offsets` itself
    (the difference between its own start position and the next
    recording's, or the trip's own total for the last one) rather than
    reprobed - `video_offsets` already reflects the exact
    (possibly front/rear-trimmed) length each recording actually
    contributes to the concatenated video, the same number
    `_concatenate_asset()` itself built the video from.

    The generated silence matches an existing real `.aac`'s own
    sample rate/channel layout (probed via `probe_audio_format()`),
    not a hardcoded default - ffmpeg's concat demuxer doesn't
    harmonize mismatched audio parameters across a `-c copy` list any
    more than it does for video (see `concatenate_media()`'s own
    docstring for the video-side version of this exact class of bug).
    If no recording in the trip has any real audio at all, there is no
    reference format to match and nothing downstream would use the
    silence for anyway (`_concatenate_asset(trip, Asset.AUDIO, ...)`
    returns None when its sources are empty, same as always) - this
    function is a no-op in that case.
    """

    reference_format: tuple[int, int] | None = None
    for recording in trip.recordings:
        audio_file = recording.file(Asset.AUDIO)
        if audio_file is None:
            continue
        try:
            reference_format = probe_audio_format(audio_file.path)
        except MediaToolError:
            reference_format = None
        if reference_format is not None:
            break

    if reference_format is None:
        return

    sample_rate, channels = reference_format

    ordered = sorted(
        (
            (recording, video_offsets[recording.id])
            for recording in trip.recordings
            if recording.id in video_offsets
        ),
        key=lambda pair: pair[1],
    )

    for index, (recording, start_offset) in enumerate(ordered):
        if recording.has(Asset.AUDIO):
            continue

        if index + 1 < len(ordered):
            end_offset = ordered[index + 1][1]
        elif total_video_duration_seconds is not None:
            end_offset = total_video_duration_seconds
        else:
            continue

        duration_seconds = end_offset - start_offset
        if duration_seconds <= 0:
            continue

        destination = silence_dir / f"{recording.id}_silence.aac"
        try:
            generate_silence(
                destination,
                duration_seconds,
                sample_rate=sample_rate,
                channels=channels,
            )
        except MediaToolError as exc:
            message = (
                f"{recording.id}: could not generate silent audio to keep "
                f"audio.aac in sync ({exc}) - audio may drift out of sync "
                "from here on"
            )
            warnings.append(message)
            if log is not None:
                log.warning(message)
            continue

        recording.assets[Asset.AUDIO] = AssetFile(Asset.AUDIO, destination)
        if debug:
            print(
                f"bv-export: {recording.id}: generated "
                f"{destination.name} ({duration_seconds:.1f}s silence, "
                "keeps audio.aac in sync with the video)",
                file=sys.stderr,
            )


def _concatenate_asset(
    trip: Trip,
    asset: Asset,
    filename: str,
    destination: Path,
    warnings: list[str],
    log: TripLog | None = None,
    *,
    include_parking: bool = True,
    duration_overrides: dict[tuple[RecordingId, Asset], Path] | None = None,
    video_only: bool = False,
) -> Path | None:
    """Build `sources` in trip order, leaving out any Parking-mode
    recording entirely - wherever it falls in the trip - whenever
    `include_parking` is False. `include_parking=True` reproduces the
    original behavior: every recording's own file, unconditionally.

    A synthetic transition clip used to stand in for a *mid-trip*
    Parking recording here (still included as-is at the very start or
    end of a trip) - dropped in favor of a plain, uniform "just leave
    it out" for every position, after a real-world HEVC-camera export
    from Christer showed the placeholder approach corrupting
    front.mp4/rear.mp4 from the splice point onward. Root cause: MP4's
    "hvc1"/"avc1" tagging declares a track's SPS/PPS/VPS parameter
    sets once, at the container level - any two files from *separate*
    encoder sessions (the dashcam's own hardware encoder vs. anything
    bv-export renders itself) generally don't share compatible
    parameter sets, so ffmpeg's concat demuxer (concatenate_media()'s
    stream copy, not a re-encode) can mux them together with no error
    at export time, but no real decoder can parse the result past that
    point - confirmed directly against Christer's own archive:
    "Invalid NAL unit size", "No ref lists in the SPS", the picture
    corrupted/frozen while the separately-muxed audio track kept
    playing. A full re-encode would sidestep this, but at real
    trip-length/4K cost for comparatively little benefit - Christer:
    "we don't want time consuming stuff if it not gives us something
    great back. Just skip it altogether" - so a Parking recording is
    now simply left out, the same as if it never had this asset in the
    first place, with no substitute of any kind.

    `duration_overrides`, if given, is the `{(recording id, asset):
    trimmed path}` map `_align_front_rear_durations()` builds - for
    any recording/asset pair present there, the trimmed file is
    spliced in instead of the recording's own real one, keeping this
    asset's total duration in step with its counterpart (FRONT with
    REAR) even when the two sides' own real files ran different
    lengths. See that function's own docstring for why.

    `video_only`, forwarded straight to `concatenate_media()`, is what
    `export_trip()` passes for FRONT specifically - see that
    function's own docstring for the real corruption it avoids
    (mixing a video-only repaired Parking segment in among ordinary
    video+audio FRONT recordings). `export_trip()` remuxes the trip's
    own `audio.aac` back into the result afterward, once both are
    ready.

    Every source is probed before being handed to ffmpeg's concat
    demuxer - a single unreadable file (most often one whose moov atom
    never got written, because the camera lost power or was unplugged
    mid-recording) otherwise makes the *entire* concat fail in one
    shot via `concatenate_media()`, discarding every other recording's
    otherwise-good footage for this asset along with it. Christer, hit
    exactly this on a real export: "ffmpeg concat failed for rear.mp4
    ... moov atom not found ... 20260731_173318_NR.mp4" - one corrupt
    rear file took rear.mp4 down completely even though only one of
    several recordings in the trip was actually bad. An unreadable
    source is now left out, with a warning, the same "just leave it
    out" treatment already used for excluded Parking recordings above
    - the corrupted footage is genuinely gone either way; the fix here
    is only to stop it from taking healthy footage down with it.
    """

    sources: list[Path] = []

    for recording in trip.recordings:
        if not recording.has(asset):
            continue

        if recording.id.is_parking and not include_parking:
            continue

        override_path = (
            duration_overrides.get((recording.id, asset))
            if duration_overrides is not None
            else None
        )
        sources.append(override_path or recording.file(asset).path)

    if not sources:
        if log is not None:
            log.step(f"no source recordings for {filename} - skipped")
        return None

    readable_sources: list[Path] = []
    for source in sources:
        try:
            check_readable(source)
        except MediaToolError as exc:
            message = (
                f"{filename}: {source.name} could not be read and was "
                f"left out ({exc}) - likely an incomplete recording "
                f"(e.g. the camera lost power mid-write)"
            )
            warnings.append(message)
            if log is not None:
                log.warning(message)
            continue
        readable_sources.append(source)

    if not readable_sources:
        if log is not None:
            log.step(f"no readable source recordings for {filename} - skipped")
        return None

    out = destination / filename
    try:
        concatenate_media(readable_sources, out, video_only=video_only)
    except MediaToolError as exc:
        warnings.append(str(exc))
        if log is not None:
            log.warning(str(exc))
        return None

    if log is not None:
        log.step(f"concatenated {filename} from {len(readable_sources)} recording(s)")

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


def _merge_gsensor(
    trip: Trip,
    video_offsets: dict[RecordingId, float] | None = None,
    gsensor_overrides: dict[RecordingId, tuple[GSensorSample, ...]] | None = None,
) -> tuple[GSensorSample, ...]:
    """Merge every recording's g-sensor samples into one trip-relative
    stream, positioned to match the actual concatenated video wherever
    possible: a recording present in `video_offsets` (see
    _recording_video_offsets()) is rebased by its own real position in
    the video; a recording without one (no video at all for this trip,
    or its FRONT/REAR couldn't be probed) falls back to the old
    "rebase by how far its ID timestamp is after the trip's first
    recording" behavior - still useful for a GPS/g-sensor-only "trip"
    with no video to align against at all, just not exact whenever a
    real video does exist and recordings gap/overlap/trim relative to
    their own nominal ID timestamps (the near-universal case - see
    _recording_video_offsets()'s own docstring for why positioning by
    ID timestamp alone drifted trip.3gf/gsensor.mp4 out of sync with
    real footage).

    `gsensor_overrides`, if given, is _trim_prebuffers()'s own
    `{recording id: trimmed samples}` return value - a recording
    present there gets those already-prebuffer-trimmed samples used
    directly, in place of a fresh read_gsensor() off its own real
    file, so its g-sensor data stays in step with whatever FRONT/REAR
    trim the same recording got there. Unlike `duration_overrides`
    elsewhere in this module, this is plain in-memory data, not a
    path - _trim_prebuffers() never writes a trimmed .3gf back out to
    disk, since nothing here needs it as a real file the way ffmpeg
    needs FRONT/REAR/AUDIO to be."""

    samples: list[GSensorSample] = []
    trip_start = trip.start_timestamp

    for recording in trip:
        override_samples = (
            gsensor_overrides.get(recording.id)
            if gsensor_overrides is not None
            else None
        )
        if override_samples is not None:
            recording_samples = override_samples
        else:
            gsensor_file = recording.file(Asset.GSENSOR)
            if gsensor_file is None:
                continue
            try:
                recording_samples = read_gsensor(gsensor_file.path)
            except MediaToolError:
                continue

        if video_offsets is not None and recording.id in video_offsets:
            rebase = timedelta(seconds=video_offsets[recording.id])
        else:
            rebase = recording.id.timestamp - trip_start

        samples.extend(
            GSensorSample(offset=rebase + sample.offset, x=sample.x, y=sample.y, z=sample.z)
            for sample in recording_samples
        )

    return tuple(sorted(samples, key=lambda sample: sample.offset))


def _load_trip_roads(
    fixes: tuple,
    map_cache_dir: Path,
    warnings: list[str],
    log: TripLog | None = None,
) -> tuple:
    """Fetch/cache OSM road geometry (and water/green-area geometry)
    for a trip's whole bounding box - shared by both the static
    map.mp4 render and any zoomed map_zoom_*m.mp4 render, so a
    network/cache failure produces one "map" warning rather than one
    per map output requested. Returns (bbox, roads, areas); bbox and
    roads are both None if there's no bbox to fetch for (no positioned
    fixes) or the road fetch itself failed.

    Always fetched for the *whole* trip's bounding box, even for a
    zoomed "follow camera" render - the camera only frames a small
    area at once, but which small area varies every frame, so road/
    area data has to be available anywhere along the route.

    A failed area fetch degrades separately from a failed road fetch:
    roads are load-bearing for the whole map phase (no roads, no
    point rendering a map at all), but water/green areas are a purely
    cosmetic addition on top of an otherwise-working map - so an area
    fetch failure produces its own "map areas" warning and falls back
    to `areas = ()` (background renders exactly as it did before this
    feature existed) rather than aborting the map phase.
    """

    bbox = bounding_box_for_fixes(fixes)
    if bbox is None:
        return None, None, None

    try:
        roads = load_or_fetch_roads(bbox, map_cache_dir)
    except MediaToolError as exc:
        # Shared by both map.mp4 and any map_zoom_*m.mp4 - "map data"
        # rather than "map" specifically, since this failure isn't
        # about either one output file over the other.
        warnings.append(f"map data: {exc}")
        if log is not None:
            log.warning(f"map data: {exc}")
        return None, None, None

    try:
        areas = load_or_fetch_areas(bbox, map_cache_dir)
    except MediaToolError as exc:
        warnings.append(f"map areas: {exc}")
        if log is not None:
            log.warning(f"map areas: {exc}")
        areas = ()

    return bbox, roads, areas


def _render_map_variant(
    fixes: tuple,
    bbox,
    roads,
    destination: Path,
    warnings: list[str],
    *,
    warning_label: str,
    areas: tuple = (),
    map_icon: Path | None = None,
    zoom_meters: float | None = None,
    width: int | None = None,
    height: int | None = None,
    video_start: datetime | None = None,
    video_duration_seconds: float | None = None,
    recording_breakpoints: tuple[tuple[float, datetime], ...] | None = None,
    log: TripLog | None = None,
) -> Path | None:
    """Render one map video (either the static map.mp4 or a zoomed
    map_zoom_*m.mp4) at `destination`, degrading to a warning (not a
    failed export) on any image-loading or ffmpeg problem - the rest
    of the trip's export is still worth having even if this one
    output couldn't be built.

    `width`/`height`, if given, are forwarded straight to
    render_map_video() in place of its own square 640x640 default -
    see stitch.map_zoom_dimensions() for where a caller derives these
    to match the trip's real front/rear video instead of a fixed
    square (used for map_zoom_*.mp4 only; the static map.mp4 call
    below still leaves these as None, i.e. the old square default,
    since Christer's own request was specifically about "Map zoom
    layout").

    `video_start`/`video_duration_seconds`, if given, are forwarded
    straight to render_map_video() - see its own docstring for why
    this matters whenever a recording somewhere in the trip has no GPS
    data: without them, the render's own timeline is derived purely
    from whichever fixes exist, which can start later (and run
    shorter) than the trip's real video, going out of sync with it.

    `recording_breakpoints` (see _video_position_breakpoints()), if
    given, is also forwarded straight through - positions every
    frame's GPS lookup by each recording's own real position in the
    concatenated video rather than one single wall-clock anchor. See
    render_map_video()'s own docstring for why that distinction
    matters (confirmed as a real sync bug, not just theoretical - see
    WORKING_CONTEXT.md).
    """

    if log is not None:
        log.step(f"starting {destination.name} render")

    render_kwargs = {}
    if width is not None:
        render_kwargs["width"] = width
    if height is not None:
        render_kwargs["height"] = height

    try:
        result = render_map_video(
            fixes, roads, bbox, destination,
            areas=areas,
            marker_image_path=map_icon,
            zoom_meters=zoom_meters,
            video_start=video_start,
            video_duration_seconds=video_duration_seconds,
            recording_breakpoints=recording_breakpoints,
            **render_kwargs,
        )
    except MediaToolError as exc:
        warnings.append(f"{warning_label}: {exc}")
        if log is not None:
            log.warning(f"{warning_label}: {exc}")
        return None

    if log is not None:
        log.step(f"rendered {destination.name}")

    return result


def _replace_with_retry(
    source: Path,
    destination: Path,
    *,
    attempts: int = 5,
    delay_seconds: float = 0.5,
) -> None:
    """Swap `source` into `destination`'s place (`Path.replace()`),
    retrying a few times on a transient permission error before giving
    up.

    Built for export_trip()'s own front.mp4 audio-remux swap, after
    Christer hit exactly this on a real export: "Access is denied:
    z:\\...\\.bv_export_mux_.../front_with_audio.mp4" - the drive
    -crossing bug from the entry above this one was already fixed
    (the temp dir is on the same volume as `destination`), so this is
    a different failure: something else (most often real-time
    antivirus scanning a large media file the instant ffmpeg finishes
    writing it, sometimes a network share's own locking semantics)
    can briefly hold `destination` open right when the swap wants to
    replace it. A single immediate attempt isn't reliable for that;
    a short retry loop is - the lock is almost always gone within a
    second or two.

    Falls back to a plain overwrite copy (`shutil.copyfile`, a
    different Windows API path than `MoveFileExW`, which sometimes
    succeeds where a rename doesn't - e.g. some SMB share
    configurations restrict rename-over-existing more strictly than a
    plain write) if every replace attempt still fails, before finally
    re-raising the last real error and letting the caller's own
    exception handling decide what happens to `source` - the same
    "leave the already-good file in place rather than lose it" spirit
    as every other failure path in this function.
    """

    last_error: OSError | None = None
    for attempt in range(attempts):
        try:
            source.replace(destination)
            return
        except OSError as exc:
            last_error = exc
            if attempt < attempts - 1:
                time.sleep(delay_seconds)

    try:
        shutil.copyfile(source, destination)
    except OSError:
        raise last_error from None
    source.unlink(missing_ok=True)


def export_trip(
    trip: Trip,
    destination: Path,
    *,
    render_map: bool = False,
    map_cache_dir: Path | None = None,
    map_icon: Path | None = None,
    map_zoom_meters: float | None = None,
    render_gsensor: bool = False,
    render_gsensor_graph: bool = False,
    gsensor_graph_z: bool = False,
    stitch_layout: str | None = None,
    stitch_resolution: tuple[int, int] | None = None,
    stitch_bitrate: str | None = None,
    stitch_scale: float | None = None,
    stitch_max_width: int | None = None,
    stitch_max_height: int | None = None,
    stitch_mirror_size: float = DEFAULT_MIRROR_SIZE_PERCENT,
    stitch_mirror_radius: float = DEFAULT_MIRROR_RADIUS_PERCENT,
    stitch_mirror_zoom: float = DEFAULT_MIRROR_ZOOM_PERCENT,
    stitch_mirror_pan_x: float = DEFAULT_MIRROR_PAN_X_PERCENT,
    stitch_mirror_pan_y: float = DEFAULT_MIRROR_PAN_Y_PERCENT,
    stitch_mirror_icon: Path | None = None,
    stitch_map: str | None = None,
    stitch_map_side: str | None = None,
    stitch_map_size: float | None = None,
    stitch_gsensor: bool = False,
    stitch_gsensor_size: float = DEFAULT_GSENSOR_SIZE_PERCENT,
    stitch_gsensor_pos: str | None = None,
    stitch_gsensor_xy: tuple[float, float] | None = None,
    stitch_graph: bool = False,
    stitch_graph_side: str | None = None,
    stitch_graph_size: float | None = None,
    stitch_subtitles: bool = False,
    stitch_subtitles_background: bool = True,
    include_parking: bool = False,
    parking_speed: float = 1.0,
    command_line: str | None = None,
    reasons: dict[RecordingId, str] | None = None,
    debug: bool = False,
) -> ExportResult:
    """Assemble one trip's concatenated video/audio/text, GPX track,
    and g-sensor log into `destination`.

    `destination` is created if missing. bv-export's CLI is
    responsible for the create/overwrite-existing-folder policy
    before calling this - export_trip just writes into whatever
    directory it's given.

    `render_map=True` additionally renders map.mp4 - a route/position/
    speed overlay on an OSM-road basemap (see osm_roads.py/map_video.py
    for why this uses Overpass data rather than live map tiles), always
    framing the whole trip at once (a static overview). The position
    marker is an arrow rotated to the GPS course over ground, or a
    custom image given via `map_icon` (also rotated to match course -
    see map_render.py).

    `map_zoom_meters`, if given, is independent of `render_map` and
    additionally renders its own map_zoom_{METERS}m.mp4 - a "follow
    camera" instead of a static overview: a tight, scrolling view of
    real-world half-width `map_zoom_meters`, centered on the vehicle's
    current position every frame (see map_video.render_map_video()).
    `render_map` and `map_zoom_meters` can be used separately or
    together - together, both files get rendered.

    `map_cache_dir` is where fetched OSM road data is cached between
    trips/runs (defaults to a `.osm_cache`
    folder next to `destination` - bv-export's CLI points this at
    --target so it's shared across every trip in one export run, not
    wiped when a trip folder is refreshed). Off by default: it needs
    network the first time a region is exported, and adds real render
    time.

    `render_gsensor=True` additionally renders gsensor.mp4 - a dot
    moving around a gauge, tracking the trip's g-sensor (x, y)
    readings with a short fading trail, on a flat chroma-key green
    background meant to be composited over the front/rear footage
    later (see gsensor_render.py/gsensor_video.py). No network
    involved, but off by default since it's extra render time most
    exports won't want.

    `render_gsensor_graph=True` additionally renders
    gsensor_graph.mp4 - a second, alternate g-sensor visualization: a
    static whole-trip strip chart of the trip's X/Y (and Z, see
    `gsensor_graph_z` below) g-sensor readings as colored line traces,
    with a vertical playhead marking the current position, modeled on
    the BlackVue SD Card Viewer app's own g-sensor panel (see
    gsensor_graph_render.py/gsensor_graph_video.py). Independent of
    `render_gsensor` - either, both, or neither can be requested; this
    is a standalone file alongside gsensor.mp4, not a replacement for
    it. Also off by default for the same reason.

    `gsensor_graph_z` (default False) controls whether that strip
    chart plots Z at all - forwarded to both `render_gsensor_graph`'s
    own gsensor_graph.mp4 render and `stitch_graph`'s own panel below,
    one switch for both (see gsensor_graph_render.py's own module
    docstring for Christer's reasoning: "Z is just not useful, unless
    you hit a giant pothole, but then the video probably got that and
    the reaction of the driver" - the one situation where Z genuinely
    matters is already captured by the footage itself). Meaningless on
    its own without one of `render_gsensor_graph`/`stitch_graph` also
    being set.

    `stitch_layout`, if given ('side_by_side', 'top_down',
    'rearview_mirror', or stitch.AUTO_LAYOUT - see stitch.py),
    additionally renders stitch.mp4: the trip's front and rear footage
    composed into one video. The first two are a plain ffmpeg hstack/
    vstack of both full-size cameras; 'rearview_mirror' is different in
    kind - front stays full-frame and rear becomes a small flipped (a
    real mirror shows things reversed), scaled inset overlaid top
    -center, sized via `stitch_mirror_size`. stitch.AUTO_LAYOUT picks
    between 'side_by_side'/'top_down' from this trip's own north-south
    vs. east-west GPS extent (see stitch.pick_stitch_layout()) -
    'rearview_mirror' is never auto-picked, only ever chosen by name.
    No GPS data to pick from degrades to a warning and a
    'side_by_side' default, not a failed stitch. A trip with only one
    camera falls back to a plain copy of whichever one exists, ignoring
    `stitch_layout` entirely (unless `stitch_resolution`/
    `stitch_bitrate` are also given, which force a re-encode even for a
    single camera) - the map panel and g-sensor overlay below are
    ignored for that single-camera path too. See WORKING_CONTEXT.md for
    the full --stitch spec.

    This same call's own concatenated `audio` (see `audio.aac` above)
    is always forwarded into stitch.mp4 as a stream-copied audio track
    whenever both cameras exist (stitch.stitch_cameras()'s two-camera
    `_stack()` path) - not a separate flag, since there's no reason to
    ever want a silent stitch.mp4 when the trip's own audio is already
    sitting right there. Only wired up for that two-camera path; the
    single-camera fallback above stays silent, a known gap rather than
    an oversight (see stitch.py's own docstring).

    `stitch_mirror_size` (percent of the composite's own width, 10-50,
    default stitch.DEFAULT_MIRROR_SIZE_PERCENT) controls the mirror
    inset's size when `stitch_layout='rearview_mirror'` - ignored for
    the other two layouts. `stitch_mirror_radius` (percent of the
    inset's own min(width, height)/2, 0-100, default
    stitch.DEFAULT_MIRROR_RADIUS_PERCENT) rounds its four corners - 0
    (the default) leaves them square, matching the layout's original
    plain-rectangle look. `stitch_mirror_zoom` (percent of the rear
    source cropped away from each edge toward its center before
    scaling, 0-95, default stitch.DEFAULT_MIRROR_ZOOM_PERCENT) zooms
    the mirror inset in - 0 shows the whole rear frame, unchanged.
    `stitch_mirror_pan_x`/`stitch_mirror_pan_y` (-100 to 100, default
    stitch.DEFAULT_MIRROR_PAN_X_PERCENT/DEFAULT_MIRROR_PAN_Y_PERCENT)
    slide that crop off-center within the margin `stitch_mirror_zoom`
    cropped away - 0 stays centered; only has room to move once
    `stitch_mirror_zoom` > 0. `stitch_mirror_icon`, if given, is a path
    to a photo of a real physical rearview mirror - replaces the plain
    procedural inset with rear footage composited into that photo's
    own glass area, see stitch.stitch_cameras()'s own docstring for the
    full mechanism. This function's own default is plain `None` (the
    procedural inset) - bv_export.py's CLI/library entry point is what
    resolves an omitted --stitch-mirror-icon to the bundled default
    photo instead, see mirror_icon.DEFAULT_MIRROR_ICON_PATH. `stitch
    _mirror_radius` is ignored when an icon is given; `stitch_mirror
    _zoom`/`stitch_mirror_pan_x`/`stitch_mirror_pan_y` still apply.

    `stitch_resolution` (a (width, height) pixel pair) and
    `stitch_bitrate` (e.g. "256k", passed straight to ffmpeg's -b:v)
    scale/constrain stitch.mp4 - handy for a fast, small test render
    instead of waiting on a full-resolution encode. Both only apply
    when `stitch_layout` is also given.

    `stitch_scale` (percent, 1-100), `stitch_max_width`, and
    `stitch_max_height` (pixels) are a padding-free alternative to
    `stitch_resolution` for just shrinking the output - they scale the
    whole final frame (camera composite plus any map panel) down by a
    uniform factor instead of fitting it into an exact WxH, so the
    aspect ratio is always preserved and no letterbox/pillarbox bars
    are ever added. All three combine freely (whichever produces the
    smallest result wins) - see stitch.py's own docstring for the full
    reasoning. Also only apply when `stitch_layout` is given.

    `stitch_map` ('map' or 'zoom'), if given (also requires
    `stitch_layout`), additionally composes a map panel alongside the
    camera composite in stitch.mp4 - a dedicated render, sized to fit
    the composite exactly (see stitch.py's _map_panel_dimensions()/
    _render_map_panel()), separate from any general-purpose map.mp4/
    map_zoom_*m.mp4 `render_map`/`map_zoom_meters` may also produce in
    this same run. 'zoom' reuses `map_zoom_meters` as the panel's
    follow-camera radius - it must also be given, or the panel is
    skipped with a warning. `stitch_map_side` ('left', 'right', 'top',
    or 'down') overrides the panel's default side, which is otherwise
    picked from `stitch_layout` (left for top_down, down for
    side_by_side or rearview_mirror). Needs the trip's own GPS fixes
    (and, for roads to draw, a successful OSM fetch/cache) the same way
    `render_map`/`map_zoom_meters` do - degrades to a warning and no
    panel (not a failed stitch) if there's no GPS data, no default
    side, or a missing zoom radius. Capped at 30% of width/height
    (rather than the general 50%) when `stitch_layout='rearview_mirror'`
    specifically - most of that frame still needs to stay the primary
    front view, with the mirror inset already claiming some of it too.
    `stitch_map_size`, if given (a percent, MIN_/MAX_MAP_SIZE_PERCENT
    in stitch.py), overrides the panel's own automatic geography
    -aspect-ratio sizing (which otherwise floors at 20% of the
    composite's matching dimension - can read as "too thin" for a
    near-straight-line trip) with an exact fraction instead.

    `stitch_gsensor=True` (also requires `stitch_layout`) composites a
    gsensor.mp4 as a transparent chroma-keyed overlay on top of the
    camera footage. If `destination/gsensor.mp4` already exists on
    disk (from `render_gsensor=True` earlier in this same call, or a
    previous run that wasn't wiped), that file is reused as-is rather
    than re-rendered; otherwise it's now rendered fresh right here,
    same as `stitch_graph` below already does for gsensor_graph.mp4 -
    Christer: "on bv-generate ... --translate ... Implies transcription
    internally ... do you think that same behaviour [should apply] to
    bv-export like ... --stitch-graph should imply --gsensor-graph-video"
    (--stitch-graph turned out to already work this way; this is the
    other half, --stitch-gsensor, brought in line with it). This
    reverses an earlier, deliberate "compose only what's already
    there" choice that matched --stitch's other inputs (front/rear
    video, audio, subtitles) - worth remembering if a future overlay
    input is added and its own default behavior needs picking again.
    `stitch_gsensor_size` (percent of the
    camera composite's width, 5-40, default
    stitch.DEFAULT_GSENSOR_SIZE_PERCENT) and either
    `stitch_gsensor_pos` (a named position like "top-right" - see
    stitch.parse_gsensor_position(), defaults to
    stitch.DEFAULT_GSENSOR_POSITION) or `stitch_gsensor_xy` (an
    explicit (x_percent, y_percent) override, allowed to land anywhere
    including on the map panel) control size/placement - see
    stitch_cameras()'s own docstring for the full detail.

    `stitch_graph=True` (also requires `stitch_layout`) additionally
    composes a --stitch-graph panel: a strip chart of this trip's X/Y/Z
    g-sensor readings with a moving playhead (see
    gsensor_graph_render.py's own module docstring), a second, alternate
    g-sensor visualization alongside the dot-gauge `stitch_gsensor`
    composites. Unlike `stitch_gsensor`, this follows the map panel's
    own "rendered fresh, at the exact panel size, grows the composite"
    shape rather than "must already exist, just overlaid" - Christer:
    "I want to be able to select the graph like i selects map."
    `stitch_graph_side` ('left', 'right', 'top', or 'down') overrides
    the panel's default side; left unset, stitch.py's own _stack()
    derives the default from wherever the map panel actually ended up
    (see `map_panel_side_used` there) - Christer: "if map to the left
    then graph should be at the bottom, if map at bottom then graph to
    the left ... in order to get close to 16x9 format. if no map then
    bottom" - so a plain `stitch_graph=True` alongside a plain
    `stitch_map`, both left at their own defaults, grow the frame on
    perpendicular axes rather than compounding onto the same one, and
    default to 'down' whenever no map panel actually ended up in the
    composite at all; `stitch_graph_size`, if given (a percent, MIN_/
    MAX_GRAPH_SIZE_PERCENT in stitch.py), overrides the fixed
    DEFAULT_GRAPH_SIZE_PERCENT fraction otherwise used - there's no
    map-panel-style automatic geography-based sizing here, a synthetic
    chart has no equivalent real-world shape to derive one from. The
    panel's own orientation is picked automatically from the resolved
    side: a tall, narrow 'left'/'right' panel renders with upright
    tick labels and time running top to bottom; a short, wide 'top'/
    'down' one renders like the standalone gsensor_graph.mp4 default,
    time running left to right - Christer's own reason for wanting a
    vertical mode at all was fitting the graph beside a map panel
    that's already claimed the bottom of the frame, which is exactly
    what composing both together (`stitch_map` on 'down', `stitch_graph`
    defaulting to 'left' as a result) now produces. Composed after any
    map panel, so the two combine rather than either one overwriting the
    other. Degrades to a `warnings` entry and no panel (never a failed
    stitch) if there's fewer than two g-sensor samples for this trip.
    Plots Z too when `gsensor_graph_z=True` (see that parameter's own
    docstring above) - same as the standalone gsensor_graph.mp4 case,
    Z is hidden by default.

    `stitch_subtitles=True` (also requires `stitch_layout`) burns this
    same call's own trip.srt (see `srt_path` above) into stitch.mp4's
    final frame, after any gsensor overlay/map panel - never
    trip.lrc, which has no real per-line duration (merge_lrc() always
    sets `end == start`). Unlike `stitch_gsensor`, there's no "go
    render it first" step: trip.srt is written earlier in this same
    call whenever the trip has any transcript data at all, not gated
    behind its own flag, so it's always fresh for this run's
    recordings by the time this check runs. If the trip has no
    transcript data (srt_path stays None), the burn-in is skipped with
    a warning rather than failing the stitch.
    `stitch_subtitles_background` (default True) draws a solid, semi
    -transparent bar behind the text for readability - see
    stitch.py's _subtitles_filter().

    `include_parking=False` (the default) leaves every Parking-mode
    recording out of front.mp4/rear.mp4/audio.aac entirely, wherever
    it falls in the trip - leading, trailing, or mid-trip - with
    nothing substituted in its place. Set `include_parking=True` to
    include every Parking recording's own footage/audio
    unconditionally instead, the original behavior.

    `parking_speed` (default 1.0, a strict no-op) re-encodes every
    included Parking recording's own FRONT/REAR at that playback
    speed - 2.0 plays it twice as fast (half the real-world
    duration), 0.5 half as fast - before it's ever concatenated,
    aligned, or offset-calculated (see `_apply_parking_speed()`'s own
    docstring). Christer's own reasoning for wanting this: Parking
    footage is motion-triggered and sparse, so a long real-world span
    can compress into a slow-to-watch clip in the final export; a
    speed control lets it play back faster without touching the rest
    of the trip's own pace. Has no effect at all when
    `include_parking=False` - there's nothing in the video to speed
    up in that case. Range-validated by the CLI layer
    (`cli/bv_export.py`'s `--parking-speed`, 0.10-5.0), not here.

    An earlier version of this feature spliced in a short synthetic
    "PARKING FOOTAGE SKIPPED" clip for mid-trip Parking recordings
    (leaving recordings at the very start/end of the trip untouched
    either way). That approach was dropped after a real export from
    Christer's own 4K HEVC dashcam showed it silently corrupting
    front.mp4/rear.mp4 from the splice point onward - the picture froze
    or broke up while the separately-muxed audio kept playing normally.
    Root cause: MP4's "hvc1"/"avc1" sample-entry tagging declares a
    track's SPS/PPS/VPS parameter sets once, at the container level;
    two files from separate encoder sessions (the dashcam's own
    hardware encoder vs. anything bv-export rendered itself) generally
    don't share compatible parameter sets, so `concatenate_media()`'s
    stream-copy splice (ffmpeg's concat demuxer, `-c copy` - no
    validation, unlike the concat filter) muxes them together without
    complaint at export time, but no real decoder can parse the result
    past that point. A full decode+re-encode would avoid this, but at
    real trip-length/4K cost for one skipped recording's worth of
    benefit - Christer: "we don't want time consuming stuff if it not
    gives us something great back. Just skip it altogether" - so a
    Parking recording is now simply left out, matching the treatment
    leading/trailing Parking recordings already had.

    `command_line`, if given, is written verbatim into this trip's own
    trip.log (see below) as the exact command that produced it - bv-
    export's CLI reconstructs it from sys.argv (see bv_export.py's
    main()).

    `reasons`, if given, is TripBuilder.build()'s own per-recording
    membership explanation (see trip_builder.py) - written into
    trip.log so a surprising trip membership decision can be checked
    against the real reasoning that produced it, not re-derived after
    the fact.

    Every call writes `destination/trip.log`: the invoking command,
    why each of this trip's recordings was judged to belong to it, and
    a timestamped account of every phase below as it happens -
    including a line written *before* a slow phase (map/gsensor/stitch
    rendering) starts, not just after it finishes, specifically so a
    run that hangs still leaves a trail showing which phase it was in
    and how long it had been running when it stopped. See
    export/trip_log.py.

    `debug=True` prints wall-clock timing to stderr for the
    concatenation/map/gsensor/stitch phases below, plus (from stitch.py)
    which decode method --stitch actually used - see bv_export.py's
    --debug flag. Independent of trip.log, which always records this
    same timing (and more) regardless of --debug.
    """

    destination.mkdir(parents=True, exist_ok=True)
    warnings: list[str] = []

    log = TripLog.open(
        destination, trip_label=trip.label, command=command_line or "(not recorded)"
    )
    if reasons is not None:
        for recording in trip:
            reason = reasons.get(recording.id)
            if reason is not None:
                log.membership(recording.id, reason)

    # front/rear/audio concatenation are three independent ffmpeg
    # subprocess calls - none reads another's output - so running them
    # concurrently rather than one after another cuts real wall-clock
    # time instead of leaving CPU idle while only one ffmpeg process
    # runs at a time (Christer measured ~50% CPU on a real export).
    # Safe with plain threads despite Python's GIL: each worker mostly
    # just blocks in subprocess.run() waiting on ffmpeg, which releases
    # the GIL for the wait, and list.append() (warnings, on a failure)
    # is itself atomic in CPython. Deliberately scoped to just these
    # three for now - map/gsensor rendering do real CPU-bound Python
    # work (PIL frame drawing) that would contend for the GIL if also
    # threaded alongside each other, a separate change if wanted later.
    # Self-heals any recording in this trip that's missing its own
    # <recording>.aac before the audio concatenation below gathers
    # sources - see _ensure_recording_audio()'s own docstring. Must
    # finish before the ThreadPoolExecutor block starts (its audio
    # worker reads recording.assets synchronously the moment it
    # starts), so this runs here rather than as a fourth concurrent
    # task alongside front/rear/audio.
    _ensure_recording_audio(trip, warnings, log, debug=debug)

    log.step("starting concatenation (front/rear/audio)")
    concat_start = time.monotonic()

    # Scoped to just this phase - no later phase (map/gsensor/stitch
    # rendering, subtitle merging) reads from the aligned files
    # directly, they all work from front.mp4/rear.mp4 once
    # concatenation is done.
    with tempfile.TemporaryDirectory(prefix="bv_export_align_") as align_dir:
        # Prebuffer trimming runs first: a detected pre-record-buffer
        # overlap is real duplicate content at the very front of an
        # Event/Manual recording, so it needs to come off before
        # _align_front_rear_durations() compares FRONT against REAR -
        # otherwise alignment would be trimming (from the tail) files
        # that still have this duplicate content sitting at their
        # head. See _trim_prebuffers()'s own docstring for the full
        # reasoning and what gets trimmed.
        # Parking-container repair runs first of all: a Parking
        # recording's own raw video otherwise fails ffprobe outright
        # (see _repair_parking_sources()'s own docstring), so both
        # _trim_prebuffers() (which never touches Parking recordings,
        # but shares this tempdir) and _align_front_rear_durations()
        # (which does, when include_parking=True) need the repaired
        # path to probe successfully rather than treating it as
        # unreadable the way _concatenate_asset() otherwise would.
        parking_repair_overrides = _repair_parking_sources(trip)
        # --parking-speed runs right after repair, before prebuffer
        # trim/alignment - it needs the repaired container to
        # re-encode (change_playback_speed() shells out to ffmpeg,
        # same requirement as repair's own consumers below), and both
        # of FRONT/REAR need to already be at their final speed before
        # alignment compares their durations (see
        # _apply_parking_speed()'s own docstring for why running it
        # any later would fight this ordering). A strict no-op - zero
        # ffmpeg calls, empty dict back - whenever parking_speed is
        # left at its 1.0 default or include_parking is False, so a
        # trip that never asked for this flag pays nothing extra here.
        parking_speed_overrides = _apply_parking_speed(
            trip, Path(align_dir), warnings, log,
            speed=parking_speed, include_parking=include_parking,
            source_overrides=parking_repair_overrides,
        )
        prebuffer_overrides, gsensor_overrides, prebuffer_offsets = _trim_prebuffers(
            trip, Path(align_dir), warnings, log,
        )
        alignment_overrides = _align_front_rear_durations(
            trip, Path(align_dir), warnings, log, include_parking=include_parking,
            source_overrides={
                **parking_repair_overrides,
                **parking_speed_overrides,
                **prebuffer_overrides,
            },
        )
        # alignment_overrides wins per-(recording, asset) pair where it
        # touched one (its own trimmed file was itself built from
        # whatever parking_repair_overrides/parking_speed_overrides/
        # prebuffer_overrides supplied, so it already carries that
        # repair/speed-change/trim forward); the earlier maps' own
        # entries are kept as-is for anything alignment didn't need to
        # touch further - e.g. AUDIO, which alignment never looks at
        # at all, or a FRONT/REAR pair that already matched post
        # -repair/post-speed-change/post-prebuffer-trim.
        duration_overrides = {
            **parking_repair_overrides,
            **parking_speed_overrides,
            **prebuffer_overrides,
            **alignment_overrides,
        }

        # Computed here, still inside this tempdir's own lifetime -
        # _recording_video_offsets() probes each recording's own
        # (possibly trimmed) video file, and duration_overrides' own
        # trimmed paths live in align_dir, which is gone the moment
        # this `with` block exits. video_offsets/recording_breakpoints/
        # summed_video_duration_seconds themselves are plain data
        # (RecordingId -> float, (float, datetime) pairs, and a bare
        # float respectively) with no dependency on any file still
        # existing, so they're safe to keep using well past this
        # block - see _merge_gsensor()/_render_map_variant()/
        # stitch_cameras() below, and the video_duration_seconds
        # computation right after this block exits. gsensor_overrides
        # is likewise plain in-memory data (see _merge_gsensor()'s own
        # docstring), safe to keep past this block too.
        video_offsets, summed_video_duration_seconds = _recording_video_offsets(
            trip, include_parking=include_parking,
            duration_overrides=duration_overrides,
        )
        recording_breakpoints = _video_position_breakpoints(
            trip, video_offsets, prebuffer_offsets,
        )

        # Must also finish before the ThreadPoolExecutor block starts,
        # same reasoning as _ensure_recording_audio() above - the
        # audio worker reads recording.assets synchronously the
        # moment it starts. Silent files are written into align_dir,
        # already open for the rest of this block's own lifetime (see
        # its own comment above).
        _pad_missing_audio_with_silence(
            trip, video_offsets, summed_video_duration_seconds,
            Path(align_dir), warnings, log, debug=debug,
        )

        with concurrent.futures.ThreadPoolExecutor(max_workers=3) as executor:
            front_future = executor.submit(
                _concatenate_asset, trip, Asset.FRONT, "front.mp4", destination, warnings, log,
                include_parking=include_parking, duration_overrides=duration_overrides,
                video_only=True,
            )
            rear_future = executor.submit(
                _concatenate_asset, trip, Asset.REAR, "rear.mp4", destination, warnings, log,
                include_parking=include_parking, duration_overrides=duration_overrides,
            )
            audio_future = executor.submit(
                _concatenate_asset, trip, Asset.AUDIO, "audio.aac", destination, warnings, log,
                include_parking=include_parking, duration_overrides=duration_overrides,
            )
            front_video = front_future.result()
            rear_video = rear_future.result()
            audio = audio_future.result()

    # front.mp4 was just concatenated video-only (video_only=True
    # above - see concatenate_media()'s own docstring for the real
    # corruption this avoids: mixing a video-only repaired Parking
    # segment in among ordinary two-stream FRONT recordings used to
    # produce a front.mp4 whose own video stream reported a garbage
    # time_base/duration, even though every individual source probed
    # correctly). Remux the trip's own audio.aac back into it now that
    # both are ready, so front.mp4 still carries real, correctly
    # -timed sound for anyone playing it directly - bv-web's trip
    # -detail page falls back to exactly this file whenever --stitch
    # wasn't used (see web/trips.py's own VIDEO_FILENAMES order).
    # Muxed into a throwaway temp file first and swapped into place
    # only on success, since ffmpeg can't read and write front.mp4 in
    # the same pass - a mux failure leaves the already-good video-only
    # front.mp4 in place rather than losing it, the same "don't take
    # good footage down with a secondary failure" spirit as
    # _concatenate_asset()'s own per-source readability handling.
    #
    # The temp dir is created inside front_video's own parent
    # directory (dir=...), not the OS default (tempfile's usual
    # C:\Users\...\AppData\Local\Temp on Windows). Exports commonly
    # land on a different drive/network share than the OS temp
    # location (e.g. Z:\data\trips\...), and Path.replace() (os.
    # rename/os.replace) cannot move a file across drives on Windows -
    # confirmed by Christer hitting exactly that: "The system cannot
    # move the file to a different disk drive:
    # C:\Users\...\Temp\bv_export_mux_.../front_with_audio.mp4".
    # Building the temp file on the same drive as the destination up
    # front keeps the final swap a same-volume rename, which always
    # succeeds.
    # ignore_cleanup_errors=True: Christer hit a real leftover-lock
    # case (some other process - antivirus is the usual suspect -
    # still had front_with_audio.mp4 open by the time this `with`
    # block tried to tear the temp dir down), which would otherwise
    # raise out of this block's own cleanup and take the whole export
    # down even after _replace_with_retry() above already succeeded
    # or already recovered gracefully. A directory tempfile itself
    # can't fully delete is a stale leftover, not lost footage - the
    # same "don't let a secondary failure take good output down with
    # it" spirit as everywhere else in this function.
    if front_video is not None and audio is not None:
        with tempfile.TemporaryDirectory(
            prefix=".bv_export_mux_",
            dir=front_video.parent,
            ignore_cleanup_errors=True,
        ) as mux_dir:
            muxed = Path(mux_dir) / "front_with_audio.mp4"
            try:
                mux_audio_track(front_video, audio, muxed)
            except MediaToolError as exc:
                message = (
                    f"front.mp4: could not remux audio back in ({exc}) - "
                    "front.mp4 will have no sound"
                )
                warnings.append(message)
                log.warning(message)
            else:
                try:
                    _replace_with_retry(muxed, front_video)
                except OSError as exc:
                    message = (
                        "front.mp4: remuxed audio successfully but "
                        f"couldn't swap it into place ({exc}) - "
                        "front.mp4 will have no sound"
                    )
                    warnings.append(message)
                    log.warning(message)
                else:
                    log.step("remuxed audio.aac into front.mp4")

    if debug:
        print(
            f"bv-export: concatenation phase took "
            f"{time.monotonic() - concat_start:.1f}s",
            file=sys.stderr,
        )

    text_paths = []
    for asset, filename in TEXT_ASSETS:
        merged = merge_text_assets(trip, asset)
        if merged is None:
            continue
        out = destination / filename
        out.write_text(merged, encoding="utf-8")
        text_paths.append(out)
    if text_paths:
        log.step(
            "merged text asset(s): " + ", ".join(p.name for p in text_paths)
        )
    else:
        log.step("no text assets for this trip - skipped")

    # Whisper only emits segments for actual speech, so a trip with a
    # quiet stretch at the end (nobody talking for the last couple of
    # minutes, say) produces a merged subtitle file that stops well
    # before the video does. This also feeds map.mp4/map_zoom's own
    # frame_count math and the --stitch map/graph panel durations
    # below - the trip's own real video length, not just subtitle
    # padding, so getting it right matters well beyond trip.srt/.lrc.
    #
    # Preferring summed_video_duration_seconds (computed above, while
    # duration_overrides' own trimmed paths were still valid) over
    # probing the concatenated video file itself - the reverse of this
    # function's own history - because Christer hit a real case where
    # the concatenated file's own container metadata was wrong by
    # roughly 16x: see _recording_video_offsets()'s own docstring for
    # the full story (a repaired Parking recording's timescale
    # surviving `-c copy` concatenation into a front.mp4 whose reported
    # avg_frame_rate/duration described footage that never happened,
    # while the identical real frame count on the rear side reported
    # correctly). Summing each source's own individually-probed
    # duration was already proven reliable elsewhere in this file
    # (_align_front_rear_durations(), _recording_video_offsets()'s own
    # per-recording offsets) - falling back to the old probe-the-
    # concatenated-file behavior only if the summed approach found
    # nothing at all (no recording's video could be probed), which by
    # construction should coincide with front_video/rear_video both
    # being None anyway.
    video_duration_seconds = summed_video_duration_seconds
    if video_duration_seconds is None:
        video_for_duration = front_video or rear_video
        if video_for_duration is not None:
            try:
                video_duration_seconds = probe(video_for_duration).duration_seconds
            except MediaToolError as exc:
                warnings.append(f"subtitle padding: {exc}")
                log.warning(f"subtitle padding: {exc}")

    srt_path = None
    merged_srt = merge_srt(
        trip,
        total_duration_seconds=video_duration_seconds,
        video_offsets=video_offsets,
    )
    if merged_srt is not None:
        srt_path = destination / "trip.srt"
        srt_path.write_text(merged_srt + "\n", encoding="utf-8")
        log.step("merged trip.srt")
    else:
        log.step("no transcript data for this trip - trip.srt skipped")

    lrc_path = None
    merged_lrc = merge_lrc(
        trip,
        total_duration_seconds=video_duration_seconds,
        video_offsets=video_offsets,
    )
    if merged_lrc is not None:
        lrc_path = destination / "trip.lrc"
        lrc_path.write_text(merged_lrc + "\n", encoding="utf-8")
        log.step("merged trip.lrc")
    else:
        log.step("no transcript data for this trip - trip.lrc skipped")

    gpx_path = None
    fixes = _merge_gps(trip)
    if fixes:
        gpx_path = destination / "trip.gpx"
        write_gpx(fixes, gpx_path, name=trip.label)
        log.step(f"wrote trip.gpx ({len(fixes)} fix(es))")
    else:
        log.step("no GPS data for this trip - trip.gpx skipped")

    # trip_info.txt: a short, human-readable summary (duration,
    # distance/speed, start/end address) - always attempted, unlike
    # the map/stitch outputs above, since none of this needs an
    # explicit flag: duration is always known (see Trip.end_timestamp),
    # distance/speed only need the same merged `fixes` trip.gpx already
    # has, and reverse geocoding is a light, occasional lookup (two
    # points per trip) explicitly within Nominatim's own public usage
    # policy - see geocoding.py's own docstring for why this is treated
    # the same as OSM road/area fetching rather than gated behind its
    # own opt-in flag.
    info_path = destination / "trip_info.txt"
    stats = compute_trip_stats(fixes)
    positioned_fixes = tuple(
        fix
        for fix in fixes
        if fix.valid and fix.latitude is not None and fix.longitude is not None
    )
    geocode_cache_dir = map_cache_dir or (destination.parent / ".osm_cache")
    start_address = None
    end_address = None
    if positioned_fixes:
        first_fix = positioned_fixes[0]
        try:
            start_address = load_or_reverse_geocode(
                first_fix.latitude, first_fix.longitude, geocode_cache_dir
            )
        except MediaToolError as exc:
            warnings.append(f"trip info: could not geocode start location: {exc}")
            log.warning(f"trip info: could not geocode start location: {exc}")

        last_fix = positioned_fixes[-1]
        if last_fix is first_fix:
            end_address = start_address
        else:
            try:
                end_address = load_or_reverse_geocode(
                    last_fix.latitude, last_fix.longitude, geocode_cache_dir
                )
            except MediaToolError as exc:
                warnings.append(f"trip info: could not geocode end location: {exc}")
                log.warning(f"trip info: could not geocode end location: {exc}")

    content_start, content_end = _content_timestamps(
        trip, include_parking=include_parking,
    )
    write_trip_info(
        info_path,
        duration=content_end - content_start,
        start_timestamp=content_start,
        end_timestamp=content_end,
        stats=stats,
        start_address=start_address,
        end_address=end_address,
        has_parking_footage=trip.has_parking_footage,
        total_size_bytes=trip.total_size,
    )
    log.step("wrote trip_info.txt")

    map_path = None
    map_zoom_path = None
    # Also loaded for --stitch-map, not just --map/--map-zoom - the
    # panel it renders needs the same fixes/roads/areas, just at its
    # own dedicated size (see the stitch_cameras() call below).
    stitch_map_roads: tuple = ()
    stitch_map_areas: tuple = ()
    if (render_map or map_zoom_meters is not None or stitch_map is not None) and fixes:
        log.step("starting map data phase (fetch/cache OSM roads)")
        map_start = time.monotonic() if debug else None
        cache_dir = map_cache_dir or (destination.parent / ".osm_cache")
        bbox, roads, areas = _load_trip_roads(fixes, cache_dir, warnings, log)

        if bbox is not None and roads is not None:
            stitch_map_roads = roads
            stitch_map_areas = areas
            if render_map:
                map_path = _render_map_variant(
                    fixes, bbox, roads, destination / "map.mp4", warnings,
                    warning_label="map", areas=areas, map_icon=map_icon,
                    video_start=trip.start_timestamp,
                    video_duration_seconds=video_duration_seconds,
                    recording_breakpoints=recording_breakpoints,
                    log=log,
                )

            if map_zoom_meters is not None:
                zoom_filename = f"map_zoom_{map_zoom_meters:g}m.mp4"
                # Christer: "Map zoom layout shouldn't be square, it
                # should match the videos height or width depending on
                # layout... just as the other map" - "the other map"
                # being --stitch-map's embedded panel. Reuses that
                # panel's exact sizing rule via
                # stitch.map_zoom_dimensions(), probing whichever of
                # front/rear video actually exists (front preferred,
                # same "video_for_duration" preference used just above
                # for subtitle padding). Degrades to the old fixed
                # square default (leaving width/height as None) rather
                # than failing the export if the probe itself fails -
                # a mis-shaped map_zoom.mp4 is still worth having.
                zoom_width = zoom_height = None
                video_for_zoom_shape = front_video or rear_video
                if video_for_zoom_shape is not None:
                    try:
                        zoom_width, zoom_height = map_zoom_dimensions(
                            video_for_zoom_shape, fixes
                        )
                    except MediaToolError as exc:
                        warnings.append(
                            f"map_zoom: could not size panel to match "
                            f"video, using square default: {exc}"
                        )
                        log.warning(
                            f"map_zoom: could not size panel to match "
                            f"video: {exc}"
                        )
                map_zoom_path = _render_map_variant(
                    fixes, bbox, roads, destination / zoom_filename, warnings,
                    warning_label="map_zoom", areas=areas, map_icon=map_icon,
                    zoom_meters=map_zoom_meters,
                    width=zoom_width, height=zoom_height,
                    video_start=trip.start_timestamp,
                    video_duration_seconds=video_duration_seconds,
                    recording_breakpoints=recording_breakpoints,
                    log=log,
                )
        if debug:
            print(
                f"bv-export: map phase took {time.monotonic() - map_start:.1f}s",
                file=sys.stderr,
            )
    else:
        log.step("no map/map-zoom/stitch-map requested or no GPS data - map phase skipped")

    gsensor_path = None
    samples = _merge_gsensor(trip, video_offsets, gsensor_overrides)
    if samples:
        gsensor_path = destination / "trip.3gf"
        write_gsensor(samples, gsensor_path)
        log.step(f"wrote trip.3gf ({len(samples)} sample(s))")
    else:
        log.step("no g-sensor data for this trip - trip.3gf skipped")

    gsensor_video_path = None
    if render_gsensor and samples:
        # Sample count logged here on purpose - the render loop's own
        # cost scales with it (see gsensor_video.py's
        # _advance_search_index()/_interpolate_from_index()), so a
        # future run that looks stuck at this same line can tell from
        # trip.log alone whether it's a huge trip genuinely taking a
        # while, or something worth investigating further.
        log.step(f"starting gsensor.mp4 render ({len(samples)} sample(s))")
        gsensor_start = time.monotonic()
        try:
            gsensor_video_path = render_gsensor_video(
                samples, destination / "gsensor.mp4",
                duration_seconds=video_duration_seconds,
            )
        except MediaToolError as exc:
            warnings.append(f"gsensor video: {exc}")
            log.warning(f"gsensor video: {exc}")
        else:
            log.step(
                "rendered gsensor.mp4",
                elapsed_seconds=time.monotonic() - gsensor_start,
            )
        if debug:
            print(
                f"bv-export: gsensor phase took "
                f"{time.monotonic() - gsensor_start:.1f}s",
                file=sys.stderr,
            )

    gsensor_graph_video_path = None
    if render_gsensor_graph and samples:
        log.step(f"starting gsensor_graph.mp4 render ({len(samples)} sample(s))")
        gsensor_graph_start = time.monotonic()
        try:
            gsensor_graph_video_path = render_gsensor_graph_video(
                samples, destination / "gsensor_graph.mp4",
                duration_seconds=video_duration_seconds,
                show_z=gsensor_graph_z,
            )
        except MediaToolError as exc:
            warnings.append(f"gsensor graph video: {exc}")
            log.warning(f"gsensor graph video: {exc}")
        else:
            log.step(
                "rendered gsensor_graph.mp4",
                elapsed_seconds=time.monotonic() - gsensor_graph_start,
            )
        if debug:
            print(
                f"bv-export: gsensor_graph phase took "
                f"{time.monotonic() - gsensor_graph_start:.1f}s",
                file=sys.stderr,
            )

    # --stitch-gsensor reuses gsensor.mp4 if one already exists on
    # disk (this run's own render_gsensor=True, or a previous run's
    # that wasn't wiped) - avoids paying for a second render of
    # something already sitting right there. If it's missing, this
    # now renders it fresh right here instead of warning Christer to
    # go run --gsensor-video separately first - Christer: "do you
    # think that same behaviour [--translate implying --transcribe]
    # [should apply] to bv-export like that --stitch-graph should
    # imply --gsensor-graph-video" (turned out --stitch-graph already
    # worked this way; this reverses the same "compose only what's
    # already there" choice --stitch-gsensor used to deliberately
    # make, to match).
    #
    # Two distinct reasons the file can still end up missing, and they
    # need different handling: `samples` (computed above from
    # _merge_gsensor()) being empty means this trip has no g-sensor
    # data at all - no render attempt would ever produce a gsensor.mp4
    # for it, so there's nothing to try, straight to a warning. Only
    # when samples exist but no gsensor.mp4 is on disk yet is a fresh
    # render actually attempted below.
    stitch_gsensor_source = None
    if stitch_gsensor and stitch_layout is not None:
        candidate = destination / "gsensor.mp4"
        if candidate.exists():
            stitch_gsensor_source = candidate
            log.step("using existing gsensor.mp4 for stitch overlay")
            if debug:
                print(
                    "bv-export: gsensor.mp4 already exists - reusing for "
                    "stitch overlay (render skipped)",
                    file=sys.stderr,
                )
        elif not samples:
            warnings.append(
                "stitch gsensor overlay: no g-sensor data for this "
                "trip - skipped"
            )
            log.warning(
                "stitch gsensor overlay: no g-sensor data for this "
                "trip - skipped"
            )
        else:
            log.step(
                f"stitch gsensor overlay: gsensor.mp4 not found - "
                f"rendering it now ({len(samples)} sample(s))"
            )
            stitch_gsensor_render_start = time.monotonic()
            try:
                stitch_gsensor_source = render_gsensor_video(
                    samples, candidate, duration_seconds=video_duration_seconds,
                )
            except MediaToolError as exc:
                warnings.append(f"stitch gsensor overlay: {exc}")
                log.warning(f"stitch gsensor overlay: {exc}")
            else:
                elapsed = time.monotonic() - stitch_gsensor_render_start
                if stitch_gsensor_source is not None:
                    log.step(
                        "rendered gsensor.mp4 for stitch overlay",
                        elapsed_seconds=elapsed,
                    )
                    if debug:
                        print(
                            "bv-export: stitch gsensor overlay - rendered "
                            f"gsensor.mp4 ({elapsed:.1f}s)",
                            file=sys.stderr,
                        )
                else:
                    # render_gsensor_video() itself returns None (writes
                    # nothing) for fewer than two samples, or samples
                    # spanning zero time - see its own docstring. Same
                    # "not enough data" shape as the `not samples` branch
                    # above, just caught one level later since `samples`
                    # itself was non-empty here.
                    warnings.append(
                        "stitch gsensor overlay: not enough g-sensor "
                        "data to render an overlay - skipped"
                    )
                    log.warning(
                        "stitch gsensor overlay: not enough g-sensor "
                        "data to render an overlay - skipped"
                    )

    # --stitch-subtitles reuses this same call's own srt_path - unlike
    # --stitch-gsensor, trip.srt isn't gated behind its own render
    # flag (merge_srt() above always writes one when the trip has any
    # transcript data), so there's no "missing, go render it first"
    # case the way there is for gsensor.mp4 - only "no transcript data
    # for this trip at all".
    stitch_subtitles_source = None
    if stitch_subtitles and stitch_layout is not None:
        if srt_path is not None:
            stitch_subtitles_source = srt_path
            log.step("using trip.srt for stitch subtitle burn-in")
        else:
            warnings.append(
                "stitch subtitles: no transcript data for this trip - "
                "trip.srt was not written - skipped"
            )
            log.warning(
                "stitch subtitles: no transcript data for this trip - "
                "trip.srt was not written - skipped"
            )

    # AUTO_LAYOUT ("auto" - --stitch-layout's own default when not
    # given explicitly) never reaches stitch_cameras() itself - it's
    # resolved to a concrete side_by_side/top_down right here, from
    # this trip's own already-loaded GPS fixes (see
    # pick_stitch_layout()). rearview_mirror is never auto-picked -
    # someone has to ask for it by name. No GPS data to pick from
    # degrades to a warning and the same side_by_side default the CLI
    # used before auto-pick existed, not a failed stitch.
    resolved_stitch_layout = stitch_layout
    if stitch_layout == AUTO_LAYOUT:
        picked_layout = pick_stitch_layout(fixes)
        if picked_layout is None:
            resolved_stitch_layout = "side_by_side"
            warnings.append(
                "stitch: no GPS data to auto-pick a layout from - "
                "defaulting to side_by_side"
            )
            log.warning(
                "stitch: no GPS data to auto-pick a layout from - "
                "defaulting to side_by_side"
            )
        else:
            resolved_stitch_layout = picked_layout
            log.step(f"auto-picked stitch layout: {resolved_stitch_layout}")

    stitch_path = None
    if stitch_layout is not None:
        log.step(f"starting stitch.mp4 render (layout={resolved_stitch_layout})")
        stitch_start = time.monotonic() if debug else None
        # Diffing warnings' own length across the call, rather than
        # threading `log` into stitch_cameras() itself, catches both
        # its own internal degraded-feature warnings (map panel/
        # gsensor overlay/subtitle issues - see stitch_cameras()'s own
        # docstring) and the `except` below, in one place, without
        # widening stitch.py's own scope in this same change.
        warnings_before_stitch = len(warnings)
        try:
            stitch_path = stitch_cameras(
                front_video, rear_video, destination / "stitch.mp4",
                layout=resolved_stitch_layout,
                resolution=stitch_resolution,
                bitrate=stitch_bitrate,
                scale=stitch_scale,
                max_width=stitch_max_width,
                max_height=stitch_max_height,
                mirror_size=stitch_mirror_size,
                mirror_radius=stitch_mirror_radius,
                mirror_zoom=stitch_mirror_zoom,
                mirror_pan_x=stitch_mirror_pan_x,
                mirror_pan_y=stitch_mirror_pan_y,
                mirror_icon=stitch_mirror_icon,
                map_mode=stitch_map,
                map_side=stitch_map_side,
                map_size=stitch_map_size,
                map_zoom_meters=map_zoom_meters,
                map_fixes=fixes if stitch_map is not None else (),
                map_roads=stitch_map_roads,
                map_areas=stitch_map_areas,
                map_icon=map_icon,
                map_video_start=trip.start_timestamp,
                map_video_duration_seconds=video_duration_seconds,
                map_recording_breakpoints=recording_breakpoints,
                gsensor_video=stitch_gsensor_source,
                gsensor_size=stitch_gsensor_size,
                gsensor_pos=stitch_gsensor_pos,
                gsensor_xy=stitch_gsensor_xy,
                graph_samples=samples if stitch_graph else (),
                graph_side=stitch_graph_side,
                graph_size=stitch_graph_size,
                graph_video_duration_seconds=video_duration_seconds,
                graph_z=gsensor_graph_z,
                subtitles_path=stitch_subtitles_source,
                subtitles_background=stitch_subtitles_background,
                audio_path=audio,
                debug=debug,
                warnings=warnings,
            )
        except MediaToolError as exc:
            warnings.append(f"stitch: {exc}")
        for new_warning in warnings[warnings_before_stitch:]:
            log.warning(new_warning)
        if stitch_path is not None:
            log.step("rendered stitch.mp4")
        if debug:
            print(
                f"bv-export: stitch phase took "
                f"{time.monotonic() - stitch_start:.1f}s",
                file=sys.stderr,
            )

    log.close()

    return ExportResult(
        front_video=front_video,
        rear_video=rear_video,
        audio=audio,
        gpx=gpx_path,
        trip_info=info_path,
        gsensor=gsensor_path,
        map=map_path,
        map_zoom=map_zoom_path,
        gsensor_video=gsensor_video_path,
        gsensor_graph_video=gsensor_graph_video_path,
        stitch=stitch_path,
        srt=srt_path,
        lrc=lrc_path,
        text=tuple(text_paths),
        warnings=tuple(warnings),
    )
