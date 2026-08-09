"""
Map-overlay frame rendering for bv-export.

Draws one frame of a trip's route on a simple basemap built from OSM
road/water/green-area geometry (blackvue.export.osm_roads) - no live
map tiles are fetched or drawn here, this module only draws filled
polygons/lines/dots/text with Pillow from data already in memory.
Roads and areas are projected from lat/lon into pixel space with a
simple equirectangular projection (longitude scaled by cos(mean
latitude)); a full Mercator projection would be overkill at the scale
a single driving trip covers and adds complexity for no visible
benefit.

Water/green areas (osm_roads.Area) are drawn as filled polygons
*before* roads, so road lines stay visible on top of them - the same
background-then-foreground layering a real map uses. Optional (an
empty `areas` tuple, the default everywhere below, draws nothing new
and looks exactly like before this was added) - see osm_roads.py's
fetch_areas()/load_or_fetch_areas().

Roads are drawn (and, for major named ones, labeled) by `_draw_roads()`
- color/width vary by each road's own OSM `highway=*` tag (a motorway
reads as a bold, distinct line; a residential street a plain muted
one; a footpath thinner still - see `_ROAD_STYLE_BY_HIGHWAY`), and a
sufficiently long, major, named road gets its own street name label at
its longest visible segment (see `_LABELED_HIGHWAY_TYPES`) - one label
per distinct name per frame, not one per OSM way, since a single real
street is typically split into many ways.

The current-position marker is an arrow rotated to the GPS course
over ground by default, or a custom image (also rotated) when one is
supplied (bv-export --map-icon) - see render_frame()'s docstring.

Copyright (C) 2026 Christer R. (sssreh)

SPDX-License-Identifier: GPL-3.0-or-later
"""

from __future__ import annotations

import math
from pathlib import Path

from PIL import Image
from PIL import ImageDraw
from PIL import ImageFont

from .osm_roads import Area
from .osm_roads import BoundingBox
from .osm_roads import Road

BACKGROUND_COLOR = (247, 244, 238)
ROAD_COLOR = (140, 134, 122)
WATER_COLOR = (176, 205, 219)
GREEN_COLOR = (199, 217, 178)
ROUTE_COLOR = (230, 57, 70)
POSITION_DOT_COLOR = (230, 57, 70)
POSITION_DOT_OUTLINE = (255, 255, 255)
MARKER_FILL_COLOR = (230, 57, 70)
MARKER_OUTLINE_COLOR = (255, 255, 255)
TEXT_COLOR = (40, 40, 40)

# OSM highway=* tag values this project recognizes for styling
# (color, width) - loosely modeled on how common light basemap styles
# (e.g. CartoDB Positron, the muted look BACKGROUND_COLOR/ROAD_COLOR
# above already borrow from) differentiate a motorway from a
# residential street from a footpath, tuned to still read clearly at
# this project's typical 640x640 render. Christer asked for "street
# names road colors" without specifying an exact palette - this is a
# first pass, easy to retune (just this one table) once he's seen a
# real render; not yet confirmed against his own taste.
_ROAD_STYLE_BY_HIGHWAY: dict[str, tuple[tuple[int, int, int], int]] = {
    "motorway": ((237, 139, 47), 5),
    "motorway_link": ((237, 139, 47), 4),
    "trunk": ((242, 168, 74), 5),
    "trunk_link": ((242, 168, 74), 4),
    "primary": ((247, 193, 110), 4),
    "primary_link": ((247, 193, 110), 3),
    "secondary": ((250, 214, 145), 3),
    "secondary_link": ((250, 214, 145), 3),
    "tertiary": ((214, 209, 197), 3),
    "tertiary_link": ((214, 209, 197), 3),
    "unclassified": ((190, 184, 172), 2),
    "residential": ((190, 184, 172), 2),
    "living_street": ((190, 184, 172), 2),
    "service": ((205, 200, 190), 1),
    "track": ((205, 200, 190), 1),
    "footway": ((180, 178, 172), 1),
    "path": ((180, 178, 172), 1),
    "cycleway": ((180, 178, 172), 1),
    "pedestrian": ((180, 178, 172), 1),
    "steps": ((180, 178, 172), 1),
}
# Anything not in the table above - an unmapped/uncommon highway=*
# value, or "" for a Road cached/constructed before this project kept
# the tag at all (see osm_roads.Road's own docstring) - renders
# identically to this project's original flat, single-color styling,
# so nothing regresses for a road type this table doesn't happen to
# name.
_DEFAULT_ROAD_STYLE: tuple[tuple[int, int, int], int] = (ROAD_COLOR, 2)

