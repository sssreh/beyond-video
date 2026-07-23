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
import time
from dataclasses import dataclass, field
from datetime import datetime
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
from .gpx_writer import write_gpx
from .gsensor_video import render_gsensor_video
from .map_video import render_map_video
from .media import concatenate_media
from .osm_roads import bounding_box_for_fixes
from .osm_roads import load_or_fetch_roads
from .stitch import AUTO_LAYOUT
from .stitch import DEFAULT_GSENSOR_SIZE_PERCENT
from .stitch import DEFAULT_MIRROR_SIZE_PERCENT
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
    gsensor: Path | None = None
    map: Path | None = None
    map_zoom: Path | None = None
    gsensor_video: Path | None = None
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


def _concatenate_asset(
    trip: Trip,
    asset: Asset,
    filename: str,
    destination: Path,
    warnings: list[str],
    log: TripLog | None = None,
) -> Path | None:
    sources = [
        recording.file(asset).path for recording in trip if recording.has(asset)
    ]
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
    """Fetch/cache OSM road geometry for a trip's whole bounding box -
    shared by both the static map.mp4 render and any zoomed
    map_zoom_*m.mp4 render, so a network/cache failure produces one
    "map" warning rather than one per map output requested. Returns
    (bbox, roads); both None if there's no bbox to fetch for (no
    positioned fixes) or the fetch itself failed.

    Always fetched for the *whole* trip's bounding box, even for a
    zoomed "follow camera" render - the camera only frames a small
    area at once, but which small area varies every frame, so road
    data has to be available anywhere along the route.
    """

    bbox = bounding_box_for_fixes(fixes)
    if bbox is None:
        return None, None

    try:
        roads = load_or_fetch_roads(bbox, map_cache_dir)
    except MediaToolError as exc:
        # Shared by both map.mp4 and any map_zoom_*m.mp4 - "map data"
        # rather than "map" specifically, since this failure isn't
        # about either one output file over the other.
        warnings.append(f"map data: {exc}")
        if log is not None:
            log.warning(f"map data: {exc}")
        return None, None

    return bbox, roads


