from pathlib import Path

from PIL import Image
from PIL import ImageDraw
from PIL import ImageFont

from blackvue.export import map_render as map_render_module
from blackvue.export.map_render import _arrow_points
from blackvue.export.map_render import _FONT_CANDIDATES
from blackvue.export.map_render import _load_font
from blackvue.export.map_render import _project
from blackvue.export.map_render import _rotate_point
from blackvue.export.map_render import compose_frame_overlay
from blackvue.export.map_render import DEFAULT_MARGIN_PX
from blackvue.export.map_render import render_base_map
from blackvue.export.map_render import render_frame
from blackvue.export.map_render import render_frame_visual
from blackvue.export.map_render import TEXT_MARGIN_PX
from blackvue.export.osm_roads import BoundingBox
from blackvue.export.osm_roads import Road


_BBOX = BoundingBox(min_lat=59.30, min_lon=18.00, max_lat=59.34, max_lon=18.08)


def test_render_frame_returns_image_of_requested_size():
    image = render_frame(
        _BBOX,
        roads=(),
        route_points=(),
        position=None,
        width=320,
        height=240,
    )

    assert image.size == (320, 240)
    assert image.mode == "RGB"


def test_render_frame_draws_something_when_route_and_roads_given():
    background = render_frame(_BBOX, roads=(), route_points=(), position=None)

    roads = (Road(points=((59.30, 18.00), (59.34, 18.08))),)
    route = ((59.31, 18.02), (59.33, 18.06))

    with_content = render_frame(
        _BBOX, roads=roads, route_points=route, position=route[-1]
    )

    # Not a pixel-exact check (font rendering/AA can vary across
    # environments) - just confirms drawing actually changed pixels
    # relative to a blank background of the same size.
    assert list(background.getdata()) != list(with_content.getdata())


def test_render_frame_draws_timestamp_text_close_to_the_left_edge():
    # Regression test: on a narrow --stitch-map vertical panel,
    # Christer found the timestamp's seconds running past the right
    # edge and getting clipped off-frame, since the text is drawn at
    # a fixed font size starting at the same wide margin used to frame
    # the route/roads projection. TEXT_MARGIN_PX gives the text its
    # own, much tighter left inset (matching GPS_BADGE_MARGIN_PX)
    # instead, buying back real width for the line before it runs out
    # of room - this only checks the text now starts left of the old
    # shared DEFAULT_MARGIN_PX, not an exact pixel offset (font
    # rendering/AA can vary across environments).
    width, height = 320, 240
    with_text = render_frame(
        _BBOX, roads=(), route_points=(), position=None,
        timestamp_text="2026-07-27 14:25:17", width=width, height=height,
    )
    without_text = render_frame(
        _BBOX, roads=(), route_points=(), position=None,
        width=width, height=height,
    )

    diff_columns = [
        x for x in range(width)
        if any(
            with_text.getpixel((x, y)) != without_text.getpixel((x, y))
            for y in range(height)
        )
    ]

    assert diff_columns, "expected timestamp text to draw something"
    assert min(diff_columns) < DEFAULT_MARGIN_PX
    assert min(diff_columns) >= 0


def test_text_margin_is_tighter_than_the_default_projection_margin():
    # The whole point of a separate constant - if these ever became
    # equal (or TEXT_MARGIN_PX ever grew past DEFAULT_MARGIN_PX) the
    # fix above would silently stop doing anything.
    assert TEXT_MARGIN_PX < DEFAULT_MARGIN_PX


def test_render_frame_handles_a_single_route_point_without_crashing():
    # len(route_points) < 2 means draw.line() would be skipped -
    # exercised here to make sure a trip with only one fix doesn't
    # crash frame rendering.
    image = render_frame(
        _BBOX, roads=(), route_points=((59.31, 18.02),), position=(59.31, 18.02)
    )

    assert image.size == (640, 640)