# Only major through-roads get a name label. This was originally the
# 8 types just below the comment block (down through
# unclassified/residential/living_street), but Christer flagged real
# city driving as too cluttered - a dense residential grid has far more
# named streets than a frame this size can label without every name
# overlapping its neighbors. Confirmed with a synthetic dense-grid
# render (8 residential cross-streets alongside 4 arterials): the old
# set produced 12 overlapping labels crowding the top of the frame,
# this narrower set produces 4 clearly separated ones. Deliberately a
# strict subset of _ROAD_STYLE_BY_HIGHWAY's own keys (every entry here
# has a styling entry too), not the inverse.
_LABELED_HIGHWAY_TYPES = frozenset({
    "motorway", "trunk", "primary", "secondary", "tertiary",
})
# A road whose own on-screen length is shorter than this isn't worth
# labeling - the text would be wider than the road itself, illegible
# clutter rather than a useful label.
_MIN_LABELED_ROAD_LENGTH_PX = 40
# Smaller than the default 18px speed/timestamp font (DEFAULT_MARGIN_PX
# and friends were sized around that one) - a label sits directly on
# top of its own road, not tucked in a corner with room to spare.
_ROAD_LABEL_FONT_SIZE = 12

# The "live GPS fix" badge (see render_map_video()'s `show_gps_badge`
# handling in map_video.py, and _draw_gps_badge() below) - a small
# satellite glyph on a translucent dark circle, top-right corner, on
# whenever the current frame's position comes from a real bracketing
# fix rather than being frozen at the nearest known one because the
# frame's timestamp falls before the first (or after the last) fix.
GPS_BADGE_BG_COLOR = (20, 20, 20, 170)
GPS_BADGE_ICON_COLOR = (99, 187, 108, 255)
# Christer's own read on the original 11px radius (a 22px circle on a
# 640px-wide frame): too small, hard to make out the satellite glyph's
# own detail. Doubled per his explicit choice ("100% bigger").
GPS_BADGE_RADIUS_PX = 22
GPS_BADGE_MARGIN_PX = 10

# render_intro_flyover()'s own bottom-centered title caption (see
# draw_caption() below) - Christer: "with the prefix and trip name on
# like subtitles", after the flyover feature itself was already built.
# Styled to echo --stitch-subtitles' own burned-in look (white text on
# a translucent black bar - see stitch.py's _SUBTITLES_BG_COLOR) rather
# than this module's usual small dark-on-light corner text
# (TEXT_COLOR/compose_frame_overlay()), since a bottom-centered white
# -on-black bar is what reads as "subtitles" to begin with; TEXT_COLOR
# would be close to invisible against BACKGROUND_COLOR's own light
# basemap tone anyway with no bar behind it.
CAPTION_BG_COLOR = (0, 0, 0, 140)
CAPTION_TEXT_COLOR = (255, 255, 255)
CAPTION_FONT_SIZE = 22
CAPTION_MARGIN_PX = 18
CAPTION_PADDING_PX = 10

DEFAULT_WIDTH = 640
DEFAULT_HEIGHT = 640
DEFAULT_MARGIN_PX = 24

# The speed/timestamp text's own left inset - deliberately much
# tighter than DEFAULT_MARGIN_PX (which also frames the route/roads
# projection and needs real breathing room). Christer, on a narrow
# --stitch-map vertical panel at its default size: the timestamp's
# seconds were running past the right edge and getting clipped off
# -frame, since the text is a fixed 18px font drawn at a fixed x
# regardless of how narrow the panel is. Moving the text as far left
# as this project's other small-badge margins go (matches
# GPS_BADGE_MARGIN_PX) buys back real width for the line before it
# runs out of room, without touching the route's own framing margin.
TEXT_MARGIN_PX = 10
DEFAULT_MARKER_LENGTH_PX = 16
DEFAULT_MARKER_HALF_WIDTH_PX = 8

# Christer: "would it be possible to get correct å, ä and ö characters
# for map street names" - the two paths below used to be the *only*
# candidates, and both are unreliable: the first is a Linux-only
# system path (absent on Christer's Windows machine, and
# Dockerfile.cli's ffmpeg-only apt install never puts a font package
# there either), and the second only resolves if a same-named file
# happens to already sit in the current working directory. When both
# fail, ImageFont.truetype() raises OSError for every candidate and
# the code fell through to ImageFont.load_default() - a tiny built-in
# bitmap font confirmed (by direct rendering test) to have no Swedish-
# letter glyphs at all, so road names like "Åkergatan" rendered with
# blank/tofu boxes for å/ä/ö. The bundled copy (see assets/, and
# pyproject.toml's package-data entry for assets/*.ttf) is tried
# first and is expected to always resolve from a real install; the
# two old paths stay afterwards purely as a best-effort fallback if
# the bundled file is ever missing for some reason.
_FONT_CANDIDATES = (
    str(Path(__file__).parent / "assets" / "DejaVuSans-Bold.ttf"),
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "DejaVuSans-Bold.ttf",
)

