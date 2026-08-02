"""
Map-overlay video encoding for bv-export: turns a trip's merged GPS
fixes into map.mp4 - rendering one frame per interval (route driven
so far, current position/heading, speed, timestamp) against a
locally-drawn OSM-road basemap, then handing the frame sequence to
ffmpeg.

Copyright (C) 2026 Christer R. (sssreh)

SPDX-License-Identifier: GPL-3.0-or-later
"""

from __future__ import annotations

import math
import tempfile
from datetime import datetime
from datetime import timedelta
from pathlib import Path

from PIL import Image

from ..generate.media import MediaToolError
from ..telemetry.gps_reader import GpsFix
from .map_render import DEFAULT_HEIGHT
from .map_render import DEFAULT_WIDTH
from .map_render import render_base_map
from .map_render import render_frame
from .media import encode_frame_sequence
from .osm_roads import Area
from .osm_roads import BoundingBox
from .osm_roads import Road
from .osm_roads import bounding_box_around_point
from .osm_roads import features_within_bbox
from .osm_roads import index_features
from .osm_roads import index_roads
from .osm_roads import roads_within_bbox

# 5 frames/second is enough for the position dot to read as smooth
# motion without generating an excessive number of frames for a long
# trip. If this ever gets composited alongside real footage (the
# future --stitch item), ffmpeg can retime it there; map.mp4 doesn't
# need to match the front/rear video's own frame rate.
DEFAULT_FPS = 5

# bv-export's own bundled default --map-icon: a top-down red car,
# pointing "up" in its own file (see render_frame()'s marker_image
# docstring), rotated per frame to the GPS course over ground just
# like a custom --map-icon would be. Bundled alongside this module
# (see pyproject.toml's package-data entry for "blackvue.export",
# shared with mirror_icon.py's own DEFAULT_MIRROR_ICON_PATH) so it's
# available wherever bv-export actually runs, not just inside a repo
# checkout - same Path(__file__).parent-relative convention used
# there and by blackvue.web.app's TEMPLATES_DIR. This is bv_export.py's
# own CLI-level default (see that module's --map-icon handling for the
# "omit the flag -> use this; pass the literal string 'none' -> fall
# back to the plain procedural arrow instead" convention) - this
# module's own render_map_video() keeps a plain None default (no icon,
# arrow), unchanged, matching how DEFAULT_MIRROR_ICON_PATH is kept out
# of stitch.py/trip_export.py's own defaults too.
DEFAULT_MAP_ICON_PATH = Path(__file__).parent / "assets" / "red_car.png"

# Christer's own read on the marker rendering at a loaded image's full
# native pixel size (the only sizing this ever had - see the
# marker_image loading below): too big, dominating the frame rather
# than reading as a small position indicator. Applied uniformly to
# whatever marker image is in play, the bundled red car above or a
# custom --map-icon alike, rather than special-casing just the bundled
# asset - the same "reasonable default, no reason to make an exception"
# call as the rest of this module's own sizing constants. This also
# fixes a real, if minor, inconsistency: a custom --map-icon used to
# render at whatever size its own source file happened to be saved at,
# with nothing normalizing it against the map canvas at all.
MARKER_IMAGE_SCALE = 0.5

# The live-GPS satellite badge (see render_map_video()'s `live_fix`
# handling and map_render.py's `show_gps_badge`) treats a frame as
# "live" only if its bracketing pair of real fixes is no more than
# this many seconds apart. GPS receivers normally reacquire within a
# couple of seconds after a brief obstruction (an overpass, a parking
# garage entrance); a bracket wider than this means the interpolated
# position between those two fixes is a straight-line guess across a
# real signal-loss gap (a tunnel, underground parking), not a live
# fix, even though both of the fixes bracketing it are individually
# real. Originally set at 10 seconds; tightened to 3 after checking
# real inter-fix gaps across six of Christer's own raw .gps files
# (1,078 consecutive gaps, ~1Hz nominal rate): the widest real gap
# seen was 1.248s, so 3s catches a real outage promptly with zero
# false positives against that sample - a 1s threshold, by contrast,
# would have flagged ~41% of those same real, healthy gaps as stale
# (normal receiver jitter routinely nudges a "1 second" gap to
# 1.0-1.25s), which would make the badge flicker during ordinary
# driving rather than actually mean something.
MAX_LIVE_FIX_GAP_SECONDS = 3.0