def test_arrow_points_noses_toward_north_for_heading_zero():
    nose, _right, _left = _arrow_points((100.0, 100.0), 0.0, length=10, half_width=5)

    # Heading 0 = north = screen "up" = smaller y, same x as center.
    assert round(nose[0], 5) == 100.0
    assert round(nose[1], 5) == 90.0


def test_arrow_points_noses_toward_east_for_heading_90():
    nose, _right, _left = _arrow_points((100.0, 100.0), 90.0, length=10, half_width=5)

    # Heading 90 = east = screen right = larger x, same y as center.
    assert round(nose[0], 5) == 110.0
    assert round(nose[1], 5) == 100.0


def test_arrow_points_back_corners_are_symmetric_and_behind_the_nose():
    center = (100.0, 100.0)
    nose, right, left = _arrow_points(center, 0.0, length=10, half_width=5)

    # Both back corners are further "south" (larger y) than the nose,
    # and mirror each other around the heading axis (same y, x
    # equidistant from center).
    assert right[1] > nose[1]
    assert left[1] > nose[1]
    assert round(right[1], 5) == round(left[1], 5)
    assert round(right[0] - center[0], 5) == round(center[0] - left[0], 5)


def test_render_frame_draws_an_arrow_when_heading_is_given():
    dot = render_frame(_BBOX, roads=(), route_points=(), position=(59.31, 18.02))
    arrow = render_frame(
        _BBOX, roads=(), route_points=(), position=(59.31, 18.02), heading=45.0
    )

    # Different marker shapes should produce a visibly different frame.
    assert list(dot.getdata()) != list(arrow.getdata())


def test_render_frame_uses_a_custom_marker_image_when_given():
    icon = Image.new("RGBA", (20, 20), (0, 0, 255, 255))

    background = render_frame(_BBOX, roads=(), route_points=(), position=None)
    with_icon = render_frame(
        _BBOX,
        roads=(),
        route_points=(),
        position=(59.31, 18.02),
        heading=0.0,
        marker_image=icon,
    )

    assert list(background.getdata()) != list(with_icon.getdata())
    # A heading of 0 means no rotation, so the icon's own solid color
    # should land, unmodified, at the projected center pixel.
    x, y = _project(59.31, 18.02, _BBOX, 640, 640, 24)
    assert with_icon.getpixel((int(x), int(y))) == (0, 0, 255)


def test_render_frame_suppresses_the_marker_when_show_marker_is_false():
    # Christer: the car shouldn't be seen before it gets real
    # coordinates for the first time - render_map_video() passes
    # show_marker=False for exactly that leading-gap case, even though
    # `position` is still a real (clamped) value.
    icon = Image.new("RGBA", (20, 20), (0, 0, 255, 255))

    background = render_frame(_BBOX, roads=(), route_points=(), position=None)
    hidden = render_frame(
        _BBOX,
        roads=(),
        route_points=(),
        position=(59.31, 18.02),
        heading=0.0,
        marker_image=icon,
        show_marker=False,
    )

    assert list(background.getdata()) == list(hidden.getdata())


def test_render_frame_shows_the_marker_by_default():
    icon = Image.new("RGBA", (20, 20), (0, 0, 255, 255))

    background = render_frame(_BBOX, roads=(), route_points=(), position=None)
    shown = render_frame(
        _BBOX,
        roads=(),
        route_points=(),
        position=(59.31, 18.02),
        heading=0.0,
        marker_image=icon,
    )

    assert list(background.getdata()) != list(shown.getdata())


def test_render_frame_suppresses_the_plain_dot_marker_too():
    # show_marker also covers the no-marker_image/no-heading fallback
    # (a plain dot), not just a custom marker_image.
    background = render_frame(_BBOX, roads=(), route_points=(), position=None)
    hidden = render_frame(
        _BBOX, roads=(), route_points=(), position=(59.31, 18.02),
        show_marker=False,
    )

    assert list(background.getdata()) == list(hidden.getdata())