# Cached by size after the first request for that size - a map.mp4
# export calls this once per frame for the speed/timestamp text (every
# frame draws it), and re-opening and re-parsing the same TTF file from
# disk that many times over was a real, measured chunk of render time
# for no benefit (the font never changes mid-export). Keyed by size
# (rather than the single-slot cache this used to be) since road-name
# labels (_draw_roads() below) need a second, smaller size alongside
# the speed/timestamp overlay's own - same dict-cache pattern
# gsensor_graph_render.py's own _load_font() already uses for the same
# reason.
_CACHED_FONT_BY_SIZE: dict[int, ImageFont.ImageFont] = {}


def _load_font(size: int = 18) -> ImageFont.ImageFont:
    if size not in _CACHED_FONT_BY_SIZE:
        for candidate in _FONT_CANDIDATES:
            try:
                _CACHED_FONT_BY_SIZE[size] = ImageFont.truetype(candidate, size)
                break
            except OSError:
                continue
        else:
            _CACHED_FONT_BY_SIZE[size] = ImageFont.load_default()

    return _CACHED_FONT_BY_SIZE[size]


def _project(
    lat: float,
    lon: float,
    bbox: BoundingBox,
    width: int,
    height: int,
    margin: int,
) -> tuple[float, float]:
    mean_lat_rad = math.radians((bbox.min_lat + bbox.max_lat) / 2)
    lon_scale = math.cos(mean_lat_rad) or 1e-9

    lon_span = (bbox.max_lon - bbox.min_lon) * lon_scale or 1e-9
    lat_span = (bbox.max_lat - bbox.min_lat) or 1e-9

    usable_width = width - 2 * margin
    usable_height = height - 2 * margin

    x = margin + ((lon - bbox.min_lon) * lon_scale / lon_span) * usable_width
    # Pixel y grows downward; latitude grows upward - flip it.
    y = margin + (1 - (lat - bbox.min_lat) / lat_span) * usable_height

    return x, y


def bbox_pixel_rect(
    target_bbox: BoundingBox,
    reference_bbox: BoundingBox,
    width: int,
    height: int,
    margin: int = DEFAULT_MARGIN_PX,
) -> tuple[float, float, float, float]:
    """Return the (left, top, right, bottom) pixel rectangle `target_bbox`
    occupies within a `width`x`height` image that was rendered (via
    render_frame_visual()) for `reference_bbox` - i.e. the inverse of
    what `_project()` does for a single point, applied to a whole
    nested bbox at once.

    Built for map_video.py's render_intro_flyover(): its zoom animates
    between two bboxes that share the same center by construction (see
    that function's own `start_bbox`/`_lerp_bbox()`), so every
    in-between frame's own bbox is always a smaller, concentric
    sub-rectangle of the widest one - which means a *single* high-
    resolution render of the widest bbox already contains everything
    every other frame needs to show, just at a different crop/scale.
    This function is what finds that crop rectangle, letting the
    per-frame loop become a cheap `Image.crop().resize()` instead of
    redrawing roads/areas from scratch on every single frame (the O(n)
    per-frame cost of roads_within_bbox()/render_frame_visual(),
    confirmed as the actual cause of a real ~20x render-time blowup
    Christer hit once `--map-intro`'s own establishing shot widened
    the frame it needed to draw - see that function's own docstring).

    Not meaningful (and not used) for track-up rendering, where the
    scene itself rotates per frame around a fixed heading - a plain
    crop of a single unrotated raster can't reproduce that, so
    render_intro_flyover() doesn't expose a `track_up` option at all.
    """

    left, top = _project(
        target_bbox.max_lat, target_bbox.min_lon, reference_bbox, width, height, margin
    )
    right, bottom = _project(
        target_bbox.min_lat, target_bbox.max_lon, reference_bbox, width, height, margin
    )
    return left, top, right, bottom


