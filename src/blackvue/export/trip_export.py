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
import sys
import tempfile
import time
from dataclasses import dataclass, field
from datetime import datetime
from datetime import timedelta
from pathlib import Path

from ..archive.asset import Asset
from ..archive.recording_id import RecordingId
from ..generate.media import MediaToolError
from ..generate.media import probe
from ..telemetry.gps_reader import read_gps
from ..telemetry.gsensor_reader import GSensorSample
from ..telemetry.gsensor_reader import read_gsensor
from ..telemetry.gsensor_reader import write_gsensor
from ..trip.trip import Trip
from .geocoding import load_or_reverse_geocode
from .gpx_writer import write_gpx
from .gsensor_graph_video import render_gsensor_graph_video
from .gsensor_video import render_gsensor_video
from .map_video import render_map_video
from .media import check_readable
from .media import concatenate_media
from .media import trim_media
from .osm_roads import bounding_box_for_fixes
from .osm_roads import load_or_fetch_areas
from .osm_roads import load_or_fetch_roads
from .trip_info import write_trip_info
from .trip_stats import compute_trip_stats
from .stitch import AUTO_LAYOUT
from .stitch import DEFAULT_GSENSOR_SIZE_PERCENT
from .stitch import DEFAULT_MIRROR_RADIUS_PERCENT
from .stitch import DEFAULT_MIRROR_SIZE_PERCENT
from .stitch import DEFAULT_MIRROR_PAN_X_PERCENT
from .stitch import DEFAULT_MIRROR_PAN_Y_PERCENT
from .stitch import DEFAULT_MIRROR_ZOOM_PERCENT
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


def folder_name_for_trip(trip: Trip, prefix: str | None) -> str:
    """Return the subfolder name bv-export uses for a trip, e.g.
    'Holiday_trip_20260715_133458_20260715_141235' when prefix is
    'Holiday', or just 'trip_20260715_133458_20260715_141235' with no
    prefix."""

    if prefix:
        return f"{prefix}_{trip.label}"
    return trip.label


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