def test_render_frame_handles_a_degenerate_bounding_box():
    # A bbox with zero width/height (e.g. a stationary trip) shouldn't
    # raise a ZeroDivisionError.
    point_bbox = BoundingBox(
        min_lat=59.30, min_lon=18.00, max_lat=59.30, max_lon=18.00
    )

    image = render_frame(
        point_bbox, roads=(), route_points=(), position=(59.30, 18.00)
    )

    assert image.size == (640, 640)


def test_render_base_map_draws_roads_on_a_blank_background():
    blank = render_frame(_BBOX, roads=(), route_points=(), position=None)

    roads = (Road(points=((59.30, 18.00), (59.34, 18.08))),)
    base = render_base_map(_BBOX, roads)

    assert base.size == blank.size
    assert list(base.getdata()) != list(blank.getdata())


def test_render_frame_with_base_image_ignores_the_roads_argument():
    # render_map_video()'s whole point in passing base_image is to
    # skip re-drawing roads every frame - confirmed here by giving
    # render_frame a `roads` argument that draws nothing (empty tuple)
    # alongside a base_image that already has a road baked in, and
    # checking the road still shows up in the result.
    baked_in_road = (Road(points=((59.30, 18.00), (59.34, 18.08))),)
    base = render_base_map(_BBOX, baked_in_road)

    frame = render_frame(
        _BBOX, roads=(), route_points=(), position=None, base_image=base
    )

    assert list(frame.getdata()) == list(base.getdata())


def test_render_frame_with_base_image_does_not_mutate_it():
    base = render_base_map(_BBOX, roads=())
    base_pixels_before = list(base.getdata())

    render_frame(
        _BBOX,
        roads=(),
        route_points=((59.31, 18.02), (59.33, 18.06)),
        position=(59.33, 18.06),
        heading=45.0,
        base_image=base,
    )

    # render_frame() must copy base_image, not draw onto it directly -
    # otherwise every frame's route/marker would permanently scar the
    # one base image render_map_video() reuses for every later frame.
    assert list(base.getdata()) == base_pixels_before


def test_render_frame_draws_a_gps_badge_when_requested():
    without_badge = render_frame(
        _BBOX, roads=(), route_points=(), position=(59.31, 18.02)
    )
    with_badge = render_frame(
        _BBOX, roads=(), route_points=(), position=(59.31, 18.02),
        show_gps_badge=True,
    )

    assert list(without_badge.getdata()) != list(with_badge.getdata())


def test_render_frame_omits_the_gps_badge_by_default():
    default = render_frame(
        _BBOX, roads=(), route_points=(), position=(59.31, 18.02)
    )
    explicit_false = render_frame(
        _BBOX, roads=(), route_points=(), position=(59.31, 18.02),
        show_gps_badge=False,
    )

    assert list(default.getdata()) == list(explicit_false.getdata())


def test_render_frame_draws_the_gps_badge_in_the_top_right_corner():
    from blackvue.export.map_render import GPS_BADGE_MARGIN_PX
    from blackvue.export.map_render import GPS_BADGE_RADIUS_PX

    blank = render_frame(_BBOX, roads=(), route_points=(), position=None)
    with_badge = render_frame(
        _BBOX, roads=(), route_points=(), position=None, show_gps_badge=True,
    )

    # The badge sits at a fixed top-right offset, independent of the
    # route/marker (position=None here) - its own center pixel (well
    # inside the circular background, unlike a corner of its bounding
    # box) should have changed, while the opposite (bottom-left)
    # corner, well outside the badge, should not have.
    diameter = GPS_BADGE_RADIUS_PX * 2
    badge_center = (
        640 - GPS_BADGE_MARGIN_PX - GPS_BADGE_RADIUS_PX,
        GPS_BADGE_MARGIN_PX + GPS_BADGE_RADIUS_PX,
    )
    far_corner_pixel = (5, 635)

    assert diameter > 0  # sanity: badge has a real size to center within
    assert blank.getpixel(badge_center) != with_badge.getpixel(badge_center)
    assert blank.getpixel(far_corner_pixel) == with_badge.getpixel(far_corner_pixel)