def _rotate_point(
    point: tuple[float, float],
    center: tuple[float, float],
    angle_degrees: float,
) -> tuple[float, float]:
    """Rotate `point` by `angle_degrees` around `center`, in pixel space
    (+x right, +y down, same axes _project() and everything drawn from
    it already use).

    Track-up rotation (task #512, Christer: "Could we let the maps
    (booth of them) rotate as the car turns, instead of having north at
    the top") works by rotating every projected point - roads, areas,
    the route line, and the marker's own position - by the negative of
    the current heading before drawing anything, so whichever direction
    the vehicle is currently facing always ends up pointing "up" on
    screen. Passing `angle_degrees=-heading_degrees` for a point whose
    un-rotated bearing from `center` was exactly `heading_degrees` puts
    it straight above `center` - the same derivation _paste_marker_image()/
    _arrow_points() already rely on for the marker glyph alone (compass
    heading clockwise from north, negated because rotating the *scene*
    by -heading is what makes that heading read as "up", vs. those two
    functions instead rotating the *glyph* itself to point at heading
    against a fixed, unrotated scene). See render_frame_visual()'s own
    `track_up` handling for how the two combine without double-rotating
    the marker glyph on top of an already-rotated scene.
    """

    angle = math.radians(angle_degrees)
    cos_a, sin_a = math.cos(angle), math.sin(angle)
    x, y = point[0] - center[0], point[1] - center[1]
    return (
        center[0] + x * cos_a - y * sin_a,
        center[1] + x * sin_a + y * cos_a,
    )


def _arrow_points(
    center: tuple[float, float],
    heading_degrees: float,
    *,
    length: float = DEFAULT_MARKER_LENGTH_PX,
    half_width: float = DEFAULT_MARKER_HALF_WIDTH_PX,
) -> tuple[tuple[float, float], tuple[float, float], tuple[float, float]]:
    """Return the 3 corners of a triangle pointing at `heading_degrees`
    (compass degrees, clockwise from north/"up") centered on `center`.
    """

    angle = math.radians(heading_degrees)
    # Screen coords: +x is east (right), +y is south (down) - up (north,
    # heading 0) is therefore -y.
    dx, dy = math.sin(angle), -math.cos(angle)
    # 90-degrees-clockwise perpendicular of (dx, dy), for the two back
    # corners either side of the nose.
    px, py = -dy, dx

    cx, cy = center
    nose = (cx + dx * length, cy + dy * length)
    back_x, back_y = cx - dx * length * 0.6, cy - dy * length * 0.6
    left = (back_x - px * half_width, back_y - py * half_width)
    right = (back_x + px * half_width, back_y + py * half_width)

    return (nose, right, left)


def _paste_marker_image(
    image: Image.Image,
    marker_image: Image.Image,
    center: tuple[float, float],
    heading_degrees: float | None,
) -> None:
    """Rotate `marker_image` (expected to point "up"/north in its own
    file, RGBA so its own alpha channel can serve as the paste mask)
    to `heading_degrees` and paste it centered on `center`.

    PIL rotates counter-clockwise for a positive angle; compass
    heading is clockwise from north, so the rotation angle is negated.
    """

    angle = -(heading_degrees or 0.0)
    rotated = marker_image.rotate(angle, expand=True, resample=Image.BICUBIC)
    x = int(center[0] - rotated.width / 2)
    y = int(center[1] - rotated.height / 2)
    image.paste(rotated, (x, y), rotated)


