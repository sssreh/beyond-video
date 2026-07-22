from PIL import Image
from PIL import ImageFont

from blackvue.export import map_render as map_render_module
from blackvue.export.map_render import _arrow_points
from blackvue.export.map_render import _load_font
from blackvue.export.map_render import _project
from blackvue.export.map_render import render_frame
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


def test_load_font_only_opens_the_font_file_once(monkeypatch):
    monkeypatch.setattr(map_render_module, "_CACHED_FONT", None)

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
