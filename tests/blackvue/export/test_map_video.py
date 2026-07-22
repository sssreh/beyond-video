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