def _valid_positioned_fixes(fixes: tuple[GpsFix, ...]) -> tuple[GpsFix, ...]:
    return tuple(
        fix
        for fix in fixes
        if fix.valid and fix.latitude is not None and fix.longitude is not None
    )


def _interpolate_course(
    a: float | None, b: float | None, t: float
) -> float | None:
    """Interpolate between two compass courses (degrees, 0-360),
    correctly handling the 0/360 wraparound a plain linear
    interpolation would get wrong (e.g. 350 degrees -> 10 degrees
    should pass through 0/360, not swing back down through 180).

    Falls back to whichever course is given if only one is (a fix's
    course field can be empty in the raw NMEA data).
    """

    if a is None:
        return b
    if b is None:
        return a

    a_rad, b_rad = math.radians(a), math.radians(b)
    x = (1 - t) * math.cos(a_rad) + t * math.cos(b_rad)
    y = (1 - t) * math.sin(a_rad) + t * math.sin(b_rad)

    if x == 0.0 and y == 0.0:
        # Exactly opposite courses (e.g. interpolating across a
        # U-turn) - no single "average" direction is more correct
        # than the other; picking `a` is an arbitrary but stable
        # choice rather than an error.
        return a

    result = math.degrees(math.atan2(y, x)) % 360
    # A result that should mathematically be a hair below 0 (e.g.
    # -1e-15) can round to exactly 360.0 in floating point rather than
    # landing in [0, 360) - fold that edge case back to 0.0 so callers
    # never have to special-case 360 meaning the same thing as 0.
    return 0.0 if result == 360.0 else result


def interpolate_position(
    fixes: tuple[GpsFix, ...], timestamp: datetime
) -> tuple[float, float, float | None, float | None]:
    """Linearly interpolate (lat, lon, speed_kmh, course) at
    `timestamp` between the two fixes bracketing it (course uses
    circular interpolation - see _interpolate_course()).

    `fixes` must be sorted by timestamp and non-empty. A timestamp
    outside the fixes' own range clamps to the nearest end fix rather
    than extrapolating.

    Scans `fixes` from the start every call - fine for a one-off
    lookup, but render_map_video()'s own per-frame loop does NOT call
    this anymore - see _advance_fix_index()/_interpolate_position_
    from_index() for the O(fixes + frames) path it uses instead. Same
    bug class (and same fix) as gsensor_video.py's interpolate_sample()/
    _advance_search_index()/_interpolate_from_index() - flagged as a
    latent risk here when that fix landed (a long enough trip would
    hit the same O(fixes x frames) cost, just at GPS's slower ~1Hz
    rate rather than g-sensor's ~10Hz), fixed here once a real trip
    (Christer's own, ~5,400 fixes) actually reached the point where it
    started to matter.
    """

    if timestamp <= fixes[0].timestamp:
        first = fixes[0]
        return first.latitude, first.longitude, first.speed_kmh, first.course

    if timestamp >= fixes[-1].timestamp:
        last = fixes[-1]
        return last.latitude, last.longitude, last.speed_kmh, last.course

    for previous, current in zip(fixes, fixes[1:]):
        if previous.timestamp <= timestamp <= current.timestamp:
            span = (current.timestamp - previous.timestamp).total_seconds()

            if span <= 0:
                return (
                    previous.latitude, previous.longitude,
                    previous.speed_kmh, previous.course,
                )

            t = (timestamp - previous.timestamp).total_seconds() / span
            lat = previous.latitude + (current.latitude - previous.latitude) * t
            lon = previous.longitude + (current.longitude - previous.longitude) * t

            if previous.speed_kmh is not None and current.speed_kmh is not None:
                speed = (
                    previous.speed_kmh
                    + (current.speed_kmh - previous.speed_kmh) * t
                )
            else:
                speed = previous.speed_kmh or current.speed_kmh

            course = _interpolate_course(previous.course, current.course, t)

            return lat, lon, speed, course

    # Unreachable given the clamp checks above, but keeps the return
    # type honest if it's ever reached.
    last = fixes[-1]
    return last.latitude, last.longitude, last.speed_kmh, last.course


