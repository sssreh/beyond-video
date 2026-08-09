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
from .map_render import bbox_pixel_rect
from .map_render import compose_frame_overlay
from .map_render import draw_caption
from .map_render import render_base_map
from .map_render import render_frame_visual
from .media import encode_frame_sequence
from .osm_roads import Area
from .osm_roads import BoundingBox
from .osm_roads import Road
from .osm_roads import bounding_box_around_point
from .osm_roads import bounding_box_for_fixes
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

# render_intro_flyover()'s own defaults (task: Christer, having just
# imported a trip's KML into Google Earth to see the route flown
# through, asked "is it possible to extract that slide show and by an
# option be the introduction to the trip" - Google Earth Web's own
# flyover tour has no export API and screen-recording it would be
# fragile and against OSM/Google's own terms for automated capture, so
# this renders an equivalent establishing shot natively instead, from
# the same OSM road data map.mp4 already draws from).
#
# 5 seconds reads as a real "arriving at the map" beat without
# stalling the video open on something nobody asked to watch for long -
# short enough that even someone who skips intros wouldn't bother.
DEFAULT_INTRO_SECONDS = 5.0

# How many times wider than the final, "arrived" framing (the same
# whole-trip bbox map.mp4's own static overview uses) the flyover's
# very first frame starts at. 8x reads as a genuine "establishing shot"
# - enough context to place the trip in its surroundings before the
# camera commits to the route itself - without the starting view being
# so wide the roads are unrecognizable smudges for the flyover's own
# first second or two.
INTRO_ZOOM_START_MULTIPLIER = 8.0

# Cap on how large render_intro_flyover()'s own one-time high-resolution
# raster is allowed to get (see that function's docstring for why it
# renders one big raster at all rather than redrawing per frame). At
# INTRO_ZOOM_START_MULTIPLIER's default (8x), an uncapped raster would
# be 8x the output's own width/height on each axis - fine at map.mp4's
# usual few-hundred-pixel panel sizes, but sized to match a full
# stitch.mp4 output (this project's own most common --map-intro case)
# that raster could reach tens of thousands of pixels per side. Capping
# the long edge here trades a little sharpness in the flyover's very
# widest opening frames (where crops get upscaled slightly beyond 1:1)
# for a bounded, predictable render cost regardless of output size.
INTRO_MAX_RASTER_DIMENSION = 4096

# In `--map-zoom` (follow-camera) mode, how many of the most recent
# fixes' worth of "route driven so far" get projected/drawn each
# frame - unlike the static overview map, a follow-camera view only
# ever needs the trailing few seconds/minutes of the route (whatever's
# actually near the current position), not the whole trip's path.
# Fixes land roughly every second (~1Hz - see MAX_LIVE_FIX_GAP_SECONDS'
# own comment for real-world gap measurements), so 300 is a generous
# ~5 minutes of trailing trail at typical speeds - far more than a
# reasonable `--map-zoom` radius could ever actually show on screen,
# but small and constant regardless of the trip's own total fix count.
#
# Christer, on a real export with a Parking recording included: "the
# map phase took vvery long time" - 946.5s, wildly disproportionate to
# every other phase (gsensor.mp4: 131.2s, the entire --stitch encode:
# 189.3s). Root cause: every zoom frame passed the *entire* growing
# `route_so_far` (every real fix accumulated up to that frame) to
# render_frame(), which re-projects and redraws every single point of
# it from scratch, every frame - roads/areas already got exactly this
# same per-frame bbox-filtering treatment (see index_roads()/
# roads_within_bbox()), but the route polyline never did. A GPS fix
# stream that always includes every Parking-mode recording's own data
# regardless of --include-parking (see _merge_gps()) made this
# concretely worse: a Parking recording sitting at one spot for tens
# of minutes can log hundreds to thousands of nearly-identical fixes,
# every one of which used to get re-projected and redrawn on every
# single frame of the render for the rest of the trip - true O(frames
# x total fixes) cost, the exact bug class already fixed once for
# position interpolation (see _advance_fix_index()'s own docstring)
# but never applied to the route line itself. Capping the trailing
# window here fixes both: it was always wasteful even without Parking
# footage (a whole trip's worth of fixes redrawn every zoom frame,
# most of it permanently off-canvas), Parking-mode GPS logging just
# made the waste dramatically larger.
MAX_ZOOM_ROUTE_TRAIL_FIXES = 300

