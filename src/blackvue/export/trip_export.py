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
from pathlib import Path

from ..archive.asset import Asset
from ..archive.recording import Recording
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
from .media import concatenate_media
from .osm_roads import bounding_box_for_fixes
from .osm_roads import load_or_fetch_areas
from .osm_roads import load_or_fetch_roads
from .parking_transition import ParkingTransitionCache
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


# The other camera's own video asset for the same recording - used as
# a probe fallback below when one side's Parking file is corrupted,
# since front/rear share resolution/frame rate in practice.
_SIBLING_VIDEO_ASSET = {
    Asset.FRONT: Asset.REAR,
    Asset.REAR: Asset.FRONT,
}


def _parking_transition_source(
    asset: Asset,
    recording: Recording,
    parking_transitions: ParkingTransitionCache | None,
    warnings: list[str],
    log: TripLog | None,
) -> Path | None:
    """Return a placeholder clip standing in for `recording`'s own
    `asset` file - a silent audio clip for Asset.AUDIO, a "parking
    footage skipped" video clip for anything else (Asset.FRONT/REAR) -
    or None if no placeholder could be produced, in which case the
    caller leaves this recording's contribution to `asset` out
    entirely (the same as if it never had the asset in the first
    place, rather than falling back to the real Parking footage
    Christer asked to have skipped).

    For Asset.FRONT/REAR, the sibling camera's own file (same
    `recording`) is passed to ParkingTransitionCache.video_for() as a
    fallback probe source - see that function's docstring for why:
    it's what lets one corrupted side (e.g. a truncated _PF.mp4) still
    get a correctly-sized placeholder from its working sibling
    (_PR.mp4), rather than the two ending up different lengths."""

    if parking_transitions is None:
        return None

    source = recording.file(asset).path
    try:
        if asset is Asset.AUDIO:
            return parking_transitions.silence_for(source)
        sibling_asset = _SIBLING_VIDEO_ASSET.get(asset)
        sibling_file = (
            recording.file(sibling_asset) if sibling_asset is not None else None
        )
        fallback_source = sibling_file.path if sibling_file is not None else None
        return parking_transitions.video_for(source, fallback_source=fallback_source)
    except MediaToolError as exc:
        message = f"parking transition clip for {recording.id}: {exc}"
        warnings.append(message)
        if log is not None:
            log.warning(message)
        return None


def _concatenate_asset(
    trip: Trip,
    asset: Asset,
    filename: str,
    destination: Path,
    warnings: list[str],
    log: TripLog | None = None,
    *,
    include_parking: bool = True,
    parking_transitions: ParkingTransitionCache | None = None,
) -> Path | None:
    """Build `sources` in trip order, substituting a synthetic
    placeholder (see _parking_transition_source() above) for any
    Parking-mode recording strictly in the middle of the trip -
    flanked by another recording on both sides, so a Parking
    recording that happens to start or end the trip is always left
    untouched - whenever `include_parking` is False. Christer: "we
    include parking files as is in the middle of a trip otherwise we
    skip them with a nice transition". `include_parking=True` (or no
    `parking_transitions` cache given) reproduces the original
    behavior: every recording's own file, unconditionally.
    """

    sources: list[Path] = []
    last_index = len(trip.recordings) - 1

    for index, recording in enumerate(trip.recordings):
        if not recording.has(asset):
            continue

        is_mid_trip_parking = recording.id.is_parking and 0 < index < last_index
        if is_mid_trip_parking and not include_parking:
            placeholder = _parking_transition_source(
                asset, recording, parking_transitions, warnings, log
            )
            if placeholder is not None:
                sources.append(placeholder)
            continue

        sources.append(recording.file(asset).path)

    if not sources:
        if log is not None:
            log.step(f"no source recordings for {filename} - skipped")
        return None

    out = destination / filename
    try:
        concatenate_media(sources, out)
    except MediaToolError as exc:
        warnings.append(str(exc))
        if log is not None:
            log.warning(str(exc))
        return None

    if log is not None:
        log.step(f"concatenated {filename} from {len(sources)} recording(s)")

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
    parking_transition_image: Path | None = None,
    parking_transition_clip: Path | None = None,
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

    `include_parking=False` (the default) leaves any Parking-mode
    recording strictly in the middle of the trip - flanked by another
    recording on both sides - out of front.mp4/rear.mp4/audio.aac,
    replacing it with a short synthetic clip instead: a still frame
    reading "PARKING FOOTAGE SKIPPED" for the video assets, and
    matching silence for audio.aac, both exactly
    parking_transition.PARKING_TRANSITION_DURATION_SECONDS long so
    every substituted asset for that one recording stays in sync with
    the others (Christer: "swap in matching silence" - otherwise
    stitch.mp4's muxed audio would drift out of sync with the video
    for the rest of the trip). A Parking recording at the very start
    or end of the trip is always left as-is, regardless of this flag
    - there's nothing to transition from/to on one side. Set
    `include_parking=True` to turn this off and include every
    Parking recording's own footage/audio unconditionally, the
    original behavior. `parking_transition_image`, if given, replaces
    the default "no parking" placeholder frame with Christer's own
    picture (fitted/padded to match, see parking_transition.py) - the
    same bundled-default-with-override convention as `map_icon`/
    `stitch_mirror_icon`. `parking_transition_clip`, if given, takes
    priority over `parking_transition_image` and replaces the
    placeholder with a real video instead of a still frame (e.g. one
    of Christer's own AI-generated "no parking" clips) - looped or
    trimmed to match the fixed transition duration exactly, re-encoded
    to each trip's own resolution/frame rate, and always stripped of
    its own audio track (front.mp4/rear.mp4 are video-only throughout
    this whole pipeline; the matching-silence audio.aac swap above is
    unaffected either way).

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

    # Only spun up when actually needed - most trips have no Parking
    # footage at all, and this cache's own directory/tempfile.
    # TemporaryDirectory() call is pure overhead for them. Scoped to
    # just the concatenation phase below: no later phase (map/gsensor/
    # stitch rendering, subtitle merging) reads from it, they all work
    # from front.mp4/rear.mp4/audio.aac once concatenation is done.
    parking_transitions = None
    parking_transitions_dir = None
    if not include_parking and trip.has_parking_footage:
        parking_transitions_dir = tempfile.TemporaryDirectory(
            prefix="bv_export_parking_transition_"
        )
        parking_transitions = ParkingTransitionCache(
            work_dir=Path(parking_transitions_dir.name),
            image_path=parking_transition_image,
            clip_path=parking_transition_clip,
        )

    try:
        with concurrent.futures.ThreadPoolExecutor(max_workers=3) as executor:
            front_future = executor.submit(
                _concatenate_asset, trip, Asset.FRONT, "front.mp4", destination, warnings, log,
                include_parking=include_parking, parking_transitions=parking_transitions,
            )
            rear_future = executor.submit(
                _concatenate_asset, trip, Asset.REAR, "rear.mp4", destination, warnings, log,
                include_parking=include_parking, parking_transitions=parking_transitions,
            )
            audio_future = executor.submit(
                _concatenate_asset, trip, Asset.AUDIO, "audio.aac", destination, warnings, log,
                include_parking=include_parking, parking_transitions=parking_transitions,
            )
            front_video = front_future.result()
            rear_video = rear_future.result()
            audio = audio_future.result()
    finally:
        if parking_transitions_dir is not None:
            parking_transitions_dir.cleanup()

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
    samples = _merge_gsensor(trip)
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