def _draw_gps_badge(
    image: Image.Image,
    width: int,
    margin: int = GPS_BADGE_MARGIN_PX,
) -> None:
    """Draw a small satellite badge in the frame's top-right corner -
    render_frame()'s `show_gps_badge` signal that this frame's
    position comes from a real, bracketing GPS fix rather than being
    frozen at the nearest known one (see render_map_video()'s
    `live_fix` check in map_video.py). Christer asked for this after
    the leading-gap trip.gpx bug (see gps_reader.py) made it clear a
    frozen position and a real one look identical on the rendered map
    otherwise.

    Drawn as a small RGBA image with its own alpha channel, then
    pasted using itself as the mask - same compositing approach
    _paste_marker_image() already uses for a custom --map-icon, so a
    translucent circular background blends over whatever roads/route
    happen to be underneath instead of hard-cutting a solid box.
    """

    diameter = GPS_BADGE_RADIUS_PX * 2
    badge = Image.new("RGBA", (diameter, diameter), (0, 0, 0, 0))
    badge_draw = ImageDraw.Draw(badge)
    badge_draw.ellipse((0, 0, diameter - 1, diameter - 1), fill=GPS_BADGE_BG_COLOR)

    cx = cy = GPS_BADGE_RADIUS_PX

    # Every glyph dimension below is expressed relative to the badge's
    # own radius (scale = 1.0 at the original 11px-radius baseline this
    # glyph was first drawn at), not as fixed pixel counts - so a
    # bigger GPS_BADGE_RADIUS_PX (e.g. Christer's 100%-bigger request)
    # actually draws a bigger, more detailed satellite rather than the
    # same small glyph adrift in a larger circle.
    scale = GPS_BADGE_RADIUS_PX / 11.0
    body_half = 2.5 * scale
    panel_w, panel_half = 3 * scale, 3.5 * scale
    line_width = max(1, round(scale))

    # Satellite body (small square) ...
    badge_draw.rectangle(
        (cx - body_half, cy - body_half, cx + body_half, cy + body_half),
        fill=GPS_BADGE_ICON_COLOR,
    )
    # ... two solar panels either side ...
    badge_draw.rectangle(
        (
            cx - body_half - panel_w - scale, cy - panel_half,
            cx - body_half - scale, cy + panel_half,
        ),
        fill=GPS_BADGE_ICON_COLOR,
    )
    badge_draw.rectangle(
        (
            cx + body_half + scale, cy - panel_half,
            cx + body_half + panel_w + scale, cy + panel_half,
        ),
        fill=GPS_BADGE_ICON_COLOR,
    )
    # ... and a short antenna angled up toward the top-right corner of
    # the badge, broadcasting three concentric signal arcs from its tip
    # instead of the small dot the original design used there -
    # Christer, shown several bolder alternatives side by side, picked
    # this "satellite + signal arcs" shape (candidate 5) but asked to
    # keep the existing colors (green icon on the dark circle) rather
    # than that candidate's own red/orange - the arcs read as clearly
    # "transmitting" even at the small size a --map badge renders at,
    # more so than a lone dot did.
    tip_x = cx + body_half + 4 * scale
    tip_y = cy - body_half - 4 * scale
    badge_draw.line(
        (cx + body_half, cy - body_half, tip_x, tip_y),
        fill=GPS_BADGE_ICON_COLOR,
        width=line_width,
    )
    for arc_radius in (2 * scale, 3.5 * scale, 5 * scale):
        badge_draw.arc(
            (
                tip_x - arc_radius, tip_y - arc_radius,
                tip_x + arc_radius, tip_y + arc_radius,
            ),
            300, 30,
            fill=GPS_BADGE_ICON_COLOR,
            width=line_width,
        )

    x = width - margin - diameter
    y = margin
    image.paste(badge, (x, y), badge)


def _polyline_length(pixels: list[tuple[float, float]]) -> float:
    return sum(
        math.hypot(x2 - x1, y2 - y1)
        for (x1, y1), (x2, y2) in zip(pixels, pixels[1:])
    )