def test_render_frame_equals_visual_plus_overlay_composed_together():
    # render_frame() is documented as a thin wrapper around
    # render_frame_visual() + compose_frame_overlay() - this confirms
    # the split didn't change its combined output for a real frame with
    # roads, a route, a marker, timestamp/speed text, and the GPS
    # badge all present at once.
    roads = (Road(points=((59.30, 18.00), (59.34, 18.08))),)
    route = ((59.31, 18.02), (59.33, 18.06))

    combined = render_frame(
        _BBOX, roads=roads, route_points=route, position=route[-1],
        heading=45.0, speed_kmh=42.0, timestamp_text="2026-08-08 12:00:00",
        show_gps_badge=True,
    )

    visual = render_frame_visual(
        _BBOX, roads=roads, route_points=route, position=route[-1],
        heading=45.0,
    )
    split = compose_frame_overlay(
        visual, speed_kmh=42.0, timestamp_text="2026-08-08 12:00:00",
        show_gps_badge=True,
    )

    assert list(combined.getdata()) == list(split.getdata())


def test_render_frame_visual_omits_timestamp_and_speed_text():
    # The whole point of the split - render_frame_visual() alone should
    # never draw the timestamp/speed text or GPS badge, even when a
    # position/route is present (those come from compose_frame_overlay()
    # only, called separately by render_map_video()'s per-frame loop).
    route = ((59.31, 18.02), (59.33, 18.06))

    visual_only = render_frame_visual(
        _BBOX, roads=(), route_points=route, position=route[-1],
    )
    visual_then_blank_overlay = compose_frame_overlay(visual_only)

    # compose_frame_overlay() with no text/badge args should be a
    # no-op copy - confirms render_frame_visual()'s own output already
    # has nothing in the text/badge corners to begin with.
    assert list(visual_only.getdata()) == list(visual_then_blank_overlay.getdata())


def test_compose_frame_overlay_does_not_mutate_the_visual():
    route = ((59.31, 18.02), (59.33, 18.06))
    visual = render_frame_visual(
        _BBOX, roads=(), route_points=route, position=route[-1],
    )
    visual_pixels_before = list(visual.getdata())

    compose_frame_overlay(
        visual, speed_kmh=50.0, timestamp_text="2026-08-08 12:00:00",
        show_gps_badge=True,
    )

    # compose_frame_overlay() must copy its `visual` argument, not draw
    # onto it directly - render_map_video()'s own frame-holding cache
    # (see map_video.py's STATIONARY_VISUAL_ROUND_DECIMALS) depends on
    # reusing the exact same visual object across many frames, each
    # getting its own fresh text/badge overlay - a mutating
    # compose_frame_overlay() would permanently scar that shared object
    # after its first use.
    assert list(visual.getdata()) == visual_pixels_before


def test_compose_frame_overlay_draws_different_text_on_each_call():
    # Directly exercises the reason render_map_video() redraws the
    # overlay every frame even when it reuses a cached visual: two
    # different timestamp_text values on the same visual must produce
    # visibly different images, so the on-screen clock never appears
    # frozen during a held-frame span.
    route = ((59.31, 18.02), (59.33, 18.06))
    visual = render_frame_visual(
        _BBOX, roads=(), route_points=route, position=route[-1],
    )

    first = compose_frame_overlay(visual, timestamp_text="2026-08-08 12:00:00")
    second = compose_frame_overlay(visual, timestamp_text="2026-08-08 12:00:05")

    assert list(first.getdata()) != list(second.getdata())


