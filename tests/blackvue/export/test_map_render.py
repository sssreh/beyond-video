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
