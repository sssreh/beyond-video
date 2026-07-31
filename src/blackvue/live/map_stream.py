"""
Live scrolling map rendering for bv-live: follows the camera's current
live position at a fixed real-world radius (bv-live's own --map-zoom,
default 100m - "scrolling map ... with default zoom of 100m", per
Christer), the same "follow camera" framing bv-export's own
--map-zoom uses (see export/map_video.py), but driven by
TelemetryState's live, continuously-growing buffer instead of a
trip's already-complete, known-length set of GPS fixes.

Reuses export/map_render.py's render_frame() as-is for the actual
per-frame drawing (background/roads/areas/route/marker/text - none of
that cares whether the data behind it came from a finished trip or a
live feed) and export/osm_roads.py's fetch/cache/bbox-filter machinery
for road and water/green-area geometry. What's new here is entirely
about *managing a live, open-ended stream* rather than drawing a
single frame: LiveMapRegion decides when the live view has scrolled
far enough to need fresh OSM data fetched/cached for a new area, and
render_live_map_frame() computes the live position/route/heading a
finished-trip render would otherwise get handed already-known.

Copyright (C) 2026 Christer R. (sssreh)

SPDX-License-Identifier: GPL-3.0-or-later
"""

from __future__ import annotations

import math
import time
from pathlib import Path

from PIL import Image
from PIL import ImageDraw
from PIL import ImageFont

from ..export.map_render import BACKGROUND_COLOR
from ..export.map_render import DEFAULT_MARGIN_PX
from ..export.map_render import render_frame
from ..export.map_video import DEFAULT_MAP_ICON_PATH
from ..export.map_video import MARKER_IMAGE_SCALE
from ..export.osm_roads import Area
from ..generate.media import MediaToolError
from ..export.osm_roads import BoundingBox
from ..export.osm_roads import Road
from ..export.osm_roads import bounding_box_around_point
from ..export.osm_roads import features_within_bbox
from ..export.osm_roads import index_features
from ..export.osm_roads import index_roads
from ..export.osm_roads import load_or_fetch_areas
from ..export.osm_roads import load_or_fetch_roads
from ..export.osm_roads import roads_within_bbox
from .telemetry import GpsSample
from .telemetry import TelemetryState

# "a little bigger than usual" - Christer, since the live camera feed
# itself is small. export/map_render.py's own standalone default is
# 640x640; this is deliberately a step up from that for bv-live's own
# panel, not a change to the export default itself.
DEFAULT_WIDTH = 720
DEFAULT_HEIGHT = 720

# Christer's own explicit instruction: "a scrolling map to the left ...
# with default zoom of 100m" - matches --map-zoom's own meaning (a
# real-world half-width in meters; see
# osm_roads.bounding_box_around_point()'s docstring).
DEFAULT_ZOOM_METERS = 100.0

# How much of the live view's own trailing route to draw behind the
# current position - long enough to read as a real trail of recent
# movement, short enough not to clutter a small-radius, close-up
# follow-camera view (unlike a trip's own whole-route overview) with a
# trail that's mostly scrolled off-frame anyway.
DEFAULT_ROUTE_SECONDS = 120.0

# LiveMapRegion fetches/caches an area this many times wider than the
# current --map-zoom radius, so ordinary driving doesn't need a fresh
# Overpass request every time the view scrolls a little - see its own
# ensure_covers() docstring.
REGION_PADDING_FACTOR = 8.0

# How long LiveMapRegion waits after a failed Overpass fetch (a 504
# Gateway Timeout, a network blip) before trying again - Christer,
# after the live map stream went black on a real Overpass timeout: "it
# shouldn't go totally black, it should wait a little and then try
# again." Long enough not to hammer a public, rate-limited, possibly
# already-overloaded API every single frame (bv-live's map panel
# renders at multiple fps - see live/app.py's own frame rate); short
# enough that a transient outage recovers within a session rather than
# needing a restart. A first guess, not a measured value - easy to
# widen if Overpass ever needs a gentler retry cadence in practice.
RETRY_DELAY_SECONDS = 30.0