def test_load_font_only_opens_the_font_file_once(monkeypatch):
    monkeypatch.setattr(map_render_module, "_CACHED_FONT_BY_SIZE", {})

    calls = []
    # A plain sentinel, not a real font - load_default() itself calls
    # truetype() internally on modern Pillow to load its bundled font,
    # which would recurse into this same fake if called here.
    fake_font = object()

    def fake_truetype(path, size, *args, **kwargs):
        calls.append(path)
        return fake_font

    monkeypatch.setattr(ImageFont, "truetype", fake_truetype)

    first = _load_font()
    second = _load_font()

    assert first is second
    assert len(calls) == 1


def test_load_font_caches_separately_per_size(monkeypatch):
    # _load_font() is also used for the smaller road-name-label font
    # (see _draw_roads()) alongside the default speed/timestamp size -
    # a single-slot cache (this function's own original shape) would
    # silently hand back the wrong-sized font for whichever one wasn't
    # requested first.
    monkeypatch.setattr(map_render_module, "_CACHED_FONT_BY_SIZE", {})

    calls = []

    def fake_truetype(path, size, *args, **kwargs):
        calls.append(size)
        return object()

    monkeypatch.setattr(ImageFont, "truetype", fake_truetype)

    default_size = _load_font()
    small_size = _load_font(12)
    default_size_again = _load_font()

    assert default_size is not small_size
    assert default_size is default_size_again
    assert calls == [18, 12]


def test_font_candidates_lists_the_bundled_font_first():
    # Christer: "would it be possible to get correct å, ä and ö
    # characters for map street names" - the bundled copy under
    # assets/ has to come before the two old system-path candidates
    # (a Linux-only path absent on Christer's Windows machine and the
    # ffmpeg-only Docker image, and a bare filename that only resolves
    # from the current working directory) or a real install would
    # still silently fall through to those unreliable paths first.
    bundled = Path(__file__).resolve().parents[3] / "src" / "blackvue" / "export" / "assets" / "DejaVuSans-Bold.ttf"
    assert _FONT_CANDIDATES[0] == str(Path(map_render_module.__file__).parent / "assets" / "DejaVuSans-Bold.ttf")
    assert Path(_FONT_CANDIDATES[0]) == bundled


def test_bundled_font_file_exists_on_disk():
    # Guards against the exact bug class pyproject.toml's package-data
    # comment warns about (templates/*.html omitted, TemplateNotFound
    # on the real NAS install) - if this file is ever deleted or
    # renamed without updating _FONT_CANDIDATES/package-data to match,
    # this test catches it immediately instead of only surfacing as
    # tofu boxes on someone's real export.
    assert Path(_FONT_CANDIDATES[0]).is_file()


def test_bundled_font_loads_as_a_real_truetype_font(monkeypatch):
    monkeypatch.setattr(map_render_module, "_CACHED_FONT_BY_SIZE", {})

    font = _load_font()

    assert isinstance(font, ImageFont.FreeTypeFont)


def test_rotate_point_rotates_a_point_east_of_center_to_straight_up():
    # Task #512 track-up: rotating by -heading should always land a
    # point that was originally `heading` degrees clockwise from
    # center's own "up" direction straight above center - here heading
    # 90 (east) rotated by -90.
    center = (100.0, 100.0)
    east = (150.0, 100.0)

    rotated = _rotate_point(east, center, -90.0)

    assert round(rotated[0], 6) == 100.0
    assert round(rotated[1], 6) == 50.0


def test_rotate_point_is_a_no_op_for_zero_angle():
    point = (123.4, 56.7)
    center = (10.0, 10.0)

    assert _rotate_point(point, center, 0.0) == point


def test_rotate_point_south_by_180_also_lands_straight_up():
    center = (100.0, 100.0)
    south = (100.0, 150.0)

    rotated = _rotate_point(south, center, -180.0)

    assert round(rotated[0], 6) == 100.0
    assert round(rotated[1], 6) == 50.0