def _advance_fix_index(
    fixes: tuple[GpsFix, ...], timestamp: datetime, index: int
) -> int:
    """Move `index` forward (never backward) to the largest index i
    such that fixes[i].timestamp <= timestamp - or len(fixes) - 1 if
    `timestamp` is past every fix.

    Only correct when called with a non-decreasing sequence of
    `timestamp` values across successive calls, each time passing back
    in the index the previous call returned - exactly the shape of
    render_map_video()'s own per-frame loop below, where `timestamp`
    is start + frame_number/fps and only ever increases. Identical
    pattern to gsensor_video.py's _advance_search_index() - see that
    function's own docstring for why the distinction matters in
    practice.
    """

    last = len(fixes) - 1
    while index < last and fixes[index + 1].timestamp <= timestamp:
        index += 1
    return index


def _interpolate_position_from_index(
    fixes: tuple[GpsFix, ...], timestamp: datetime, index: int
) -> tuple[float, float, float | None, float | None]:
    """Same interpolation result as interpolate_position(fixes,
    timestamp) - identical clamp-before-first/clamp-after-last/linear
    -interpolate (course uses the same circular interpolation) behavior
    - but taking an already-known bracketing `index` (see
    _advance_fix_index()) instead of scanning `fixes` for one.

    This exists because interpolate_position() rescans `fixes` from
    its own start on every call - fine for an occasional one-off
    lookup, but render_map_video()'s frame loop below calls it once
    per output frame, and both fix count and frame count scale with
    trip duration - the same O(fixes x frames) shape
    gsensor_video.py's interpolate_sample() had before
    _advance_search_index()/_interpolate_from_index() fixed it there.
    """

    current = fixes[index]

    if index == 0 and timestamp <= current.timestamp:
        return current.latitude, current.longitude, current.speed_kmh, current.course

    if index == len(fixes) - 1:
        return current.latitude, current.longitude, current.speed_kmh, current.course

    nxt = fixes[index + 1]
    span = (nxt.timestamp - current.timestamp).total_seconds()

    if span <= 0:
        return current.latitude, current.longitude, current.speed_kmh, current.course

    t = (timestamp - current.timestamp).total_seconds() / span
    lat = current.latitude + (nxt.latitude - current.latitude) * t
    lon = current.longitude + (nxt.longitude - current.longitude) * t

    if current.speed_kmh is not None and nxt.speed_kmh is not None:
        speed = current.speed_kmh + (nxt.speed_kmh - current.speed_kmh) * t
    else:
        speed = current.speed_kmh or nxt.speed_kmh

    course = _interpolate_course(current.course, nxt.course, t)

    return lat, lon, speed, course


def _wallclock_for_elapsed(
    elapsed_seconds: float,
    breakpoints: tuple[tuple[float, datetime], ...],
    fallback_start: datetime,
) -> datetime:
    """Convert a video-elapsed-seconds position into the real
    wall-clock instant it corresponds to.

    `breakpoints` (see trip_export._video_position_breakpoints()) is a
    sequence of (video_position_seconds, wallclock_start) pairs, one
    per recording actually included in the concatenated video, sorted
    by video position. Within a recording's own span, wall-clock time
    advances 1:1 with video-elapsed time - so this just finds which
    recording's span `elapsed_seconds` falls in, then adds however far
    past that recording's own start position we are. A plain single
    `fallback_start + elapsed_seconds` (this function's own fallback,
    and this module's entire behavior before `breakpoints` existed)
    gets this wrong whenever recordings gap, overlap, or get front/
    rear-trimmed relative to their own nominal ID timestamps - which
    in practice is nearly always (see trip_export.
    _recording_video_offsets()'s own docstring for a real confirmed
    case: a ~4.7s position error from one single recording boundary).

    Falls back to `fallback_start + elapsed_seconds` when `breakpoints`
    is empty (e.g. no video at all for this trip - a GPS/g-sensor-only
    "trip", where there's no concatenated video to align against
    regardless) - preserves this module's original behavior exactly
    for that case rather than requiring every caller to know it needs
    breakpoints.
    """

    if not breakpoints:
        return fallback_start + timedelta(seconds=elapsed_seconds)

    position, wallclock_start = breakpoints[0]
    for candidate_position, candidate_wallclock in breakpoints:
        if candidate_position > elapsed_seconds:
            break
        position, wallclock_start = candidate_position, candidate_wallclock

    return wallclock_start + timedelta(seconds=elapsed_seconds - position)