# How many decimal degrees of latitude/longitude precision matter when
# deciding whether consecutive frames' *visual* (background/roads/
# areas/route/marker - see map_render.render_frame_visual()) can be
# reused rather than redrawn from scratch. 5 decimal places is roughly
# 1.1m at the equator - well under a single GPS receiver's own normal
# jitter, so a genuinely parked car's tiny fix-to-fix wobble still
# counts as "the same place" here, while any real movement (even a
# slow crawl) still gets its own fresh frame.
#
# Christer, following up on the MAX_ZOOM_ROUTE_TRAIL_FIXES fix above
# with the real numbers behind why that render was still so slow: "the
# overall time of the video was over 1 hour and the fps on stitch is
# 6.84" - the Parking recording wasn't a fast, compressed timelapse (as
# this project had generally assumed - see trip_builder.py's own
# "1fps timelapse" comment), it was a real hour-plus of continuous,
# sparsely-captured (low native fps) parked footage. That means
# frame_count itself (total_seconds * fps below) was already enormous
# for that one recording's span - roughly 18,000 frames at this
# module's own 5fps - independent of the route-trail cost the constant
# above addresses. Nearly every one of those frames shows an
# essentially identical map view (the car hasn't moved), yet each used
# to trigger a full background+roads+areas+route+marker redraw anyway.
#
# Caching the last-rendered visual and reusing it whenever the
# rounded position, bbox, heading, marker-visibility, and route length
# all match the last redrawn frame's turns that redundant work into a
# single render_frame_visual() call reused for the whole stationary
# span - the timestamp/speed text and GPS badge (see
# map_render.compose_frame_overlay()) are cheap enough to still redraw
# on every single frame regardless, so the on-screen clock/speed
# readout never visibly freezes even while the map underneath is
# being reused.
STATIONARY_VISUAL_ROUND_DECIMALS = 5

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