def _draw_roads(
    draw: ImageDraw.ImageDraw,
    proj,
    roads: tuple[Road, ...],
) -> None:
    """Draw every road's own line, styled by its OSM `highway=*` tag
    (see `_ROAD_STYLE_BY_HIGHWAY`), then - in a second pass, so a label
    is never immediately overdrawn by a later road's own line crossing
    it - a name label for each sufficiently long, major, named road
    (see `_LABELED_HIGHWAY_TYPES`/`_MIN_LABELED_ROAD_LENGTH_PX`).

    A single real street is usually split into many separate OSM ways
    (one per intersection) - labeling every segment would repeat the
    same name over and over along one street, so this keeps only the
    single longest on-screen segment per distinct name and labels that
    one. `stroke_width`/`stroke_fill` draws a background-colored halo
    behind each label so it stays legible over a road/area fill of a
    similar shade to the text itself, without needing a separate
    background box.

    Shared by `render_base_map()` and `render_frame()`'s own
    from-scratch (no `base_image`) path so the two styling/labeling
    rules can't drift apart from each other.
    """

    labels_by_name: dict[str, tuple[tuple[float, float], float]] = {}

    for road in roads:
        pixels = [proj(lat, lon) for lat, lon in road.points]
        if len(pixels) < 2:
            continue

        color, width = _ROAD_STYLE_BY_HIGHWAY.get(road.highway, _DEFAULT_ROAD_STYLE)
        draw.line(pixels, fill=color, width=width)

        if road.name and road.highway in _LABELED_HIGHWAY_TYPES:
            length = _polyline_length(pixels)
            if length >= _MIN_LABELED_ROAD_LENGTH_PX:
                existing = labels_by_name.get(road.name)
                if existing is None or length > existing[1]:
                    labels_by_name[road.name] = (pixels[len(pixels) // 2], length)

    if labels_by_name:
        font = _load_font(_ROAD_LABEL_FONT_SIZE)
        for name, (point, _length) in labels_by_name.items():
            draw.text(
                point, name, font=font, fill=TEXT_COLOR, anchor="mm",
                stroke_width=2, stroke_fill=BACKGROUND_COLOR,
            )


def render_base_map(
    bbox: BoundingBox,
    roads: tuple[Road, ...],
    *,
    areas: tuple[Area, ...] = (),
    width: int = DEFAULT_WIDTH,
    height: int = DEFAULT_HEIGHT,
    margin: int = DEFAULT_MARGIN_PX,
) -> Image.Image:
    """Render just the background + road network for `bbox` - the
    part of render_frame()'s output that's identical on every frame of
    a *static*-bbox render (map.mp4's default whole-trip overview
    mode, as opposed to --map-zoom's follow-camera mode, where bbox/
    roads are freshly recomputed every frame and there's no single
    base image to reuse).

    render_map_video() calls this once for a static-bbox render and
    passes the result back into render_frame() as `base_image`, so
    each frame draws only its own route/position/text on a copy of
    this instead of every frame re-projecting and re-drawing the same
    `roads` from scratch. Confirmed via profiling (a synthetic
    5,402-fix/3,000-road trip) to be the dominant cost of a static
    -mode map.mp4 render - well past interpolation, which
    render_map_video()'s own O(fixes x frames) fix already addressed -
    ~27 million road-point projections for a mere 600-frame slice, all
    recomputing an answer that never changes since `bbox` and `roads`
    are the same object on every call.
    """

    image = Image.new("RGB", (width, height), BACKGROUND_COLOR)
    draw = ImageDraw.Draw(image)

    def proj(lat: float, lon: float) -> tuple[float, float]:
        return _project(lat, lon, bbox, width, height, margin)

    for area in areas:
        pixels = [proj(lat, lon) for lat, lon in area.points]
        if len(pixels) >= 3:
            fill = WATER_COLOR if area.kind == "water" else GREEN_COLOR
            draw.polygon(pixels, fill=fill)

    _draw_roads(draw, proj, roads)

    return image


def render_frame_visual(
    bbox: BoundingBox,
    roads: tuple[Road, ...],
    route_points: tuple[tuple[float, float], ...],
    position: tuple[float, float] | None,
    *,
    areas: tuple[Area, ...] = (),
    heading: float | None = None,
    marker_image: Image.Image | None = None,
    show_marker: bool = True,
    width: int = DEFAULT_WIDTH,
    height: int = DEFAULT_HEIGHT,
    margin: int = DEFAULT_MARGIN_PX,
    base_image: Image.Image | None = None,
    track_up: bool = False,
) -> Image.Image:
    """Render everything in a map-overlay frame that depends only on
    position/route/roads - background, roads, areas, the route driven
    so far, and the position marker - but *not* the timestamp/speed
    text or the live-GPS badge (see compose_frame_overlay() for those).

    `track_up` (default False, task #512): when True and `heading` is
    known, every projected point (roads, areas, the route line, and the
    marker's own position) is rotated around the frame's own center by
    `-heading` via `_rotate_point()`, so the vehicle's current heading
    always points "up" on screen instead of true north - the same
    convention phone turn-by-turn apps default to. The marker glyph
    itself is drawn pointing straight up in this mode (its own rotation
    would otherwise double up with the scene rotation it's now sitting
    inside) rather than at `heading` against a fixed north-up scene.
    Requires `base_image=None` when combined with a static (whole-trip)
    `bbox`, since a cached base image was drawn for one specific
    rotation (none) and can't be reused once rotation changes every
    frame - render_map_video() enforces this by not building/passing a
    base_image at all whenever `track_up` is on, even in its normally-
    cached static overview mode (see that module's own `track_up`
    handling). No effect when `heading` is None (nothing to rotate
    to) - same "falls back to the unrotated/no-arrow behavior" spirit
    as the marker's own heading=None fallback below.

    Split out of what used to be all of render_frame() so
    render_map_video() can cache and reuse this (expensive: background
    +roads+areas+route projection and redraw) part of a frame across
    several output frames whenever the underlying position/route/bbox
    genuinely hasn't changed - e.g. a Parking recording sitting still
    for a long real span, see MAX_ZOOM_ROUTE_TRAIL_FIXES's own comment
    for the "946.5s map phase" report this addresses the other half of.
    The timestamp/speed text and GPS badge stay cheap enough (a couple
    of small text/shape draws) to redraw on every single frame
    regardless, in compose_frame_overlay() - keeping the on-screen
    clock/speed readout always current even while the visual underneath
    is being reused, rather than letting it visibly freeze for however
    long a reused visual spans.

    See render_frame()'s own docstring (still the combined convenience
    wrapper most callers should use) for what each parameter means -
    this function accepts the same ones, just without `speed_kmh`,
    `timestamp_text`, and `show_gps_badge`, which belong to the overlay
    step instead.
    """

    if base_image is not None:
        image = base_image.copy()
    else:
        image = Image.new("RGB", (width, height), BACKGROUND_COLOR)
    draw = ImageDraw.Draw(image)

    rotate_scene = track_up and heading is not None
    center = (width / 2, height / 2)

    def proj(lat: float, lon: float) -> tuple[float, float]:
        point = _project(lat, lon, bbox, width, height, margin)
        if rotate_scene:
            return _rotate_point(point, center, -heading)
        return point

    # In track-up mode the *scene* itself is already rotated so the
    # current heading reads as "up" (via `proj` above) - the marker
    # glyph then just needs to point straight up (heading 0) rather
    # than at `heading` again, or it would visibly over-rotate, always
    # pointing off to one side instead of always forward. Left as
    # `heading` unchanged in the normal (non-track-up) case.
    marker_heading = 0.0 if rotate_scene else heading

    if base_image is None:
        for area in areas:
            pixels = [proj(lat, lon) for lat, lon in area.points]
            if len(pixels) >= 3:
                fill = WATER_COLOR if area.kind == "water" else GREEN_COLOR
                draw.polygon(pixels, fill=fill)

        _draw_roads(draw, proj, roads)

    if len(route_points) >= 2:
        pixels = [proj(lat, lon) for lat, lon in route_points]
        draw.line(pixels, fill=ROUTE_COLOR, width=4, joint="curve")

    if position is not None and show_marker:
        point = proj(*position)

        if marker_image is not None:
            _paste_marker_image(image, marker_image, point, marker_heading)
        elif marker_heading is not None:
            draw.polygon(
                _arrow_points(point, marker_heading),
                fill=MARKER_FILL_COLOR,
                outline=MARKER_OUTLINE_COLOR,
                width=2,
            )
        else:
            x, y = point
            radius = 7
            draw.ellipse(
                (x - radius, y - radius, x + radius, y + radius),
                fill=POSITION_DOT_COLOR,
                outline=POSITION_DOT_OUTLINE,
                width=2,
            )

    return image


def compose_frame_overlay(
    visual: Image.Image,
    *,
    speed_kmh: float | None = None,
    timestamp_text: str | None = None,
    show_gps_badge: bool = False,
    width: int = DEFAULT_WIDTH,
    height: int = DEFAULT_HEIGHT,
    margin: int = DEFAULT_MARGIN_PX,
) -> Image.Image:
    """Draw the timestamp/speed text and live-GPS badge onto a copy of
    `visual` (see render_frame_visual()) - the cheap, always-redrawn-
    every-frame half of what used to be all of render_frame().
    """

    image = visual.copy()
    draw = ImageDraw.Draw(image)

    lines = [line for line in (timestamp_text, _speed_text(speed_kmh)) if line]
    if lines:
        text = "\n".join(lines)
        font = _load_font()
        draw.multiline_text(
            (TEXT_MARGIN_PX, height - margin - 24 * len(lines)),
            text,
            fill=TEXT_COLOR,
            font=font,
            spacing=6,
        )

    if show_gps_badge:
        _draw_gps_badge(image, width)

    return image


def draw_caption(
    image: Image.Image,
    text: str | None,
    *,
    width: int = DEFAULT_WIDTH,
    height: int = DEFAULT_HEIGHT,
) -> Image.Image:
    """Draw `text` as a bottom-centered, subtitle-style caption (white
    text on a translucent black bar spanning the full frame width) onto
    a copy of `image` - render_intro_flyover()'s own opening title (see
    map_video.py), showing the trip's own folder name (prefix + trip
    label baked together, e.g. "Holiday_trip_20260715_133458_..." - see
    trip_export.py's folder_name_for_trip()) so the flyover reads as a
    real title card rather than an unlabeled establishing shot.

    Deliberately its own small helper rather than reusing
    compose_frame_overlay() (that one draws the small top-left
    timestamp/speed text this shot doesn't want at all) or stitch.py's
    ffmpeg-level `subtitles=` filter (that one burns an .srt file into
    already-encoded video via libass - not reachable from here, since
    intro.mp4 is built from a still-image sequence like every other
    map_video.py output, not a real subtitle track). Returns a plain,
    undrawn copy for a falsy `text` (`None` or empty string) - the same
    "nothing to draw" convention compose_frame_overlay() follows when
    given no text/badge args at all.
    """

    image = image.copy()
    if not text:
        return image

    draw = ImageDraw.Draw(image, "RGBA")
    font = _load_font(CAPTION_FONT_SIZE)

    text_bbox = draw.textbbox((0, 0), text, font=font)
    text_width = text_bbox[2] - text_bbox[0]
    text_height = text_bbox[3] - text_bbox[1]

    bar_height = text_height + CAPTION_PADDING_PX * 2
    bar_top = height - CAPTION_MARGIN_PX - bar_height
    draw.rectangle((0, bar_top, width, bar_top + bar_height), fill=CAPTION_BG_COLOR)

    text_x = (width - text_width) / 2 - text_bbox[0]
    text_y = bar_top + CAPTION_PADDING_PX - text_bbox[1]
    draw.text((text_x, text_y), text, font=font, fill=CAPTION_TEXT_COLOR)

    return image


def render_frame(
    bbox: BoundingBox,
    roads: tuple[Road, ...],
    route_points: tuple[tuple[float, float], ...],
    position: tuple[float, float] | None,
    *,
    areas: tuple[Area, ...] = (),
    speed_kmh: float | None = None,
    heading: float | None = None,
    marker_image: Image.Image | None = None,
    timestamp_text: str | None = None,
    show_gps_badge: bool = False,
    show_marker: bool = True,
    width: int = DEFAULT_WIDTH,
    height: int = DEFAULT_HEIGHT,
    margin: int = DEFAULT_MARGIN_PX,
    base_image: Image.Image | None = None,
    track_up: bool = False,
) -> Image.Image:
    """Render one map-overlay frame: background roads, the route
    driven so far, a position marker, and an optional speed/timestamp
    text overlay in the corner.

    `track_up` is forwarded straight to render_frame_visual() - see its
    own docstring.

    The position marker is an arrow rotated to `heading` (compass
    degrees, clockwise from north) when `heading` is given, `marker_image`
    (a custom RGBA image, also rotated to `heading`) when that's given
    instead, or a plain dot when neither is available (e.g. a
    single-fix/stationary trip with no course data to point an arrow
    in).

    `show_gps_badge`, when true, draws a small satellite badge in the
    top-right corner (see _draw_gps_badge()) - render_map_video() sets
    this per frame based on whether the frame's position comes from a
    real bracketing fix or is frozen at the nearest known one because
    the timestamp falls outside every fix's own range.

    `show_marker`, when false, suppresses the position marker/arrow/dot
    entirely even though `position` is given (still real - just not
    drawn) - `route_points`/`speed_kmh`/`timestamp_text` are unaffected.
    render_map_video() sets this false only for the strict leading-gap
    case (a frame whose timestamp is before the trip's very first real
    GPS fix, `position` clamped to that fix), so nothing appears to
    "know" its own location before the receiver has actually reported
    one - Christer's own framing: the clamped position landing on the
    map once real coordinates exist is fine, but showing it before that
    point isn't. True (the default) everywhere else, including the
    trailing-gap and mid-trip signal-loss cases `show_gps_badge=False`
    also covers - those still show the marker, just without the badge.

    `base_image`, if given, is used as the starting canvas (copied, not
    mutated) instead of a fresh background with `roads`/`areas` drawn
    onto it - see render_base_map(). `roads`/`areas` are then only used
    by callers that still need them for something else; this function
    itself won't re-draw them. Passing `base_image` only makes sense
    when `bbox` matches whatever bbox `base_image` was rendered with -
    it's the caller's responsibility to keep those in sync
    (render_map_video() only does this in its static, non-`--map-zoom`
    mode, where `bbox` is the same object on every call).

    A thin convenience wrapper around render_frame_visual() +
    compose_frame_overlay() (see those for why the work is split) -
    kept as a single call for existing callers (this project's own
    tests, and anyone rendering a one-off frame) that don't need
    render_map_video()'s own frame-to-frame visual caching.
    """

    visual = render_frame_visual(
        bbox, roads, route_points, position,
        areas=areas, heading=heading, marker_image=marker_image,
        show_marker=show_marker, width=width, height=height,
        margin=margin, base_image=base_image, track_up=track_up,
    )
    return compose_frame_overlay(
        visual,
        speed_kmh=speed_kmh, timestamp_text=timestamp_text,
        show_gps_badge=show_gps_badge, width=width, height=height, margin=margin,
    )


def _speed_text(speed_kmh: float | None) -> str | None:
    if speed_kmh is None:
        return None
    return f"{speed_kmh:.0f} km/h"