def _render_map_variant(
    fixes: tuple,
    bbox,
    roads,
    destination: Path,
    warnings: list[str],
    *,
    warning_label: str,
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
    stitch_layout: str | None = None,
    stitch_resolution: tuple[int, int] | None = None,
    stitch_bitrate: str | None = None,
    stitch_mirror_size: float = DEFAULT_MIRROR_SIZE_PERCENT,
    stitch_map: str | None = None,
    stitch_map_side: str | None = None,
    stitch_map_size: float | None = None,
    stitch_gsensor: bool = False,
    stitch_gsensor_size: float = DEFAULT_GSENSOR_SIZE_PERCENT,
    stitch_gsensor_pos: str | None = None,
    stitch_gsensor_xy: tuple[float, float] | None = None,
    stitch_subtitles: bool = False,
    stitch_subtitles_background: bool = True,
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
    the other two layouts.

    `stitch_resolution` (a (width, height) pixel pair) and
    `stitch_bitrate` (e.g. "256k", passed straight to ffmpeg's -b:v)
    scale/constrain stitch.mp4 - handy for a fast, small test render
    instead of waiting on a full-resolution encode. Both only apply
    when `stitch_layout` is also given.

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

    `stitch_gsensor=True` (also requires `stitch_layout`) composites
    an *already-rendered* gsensor.mp4 as a transparent chroma-keyed
    overlay on top of the camera footage - unlike the map panel,
    --stitch never generates this itself; if `destination/gsensor.mp4`
    doesn't already exist on disk (from `render_gsensor=True` earlier
    in this same call, or a previous run that wasn't wiped), the
    overlay is skipped with a warning telling Christer to run
    `--gsensor-video` first. `stitch_gsensor_size` (percent of the
    camera composite's width, 5-40, default
    stitch.DEFAULT_GSENSOR_SIZE_PERCENT) and either
    `stitch_gsensor_pos` (a named position like "top-right" - see
    stitch.parse_gsensor_position(), defaults to
    stitch.DEFAULT_GSENSOR_POSITION) or `stitch_gsensor_xy` (an
    explicit (x_percent, y_percent) override, allowed to land anywhere
    including on the map panel) control size/placement - see
    stitch_cameras()'s own docstring for the full detail.

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
    with concurrent.futures.ThreadPoolExecutor(max_workers=3) as executor:
        front_future = executor.submit(
            _concatenate_asset, trip, Asset.FRONT, "front.mp4", destination, warnings, log
        )
        rear_future = executor.submit(
            _concatenate_asset, trip, Asset.REAR, "rear.mp4", destination, warnings, log
        )
        audio_future = executor.submit(
            _concatenate_asset, trip, Asset.AUDIO, "audio.aac", destination, warnings, log
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

    map_path = None
    map_zoom_path = None
    # Also loaded for --stitch-map, not just --map/--map-zoom - the
    # panel it renders needs the same fixes/roads, just at its own
    # dedicated size (see the stitch_cameras() call below).
    stitch_map_roads: tuple = ()
    if (render_map or map_zoom_meters is not None or stitch_map is not None) and fixes:
        log.step("starting map data phase (fetch/cache OSM roads)")
        map_start = time.monotonic() if debug else None
        cache_dir = map_cache_dir or (destination.parent / ".osm_cache")
        bbox, roads = _load_trip_roads(fixes, cache_dir, warnings, log)

        if bbox is not None and roads is not None:
            stitch_map_roads = roads
            if render_map:
                map_path = _render_map_variant(
                    fixes, bbox, roads, destination / "map.mp4", warnings,
                    warning_label="map", map_icon=map_icon,
                    video_start=trip.start_timestamp,
                    video_duration_seconds=video_duration_seconds,
                    log=log,
                )

            if map_zoom_meters is not None:
                zoom_filename = f"map_zoom_{map_zoom_meters:g}m.mp4"
                map_zoom_path = _render_map_variant(
                    fixes, bbox, roads, destination / zoom_filename, warnings,
                    warning_label="map_zoom", map_icon=map_icon,
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

    # --stitch-gsensor never generates gsensor.mp4 itself - it only
    # checks whether one already exists on disk (this run's own
    # render_gsensor=True, or a previous run's that wasn't wiped), the
    # same "compose only what's already there" convention --stitch's
    # camera/subtitle inputs already follow (unlike the map panel,
    # which is a deliberate, confirmed exception to that rule).
    #
    # Two distinct reasons the file can be missing, and they need two
    # different messages: `samples` (computed above from
    # _merge_gsensor()) being empty means this trip has no g-sensor
    # data at all - no flag will ever produce a gsensor.mp4 for it, so
    # telling the user to "run --gsensor-video first" is actively
    # wrong advice. Only when samples exist but no gsensor.mp4 is on
    # disk does the "go render it" message actually apply.
    stitch_gsensor_source = None
    if stitch_gsensor and stitch_layout is not None:
        candidate = destination / "gsensor.mp4"
        if candidate.exists():
            stitch_gsensor_source = candidate
            log.step("using existing gsensor.mp4 for stitch overlay")
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
            warnings.append(
                "stitch gsensor overlay: gsensor.mp4 not found - run "
                "bv-export --gsensor-video first"
            )
            log.warning(
                "stitch gsensor overlay: gsensor.mp4 not found - run "
                "bv-export --gsensor-video first"
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
                mirror_size=stitch_mirror_size,
                map_mode=stitch_map,
                map_side=stitch_map_side,
                map_size=stitch_map_size,
                map_zoom_meters=map_zoom_meters,
                map_fixes=fixes if stitch_map is not None else (),
                map_roads=stitch_map_roads,
                map_icon=map_icon,
                map_video_start=trip.start_timestamp,
                map_video_duration_seconds=video_duration_seconds,
                gsensor_video=stitch_gsensor_source,
                gsensor_size=stitch_gsensor_size,
                gsensor_pos=stitch_gsensor_pos,
                gsensor_xy=stitch_gsensor_xy,
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
        gsensor=gsensor_path,
        map=map_path,
        map_zoom=map_zoom_path,
        gsensor_video=gsensor_video_path,
        stitch=stitch_path,
        srt=srt_path,
        lrc=lrc_path,
        text=tuple(text_paths),
        warnings=tuple(warnings),
    )