def _is_live_fix(
    positioned: tuple[GpsFix, ...], timestamp: datetime, index: int
) -> bool:
    """Whether `timestamp` (with its already-known bracketing `index`,
    see _advance_fix_index()) should show the live-GPS satellite badge
    - see MAX_LIVE_FIX_GAP_SECONDS' own docstring for what "live" means
    here.

    False in two distinct cases render_map_video()'s frame loop can't
    otherwise tell apart just from the interpolated position alone:
    `timestamp` is outside every fix's own range (clamped to the first
    or last fix - a leading/trailing gap, e.g. no GPS lock yet at trip
    start), or `timestamp` falls between two real fixes that are
    themselves more than MAX_LIVE_FIX_GAP_SECONDS apart (a signal-loss
    gap mid-trip, e.g. a tunnel) - both bracketing fixes are real, but
    the straight line between them across that much silence isn't.

    The instant of a real fix itself is always live, even if the next
    fix is a wide gap away - that reading is real regardless of how
    long the signal then goes quiet for afterward.
    """

    if timestamp < positioned[0].timestamp or timestamp > positioned[-1].timestamp:
        return False

    if timestamp == positioned[index].timestamp:
        return True

    if index >= len(positioned) - 1:
        return True

    span = (positioned[index + 1].timestamp - positioned[index].timestamp)
    return span.total_seconds() <= MAX_LIVE_FIX_GAP_SECONDS