# render_frame()'s own show_gps_badge only means anything meaningful
# for a live view: whether the position on screen right now is a
# genuinely fresh reading, or bv-live is still showing the last one it
# got because the telemetry feed has gone quiet (e.g. the camera lost
# its GPS lock, or the connection dropped and LiveTelemetryPump is
# busy reconnecting - see telemetry.py's RECONNECT_DELAY_SECONDS).
LIVE_FRESHNESS_SECONDS = 5.0

PLACEHOLDER_TEXT_COLOR = (120, 120, 120)

# Reuses bv-export's own bundled red-car marker (see export/map_video
# .py's DEFAULT_MAP_ICON_PATH/MARKER_IMAGE_SCALE) so the live map's
# position marker matches map.mp4's own default instead of falling
# back to the plain procedural arrow - Christer: "can i have my red
# car on the bv-live map". Loaded and scaled once, lazily, the first
# time a frame actually needs it rather than at import time - a bad or
# missing bundled asset should degrade the live map to the plain arrow
# marker it already had, not prevent bv-live from starting at all.
_marker_image: Image.Image | None = None
_marker_image_load_attempted = False


def _live_marker_image() -> Image.Image | None:
    global _marker_image, _marker_image_load_attempted

    if not _marker_image_load_attempted:
        _marker_image_load_attempted = True
        try:
            loaded = Image.open(DEFAULT_MAP_ICON_PATH).convert("RGBA")
            scaled_size = (
                max(1, round(loaded.width * MARKER_IMAGE_SCALE)),
                max(1, round(loaded.height * MARKER_IMAGE_SCALE)),
            )
            _marker_image = loaded.resize(scaled_size, resample=Image.LANCZOS)
        except (FileNotFoundError, OSError):
            _marker_image = None

    return _marker_image


def _bbox_contains(outer: BoundingBox, inner: BoundingBox) -> bool:
    """Whether `inner` is fully within `outer` - a plain rectangle
    -containment check on the two boxes' own lat/lon bounds, not a
    real-world-distance calculation. Used by LiveMapRegion to decide
    whether the currently-cached region still covers the live view's
    own current frame, without needing any separate distance math."""

    return (
        outer.min_lat <= inner.min_lat
        and outer.max_lat >= inner.max_lat
        and outer.min_lon <= inner.min_lon
        and outer.max_lon >= inner.max_lon
    )


