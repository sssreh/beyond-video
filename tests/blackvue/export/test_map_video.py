import json
import subprocess
from datetime import datetime
from datetime import timedelta

import pytest
from PIL import Image

from blackvue.export import map_video as map_video_module
from blackvue.export.map_video import interpolate_position
from blackvue.export.map_video import render_map_video
from blackvue.export.osm_roads import BoundingBox
from blackvue.export.osm_roads import Road
from blackvue.generate.media import MediaToolError
from blackvue.telemetry.gps_reader import GpsFix


def _fix(offset_seconds, lat, lon, speed_kmh=50.0, course=0.0, *, valid=True):
    return GpsFix(
        timestamp=datetime(2026, 7, 15, 13, 0, 0) + timedelta(seconds=offset_seconds),
        valid=valid,
        latitude=lat,
        longitude=lon,
        speed_kmh=speed_kmh,
        course=course,
    )


def _video_duration_seconds(path) -> float:
    result = subprocess.run(
        [
            "ffprobe", "-v", "error",
            "-show_entries", "format=duration",
            "-of", "json",
            str(path),
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    return float(json.loads(result.stdout)["format"]["duration"])


def _video_dimensions(path) -> tuple[int, int]:
    result = subprocess.run(
        [
            "ffprobe", "-v", "error",
            "-select_streams", "v:0",
            "-show_entries", "stream=width,height",
            "-of", "json",
            str(path),
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    stream = json.loads(result.stdout)["streams"][0]
    return stream["width"], stream["height"]


def test_interpolate_position_returns_exact_fix_at_its_own_timestamp():
    fixes = (_fix(0, 59.30, 18.00, course=45.0), _fix(10, 59.31, 18.02, course=90.0))

    lat, lon, speed, course = interpolate_position(fixes, fixes[0].timestamp)

    assert (lat, lon, speed, course) == (59.30, 18.00, 50.0, 45.0)


def test_interpolate_position_interpolates_midpoint():
    fixes = (
        _fix(0, 59.30, 18.00, speed_kmh=40.0, course=0.0),
        _fix(10, 59.32, 18.02, speed_kmh=60.0, course=90.0),
    )

    lat, lon, speed, course = interpolate_position(
        fixes, fixes[0].timestamp + timedelta(seconds=5)
    )

    assert round(lat, 5) == 59.31
    assert round(lon, 5) == 18.01
    assert speed == 50.0
    assert round(course, 5) == 45.0


def test_interpolate_position_clamps_before_first_fix():
    fixes = (_fix(0, 59.30, 18.00, course=45.0), _fix(10, 59.31, 18.02, course=90.0))

    lat, lon, speed, course = interpolate_position(
        fixes, fixes[0].timestamp - timedelta(seconds=5)
    )

    assert (lat, lon, speed, course) == (59.30, 18.00, 50.0, 45.0)


def test_interpolate_position_clamps_after_last_fix():
    fixes = (_fix(0, 59.30, 18.00, course=45.0), _fix(10, 59.31, 18.02, course=90.0))

    lat, lon, speed, course = interpolate_position(
        fixes, fixes[-1].timestamp + timedelta(seconds=5)
    )

    assert (lat, lon, speed, course) == (59.31, 18.02, 50.0, 90.0)


def test_interpolate_position_wraps_course_the_short_way_across_north():
    # 350 -> 10 degrees is a 20-degree turn through north (0/360), not
    # a 340-degree turn back down through 180 - a plain linear
    # interpolation of the raw numbers would get this wrong.
    fixes = (
        _fix(0, 59.30, 18.00, course=350.0),
        _fix(10, 59.31, 18.02, course=10.0),
    )

    _lat, _lon, _speed, course = interpolate_position(
        fixes, fixes[0].timestamp + timedelta(seconds=5)
    )

    assert round(course, 5) == 0.0


def test_interpolate_position_falls_back_to_whichever_course_is_present():
    fixes = (
        _fix(0, 59.30, 18.00, course=None),
        _fix(10, 59.31, 18.02, course=123.0),
    )

    _lat, _lon, _speed, course = interpolate_position(
        fixes, fixes[0].timestamp + timedelta(seconds=5)
    )

    assert course == 123.0


class _FakeFrameImage:
    def save(self, _path):
        pass


def test_render_map_video_uses_the_static_bbox_for_every_frame_by_default(
    tmp_path, monkeypatch
):
    captured = []

    def fake_render_frame(bbox, *_args, **_kwargs):
        captured.append(bbox)
        return _FakeFrameImage()

    monkeypatch.setattr(map_video_module, "render_frame", fake_render_frame)
    monkeypatch.setattr(
        map_video_module, "encode_frame_sequence", lambda *_a, **_k: None
    )

    fixes = (_fix(0, 59.300, 18.000), _fix(2, 59.310, 18.020))
    static_bbox = BoundingBox(
        min_lat=59.29, min_lon=17.99, max_lat=59.32, max_lon=18.03
    )

    render_map_video(
        fixes, roads=(), bbox=static_bbox,
        destination=tmp_path / "map.mp4", fps=2,
    )

    assert len(captured) >= 2
    assert all(bbox == static_bbox for bbox in captured)


def test_render_map_video_recenters_the_bbox_on_each_frame_when_zoomed(
    tmp_path, monkeypatch
):
    captured = []

    def fake_render_frame(bbox, *_args, **_kwargs):
        captured.append(bbox)
        return _FakeFrameImage()

    monkeypatch.setattr(map_video_module, "render_frame", fake_render_frame)
    monkeypatch.setattr(
        map_video_module, "encode_frame_sequence", lambda *_a, **_k: None
    )

    fixes = (_fix(0, 59.300, 18.000), _fix(2, 59.320, 18.040))
    static_bbox = BoundingBox(
        min_lat=59.29, min_lon=17.99, max_lat=59.33, max_lon=18.05
    )

    render_map_video(
        fixes, roads=(), bbox=static_bbox,
        destination=tmp_path / "map.mp4", fps=2, zoom_meters=100.0,
    )

    assert len(captured) >= 2
    # Every frame gets its own box, none of them the static whole-trip
    # box passed in - and the first/last frames' boxes differ from
    # each other, proving the view actually moves.
    assert all(bbox != static_bbox for bbox in captured)
    assert captured[0] != captured[-1]
    # Each per-frame box should be much smaller (street-level) than
    # the whole-trip static box above.
    first = captured[0]
    assert (first.max_lat - first.min_lat) < (
        static_bbox.max_lat - static_bbox.min_lat
    )


def test_render_map_video_filters_roads_to_each_frames_bbox_when_zoomed(
    tmp_path, monkeypatch
):
    captured_roads = []

    def fake_render_frame(_bbox, roads, *_args, **_kwargs):
        captured_roads.append(roads)
        return _FakeFrameImage()

    monkeypatch.setattr(map_video_module, "render_frame", fake_render_frame)
    monkeypatch.setattr(
        map_video_module, "encode_frame_sequence", lambda *_a, **_k: None
    )

    # One road right on the route, one far away - only the near one
    # should survive the per-frame filter.
    near_road = Road(points=((59.300, 18.000), (59.302, 18.004)))
    far_road = Road(points=((10.0, 10.0), (10.1, 10.1)))

    fixes = (_fix(0, 59.300, 18.000), _fix(2, 59.302, 18.004))
    static_bbox = BoundingBox(
        min_lat=59.29, min_lon=17.99, max_lat=59.31, max_lon=18.01
    )

    render_map_video(
        fixes,
        roads=(near_road, far_road),
        bbox=static_bbox,
        destination=tmp_path / "map.mp4",
        fps=2,
        zoom_meters=100.0,
    )

    assert len(captured_roads) >= 2
    assert all(far_road not in roads for roads in captured_roads)
    assert all(near_road in roads for roads in captured_roads)


def test_render_map_video_passes_all_roads_unfiltered_when_not_zoomed(
    tmp_path, monkeypatch
):
    captured_roads = []

    def fake_render_frame(_bbox, roads, *_args, **_kwargs):
        captured_roads.append(roads)
        return _FakeFrameImage()

    monkeypatch.setattr(map_video_module, "render_frame", fake_render_frame)
    monkeypatch.setattr(
        map_video_module, "encode_frame_sequence", lambda *_a, **_k: None
    )

    far_road = Road(points=((10.0, 10.0), (10.1, 10.1)))
    all_roads = (far_road,)

    fixes = (_fix(0, 59.300, 18.000), _fix(2, 59.302, 18.004))
    static_bbox = BoundingBox(
        min_lat=59.29, min_lon=17.99, max_lat=59.31, max_lon=18.01
    )

    render_map_video(
        fixes, roads=all_roads, bbox=static_bbox,
        destination=tmp_path / "map.mp4", fps=2,
    )

    assert len(captured_roads) >= 2
    assert all(roads == all_roads for roads in captured_roads)


def test_render_map_video_passes_width_and_height_to_render_frame(
    tmp_path, monkeypatch
):
    captured_kwargs = []

    def fake_render_frame(*_args, **kwargs):
        captured_kwargs.append(kwargs)
        return _FakeFrameImage()

    monkeypatch.setattr(map_video_module, "render_frame", fake_render_frame)
    monkeypatch.setattr(
        map_video_module, "encode_frame_sequence", lambda *_a, **_k: None
    )

    fixes = (_fix(0, 59.300, 18.000), _fix(2, 59.310, 18.020))
    static_bbox = BoundingBox(
        min_lat=59.29, min_lon=17.99, max_lat=59.32, max_lon=18.03
    )

    render_map_video(
        fixes, roads=(), bbox=static_bbox,
        destination=tmp_path / "map.mp4", fps=2,
        width=1280, height=480,
    )

    assert len(captured_kwargs) >= 2
    assert all(
        kwargs["width"] == 1280 and kwargs["height"] == 480
        for kwargs in captured_kwargs
    )


def test_render_map_video_defaults_width_and_height_to_map_render_defaults(
    tmp_path, monkeypatch
):
    captured_kwargs = []

    def fake_render_frame(*_args, **kwargs):
        captured_kwargs.append(kwargs)
        return _FakeFrameImage()

    monkeypatch.setattr(map_video_module, "render_frame", fake_render_frame)
    monkeypatch.setattr(
        map_video_module, "encode_frame_sequence", lambda *_a, **_k: None
    )

    fixes = (_fix(0, 59.300, 18.000), _fix(2, 59.310, 18.020))
    static_bbox = BoundingBox(
        min_lat=59.29, min_lon=17.99, max_lat=59.32, max_lon=18.03
    )

    render_map_video(
        fixes, roads=(), bbox=static_bbox,
        destination=tmp_path / "map.mp4", fps=2,
    )

    assert captured_kwargs[0]["width"] == map_video_module.DEFAULT_WIDTH
    assert captured_kwargs[0]["height"] == map_video_module.DEFAULT_HEIGHT


def test_render_map_video_derives_zoom_aspect_ratio_from_width_and_height(
    tmp_path, monkeypatch
):
    captured_ratios = []

    def fake_bounding_box_around_point(lat, lon, radius_meters, *, aspect_ratio=None):
        captured_ratios.append(aspect_ratio)
        return BoundingBox(
            min_lat=lat - 0.001, min_lon=lon - 0.001,
            max_lat=lat + 0.001, max_lon=lon + 0.001,
        )

    monkeypatch.setattr(
        map_video_module, "bounding_box_around_point", fake_bounding_box_around_point
    )
    monkeypatch.setattr(
        map_video_module, "render_frame", lambda *_a, **_k: _FakeFrameImage()
    )
    monkeypatch.setattr(
        map_video_module, "encode_frame_sequence", lambda *_a, **_k: None
    )

    fixes = (_fix(0, 59.300, 18.000), _fix(2, 59.310, 18.020))
    static_bbox = BoundingBox(
        min_lat=59.29, min_lon=17.99, max_lat=59.33, max_lon=18.05
    )

    render_map_video(
        fixes, roads=(), bbox=static_bbox,
        destination=tmp_path / "map.mp4", fps=2,
        zoom_meters=100.0, width=1280, height=640,
    )

    assert len(captured_ratios) >= 2
    assert all(round(ratio, 6) == 2.0 for ratio in captured_ratios)


def test_render_map_video_produces_a_video_at_the_requested_size(tmp_path):
    fixes = (
        _fix(0, 59.300, 18.000),
        _fix(1, 59.302, 18.004),
        _fix(2, 59.304, 18.008),
    )
    bbox = BoundingBox(min_lat=59.29, min_lon=17.99, max_lat=59.31, max_lon=18.01)
    destination = tmp_path / "map.mp4"

    result = render_map_video(
        fixes, roads=(), bbox=bbox, destination=destination, fps=2,
        width=320, height=180,
    )

    assert result == destination
    assert _video_dimensions(destination) == (320, 180)


def test_render_map_video_returns_none_for_fewer_than_two_fixes(tmp_path):
    result = render_map_video(
        (_fix(0, 59.30, 18.00),),
        roads=(),
        bbox=BoundingBox(59.29, 17.99, 59.32, 18.03),
        destination=tmp_path / "map.mp4",
    )

    assert result is None
    assert not (tmp_path / "map.mp4").exists()


def test_render_map_video_returns_none_when_all_fixes_are_invalid(tmp_path):
    result = render_map_video(
        (_fix(0, 59.30, 18.00, valid=False), _fix(10, 59.31, 18.02, valid=False)),
        roads=(),
        bbox=BoundingBox(59.29, 17.99, 59.32, 18.03),
        destination=tmp_path / "map.mp4",
    )

    assert result is None


def test_render_map_video_returns_none_for_zero_duration(tmp_path):
    result = render_map_video(
        (_fix(0, 59.30, 18.00), _fix(0, 59.30, 18.00)),
        roads=(),
        bbox=BoundingBox(59.29, 17.99, 59.32, 18.03),
        destination=tmp_path / "map.mp4",
    )

    assert result is None


def test_render_map_video_produces_a_real_video_end_to_end(tmp_path):
    fixes = (
        _fix(0, 59.300, 18.000),
        _fix(1, 59.302, 18.004),
        _fix(2, 59.304, 18.008),
    )
    bbox = BoundingBox(min_lat=59.29, min_lon=17.99, max_lat=59.31, max_lon=18.01)
    destination = tmp_path / "map.mp4"

    result = render_map_video(
        fixes, roads=(), bbox=bbox, destination=destination, fps=2
    )

    assert result == destination
    assert destination.exists()
    # 2 seconds of GPS data at 2fps -> roughly 2 seconds of video.
    assert round(_video_duration_seconds(destination)) == 2


def test_render_map_video_uses_a_custom_marker_image_when_given(tmp_path):
    icon_path = tmp_path / "car.png"
    Image.new("RGBA", (16, 16), (255, 0, 0, 255)).save(icon_path)

    fixes = (_fix(0, 59.300, 18.000), _fix(1, 59.302, 18.004))
    bbox = BoundingBox(min_lat=59.29, min_lon=17.99, max_lat=59.31, max_lon=18.01)
    destination = tmp_path / "map.mp4"

    result = render_map_video(
        fixes,
        roads=(),
        bbox=bbox,
        destination=destination,
        fps=2,
        marker_image_path=icon_path,
    )

    assert result == destination
    assert destination.exists()


def test_render_map_video_video_start_extends_render_to_cover_a_leading_gap(
    tmp_path
):
    # GPS data doesn't begin until 3s into the real video (e.g. an
    # earlier recording in the trip had no GPS data at all) - without
    # video_start, frame 0 would be anchored to the first GPS fix
    # itself, making the render start "late" relative to the real
    # video and come out too short to match it. video_start/
    # video_duration_seconds anchor frame 0 (and the render's total
    # length) to the trip's own real start/duration instead.
    video_start = datetime(2026, 7, 15, 13, 0, 0)
    fixes = (
        _fix(3, 59.300, 18.000),
        _fix(4, 59.302, 18.004),
        _fix(5, 59.304, 18.008),
    )
    bbox = BoundingBox(min_lat=59.29, min_lon=17.99, max_lat=59.31, max_lon=18.01)
    destination = tmp_path / "map.mp4"

    result = render_map_video(
        fixes, roads=(), bbox=bbox, destination=destination, fps=2,
        video_start=video_start, video_duration_seconds=6.0,
    )

    assert result == destination
    # 6 real seconds requested, not the 2-second span the fixes
    # themselves happen to cover.
    assert round(_video_duration_seconds(destination)) == 6


def test_render_map_video_video_start_clamps_position_during_the_leading_gap(
    tmp_path, monkeypatch
):
    captured = []

    def fake_render_frame(_bbox, _roads, _route, position, **_kwargs):
        captured.append(position)
        return _FakeFrameImage()

    monkeypatch.setattr(map_video_module, "render_frame", fake_render_frame)
    monkeypatch.setattr(
        map_video_module, "encode_frame_sequence", lambda *_a, **_k: None
    )

    video_start = datetime(2026, 7, 15, 13, 0, 0)
    fixes = (_fix(3, 59.300, 18.000), _fix(4, 59.310, 18.020))
    bbox = BoundingBox(min_lat=59.29, min_lon=17.99, max_lat=59.32, max_lon=18.03)

    render_map_video(
        fixes, roads=(), bbox=bbox, destination=tmp_path / "map.mp4", fps=2,
        video_start=video_start, video_duration_seconds=4.0,
    )

    # Frame 0 (elapsed=0s from video_start) is well before the first
    # real fix (at 3s past video_start) - should clamp to the first
    # fix's own position, the same clamp-before-first-fix behavior
    # interpolate_position() already has, just now actually reachable
    # for a real leading gap instead of always being masked by `start`
    # itself being derived from the fixes.
    assert captured[0] == (59.300, 18.000)


def test_render_map_video_video_duration_seconds_extends_past_a_trailing_gap(
    tmp_path
):
    # Same idea as the leading-gap test above, but for a recording at
    # the *end* of a trip with no GPS data - without an explicit
    # duration, the render stops as soon as the fixes run out, ending
    # early relative to the real video.
    fixes = (_fix(0, 59.300, 18.000), _fix(1, 59.302, 18.004))
    bbox = BoundingBox(min_lat=59.29, min_lon=17.99, max_lat=59.31, max_lon=18.01)
    destination = tmp_path / "map.mp4"

    result = render_map_video(
        fixes, roads=(), bbox=bbox, destination=destination, fps=2,
        video_duration_seconds=5.0,
    )

    assert result == destination
    # 5 real seconds requested, well past the fixes' own 1-second span
    # - a range rather than an exact round() match, since frame_count's
    # own "+1 frame" convention (see render_map_video()) means the
    # actual encoded length is never quite exactly the requested value.
    assert _video_duration_seconds(destination) >= 4.5


def test_render_map_video_falls_back_to_fixes_derived_timeline_without_video_start(
    tmp_path
):
    # Unchanged default behavior when video_start/video_duration_seconds
    # aren't given - e.g. no video exists for this trip at all (a GPS
    # -only "trip"), or the real video's duration couldn't be probed.
    fixes = (
        _fix(0, 59.300, 18.000),
        _fix(1, 59.302, 18.004),
        _fix(2, 59.304, 18.008),
    )
    bbox = BoundingBox(min_lat=59.29, min_lon=17.99, max_lat=59.31, max_lon=18.01)
    destination = tmp_path / "map.mp4"

    result = render_map_video(
        fixes, roads=(), bbox=bbox, destination=destination, fps=2,
    )

    assert result == destination
    assert round(_video_duration_seconds(destination)) == 2


def test_render_map_video_raises_for_a_missing_marker_image(tmp_path):
    fixes = (_fix(0, 59.300, 18.000), _fix(1, 59.302, 18.004))
    bbox = BoundingBox(min_lat=59.29, min_lon=17.99, max_lat=59.31, max_lon=18.01)

    with pytest.raises(MediaToolError):
        render_map_video(
            fixes,
            roads=(),
            bbox=bbox,
            destination=tmp_path / "map.mp4",
            marker_image_path=tmp_path / "does-not-exist.png",
        )