def test_render_frame_visual_track_up_has_no_effect_without_a_heading():
    # Nothing to rotate to without a course - falls back to the plain
    # unrotated draw, same spirit as the marker's own heading=None
    # fallback.
    route = ((59.31, 18.02), (59.33, 18.06))

    without_track_up = render_frame_visual(
        _BBOX, roads=(), route_points=route, position=route[-1],
    )
    with_track_up = render_frame_visual(
        _BBOX, roads=(), route_points=route, position=route[-1], track_up=True,
    )

    assert list(without_track_up.getdata()) == list(with_track_up.getdata())


def test_render_frame_visual_track_up_changes_output_when_heading_is_given():
    icon = Image.new("RGBA", (10, 10), (0, 0, 255, 255))

    without_track_up = render_frame_visual(
        _BBOX, roads=(), route_points=(), position=(59.31, 18.02),
        heading=90.0, marker_image=icon,
    )
    with_track_up = render_frame_visual(
        _BBOX, roads=(), route_points=(), position=(59.31, 18.02),
        heading=90.0, marker_image=icon, track_up=True,
    )

    assert list(without_track_up.getdata()) != list(with_track_up.getdata())


def test_render_frame_visual_track_up_points_the_marker_glyph_straight_up(
    monkeypatch,
):
    # The scene rotation (via the internal `proj` closure) already
    # moves the marker's *position* to the "up" side of center - the
    # marker glyph itself must then be drawn as if heading were 0, or
    # it would rotate a second time on top of an already-rotated scene.
    original_arrow_points = map_render_module._arrow_points
    captured_headings = []

    def fake_arrow_points(point, heading_degrees):
        captured_headings.append(heading_degrees)
        return original_arrow_points(point, heading_degrees)

    monkeypatch.setattr(map_render_module, "_arrow_points", fake_arrow_points)

    render_frame_visual(
        _BBOX, roads=(), route_points=(), position=(59.31, 18.02),
        heading=90.0, track_up=True,
    )

    assert captured_headings == [0.0]


def test_render_frame_visual_without_track_up_points_the_marker_at_heading(
    monkeypatch,
):
    original_arrow_points = map_render_module._arrow_points
    captured_headings = []

    def fake_arrow_points(point, heading_degrees):
        captured_headings.append(heading_degrees)
        return original_arrow_points(point, heading_degrees)

    monkeypatch.setattr(map_render_module, "_arrow_points", fake_arrow_points)

    render_frame_visual(
        _BBOX, roads=(), route_points=(), position=(59.31, 18.02),
        heading=90.0, track_up=False,
    )

    assert captured_headings == [90.0]


def test_render_frame_forwards_track_up_to_render_frame_visual():
    combined = render_frame(
        _BBOX, roads=(), route_points=(), position=(59.31, 18.02),
        heading=90.0, track_up=True,
    )
    visual = render_frame_visual(
        _BBOX, roads=(), route_points=(), position=(59.31, 18.02),
        heading=90.0, track_up=True,
    )

    assert list(combined.getdata()) == list(visual.getdata())


def test_bundled_font_renders_swedish_letters_with_nonzero_width(monkeypatch):
    # The bug this guards against: PIL's ImageFont.load_default()
    # fallback (reached when every _FONT_CANDIDATES path fails to
    # resolve) has no å/ä/ö glyphs at all and draws them as
    # blank/tofu boxes - confirmed by direct rendering comparison
    # during this fix. A real DejaVu font renders "Åkergatan äö" with
    # a normal, non-degenerate text width; the exact pixel width isn't
    # asserted (font hinting/version could shift it slightly), just
    # that it's comfortably wider than a handful of narrow tofu boxes
    # would be.
    monkeypatch.setattr(map_render_module, "_CACHED_FONT_BY_SIZE", {})

    font = _load_font(24)
    draw = ImageDraw.Draw(Image.new("RGB", (1, 1)))
    left, top, right, bottom = draw.textbbox((0, 0), "Åkergatan äö", font=font)

    assert (right - left) > 150
    assert (bottom - top) > 15