def _bearing_degrees(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Compass bearing (0-360, clockwise from north) from (lat1, lon1)
    to (lat2, lon2) - the standard great-circle initial-bearing
    formula.

    blackvue_livedata.cgi's own GPS object carries no course-over
    -ground field, unlike the offline .gps files' NMEA-derived one
    (see telemetry.GpsSample's own docstring) - render_live_map_frame()
    computes a heading itself from the last two distinct live
    positions instead, so the live position marker can still be a
    rotated arrow rather than always falling back to a plain dot.
    """

    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    delta_lon = math.radians(lon2 - lon1)

    x = math.sin(delta_lon) * math.cos(phi2)
    y = math.cos(phi1) * math.sin(phi2) - math.sin(phi1) * math.cos(phi2) * math.cos(
        delta_lon
    )

    return math.degrees(math.atan2(x, y)) % 360


def _heading_from_history(history: tuple[GpsSample, ...]) -> float | None:
    """Return a heading computed from the last two *distinct*
    positions in `history` (searching backward so a few stationary,
    identical-position samples in a row - normal GPS jitter while
    parked - don't just make this return None), or None if there
    aren't two distinct positions to compute one from at all."""

    if len(history) < 2:
        return None

    latest = history[-1]
    for sample in reversed(history[:-1]):
        if (sample.latitude, sample.longitude) != (latest.latitude, latest.longitude):
            return _bearing_degrees(
                sample.latitude, sample.longitude, latest.latitude, latest.longitude
            )

    return None


class LiveMapRegion:
    """Owns the OSM road/area geometry currently cached for the live
    map's surrounding area, re-fetching (via osm_roads.py's own
    fetch-then-cache-to-disk helpers) only when the live view has
    scrolled somewhere the current cache no longer covers.

    `cache_dir` is the same `.osm_cache` convention bv-export's own
    --map/--map-zoom use (see cli/bv_live.py) - a directory of raw
    Overpass JSON responses keyed by bounding box, safe to share
    across bv-live/bv-export runs over the same camera's archive.
    """

    def __init__(self, cache_dir: Path) -> None:
        self._cache_dir = cache_dir
        self._bbox: BoundingBox | None = None
        self._indexed_roads: tuple[tuple[Road, BoundingBox], ...] = ()
        self._indexed_areas: tuple[tuple[Area, BoundingBox], ...] = ()
        # See RETRY_DELAY_SECONDS's own comment - set whenever a fetch
        # fails, cleared on the next successful one.
        self._last_fetch_error_at: float | None = None

    def ensure_covers(self, lat: float, lon: float, zoom_meters: float) -> None:
        """Make sure the cached region covers a live frame centered on
        (lat, lon) at `zoom_meters` - fetching (and caching to disk) a
        fresh, more generously padded region around the current
        position first if it doesn't.

        REGION_PADDING_FACTOR means ordinary driving only triggers a
        fresh Overpass request roughly every REGION_PADDING_FACTOR/2
        multiples of `zoom_meters` of actual travel, not on every
        single frame - the same one-fetch-then-reuse spirit
        osm_roads.load_or_fetch_roads()'s own on-disk cache already
        applies across separate runs, just also applied *within* one
        live session's own continuous scrolling.

        A failed fetch (osm_roads.py raises MediaToolError for any
        network/parse failure - a 504 Gateway Timeout, Overpass briefly
        unreachable, a malformed response) does NOT raise out of this
        method. Christer, after a real Overpass timeout took the whole
        live map stream down: "it shouldn't go totally black, it
        should wait a little and then try again." Instead: whatever
        geometry was already cached stays in place untouched (a frame
        keeps rendering with it, just not yet updated for how far the
        view may have scrolled since), or - if this is the very first
        fetch of the session - renders with no roads/areas at all
        (render_frame() draws fine with empty geometry: a plain
        background, route trail, and marker, not a crash). Either way,
        the next RETRY_DELAY_SECONDS are skipped without even
        attempting another request, so a real outage doesn't turn into
        a fresh Overpass hit on every single rendered frame.
        """

        frame_bbox = bounding_box_around_point(lat, lon, zoom_meters)

        if self._bbox is not None and _bbox_contains(self._bbox, frame_bbox):
            return

        now = time.monotonic()
        if (
            self._last_fetch_error_at is not None
            and now - self._last_fetch_error_at < RETRY_DELAY_SECONDS
        ):
            return

        fetch_bbox = bounding_box_around_point(
            lat, lon, zoom_meters * REGION_PADDING_FACTOR
        )
        try:
            roads = load_or_fetch_roads(fetch_bbox, self._cache_dir)
            areas = load_or_fetch_areas(fetch_bbox, self._cache_dir)
        except MediaToolError:
            self._last_fetch_error_at = now
            return

        self._bbox = fetch_bbox
        self._indexed_roads = index_roads(roads)
        self._indexed_areas = index_features(areas)
        self._last_fetch_error_at = None

    def roads_near(self, frame_bbox: BoundingBox) -> tuple[Road, ...]:
        return roads_within_bbox(self._indexed_roads, frame_bbox)

    def areas_near(self, frame_bbox: BoundingBox) -> tuple[Area, ...]:
        return features_within_bbox(self._indexed_areas, frame_bbox)


def _placeholder_frame(width: int, height: int, text: str) -> Image.Image:
    """A plain background frame with a centered status message - shown
    in place of a real map for as long as bv-live has no GPS fix at
    all yet (just started, or the camera genuinely has no lock), so
    the panel doesn't sit blank/broken-looking with no explanation.

    Uses PIL's own built-in default font rather than map_render.py's
    bundled Unicode-capable one - the fixed English status text here
    never contains non-ASCII characters (unlike map_render.py's own
    street-name labels), so there's no a/a/o-glyph problem to solve,
    and no reason to reach into that module's own private
    _load_font()."""

    image = Image.new("RGB", (width, height), BACKGROUND_COLOR)
    draw = ImageDraw.Draw(image)
    font = ImageFont.load_default()
    draw.text(
        (width / 2, height / 2), text, font=font, fill=PLACEHOLDER_TEXT_COLOR,
        anchor="mm", align="center",
    )
    return image


def render_live_map_frame(
    state: TelemetryState,
    region: LiveMapRegion,
    *,
    zoom_meters: float = DEFAULT_ZOOM_METERS,
    route_seconds: float = DEFAULT_ROUTE_SECONDS,
    width: int = DEFAULT_WIDTH,
    height: int = DEFAULT_HEIGHT,
) -> Image.Image:
    """Render one live map frame from `state`'s current buffer -
    background roads/areas, the recent route trail, the current
    position marker (the same bundled red-car icon map.mp4 defaults
    to, rotated to heading when one could be computed - see
    _heading_from_history()/_live_marker_image() - or pointing "up"
    when it can't yet), and a live-GPS badge that's only lit while the
    latest reading is still fresh (see LIVE_FRESHNESS_SECONDS).

    Returns a plain "waiting for GPS fix" placeholder frame instead if
    `state` has no GPS reading at all yet. A failed Overpass fetch
    (see LiveMapRegion.ensure_covers()) does NOT propagate out of this
    function either - the frame still renders, just with whatever road/
    area geometry was already cached (possibly none yet).
    """

    latest = state.latest_gps()
    if latest is None:
        return _placeholder_frame(width, height, "waiting for GPS fix...")

    region.ensure_covers(latest.latitude, latest.longitude, zoom_meters)

    frame_bbox = bounding_box_around_point(
        latest.latitude, latest.longitude, zoom_meters, aspect_ratio=width / height
    )
    roads = region.roads_near(frame_bbox)
    areas = region.areas_near(frame_bbox)

    history = state.gps_history(route_seconds)
    route_points = tuple((sample.latitude, sample.longitude) for sample in history)
    heading = _heading_from_history(history)

    is_live = (time.monotonic() - latest.at) <= LIVE_FRESHNESS_SECONDS

    return render_frame(
        frame_bbox,
        roads,
        route_points,
        (latest.latitude, latest.longitude),
        areas=areas,
        heading=heading,
        marker_image=_live_marker_image(),
        show_gps_badge=is_live,
        width=width,
        height=height,
        margin=DEFAULT_MARGIN_PX,
    )


def live_map_frames(
    state: TelemetryState,
    region: LiveMapRegion,
    *,
    zoom_meters: float = DEFAULT_ZOOM_METERS,
    route_seconds: float = DEFAULT_ROUTE_SECONDS,
    width: int = DEFAULT_WIDTH,
    height: int = DEFAULT_HEIGHT,
):
    """Return a zero-argument callable that renders the current live
    map frame from `state`/`region` - the shape live/mjpeg.py's
    rendered_frame_stream() expects to call repeatedly forever (see
    live/app.py's /stream/map route). Mirrors gsensor_stream.py's own
    live_gsensor_frames() factory."""

    def _render() -> Image.Image:
        return render_live_map_frame(
            state, region, zoom_meters=zoom_meters, route_seconds=route_seconds,
            width=width, height=height,
        )

    return _render
