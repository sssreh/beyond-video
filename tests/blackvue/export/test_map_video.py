import json
import subprocess
from datetime import datetime
from datetime import timedelta

from blackvue.export.map_video import interpolate_position
from blackvue.export.map_video import render_map_video
from blackvue.export.osm_roads import BoundingBox
from blackvue.telemetry.gps_reader import GpsFix


def _fix(offset_seconds, lat, lon, speed_kmh=50.0, *, valid=True):
    return GpsFix(
        timestamp=datetime(2026, 7, 15, 13, 0, 0) + timedelta(seconds=offset_seconds),
        valid=valid,
        latitude=lat,
        longitude=lon,
        speed_kmh=speed_kmh,
        course=0.0,
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
    fixes = (_fix(0, 59.30, 18.00), _fix(10, 59.31, 18.02))

    lat, lon, speed = interpolate_position(fixes, fixes[0].timestamp)

    assert (lat, lon, speed) == (59.30, 18.00, 50.0)


def test_interpolate_position_interpolates_midpoint():
    fixes = (
        _fix(0, 59.30, 18.00, speed_kmh=40.0),
        _fix(10, 59.32, 18.02, speed_kmh=60.0),
    )

    lat, lon, speed = interpolate_position(
        fixes, fixes[0].timestamp + timedelta(seconds=5)
    )

    assert round(lat, 5) == 59.31
    assert round(lon, 5) == 18.01
    assert speed == 50.0


def test_interpolate_position_clamps_before_first_fix():
    fixes = (_fix(0, 59.30, 18.00), _fix(10, 59.31, 18.02))

    lat, lon, speed = interpolate_position(
        fixes, fixes[0].timestamp - timedelta(seconds=5)
    )

    assert (lat, lon, speed) == (59.30, 18.00, 50.0)


def test_interpolate_position_clamps_after_last_fix():
    fixes = (_fix(0, 59.30, 18.00), _fix(10, 59.31, 18.02))

    lat, lon, speed = interpolate_position(
        fixes, fixes[-1].timestamp + timedelta(seconds=5)
    )

    assert (lat, lon, speed) == (59.31, 18.02, 50.0)


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