def _load_marker_image(marker_image_path: Path | None) -> Image.Image | None:
    """Load and pre-scale a custom position-marker image, or return
    None if `marker_image_path` itself is None (the "no custom
    marker, draw the plain arrow instead" default both
    render_map_video() and render_intro_flyover() share).

    Raises MediaToolError if the image can't be loaded - same
    convention as every other external-file load in this module.

    Scaled once here (via MARKER_IMAGE_SCALE), not per frame inside
    _paste_marker_image()'s own per-frame rotate/paste - the marker
    image itself never changes mid-render, so resizing it once up
    front is free compared to redoing it on every single frame. See
    MARKER_IMAGE_SCALE's own comment for why this applies uniformly to
    any marker image, bundled or custom.

    Factored out of render_map_video() (where this logic originally
    lived inline) so render_intro_flyover() can load a marker exactly
    the same way without duplicating the try/except/resize block.
    """

    if marker_image_path is None:
        return None

    try:
        marker_image = Image.open(marker_image_path).convert("RGBA")
    except (FileNotFoundError, OSError) as exc:
        raise MediaToolError(
            f"could not load marker image {marker_image_path}: {exc}"
        ) from exc

    scaled_size = (
        max(1, round(marker_image.width * MARKER_IMAGE_SCALE)),
        max(1, round(marker_image.height * MARKER_IMAGE_SCALE)),
    )
    return marker_image.resize(scaled_size, resample=Image.LANCZOS)


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
    track_up: bool = False,
) -> Path | None:
    """Render a trip's merged GPS fixes into an overlay video at
    `destination`: the route driven so far, current position/heading,
    speed, and timestamp, drawn against `roads` and `areas` (water/
    green polygons, see osm_roads.py) - `areas` defaults to empty and
    is entirely optional, same as `roads` was before this existed.

    `track_up` (default False, task #512, Christer: "Could we let the
    maps (booth of them) rotate as the car turns, instead of having
    north at the top") rotates every frame's whole projected scene -
    roads, areas, route, and marker position, via
    map_render.render_frame_visual()'s own `track_up` handling - so the
    vehicle's current heading always points "up" instead of true north.
    Applies to both the static overview (`zoom_meters=None`) and the
    zoomed follow-camera (`zoom_meters` given) modes; in the static
    case it also disables the whole-render `base_image` reuse this
    function otherwise relies on (see the `base_image` local below),
    since a cached background was drawn for a fixed, unrotated scene
    and can't be shared across frames whose rotation now changes with
    every heading change - a real (opt-in) cost, not free like the
    default north-up render. No effect on frames with no heading
    available (a stationary/single-fix span) - those fall back to the
    plain unrotated draw, same as map_render.py's own no-heading
    fallback.

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

    marker_image = _load_marker_image(marker_image_path)

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
    # its own per-frame road drawing. `track_up` also forces this to
    # None even in static mode - a cached base image was drawn once for
    # a fixed, unrotated scene, and track-up needs a fresh rotation
    # (and therefore a fresh road/area redraw) on every frame whose
    # heading differs from the last, so there's nothing safe to reuse
    # (see this function's own `track_up` docstring paragraph).
    base_image = (
        None
        if zoom_meters is not None or track_up
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

        # Frame-to-frame visual reuse (see STATIONARY_VISUAL_ROUND_
        # DECIMALS' own comment) - `cached_visual` holds the last
        # render_frame_visual() output, `cached_signature` the inputs
        # it was rendered from. A later frame whose own signature
        # matches skips render_frame_visual() entirely and reuses the
        # cached image (still copied fresh per frame, since
        # compose_frame_overlay() mutates its own copy, not this one).
        cached_visual = None
        cached_signature = None

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

            # Zoom mode only ever shows a small area around the current
            # position, so only the most recent MAX_ZOOM_ROUTE_TRAIL_FIXES
            # points of route_so_far can possibly be on-screen - see that
            # constant's own comment for the full story (946.5s map phase
            # on a trip with a stationary Parking recording). Static
            # (whole-trip overview) mode keeps the full route: showing
            # the entire path is the point there.
            route_points = (
                route_so_far[-MAX_ZOOM_ROUTE_TRAIL_FIXES:]
                if zoom_meters is not None
                else route_so_far
            )

            show_marker = not before_first_fix
            # See STATIONARY_VISUAL_ROUND_DECIMALS' own comment. Only
            # the inputs render_frame_visual() actually draws with need
            # to match. `route_tip` stands in for the route's own
            # content: route_so_far only ever grows, so its rounded
            # last point is enough to tell whether the drawn line
            # itself could have visibly changed since the last render -
            # deliberately NOT len(route_points), which would keep
            # invalidating the cache every time a new (but
            # rounds-to-the-same-spot) fix gets folded in during a
            # truly stationary span, exactly the case this cache exists
            # for (a parked car logging a fix every ~1s for the better
            # part of an hour). `frame_bbox` already captures zoom
            # mode's own per-frame recentering, so a signature match
            # there is sufficient without separately checking
            # frame_roads/frame_areas (both pure functions of
            # frame_bbox given the same indexed_roads/indexed_areas).
            route_tip = (
                (
                    round(route_so_far[-1][0], STATIONARY_VISUAL_ROUND_DECIMALS),
                    round(route_so_far[-1][1], STATIONARY_VISUAL_ROUND_DECIMALS),
                )
                if route_so_far
                else None
            )
            signature = (
                frame_bbox,
                round(lat, STATIONARY_VISUAL_ROUND_DECIMALS),
                round(lon, STATIONARY_VISUAL_ROUND_DECIMALS),
                course,
                show_marker,
                route_tip,
            )

            if cached_visual is not None and signature == cached_signature:
                visual = cached_visual
            else:
                visual = render_frame_visual(
                    frame_bbox,
                    frame_roads,
                    tuple(route_points) + (position,),
                    position,
                    areas=frame_areas,
                    heading=course,
                    marker_image=marker_image,
                    show_marker=show_marker,
                    width=width,
                    height=height,
                    base_image=base_image,
                    track_up=track_up,
                )
                cached_visual = visual
                cached_signature = signature

            frame = compose_frame_overlay(
                visual,
                speed_kmh=speed,
                timestamp_text=timestamp.strftime("%Y-%m-%d %H:%M:%S"),
                show_gps_badge=live_fix,
                width=width,
                height=height,
            )
            frame.save(frame_dir / f"frame_{frame_number:06d}.png")

        encode_frame_sequence(frame_dir, destination, fps)

    return destination


def _ease_out_cubic(t: float) -> float:
    """Standard ease-out cubic easing (0 at t=0, 1 at t=1, decelerating
    into the landing) - used by render_intro_flyover() so the camera
    reads as "arriving" at the trip's route and settling into place,
    rather than zooming in at a constant, mechanical-looking rate."""

    return 1 - (1 - t) ** 3


def _lerp_bbox(start: BoundingBox, end: BoundingBox, t: float) -> BoundingBox:
    """Linearly interpolate each of `start`/`end`'s four corners
    independently - used to animate render_intro_flyover()'s camera
    from a wide establishing view down to the trip's own final framing.
    Plain per-corner interpolation (not a radius/center split) works
    here because both boxes share the same center and aspect ratio by
    construction (see render_intro_flyover()'s own `start_bbox`) - the
    corners alone fully describe the zoom."""

    return BoundingBox(
        min_lat=start.min_lat + (end.min_lat - start.min_lat) * t,
        min_lon=start.min_lon + (end.min_lon - start.min_lon) * t,
        max_lat=start.max_lat + (end.max_lat - start.max_lat) * t,
        max_lon=start.max_lon + (end.max_lon - start.max_lon) * t,
    )


def _scale_bbox_from_center(bbox: BoundingBox, multiplier: float) -> BoundingBox:
    """Scale `bbox` by `multiplier` on both axes, keeping its own
    center fixed - the wide-establishing-shot math render_intro_
    flyover()'s `start_bbox` and intro_start_bbox() (below) both need,
    factored out once so the two stay in exact agreement rather than
    two independently-typed copies of the same four lines drifting
    apart under a future edit."""

    center_lat = (bbox.min_lat + bbox.max_lat) / 2
    center_lon = (bbox.min_lon + bbox.max_lon) / 2
    half_lat = (bbox.max_lat - bbox.min_lat) / 2 * multiplier
    half_lon = (bbox.max_lon - bbox.min_lon) / 2 * multiplier
    return BoundingBox(
        min_lat=center_lat - half_lat,
        min_lon=center_lon - half_lon,
        max_lat=center_lat + half_lat,
        max_lon=center_lon + half_lon,
    )


def intro_start_bbox(
    fixes: tuple[GpsFix, ...],
    *,
    width: int = DEFAULT_WIDTH,
    height: int = DEFAULT_HEIGHT,
    zoom_start_multiplier: float = INTRO_ZOOM_START_MULTIPLIER,
) -> BoundingBox | None:
    """The wide establishing-shot bounding box render_intro_flyover()'s
    very first frame opens on - exposed as its own function (rather
    than staying inline in render_intro_flyover() alone) so
    trip_export.py's OSM road/area fetch can be widened to actually
    cover it ahead of time.

    That widening matters because of a real gap otherwise: roads/areas
    are normally only fetched for a trip's own bounding box plus a
    ~1km margin (see osm_roads.DEFAULT_MARGIN_DEGREES) - map.mp4's
    static overview never needs more than that, but this box is
    `zoom_start_multiplier`x wider on both axes, so without widening
    the fetch too, the flyover's own widest, most "establishing" frame
    would render as mostly blank map - the opposite of Christer's own
    ask ("started with the whole map showing"). See trip_export.py's
    _load_trip_roads() for where this gets used that way.

    Returns None under the same "nothing to bound" conditions
    bounding_box_for_fixes() does.
    """

    aspect_ratio = width / height
    end_bbox = bounding_box_for_fixes(fixes, aspect_ratio=aspect_ratio)
    if end_bbox is None:
        return None
    return _scale_bbox_from_center(end_bbox, zoom_start_multiplier)


def render_intro_flyover(
    fixes: tuple[GpsFix, ...],
    roads: tuple[Road, ...],
    destination: Path,
    *,
    areas: tuple[Area, ...] = (),
    duration_seconds: float = DEFAULT_INTRO_SECONDS,
    fps: int = DEFAULT_FPS,
    marker_image_path: Path | None = None,
    width: int = DEFAULT_WIDTH,
    height: int = DEFAULT_HEIGHT,
    zoom_start_multiplier: float = INTRO_ZOOM_START_MULTIPLIER,
    caption: str | None = None,
) -> Path | None:
    """Render a short establishing-shot "flyover" of a trip's whole
    route at `destination`: the camera starts on a wide view of the
    surrounding area and eases into the same tight framing map.mp4's
    own static overview uses, with the complete route already drawn
    (not built up over time the way map.mp4's "so far" line is - this
    is a scene-setting shot, not a position readout, so the whole path
    is there to be revealed as the camera arrives).

    Built for bv-export's `--map-intro` (Christer: "is it possible to
    extract that slide show and by an option be the introduction to
    the trip", after importing a KML export into Google Earth Web and
    watching its own flyover tour there - see kml_writer.py and
    web/app.py's trip_kml() route for that KML export feature). Google
    Earth's own flyover has no export API to pull a video out of, so
    this renders an equivalent shot natively, from the same locally-
    drawn OSM road data (osm_roads.py) map.mp4 already uses - no
    screen-recording, no dependency on a browser being open, and no
    question of whether capturing Google's own rendered tour would be
    consistent with its usage terms.

    Unlike render_map_video(), this isn't driven by real elapsed trip
    time at all - `duration_seconds` is the flyover's own fixed screen
    time (a handful of seconds), covering the whole render regardless
    of how long the actual trip took. The camera's motion is a single
    zoom (no pan): both the start and end framing share the same
    real-world center - the trip's own bounding-box center, via
    bounding_box_for_fixes(fixes, aspect_ratio=width/height), the exact
    box map.mp4's own static overview already frames itself with - so
    the flyover's last frame lines up with what a viewer sees next if
    map.mp4 (or --stitch-map) plays right after it. The starting frame
    is that same box scaled `zoom_start_multiplier`x wider on both
    axes (see intro_start_bbox(), which computes this same box - and
    is what trip_export.py widens its own OSM road/area fetch against,
    so this wide opening frame actually has real map data to show
    rather than rendering mostly blank - Christer: "started with the
    whole map showing"), still centered on the same point; every frame
    in between is a per-corner linear interpolation (_lerp_bbox()) of
    an eased progress fraction (_ease_out_cubic()) between the two,
    which reads as a smooth zoom-in that settles rather than one that
    arrives at a constant, mechanical rate.

    The position marker (the same plain arrow, or a custom
    `marker_image_path`, render_map_video() itself uses) sits at the
    trip's own first real fix for the whole shot - there's no
    "current position" concept here, just a mark of where the trip
    that's about to play actually begins.

    `caption`, if given, is drawn on every frame as a bottom-centered,
    subtitle-style title card (see map_render.py's draw_caption()) -
    Christer, in the same request that asked for the wide opening
    view above: "with the prefix and trip name on like subtitles".
    trip_export.py passes its own `destination.name` (the exported
    trip folder's own name, which already bakes prefix + trip label
    together - see folder_name_for_trip()) for this. `None` (the
    default) draws no caption at all, unchanged from before this
    parameter existed.

    Returns None (and writes nothing) if there aren't at least two
    valid, positioned fixes to draw a route from - same "nothing to
    work with" convention render_map_video() uses.

    Renders one high-resolution raster of the whole shot up front (at
    `start_bbox`, the widest framing) and produces every animated
    frame from it via a plain crop+resize, instead of redrawing roads/
    areas from scratch on every frame the way earlier versions of this
    function did. That's a valid shortcut specifically because of how
    the zoom is built: every frame_bbox in between is always a smaller,
    concentric sub-rectangle of start_bbox (both share the same center
    by construction - see _lerp_bbox()'s own docstring), so a single
    raster covering start_bbox already contains everything any later,
    tighter frame needs to show; bbox_pixel_rect() (map_render.py) is
    what finds the matching crop rectangle for each frame's own bbox.

    This exists because of a real, measured cost: redrawing per frame
    meant every frame paid roads_within_bbox()'s O(n) linear-scan cost
    against whatever road/area pool the caller passed in - normally
    small, but `--map-intro` widens that pool by design (see
    trip_export.py's _load_trip_roads()), and Christer hit this
    directly on a real export: "map phase went from 16 seconds to 335
    seconds" after asking for the wider establishing shot, then "Could
    we just get the overview intro map and then overlay the zoomed in
    flyby, then we don't need to render every step of the big map" -
    this is that idea, implemented as a proper Ken-Burns-style crop
    zoom (one real photo, panned/scaled) rather than a literal second
    video layer composited on top, since the existing smooth eased
    zoom is worth keeping and a crop of one raster reproduces it
    exactly, just without paying to redraw it every frame.

    The raster itself is oversampled by up to `zoom_start_multiplier`x
    linearly (capped at INTRO_MAX_RASTER_DIMENSION per side - see that
    constant's own comment for why) so the *tightest* (last) frame's
    crop still has enough source pixels to fill `width`x`height`
    without visibly upscaling; the wide opening frame is the raster
    itself, at full native resolution. One side effect worth knowing
    about: the position marker is now baked into the single raster
    rather than redrawn at a constant on-screen size every frame, so
    it visibly grows as the camera zooms in instead of staying a fixed
    pixel size throughout - arguably more cinematically correct for a
    "camera flying toward a fixed point" shot, and not something
    Christer has weighed in on either way yet.
    """

    positioned = _valid_positioned_fixes(fixes)
    if len(positioned) < 2:
        return None

    aspect_ratio = width / height
    end_bbox = bounding_box_for_fixes(fixes, aspect_ratio=aspect_ratio)
    if end_bbox is None:
        return None

    start_bbox = _scale_bbox_from_center(end_bbox, zoom_start_multiplier)

    marker_image = _load_marker_image(marker_image_path)

    route_points = tuple((fix.latitude, fix.longitude) for fix in positioned)
    start_position = route_points[0]
    start_heading = positioned[0].course

    # How much bigger than the output frame size to render the one
    # raster - up to zoom_start_multiplier (beyond that buys no extra
    # sharpness, since the tightest crop is already native resolution
    # at that point), capped so a huge output size (e.g. sized to
    # match a full stitch.mp4) can't blow up the raster's own memory/
    # render cost unboundedly.
    raster_scale = zoom_start_multiplier
    if width:
        raster_scale = min(raster_scale, INTRO_MAX_RASTER_DIMENSION / width)
    if height:
        raster_scale = min(raster_scale, INTRO_MAX_RASTER_DIMENSION / height)
    raster_scale = max(raster_scale, 1.0)

    raster_width = max(width, round(width * raster_scale))
    raster_height = max(height, round(height * raster_scale))

    raster_roads = roads_within_bbox(index_roads(roads), start_bbox)
    raster_areas = features_within_bbox(index_features(areas), start_bbox)

    raster = render_frame_visual(
        start_bbox,
        raster_roads,
        route_points,
        start_position,
        areas=raster_areas,
        heading=start_heading,
        marker_image=marker_image,
        show_marker=True,
        width=raster_width,
        height=raster_height,
    )

    frame_count = max(2, int(duration_seconds * fps) + 1)
    last_frame_index = frame_count - 1

    destination.parent.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory() as frame_dir_name:
        frame_dir = Path(frame_dir_name)

        for frame_number in range(frame_count):
            t = frame_number / last_frame_index if last_frame_index else 1.0
            eased_t = _ease_out_cubic(t)
            frame_bbox = _lerp_bbox(start_bbox, end_bbox, eased_t)

            crop_box = bbox_pixel_rect(
                frame_bbox, start_bbox, raster_width, raster_height
            )
            visual = raster.resize(
                (width, height), resample=Image.LANCZOS, box=crop_box
            )

            if caption:
                visual = draw_caption(visual, caption, width=width, height=height)
            visual.save(frame_dir / f"frame_{frame_number:06d}.png")

        encode_frame_sequence(frame_dir, destination, fps)

    return destination