def render_map_video(
    fixes: tuple[GpsFix, ...],
    roads: tuple[Road, ...],
    bbox: BoundingBox,
    destination: Path,
    *,
    areas: tuple[Area, ...] = (),
    fps: int = DEFAULT_FPS,
    marker_image_path: Path | None = None,
    zoom_meters: float | None = None,
    width: int = DEFAULT_WIDTH,
    height: int = DEFAULT_HEIGHT,
    video_start: datetime | None = None,
    video_duration_seconds: float | None = None,
    recording_breakpoints: tuple[tuple[float, datetime], ...] | None = None,
) -> Path | None:
    """Render a trip's merged GPS fixes into an overlay video at
    `destination`: the route driven so far, current position/heading,
    speed, and timestamp, drawn against `roads` and `areas` (water/
    green polygons, see osm_roads.py) - `areas` defaults to empty and
    is entirely optional, same as `roads` was before this existed.

    `bbox` frames the whole trip at once by default (a static
    overview, the same every frame). `zoom_meters`, if given, switches
    to a "follow camera" instead: every frame is framed by a fresh
    bounding box of that real-world half-width, centered on the
    frame's own interpolated position (see
    osm_roads.bounding_box_around_point()) - `bbox` itself is then
    unused, since every frame gets its own. This is what makes the map
    scroll/pan as the vehicle moves rather than sitting in a fixed
    static view.

    `width`/`height` set the rendered frame size (defaults to
    map_render.py's square 640x640). For a non-square panel, `bbox`
    should already be shaped to match (see bounding_box_for_fixes()'s
    `aspect_ratio` parameter) - render_frame() scales longitude and
    latitude span to the canvas independently, so an unshaped bbox on
    a non-square canvas comes out visibly stretched. In `zoom_meters`
    mode there's no pre-existing bbox to shape ahead of time (a fresh
    one is built every frame), so this derives `width / height` as an
    aspect ratio and passes it straight to
    bounding_box_around_point() instead.

    The position marker is an arrow rotated to the GPS course over
    ground by default. `marker_image_path`, if given, is used as a
    custom marker instead (also rotated to match course) - a PNG with
    transparency is recommended, drawn pointing "up"/north in its own
    file. Raises MediaToolError if the image can't be loaded.

    `video_start`/`video_duration_seconds`, if given, anchor frame 0
    and the total render length to the trip's own real start/duration
    (its concatenated front/rear video's, typically - see
    trip_export.py) instead of to whichever GPS fixes happen to exist.
    This matters whenever an earlier (or later) recording in the trip
    has no GPS data at all: without an explicit anchor, frame 0 falls
    back to the *first available fix's own* timestamp - which, if GPS
    data only starts partway through the trip, is already minutes into
    the real video. The rendered map then comes out both too short
    (only as long as the GPS-covered span) and, composited alongside
    the real front/rear footage, out of sync - playing the GPS-covered
    window starting at the wrong moment rather than starting blank/
    frozen-at-the-first-fix for however long the real gap is.
    `interpolate_position()`'s own clamp-to-nearest-fix behavior
    already does the right thing for a timestamp before the first (or
    after the last) fix - extending the rendered range via these two
    params is what actually lets that clamping cover a real leading/
    trailing no-data gap instead of it always being masked by
    `start`/`end` themselves being derived from the fixes.

    Falls back to the old fixes-derived start/duration when either is
    left as None - e.g. no video exists at all for this trip (a GPS/
    g-sensor-only "trip"), or the real video's own duration couldn't
    be probed.

    `recording_breakpoints`, if given (see trip_export.
    _video_position_breakpoints()), positions every frame's GPS lookup
    using each recording's own real position in the concatenated video
    instead of a single global `video_start` anchor - see
    _wallclock_for_elapsed()'s own docstring for why a single anchor
    drifts out of sync whenever recordings gap/overlap/trim relative
    to their own nominal ID timestamps (nearly always, in practice).
    `video_start` is still used as frame 0's own fallback reference
    (and for the leading-gap/trailing-gap clamping this function's own
    docstring above describes) whenever `recording_breakpoints` is
    None/empty or a given frame's elapsed time falls outside every
    breakpoint's own coverage.

    A small satellite badge (see render_frame()'s `show_gps_badge`)
    appears in the top-right corner of every frame whose timestamp
    falls within the real fixes' own range - i.e. whenever the
    position marker is a real, live GPS fix rather than frozen at the
    first/last fix during a leading/trailing gap like the one above.

    The position marker itself, though, is only suppressed for the
    leading side of that gap - a frame before the trip's very first
    real fix shows no marker at all (nothing to clamp to yet, and
    nothing should look like it "knows" a position no fix has actually
    reported), while a frame during a trailing gap or a mid-trip
    signal-loss gap (a tunnel) still shows the marker, clamped or
    interpolated as before - only its badge goes dark for those. See
    render_frame()'s `show_marker`.

    Returns None (and writes nothing) if there aren't at least two
    valid, positioned fixes to draw a route from - the same "nothing
    to work with" convention export_trip()'s other outputs use.
    """

    positioned = _valid_positioned_fixes(fixes)
    if len(positioned) < 2:
        return None

    start = video_start if video_start is not None else positioned[0].timestamp
    if video_duration_seconds is not None:
        total_seconds = video_duration_seconds
    else:
        total_seconds = (positioned[-1].timestamp - start).total_seconds()

    if total_seconds <= 0:
        return None

    marker_image = None
    if marker_image_path is not None:
        try:
            marker_image = Image.open(marker_image_path).convert("RGBA")
        except (FileNotFoundError, OSError) as exc:
            raise MediaToolError(
                f"could not load marker image {marker_image_path}: {exc}"
            ) from exc

        # Scaled once here, not per frame inside _paste_marker_image()'s
        # own per-frame rotate/paste - the marker image itself never
        # changes mid-render, so resizing it once up front is free
        # compared to redoing it on every single frame. See
        # MARKER_IMAGE_SCALE's own comment for why this applies
        # uniformly to any marker image, bundled or custom.
        scaled_size = (
            max(1, round(marker_image.width * MARKER_IMAGE_SCALE)),
            max(1, round(marker_image.height * MARKER_IMAGE_SCALE)),
        )
        marker_image = marker_image.resize(scaled_size, resample=Image.LANCZOS)

    frame_count = max(2, int(total_seconds * fps) + 1)

    # In follow-camera mode each frame's bbox is a small street-level
    # sliver of the whole trip - drawing every road in the whole
    # trip's dataset on every single frame (most of it far off-canvas)
    # is the dominant cost of rendering, not the ffmpeg encode step.
    # index_roads() precomputes each road's own bbox once so the
    # per-frame filter below (roads_within_bbox()) is cheap; in static
    # (non-zoomed) mode every road is already relevant to the one
    # whole-trip bbox, so there's nothing to filter.
    indexed_roads = index_roads(roads) if zoom_meters is not None else None
    indexed_areas = index_features(areas) if zoom_meters is not None else None
    zoom_aspect_ratio = width / height

    # Static (non-`--map-zoom`) mode draws the exact same `roads`
    # against the exact same `bbox` on every single frame - profiling
    # confirmed that re-projecting and re-drawing them from scratch
    # each time (render_frame()'s old behavior) was the dominant cost
    # of a real-scale render, well past the interpolation cost
    # render_map_video()'s own O(fixes x frames) fix already addressed
    # (see render_base_map()'s own docstring). Rendered once here and
    # handed to every render_frame() call below as a base to copy
    # instead. Follow-camera (`--map-zoom`) mode gets a fresh bbox/
    # road-set every frame, so there's no single base image to
    # precompute - stays None there, and render_frame() falls back to
    # its own per-frame road drawing.
    base_image = (
        None
        if zoom_meters is not None
        else render_base_map(bbox, roads, areas=areas, width=width, height=height)
    )

    destination.parent.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory() as frame_dir_name:
        frame_dir = Path(frame_dir_name)
        route_so_far: list[tuple[float, float]] = []
        fix_index = 0
        # Separate from fix_index above (which tracks how many fixes
        # have been folded into route_so_far) - this is the
        # interpolation bracket's own forward-only cursor (see
        # _advance_fix_index()/_interpolate_position_from_index()),
        # carried across iterations the same way gsensor_video.py's
        # render_gsensor_video() carries its own search_index, so each
        # frame's lookup resumes where the last one left off instead
        # of interpolate_position()'s full rescan from fixes[0] every
        # time.
        position_index = 0

        for frame_number in range(frame_count):
            elapsed = min(frame_number / fps, total_seconds)
            timestamp = _wallclock_for_elapsed(
                elapsed, recording_breakpoints or (), start
            )

            # Grow the drawn route with every real fix at or before
            # this frame's timestamp, so the line is built from real
            # fix points wherever possible, not just interpolated
            # ones.
            while (
                fix_index < len(positioned)
                and positioned[fix_index].timestamp <= timestamp
            ):
                fix = positioned[fix_index]
                route_so_far.append((fix.latitude, fix.longitude))
                fix_index += 1

            position_index = _advance_fix_index(positioned, timestamp, position_index)
            lat, lon, speed, course = _interpolate_position_from_index(
                positioned, timestamp, position_index
            )
            position = (lat, lon)

            # "Live" means this frame's timestamp falls within the
            # real fixes' own range (not clamped to the nearest one
            # because the frame is before the first/after the last,
            # e.g. video_start/video_duration_seconds covering a
            # leading/trailing GPS gap - see this function's own
            # docstring), and its bracketing pair of real fixes isn't
            # itself a signal-loss gap mid-trip (e.g. a tunnel) - see
            # _is_live_fix()/MAX_LIVE_FIX_GAP_SECONDS.
            live_fix = _is_live_fix(positioned, timestamp, position_index)

            # Stricter than live_fix/show_gps_badge above: only true
            # before the trip's very first real GPS fix, the one case
            # Christer specifically didn't want the marker drawn for at
            # all ("shouldn't be seen before it gets real coordinates
            # for the first time") - a stationary marker sitting on a
            # position nothing has actually reported yet. The trailing
            # -gap and mid-trip signal-loss cases show_gps_badge=False
            # also covers are deliberately left showing the (clamped or
            # interpolated) marker, same as before this - only the
            # badge goes off for those, not the marker itself.
            before_first_fix = timestamp < positioned[0].timestamp

            frame_bbox = (
                bounding_box_around_point(
                    lat, lon, zoom_meters, aspect_ratio=zoom_aspect_ratio
                )
                if zoom_meters is not None
                else bbox
            )
            frame_roads = (
                roads_within_bbox(indexed_roads, frame_bbox)
                if indexed_roads is not None
                else roads
            )
            frame_areas = (
                features_within_bbox(indexed_areas, frame_bbox)
                if indexed_areas is not None
                else areas
            )

            frame = render_frame(
                frame_bbox,
                frame_roads,
                tuple(route_so_far) + (position,),
                position,
                areas=frame_areas,
                speed_kmh=speed,
                heading=course,
                marker_image=marker_image,
                timestamp_text=timestamp.strftime("%Y-%m-%d %H:%M:%S"),
                show_gps_badge=live_fix,
                show_marker=not before_first_fix,
                width=width,
                height=height,
                base_image=base_image,
            )
            frame.save(frame_dir / f"frame_{frame_number:06d}.png")

        encode_frame_sequence(frame_dir, destination, fps)

    return destination