def _align_front_rear_durations(
    trip: Trip,
    work_dir: Path,
    warnings: list[str],
    log: TripLog | None,
    *,
    include_parking: bool,
    epsilon_seconds: float = FRONT_REAR_DURATION_EPSILON_SECONDS,
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
    """

    overrides: dict[tuple[RecordingId, Asset], Path] = {}

    for recording in trip.recordings:
        if recording.id.is_parking and not include_parking:
            continue
        if not recording.has(Asset.FRONT) or not recording.has(Asset.REAR):
            continue

        front_path = recording.file(Asset.FRONT).path
        rear_path = recording.file(Asset.REAR).path

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
        message = (
            f"{recording.id}: front/rear duration differs by "
            f"{abs(diff):.2f}s (front={front_duration:.2f}s, "
            f"rear={rear_duration:.2f}s) - trimmed {longer_label} to "
            f"match {shorter_label}"
        )
        warnings.append(message)
        if log is not None:
            log.warning(message)

    return overrides


def _recording_video_offsets(
    trip: Trip,
    *,
    include_parking: bool,
    duration_overrides: dict[tuple[RecordingId, Asset], Path] | None = None,
) -> dict[RecordingId, float]:
    """Return each recording's own real start position, in seconds,
    within the concatenated video `_concatenate_asset()` actually
    produces - the sum of every earlier included recording's own
    (possibly front/rear-trimmed) duration, NOT the gap between
    recording ID timestamps.

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

    return offsets


def _video_position_breakpoints(
    trip: Trip, video_offsets: dict[RecordingId, float]
) -> tuple[tuple[float, datetime], ...]:
    """Return `video_offsets` reshaped into a (video_position_seconds,
    wallclock_start) sequence, sorted by video position - what
    map_video.py's render_map_video() (`recording_breakpoints` param)
    needs to convert a video-elapsed position back into the real
    wall-clock instant it corresponds to, piecewise per recording
    instead of one global "trip start" anchor. See
    _recording_video_offsets()'s own docstring for why the two only
    agree by coincidence.
    """

    breakpoints = [
        (video_offsets[recording.id], recording.id.timestamp)
        for recording in trip.recordings
        if recording.id in video_offsets
    ]
    return tuple(sorted(breakpoints, key=lambda item: item[0]))


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
        concatenate_media(readable_sources, out)
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
    trip: Trip, video_offsets: dict[RecordingId, float] | None = None
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
    real footage)."""

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

    try:
        result = render_map_video(
            fixes, roads, bbox, destination,
            areas=areas,
            marker_image_path=map_icon,
            zoom_meters=zoom_meters,
            video_start=video_start,
            video_duration_seconds=video_duration_seconds,
            recording_breakpoints=recording_breakpoints,
        )
    except MediaToolError as exc:
        warnings.append(f"{warning_label}: {exc}")
        if log is not None:
            log.warning(f"{warning_label}: {exc}")
        return None

    if log is not None:
        log.step(f"rendered {destination.name}")

    return result


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
    log.step("starting concatenation (front/rear/audio)")
    concat_start = time.monotonic()

    # Scoped to just this phase - no later phase (map/gsensor/stitch
    # rendering, subtitle merging) reads from the aligned files
    # directly, they all work from front.mp4/rear.mp4 once
    # concatenation is done.
    with tempfile.TemporaryDirectory(prefix="bv_export_align_") as align_dir:
        duration_overrides = _align_front_rear_durations(
            trip, Path(align_dir), warnings, log, include_parking=include_parking,
        )

        # Computed here, still inside this tempdir's own lifetime -
        # _recording_video_offsets() probes each recording's own
        # (possibly trimmed) video file, and duration_overrides' own
        # trimmed paths live in align_dir, which is gone the moment
        # this `with` block exits. video_offsets/recording_breakpoints
        # themselves are plain data (RecordingId -> float, and (float,
        # datetime) pairs) with no dependency on any file still
        # existing, so they're safe to keep using well past this
        # block - see _merge_gsensor()/_render_map_variant()/
        # stitch_cameras() below.
        video_offsets = _recording_video_offsets(
            trip, include_parking=include_parking,
            duration_overrides=duration_overrides,
        )
        recording_breakpoints = _video_position_breakpoints(trip, video_offsets)

        with concurrent.futures.ThreadPoolExecutor(max_workers=3) as executor:
            front_future = executor.submit(
                _concatenate_asset, trip, Asset.FRONT, "front.mp4", destination, warnings, log,
                include_parking=include_parking, duration_overrides=duration_overrides,
            )
            rear_future = executor.submit(
                _concatenate_asset, trip, Asset.REAR, "rear.mp4", destination, warnings, log,
                include_parking=include_parking, duration_overrides=duration_overrides,
            )
            audio_future = executor.submit(
                _concatenate_asset, trip, Asset.AUDIO, "audio.aac", destination, warnings, log,
                include_parking=include_parking,
            )
            front_video = front_future.result()
            rear_video = rear_future.result()
            audio = audio_future.result()

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
            log.warning(f"subtitle padding: {exc}")

    srt_path = None
    merged_srt = merge_srt(trip, total_duration_seconds=video_duration_seconds)
    if merged_srt is not None:
        srt_path = destination / "trip.srt"
        srt_path.write_text(merged_srt + "\n", encoding="utf-8")
        log.step("merged trip.srt")
    else:
        log.step("no transcript data for this trip - trip.srt skipped")

    lrc_path = None
    merged_lrc = merge_lrc(trip, total_duration_seconds=video_duration_seconds)
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

    write_trip_info(
        info_path,
        duration=trip.duration,
        start_timestamp=trip.start_timestamp,
        end_timestamp=trip.end_timestamp,
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
                map_zoom_path = _render_map_variant(
                    fixes, bbox, roads, destination / zoom_filename, warnings,
                    warning_label="map_zoom", areas=areas, map_icon=map_icon,
                    zoom_meters=map_zoom_meters,
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
    samples = _merge_gsensor(trip, video_offsets)
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
