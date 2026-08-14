import json
import subprocess
from datetime import datetime
from datetime import timedelta

import pytest
from PIL import Image

from blackvue.export import map_video as map_video_module
from blackvue.export.map_video import DEFAULT_INTRO_SECONDS
from blackvue.export.map_video import INTRO_ZOOM_START_MULTIPLIER
from blackvue.export.map_video import MAX_LIVE_FIX_GAP_SECONDS
from blackvue.export.map_video import _advance_fix_index
from blackvue.export.map_video import _ease_out_cubic
from blackvue.export.map_video import _interpolate_position_from_index
from blackvue.export.map_video import _is_live_fix
from blackvue.export.map_video import _lerp_bbox
from blackvue.export.map_video import _scale_bbox_from_center
from blackvue.export.map_video import _wallclock_for_elapsed
from blackvue.export.map_video import interpolate_position
from blackvue.export.map_video import intro_start_bbox
from blackvue.export.map_video import render_intro_flyover
from blackvue.export.map_video import render_map_video
from blackvue.export.map_render import bbox_pixel_rect
from blackvue.export.media import ExportCancelled
from blackvue.export.osm_roads import BoundingBox
from blackvue.export.osm_roads import Road
from blackvue.export.osm_roads import bounding_box_for_fixes
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


def test_advance_and_interpolate_from_index_matches_exact_timestamp():
    fixes = (_fix(0, 59.30, 18.00, course=45.0), _fix(10, 59.31, 18.02, course=90.0))

    index = _advance_fix_index(fixes, fixes[0].timestamp, 0)
    lat, lon, speed, course = _interpolate_position_from_index(
        fixes, fixes[0].timestamp, index
    )

    assert (lat, lon, speed, course) == (59.30, 18.00, 50.0, 45.0)


def test_advance_and_interpolate_from_index_matches_midpoint():
    fixes = (
        _fix(0, 59.30, 18.00, speed_kmh=40.0, course=0.0),
        _fix(10, 59.32, 18.02, speed_kmh=60.0, course=90.0),
    )
    timestamp = fixes[0].timestamp + timedelta(seconds=5)

    index = _advance_fix_index(fixes, timestamp, 0)
    lat, lon, speed, course = _interpolate_position_from_index(fixes, timestamp, index)

    assert round(lat, 5) == 59.31
    assert round(lon, 5) == 18.01
    assert speed == 50.0
    assert round(course, 5) == 45.0


def test_advance_and_interpolate_from_index_clamps_before_first_fix():
    fixes = (_fix(0, 59.30, 18.00, course=45.0), _fix(10, 59.31, 18.02, course=90.0))
    timestamp = fixes[0].timestamp - timedelta(seconds=5)

    index = _advance_fix_index(fixes, timestamp, 0)
    lat, lon, speed, course = _interpolate_position_from_index(fixes, timestamp, index)

    assert (lat, lon, speed, course) == (59.30, 18.00, 50.0, 45.0)


def test_advance_and_interpolate_from_index_clamps_after_last_fix():
    fixes = (_fix(0, 59.30, 18.00, course=45.0), _fix(10, 59.31, 18.02, course=90.0))
    timestamp = fixes[-1].timestamp + timedelta(seconds=5)

    index = _advance_fix_index(fixes, timestamp, 0)
    lat, lon, speed, course = _interpolate_position_from_index(fixes, timestamp, index)

    assert (lat, lon, speed, course) == (59.31, 18.02, 50.0, 90.0)


def test_advance_and_interpolate_from_index_matches_interpolate_position_over_a_monotonic_sweep():
    # The exact usage shape render_map_video()'s own frame loop relies
    # on: timestamp only ever increases, and the index returned from
    # one call is fed straight back in as the next call's starting
    # point. Every result along the way should match
    # interpolate_position()'s own (slower, full-rescan) answer
    # exactly - same guarantee gsensor_video.py's equivalent test
    # gives for _advance_search_index()/_interpolate_from_index().
    fixes = tuple(
        _fix(s, 59.0 + s * 0.0001, 18.0 + s * 0.0002, course=(s * 7) % 360)
        for s in range(0, 200, 3)
    )

    index = 0
    for s in range(-50, 210, 1):
        timestamp = fixes[0].timestamp + timedelta(seconds=s)
        index = _advance_fix_index(fixes, timestamp, index)
        fast = _interpolate_position_from_index(fixes, timestamp, index)
        slow = interpolate_position(fixes, timestamp)
        assert fast == slow


def test_render_map_video_interpolation_stays_fast_for_a_large_fix_count():
    # Regression guard for the O(fixes x frames) bug interpolate_
    # position()'s full rescan-per-frame produced (see map_video.py's
    # _advance_fix_index()/_interpolate_position_from_index()
    # docstrings) - same shape as the bug gsensor_video.py's
    # interpolate_sample() had before it was fixed, just at GPS's own
    # slower ~1Hz rate. Simulates just the interpolation cost of
    # render_map_video()'s frame loop directly (not the PIL/ffmpeg
    # parts, which have their own real, expected cost at this frame
    # count) for a synthetic 4-hour trip at a real ~1Hz GPS rate - the
    # old O(n^2) path would be on the order of 3*10^8 inner-loop
    # iterations here (14,400 fixes x ~72,000 frames at map.mp4's
    # default 5fps); the fixed O(n) path should finish in well under a
    # second.
    import time

    fixes = tuple(
        _fix(s, 59.0 + s * 0.00001, 18.0 + s * 0.00002, course=(s * 3) % 360)
        for s in range(0, 4 * 60 * 60, 1)  # 4 hours at 1Hz
    )
    total_seconds = (fixes[-1].timestamp - fixes[0].timestamp).total_seconds()
    fps = 5
    frame_count = int(total_seconds * fps) + 1

    start_time = time.monotonic()
    index = 0
    for frame_number in range(frame_count):
        elapsed = min(frame_number / fps, total_seconds)
        timestamp = fixes[0].timestamp + timedelta(seconds=elapsed)
        index = _advance_fix_index(fixes, timestamp, index)
        _interpolate_position_from_index(fixes, timestamp, index)
    elapsed_wall = time.monotonic() - start_time

    assert elapsed_wall < 5.0


def test_render_map_video_computes_the_base_image_once_and_reuses_it(
    tmp_path, monkeypatch
):
    # Christer: "map phase took 186.2s / Still slow" even after the
    # interpolation fix above - profiling showed the real cost was
    # render_frame() re-projecting and re-drawing the same static
    # `roads` from scratch on every frame (see render_base_map()'s own
    # docstring). This confirms render_map_video() only ever builds
    # that base image once, then hands the exact same object to every
    # frame - not a fresh equal-but-different one each time, which
    # would defeat the point.
    base_calls = []
    sentinel_base = object()

    def fake_render_base_map(*args, **kwargs):
        base_calls.append((args, kwargs))
        return sentinel_base

    captured_base_images = []

    def fake_render_frame_visual(*_args, **kwargs):
        captured_base_images.append(kwargs.get("base_image"))
        return _FakeFrameImage()

    monkeypatch.setattr(map_video_module, "render_base_map", fake_render_base_map)
    monkeypatch.setattr(
        map_video_module, "render_frame_visual", fake_render_frame_visual
    )
    monkeypatch.setattr(
        map_video_module, "compose_frame_overlay", _passthrough_compose_frame_overlay
    )
    monkeypatch.setattr(
        map_video_module, "encode_frame_sequence", lambda *_a, **_k: None
    )

    fixes = (_fix(0, 59.300, 18.000), _fix(4, 59.310, 18.020))
    static_bbox = BoundingBox(
        min_lat=59.29, min_lon=17.99, max_lat=59.32, max_lon=18.03
    )

    render_map_video(
        fixes, roads=(), bbox=static_bbox,
        destination=tmp_path / "map.mp4", fps=2,
    )

    assert len(base_calls) == 1
    assert len(captured_base_images) >= 2
    assert all(image is sentinel_base for image in captured_base_images)


def test_render_map_video_skips_the_base_image_when_zoomed(tmp_path, monkeypatch):
    # --map-zoom recomputes bbox/roads fresh every frame (see
    # test_render_map_video_filters_roads_to_each_frames_bbox_when_
    # zoomed) - there's no single static base to precompute, so
    # render_base_map() should never be called in this mode, and every
    # render_frame() call should get base_image=None (falling back to
    # its own per-frame road drawing).
    def fail_render_base_map(*_args, **_kwargs):
        raise AssertionError("render_base_map() should not be called when zoomed")

    captured_base_images = []

    def fake_render_frame_visual(*_args, **kwargs):
        captured_base_images.append(kwargs.get("base_image"))
        return _FakeFrameImage()

    monkeypatch.setattr(map_video_module, "render_base_map", fail_render_base_map)
    monkeypatch.setattr(
        map_video_module, "render_frame_visual", fake_render_frame_visual
    )
    monkeypatch.setattr(
        map_video_module, "compose_frame_overlay", _passthrough_compose_frame_overlay
    )
    monkeypatch.setattr(
        map_video_module, "encode_frame_sequence", lambda *_a, **_k: None
    )

    fixes = (_fix(0, 59.300, 18.000), _fix(4, 59.310, 18.020))
    static_bbox = BoundingBox(
        min_lat=59.29, min_lon=17.99, max_lat=59.32, max_lon=18.03
    )

    render_map_video(
        fixes, roads=(), bbox=static_bbox,
        destination=tmp_path / "map.mp4", fps=2, zoom_meters=50.0,
    )

    assert len(captured_base_images) >= 2
    assert all(image is None for image in captured_base_images)


def test_render_map_video_skips_the_base_image_when_track_up(tmp_path, monkeypatch):
    # Task #512: track_up rotates the whole scene per-frame by the
    # current heading, so a single cached base image (drawn once for a
    # fixed, unrotated scene) can't be reused - same reasoning as the
    # zoomed-mode test above, but triggered by track_up alone even
    # though bbox is still static/non-zoomed here.
    def fail_render_base_map(*_args, **_kwargs):
        raise AssertionError("render_base_map() should not be called when track_up")

    captured_base_images = []

    def fake_render_frame_visual(*_args, **kwargs):
        captured_base_images.append(kwargs.get("base_image"))
        return _FakeFrameImage()

    monkeypatch.setattr(map_video_module, "render_base_map", fail_render_base_map)
    monkeypatch.setattr(
        map_video_module, "render_frame_visual", fake_render_frame_visual
    )
    monkeypatch.setattr(
        map_video_module, "compose_frame_overlay", _passthrough_compose_frame_overlay
    )
    monkeypatch.setattr(
        map_video_module, "encode_frame_sequence", lambda *_a, **_k: None
    )

    fixes = (_fix(0, 59.300, 18.000), _fix(4, 59.310, 18.020))
    static_bbox = BoundingBox(
        min_lat=59.29, min_lon=17.99, max_lat=59.32, max_lon=18.03
    )

    render_map_video(
        fixes, roads=(), bbox=static_bbox,
        destination=tmp_path / "map.mp4", fps=2, track_up=True,
    )

    assert len(captured_base_images) >= 2
    assert all(image is None for image in captured_base_images)


def test_render_map_video_forwards_track_up_to_render_frame_visual(
    tmp_path, monkeypatch
):
    captured_track_up = []

    def fake_render_frame_visual(*_args, **kwargs):
        captured_track_up.append(kwargs.get("track_up"))
        return _FakeFrameImage()

    monkeypatch.setattr(
        map_video_module, "render_frame_visual", fake_render_frame_visual
    )
    monkeypatch.setattr(
        map_video_module, "compose_frame_overlay", _passthrough_compose_frame_overlay
    )
    monkeypatch.setattr(
        map_video_module, "encode_frame_sequence", lambda *_a, **_k: None
    )

    fixes = (_fix(0, 59.300, 18.000), _fix(4, 59.310, 18.020))
    static_bbox = BoundingBox(
        min_lat=59.29, min_lon=17.99, max_lat=59.32, max_lon=18.03
    )

    render_map_video(
        fixes, roads=(), bbox=static_bbox,
        destination=tmp_path / "map.mp4", fps=2, track_up=True,
    )

    assert captured_track_up
    assert all(value is True for value in captured_track_up)


def test_render_map_video_defaults_track_up_to_false(tmp_path, monkeypatch):
    captured_track_up = []

    def fake_render_frame_visual(*_args, **kwargs):
        captured_track_up.append(kwargs.get("track_up"))
        return _FakeFrameImage()

    monkeypatch.setattr(
        map_video_module, "render_frame_visual", fake_render_frame_visual
    )
    monkeypatch.setattr(
        map_video_module, "compose_frame_overlay", _passthrough_compose_frame_overlay
    )
    monkeypatch.setattr(
        map_video_module, "encode_frame_sequence", lambda *_a, **_k: None
    )

    fixes = (_fix(0, 59.300, 18.000), _fix(4, 59.310, 18.020))
    static_bbox = BoundingBox(
        min_lat=59.29, min_lon=17.99, max_lat=59.32, max_lon=18.03
    )

    render_map_video(
        fixes, roads=(), bbox=static_bbox,
        destination=tmp_path / "map.mp4", fps=2,
    )

    assert captured_track_up
    assert all(value is False for value in captured_track_up)


def test_render_map_video_builds_the_track_up_raster_once_and_reuses_it(
    tmp_path, monkeypatch
):
    # The raster-based replacement for the old "redraw everything, every
    # frame" track_up cost (task #512 follow-up, Christer: "I thought
    # you only needed a few static maps and could turn them around when
    # needed, not creating a new map for every frame") - confirms
    # render_track_up_base_map() is only ever called once per render,
    # with the exact same object then handed to every render_frame_
    # visual() call as track_up_raster, mirroring the existing base
    # -image reuse test above.
    raster_calls = []
    sentinel_raster = object()

    def fake_render_track_up_base_map(*args, **kwargs):
        raster_calls.append((args, kwargs))
        return sentinel_raster

    captured_rasters = []

    def fake_render_frame_visual(*_args, **kwargs):
        captured_rasters.append(kwargs.get("track_up_raster"))
        return _FakeFrameImage()

    monkeypatch.setattr(
        map_video_module,
        "render_track_up_base_map",
        fake_render_track_up_base_map,
    )
    monkeypatch.setattr(
        map_video_module, "render_frame_visual", fake_render_frame_visual
    )
    monkeypatch.setattr(
        map_video_module, "compose_frame_overlay", _passthrough_compose_frame_overlay
    )
    monkeypatch.setattr(
        map_video_module, "encode_frame_sequence", lambda *_a, **_k: None
    )

    fixes = (_fix(0, 59.300, 18.000), _fix(4, 59.310, 18.020))
    static_bbox = BoundingBox(
        min_lat=59.29, min_lon=17.99, max_lat=59.32, max_lon=18.03
    )

    render_map_video(
        fixes, roads=(), bbox=static_bbox,
        destination=tmp_path / "map.mp4", fps=2, track_up=True,
    )

    assert len(raster_calls) == 1
    assert len(captured_rasters) >= 2
    assert all(raster is sentinel_raster for raster in captured_rasters)


def test_render_map_video_skips_the_track_up_raster_when_zoomed(
    tmp_path, monkeypatch
):
    # A follow-camera frame gets a fresh, freshly-recentered bbox every
    # frame - there's no single bbox (and therefore no single raster)
    # that could cover every frame's own view, so zoom_meters + track_up
    # together should still fall back to the old per-frame redraw path,
    # same as zoom_meters alone already does for the plain base_image.
    def fail_render_track_up_base_map(*_args, **_kwargs):
        raise AssertionError(
            "render_track_up_base_map() should not be called when zoomed"
        )

    captured_rasters = []

    def fake_render_frame_visual(*_args, **kwargs):
        captured_rasters.append(kwargs.get("track_up_raster"))
        return _FakeFrameImage()

    monkeypatch.setattr(
        map_video_module,
        "render_track_up_base_map",
        fail_render_track_up_base_map,
    )
    monkeypatch.setattr(
        map_video_module, "render_frame_visual", fake_render_frame_visual
    )
    monkeypatch.setattr(
        map_video_module, "compose_frame_overlay", _passthrough_compose_frame_overlay
    )
    monkeypatch.setattr(
        map_video_module, "encode_frame_sequence", lambda *_a, **_k: None
    )

    fixes = (_fix(0, 59.300, 18.000), _fix(4, 59.310, 18.020))
    static_bbox = BoundingBox(
        min_lat=59.29, min_lon=17.99, max_lat=59.32, max_lon=18.03
    )

    render_map_video(
        fixes, roads=(), bbox=static_bbox,
        destination=tmp_path / "map.mp4", fps=2, track_up=True, zoom_meters=50.0,
    )

    assert len(captured_rasters) >= 2
    assert all(raster is None for raster in captured_rasters)


def test_render_map_video_skips_the_track_up_raster_when_not_track_up(
    tmp_path, monkeypatch
):
    def fail_render_track_up_base_map(*_args, **_kwargs):
        raise AssertionError(
            "render_track_up_base_map() should not be called without track_up"
        )

    captured_rasters = []

    def fake_render_frame_visual(*_args, **kwargs):
        captured_rasters.append(kwargs.get("track_up_raster"))
        return _FakeFrameImage()

    monkeypatch.setattr(
        map_video_module,
        "render_track_up_base_map",
        fail_render_track_up_base_map,
    )
    monkeypatch.setattr(
        map_video_module, "render_frame_visual", fake_render_frame_visual
    )
    monkeypatch.setattr(
        map_video_module, "compose_frame_overlay", _passthrough_compose_frame_overlay
    )
    monkeypatch.setattr(
        map_video_module, "encode_frame_sequence", lambda *_a, **_k: None
    )

    fixes = (_fix(0, 59.300, 18.000), _fix(4, 59.310, 18.020))
    static_bbox = BoundingBox(
        min_lat=59.29, min_lon=17.99, max_lat=59.32, max_lon=18.03
    )

    render_map_video(
        fixes, roads=(), bbox=static_bbox,
        destination=tmp_path / "map.mp4", fps=2,
    )

    assert len(captured_rasters) >= 2
    assert all(raster is None for raster in captured_rasters)


def test_render_map_video_renders_a_real_track_up_frame_end_to_end(tmp_path):
    # No mocking - a real render_track_up_base_map() + render_frame_
    # visual() round trip through an actual moving trip, confirming the
    # raster path produces a real, playable video rather than just
    # satisfying the mocked-out wiring tests above.
    fixes = (
        _fix(0, 59.300, 18.000, course=10.0),
        _fix(1, 59.302, 18.004, course=50.0),
        _fix(2, 59.304, 18.008, course=90.0),
    )
    roads = (Road(points=((59.29, 17.99), (59.31, 18.02)), highway="primary"),)
    bbox = BoundingBox(min_lat=59.29, min_lon=17.99, max_lat=59.31, max_lon=18.01)
    destination = tmp_path / "map.mp4"

    result = render_map_video(
        fixes, roads=roads, bbox=bbox, destination=destination, fps=2,
        track_up=True,
    )

    assert result == destination
    assert destination.exists()


def test_render_map_video_stays_fast_with_many_roads_in_static_mode(tmp_path):
    # End-to-end regression guard (real render_base_map()/render_frame()
    # calls, no mocking) for the bug above: with the fix, road cost is
    # paid once via render_base_map(), not once per frame. Without it,
    # this synthetic case (1,000 roads x 20 points, 150 frames) took
    # noticeably longer in manual profiling - well past this bound.
    import random
    import time

    random.seed(1234)
    roads = tuple(
        Road(
            points=tuple(
                (59.0 + random.uniform(-0.05, 0.05), 18.0 + random.uniform(-0.05, 0.05))
                for _ in range(20)
            )
        )
        for _ in range(1000)
    )

    fixes = (_fix(0, 59.0, 18.0), _fix(30, 59.01, 18.01))
    static_bbox = BoundingBox(
        min_lat=58.9, min_lon=17.9, max_lat=59.1, max_lon=18.1
    )

    start_time = time.monotonic()
    render_map_video(
        fixes, roads=roads, bbox=static_bbox,
        destination=tmp_path / "map.mp4", fps=5,
    )
    elapsed_wall = time.monotonic() - start_time

    assert elapsed_wall < 15.0


class _FakeFrameImage:
    """Fake stand-in for a PIL Image, used wherever tests stub out
    render_frame_visual() so the per-frame loop's own calls have
    something harmless to call rather than needing a real PIL render.
    resize() - added for render_intro_flyover()'s Ken-Burns rewrite,
    which crops/resizes the one raster it renders once rather than
    redrawing per frame - logs each call's (size, resample, box)
    instead of actually cropping anything, and returns self so a chain
    of resize() -> draw_caption() (when mocked) -> save() keeps working
    against the same fake object.
    """

    def __init__(self):
        self.resize_calls = []

    def save(self, _path):
        pass

    def resize(self, size, resample=None, box=None):
        self.resize_calls.append({"size": size, "resample": resample, "box": box})
        return self


def _passthrough_compose_frame_overlay(visual, **_kwargs):
    # render_map_video()'s per-frame loop always calls
    # compose_frame_overlay() on whatever render_frame_visual() (real
    # or, in these tests, faked) returned - a test that's only
    # interested in what reaches render_frame_visual() stubs this out
    # as a no-op passthrough so `visual` (typically a _FakeFrameImage(),
    # which has no real .copy()/draw surface) reaches frame.save()
    # unchanged instead of a real compose_frame_overlay() call failing
    # on it.
    return visual


def test_render_map_video_uses_the_static_bbox_for_every_frame_by_default(
    tmp_path, monkeypatch
):
    captured = []

    def fake_render_frame_visual(bbox, *_args, **_kwargs):
        captured.append(bbox)
        return _FakeFrameImage()

    monkeypatch.setattr(
        map_video_module, "render_frame_visual", fake_render_frame_visual
    )
    monkeypatch.setattr(
        map_video_module, "compose_frame_overlay", _passthrough_compose_frame_overlay
    )
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

    def fake_render_frame_visual(bbox, *_args, **_kwargs):
        captured.append(bbox)
        return _FakeFrameImage()

    monkeypatch.setattr(
        map_video_module, "render_frame_visual", fake_render_frame_visual
    )
    monkeypatch.setattr(
        map_video_module, "compose_frame_overlay", _passthrough_compose_frame_overlay
    )
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

    def fake_render_frame_visual(_bbox, roads, *_args, **_kwargs):
        captured_roads.append(roads)
        return _FakeFrameImage()

    monkeypatch.setattr(
        map_video_module, "render_frame_visual", fake_render_frame_visual
    )
    monkeypatch.setattr(
        map_video_module, "compose_frame_overlay", _passthrough_compose_frame_overlay
    )
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

    def fake_render_frame_visual(_bbox, roads, *_args, **_kwargs):
        captured_roads.append(roads)
        return _FakeFrameImage()

    monkeypatch.setattr(
        map_video_module, "render_frame_visual", fake_render_frame_visual
    )
    monkeypatch.setattr(
        map_video_module, "compose_frame_overlay", _passthrough_compose_frame_overlay
    )
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


def test_render_map_video_caps_route_trail_when_zoomed(tmp_path, monkeypatch):
    from blackvue.export.map_video import MAX_ZOOM_ROUTE_TRAIL_FIXES

    captured_routes = []

    def fake_render_frame_visual(_bbox, _roads, route_points, *_args, **_kwargs):
        captured_routes.append(route_points)
        return _FakeFrameImage()

    monkeypatch.setattr(
        map_video_module, "render_frame_visual", fake_render_frame_visual
    )
    monkeypatch.setattr(
        map_video_module, "compose_frame_overlay", _passthrough_compose_frame_overlay
    )
    monkeypatch.setattr(
        map_video_module, "encode_frame_sequence", lambda *_a, **_k: None
    )

    # Many more real fixes than MAX_ZOOM_ROUTE_TRAIL_FIXES - simulates a
    # Parking recording sitting at one spot logging fixes for a long
    # real span (see MAX_ZOOM_ROUTE_TRAIL_FIXES's own comment: this used
    # to mean every one of those fixes got re-projected and redrawn on
    # every single output frame for the rest of the trip).
    fix_count = MAX_ZOOM_ROUTE_TRAIL_FIXES + 50
    fixes = tuple(
        _fix(offset, 59.300 + offset * 0.0001, 18.000 + offset * 0.0001)
        for offset in range(fix_count)
    )
    static_bbox = BoundingBox(
        min_lat=59.29, min_lon=17.99, max_lat=59.33, max_lon=18.05
    )

    render_map_video(
        fixes, roads=(), bbox=static_bbox,
        destination=tmp_path / "map.mp4", fps=1, zoom_meters=50.0,
    )

    assert len(captured_routes) >= 2
    # +1 for the current interpolated position appended after the
    # capped trailing window.
    assert all(
        len(route) <= MAX_ZOOM_ROUTE_TRAIL_FIXES + 1 for route in captured_routes
    )
    # By the final frame (well past MAX_ZOOM_ROUTE_TRAIL_FIXES real
    # fixes accumulated), the cap should actually be biting, not just
    # never triggered.
    assert len(captured_routes[-1]) == MAX_ZOOM_ROUTE_TRAIL_FIXES + 1


def test_render_map_video_does_not_cap_route_trail_when_not_zoomed(
    tmp_path, monkeypatch
):
    from blackvue.export.map_video import MAX_ZOOM_ROUTE_TRAIL_FIXES

    captured_routes = []

    def fake_render_frame_visual(_bbox, _roads, route_points, *_args, **_kwargs):
        captured_routes.append(route_points)
        return _FakeFrameImage()

    monkeypatch.setattr(
        map_video_module, "render_frame_visual", fake_render_frame_visual
    )
    monkeypatch.setattr(
        map_video_module, "compose_frame_overlay", _passthrough_compose_frame_overlay
    )
    monkeypatch.setattr(
        map_video_module, "encode_frame_sequence", lambda *_a, **_k: None
    )

    fix_count = MAX_ZOOM_ROUTE_TRAIL_FIXES + 50
    fixes = tuple(
        _fix(offset, 59.300 + offset * 0.0001, 18.000 + offset * 0.0001)
        for offset in range(fix_count)
    )
    static_bbox = BoundingBox(
        min_lat=59.29, min_lon=17.99, max_lat=59.33, max_lon=18.05
    )

    render_map_video(
        fixes, roads=(), bbox=static_bbox,
        destination=tmp_path / "map.mp4", fps=1,
    )

    assert len(captured_routes) >= 2
    # Static (whole-trip overview) mode is unaffected by the cap - the
    # full route accumulated so far is still passed every frame.
    assert len(captured_routes[-1]) > MAX_ZOOM_ROUTE_TRAIL_FIXES + 1


def test_render_map_video_reuses_the_cached_visual_during_a_stationary_span(
    tmp_path, monkeypatch
):
    # Christer, following up on the route-trail-cap fix above with the
    # real numbers behind why the render was still so slow: "the
    # overall time of the video was over 1 hour and the fps on stitch
    # is 6.84" - a Parking recording's own real (not compressed)
    # duration, independent of route-trail cost, meant frame_count
    # itself (see STATIONARY_VISUAL_ROUND_DECIMALS' own comment) was
    # already huge, and nearly every one of those frames shows an
    # unchanging map view since the car hasn't moved. This confirms
    # render_frame_visual() - the expensive background/roads/route/
    # marker redraw - gets called far fewer times than frame_count for
    # a genuinely stationary span, while compose_frame_overlay() (the
    # cheap timestamp/speed text + badge) still gets called once per
    # frame so the on-screen clock keeps advancing.
    visual_calls = []
    overlay_calls = []

    def fake_render_frame_visual(*_args, **kwargs):
        visual_calls.append(kwargs)
        return _FakeFrameImage()

    def fake_compose_frame_overlay(visual, **kwargs):
        overlay_calls.append(kwargs)
        return visual

    monkeypatch.setattr(
        map_video_module, "render_frame_visual", fake_render_frame_visual
    )
    monkeypatch.setattr(
        map_video_module, "compose_frame_overlay", fake_compose_frame_overlay
    )
    monkeypatch.setattr(
        map_video_module, "encode_frame_sequence", lambda *_a, **_k: None
    )

    # 20 fixes, all at the exact same coordinate (a parked car logging
    # fixes every second) - a real stationary Parking span.
    fixes = tuple(_fix(offset, 59.300, 18.000) for offset in range(20))
    static_bbox = BoundingBox(
        min_lat=59.29, min_lon=17.99, max_lat=59.31, max_lon=18.01
    )

    render_map_video(
        fixes, roads=(), bbox=static_bbox,
        destination=tmp_path / "map.mp4", fps=2,
    )

    # frame_count here is 39 (19s * 2fps + 1) - the whole point is that
    # render_frame_visual() is called dramatically fewer times than
    # that (just once, since every frame's position/route/bbox is
    # identical throughout), while compose_frame_overlay() still runs
    # once per frame.
    assert len(overlay_calls) >= 30
    assert len(visual_calls) == 1
    assert len(overlay_calls) > len(visual_calls)


def test_render_map_video_does_not_reuse_the_visual_while_moving(
    tmp_path, monkeypatch
):
    # Sanity counterpart to the stationary test above - a real driving
    # trip (position genuinely changing every frame) must not trigger
    # the cache; render_frame_visual() should still be called once per
    # frame, same as compose_frame_overlay().
    visual_calls = []
    overlay_calls = []

    def fake_render_frame_visual(*_args, **kwargs):
        visual_calls.append(kwargs)
        return _FakeFrameImage()

    def fake_compose_frame_overlay(visual, **kwargs):
        overlay_calls.append(kwargs)
        return visual

    monkeypatch.setattr(
        map_video_module, "render_frame_visual", fake_render_frame_visual
    )
    monkeypatch.setattr(
        map_video_module, "compose_frame_overlay", fake_compose_frame_overlay
    )
    monkeypatch.setattr(
        map_video_module, "encode_frame_sequence", lambda *_a, **_k: None
    )

    fixes = tuple(
        _fix(offset, 59.300 + offset * 0.0005, 18.000 + offset * 0.0005)
        for offset in range(20)
    )
    static_bbox = BoundingBox(
        min_lat=59.29, min_lon=17.99, max_lat=59.40, max_lon=18.10
    )

    render_map_video(
        fixes, roads=(), bbox=static_bbox,
        destination=tmp_path / "map.mp4", fps=2,
    )

    assert len(visual_calls) == len(overlay_calls)
    assert len(visual_calls) >= 30


def test_render_map_video_passes_width_and_height_to_render_frame_visual(
    tmp_path, monkeypatch
):
    captured_kwargs = []

    def fake_render_frame_visual(*_args, **kwargs):
        captured_kwargs.append(kwargs)
        return _FakeFrameImage()

    monkeypatch.setattr(
        map_video_module, "render_frame_visual", fake_render_frame_visual
    )
    monkeypatch.setattr(
        map_video_module, "compose_frame_overlay", _passthrough_compose_frame_overlay
    )
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

    def fake_render_frame_visual(*_args, **kwargs):
        captured_kwargs.append(kwargs)
        return _FakeFrameImage()

    monkeypatch.setattr(
        map_video_module, "render_frame_visual", fake_render_frame_visual
    )
    monkeypatch.setattr(
        map_video_module, "compose_frame_overlay", _passthrough_compose_frame_overlay
    )
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
        map_video_module, "render_frame_visual", lambda *_a, **_k: _FakeFrameImage()
    )
    monkeypatch.setattr(
        map_video_module, "compose_frame_overlay", _passthrough_compose_frame_overlay
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


def test_wallclock_for_elapsed_falls_back_to_start_plus_elapsed_without_breakpoints():
    # No video for this trip at all (GPS/g-sensor-only) - preserves the
    # module's original single-anchor behavior exactly.
    fallback_start = datetime(2026, 7, 15, 13, 0, 0)

    result = _wallclock_for_elapsed(42.0, (), fallback_start)

    assert result == fallback_start + timedelta(seconds=42.0)


def test_render_map_video_raises_export_cancelled_when_should_continue_is_false(
    tmp_path,
):
    fixes = (_fix(0, 59.300, 18.000), _fix(1, 59.302, 18.004))
    bbox = BoundingBox(min_lat=59.29, min_lon=17.99, max_lat=59.31, max_lon=18.01)

    with pytest.raises(ExportCancelled):
        render_map_video(
            fixes,
            roads=(),
            bbox=bbox,
            destination=tmp_path / "map.mp4",
            fps=2,
            should_continue=lambda: False,
        )

    # Checked at frame 0, before any real work - nothing written.
    assert not (tmp_path / "map.mp4").exists()


def test_render_intro_flyover_raises_export_cancelled_when_should_continue_is_false(
    tmp_path,
):
    fixes = (_fix(0, 59.300, 18.000), _fix(1, 59.302, 18.004))

    with pytest.raises(ExportCancelled):
        render_intro_flyover(
            fixes,
            roads=(),
            destination=tmp_path / "intro.mp4",
            duration_seconds=2.0,
            fps=2,
            should_continue=lambda: False,
        )

    assert not (tmp_path / "intro.mp4").exists()


def test_wallclock_for_elapsed_contiguous_recordings_matches_old_single_anchor():
    # Two recordings back-to-back with zero gap/overlap in both video
    # position and wall-clock - the piecewise breakpoints should agree
    # exactly with a plain single-anchor calculation in this case, the
    # same as the module's behavior before recording_breakpoints
    # existed.
    rec_a_start = datetime(2026, 7, 15, 13, 0, 0)
    rec_b_start = rec_a_start + timedelta(seconds=60)
    breakpoints = ((0.0, rec_a_start), (60.0, rec_b_start))

    # 30s into recording A.
    assert _wallclock_for_elapsed(30.0, breakpoints, rec_a_start) == (
        rec_a_start + timedelta(seconds=30)
    )
    # 10s into recording B (elapsed=70s overall).
    assert _wallclock_for_elapsed(70.0, breakpoints, rec_a_start) == (
        rec_b_start + timedelta(seconds=10)
    )


def test_wallclock_for_elapsed_uses_real_video_position_across_a_wallclock_gap():
    # Recording A's own video is only 32s long, but its ID timestamp is
    # 60s before recording B's - the old ID-timestamp-based rebase
    # would have placed video-elapsed=32s at rec_a_start+32s (still
    # inside the "gap"), but the real video has already moved on to
    # recording B's content at that point. The breakpoint for B is
    # keyed by B's own real video position (32.0), not the 60s wall-
    # clock gap between the two recordings' filenames.
    rec_a_start = datetime(2026, 7, 15, 13, 0, 0)
    rec_b_start = rec_a_start + timedelta(seconds=60)
    breakpoints = ((0.0, rec_a_start), (32.0, rec_b_start))

    # 35s of video-elapsed is 3s into recording B's own video, even
    # though only 35 wall-clock seconds have passed since rec_a_start -
    # nowhere near rec_b_start yet by the old ID-gap logic.
    result = _wallclock_for_elapsed(35.0, breakpoints, rec_a_start)

    assert result == rec_b_start + timedelta(seconds=3)


def test_wallclock_for_elapsed_handles_an_overlapping_manual_recording():
    # Repro of the real trip that motivated this fix: a Manual-mode
    # recording's prebuffer means its own video can start several
    # seconds before its ID timestamp claims and even before the
    # previous recording's ID-timestamp span nominally ends - the
    # breakpoint itself is still keyed by real video position (36.73s,
    # a confirmed real value from trip_export._recording_video_offsets
    # ()'s own docstring), regardless of the negative/overlapping ID
    # gap.
    rec_n_start = datetime(2026, 8, 2, 10, 35, 13)
    rec_m_start = datetime(2026, 8, 2, 10, 35, 45)  # 32s after rec_n's ID
    breakpoints = ((0.0, rec_n_start), (36.73, rec_m_start))

    result = _wallclock_for_elapsed(40.0, breakpoints, rec_n_start)

    assert result == rec_m_start + timedelta(seconds=40.0 - 36.73)


def test_wallclock_for_elapsed_before_the_first_breakpoint_extrapolates_from_it():
    # elapsed_seconds before the first breakpoint's own position
    # shouldn't normally happen (breakpoints[0] is always position 0.0
    # in practice), but the loop's own "keep the first pair as the
    # starting candidate" design means this still resolves sanely
    # rather than raising.
    start = datetime(2026, 7, 15, 13, 0, 0)
    breakpoints = ((5.0, start),)

    result = _wallclock_for_elapsed(2.0, breakpoints, start)

    assert result == start + timedelta(seconds=2.0 - 5.0)


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

    def fake_render_frame_visual(_bbox, _roads, _route, position, **_kwargs):
        captured.append(position)
        return _FakeFrameImage()

    monkeypatch.setattr(
        map_video_module, "render_frame_visual", fake_render_frame_visual
    )
    monkeypatch.setattr(
        map_video_module, "compose_frame_overlay", _passthrough_compose_frame_overlay
    )
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


def test_render_map_video_uses_recording_breakpoints_over_a_single_anchor(
    tmp_path, monkeypatch
):
    # Two recordings: A is only 2s of real video (gsensor-analogous
    # "prebuffer" scenario), but its ID-gap to B is 10s. GPS fixes are
    # spread out with one fix per recording's own real start. Without
    # recording_breakpoints, frame timestamps would be computed as a
    # single video_start + elapsed anchor and never "arrive" at
    # recording B's fixes until 10s in; with recording_breakpoints,
    # they arrive at 2s in - matching the real (short) video.
    captured_timestamps = []

    def fake_compose_frame_overlay(_visual, *, timestamp_text, **_kw):
        captured_timestamps.append(timestamp_text)
        return _FakeFrameImage()

    monkeypatch.setattr(
        map_video_module, "compose_frame_overlay", fake_compose_frame_overlay
    )
    monkeypatch.setattr(
        map_video_module, "encode_frame_sequence", lambda *_a, **_k: None
    )

    rec_a_start = datetime(2026, 7, 15, 13, 0, 0)
    rec_b_start = rec_a_start + timedelta(seconds=10)
    fixes = (
        _fix(0, 59.300, 18.000),
        _fix(10, 59.310, 18.020),
    )
    bbox = BoundingBox(min_lat=59.29, min_lon=17.99, max_lat=59.32, max_lon=18.03)
    breakpoints = ((0.0, rec_a_start), (2.0, rec_b_start))

    render_map_video(
        fixes, roads=(), bbox=bbox, destination=tmp_path / "map.mp4", fps=1,
        video_start=rec_a_start, video_duration_seconds=4.0,
        recording_breakpoints=breakpoints,
    )

    # Frame at video-elapsed=2s should be timestamped rec_b_start (real
    # video position), not rec_a_start+2s (what a single anchor would
    # give) - both are captured via compose_frame_overlay's own
    # timestamp_text kwarg, so confirm the frame at elapsed=2 (frame
    # index 2 at fps=1) reflects rec_b_start rather than the un-rebased
    # anchor.
    assert captured_timestamps[2] == rec_b_start.strftime("%Y-%m-%d %H:%M:%S")
    assert captured_timestamps[0] == rec_a_start.strftime("%Y-%m-%d %H:%M:%S")


def test_render_map_video_scales_the_marker_image_to_half_size(tmp_path, monkeypatch):
    from blackvue.export.map_video import MARKER_IMAGE_SCALE

    icon_path = tmp_path / "car.png"
    Image.new("RGBA", (128, 64), (255, 0, 0, 255)).save(icon_path)

    captured = []

    def fake_render_frame_visual(*_args, **kwargs):
        captured.append(kwargs.get("marker_image"))
        return _FakeFrameImage()

    monkeypatch.setattr(
        map_video_module, "render_frame_visual", fake_render_frame_visual
    )
    monkeypatch.setattr(
        map_video_module, "compose_frame_overlay", _passthrough_compose_frame_overlay
    )
    monkeypatch.setattr(
        map_video_module, "encode_frame_sequence", lambda *_a, **_k: None
    )

    fixes = (_fix(0, 59.300, 18.000), _fix(1, 59.302, 18.004))
    bbox = BoundingBox(min_lat=59.29, min_lon=17.99, max_lat=59.31, max_lon=18.01)

    render_map_video(
        fixes, roads=(), bbox=bbox, destination=tmp_path / "map.mp4", fps=2,
        marker_image_path=icon_path,
    )

    assert MARKER_IMAGE_SCALE == 0.5
    assert captured[0].size == (64, 32)


def test_marker_scale_for_zoom_is_unaffected_at_the_default_radius():
    from blackvue.export.map_video import _marker_scale_for_zoom
    from blackvue.export.osm_roads import DEFAULT_ZOOM_RADIUS_METERS

    assert _marker_scale_for_zoom(DEFAULT_ZOOM_RADIUS_METERS) == 1.0


def test_marker_scale_for_zoom_is_unaffected_in_static_overview_mode():
    # zoom_meters=None is render_map_video()'s own static whole-trip
    # overview - no single radius to scale against, see
    # _marker_scale_for_zoom()'s own docstring.
    from blackvue.export.map_video import _marker_scale_for_zoom

    assert _marker_scale_for_zoom(None) == 1.0


def test_marker_scale_for_zoom_grows_for_a_tighter_radius():
    # Christer, retrying --stitch-map circle at 30m instead of the
    # 120m default: "The car was even smaller on 30 meter", then "Yes,
    # thats the whole idea of zooming" once told the marker never
    # actually changed size with zoom radius.
    from blackvue.export.map_video import MARKER_ZOOM_SCALE_MAX
    from blackvue.export.map_video import _marker_scale_for_zoom

    scale = _marker_scale_for_zoom(30.0)
    assert scale > 1.0
    assert scale == MARKER_ZOOM_SCALE_MAX  # 120/30=4.0, clamped


def test_marker_scale_for_zoom_shrinks_for_a_wider_radius():
    from blackvue.export.map_video import _marker_scale_for_zoom

    scale = _marker_scale_for_zoom(240.0)
    assert scale < 1.0


def test_marker_scale_for_zoom_is_clamped_to_a_sane_range():
    from blackvue.export.map_video import MARKER_ZOOM_SCALE_MAX
    from blackvue.export.map_video import MARKER_ZOOM_SCALE_MIN
    from blackvue.export.map_video import _marker_scale_for_zoom

    # A radius near osm_roads.py's own MIN_ZOOM_RADIUS_METERS floor
    # would otherwise produce an absurd multiplier (120/5=24x).
    assert _marker_scale_for_zoom(5.0) == MARKER_ZOOM_SCALE_MAX
    # A very wide radius shouldn't shrink the marker to nothing either.
    assert _marker_scale_for_zoom(10000.0) == MARKER_ZOOM_SCALE_MIN


def test_render_map_video_scales_the_marker_bigger_at_a_tighter_zoom_radius(
    tmp_path, monkeypatch
):
    icon_path = tmp_path / "car.png"
    Image.new("RGBA", (128, 64), (255, 0, 0, 255)).save(icon_path)

    captured_sizes = []
    captured_marker_scales = []

    def fake_render_frame_visual(*_args, **kwargs):
        captured_sizes.append(kwargs.get("marker_image").size)
        captured_marker_scales.append(kwargs.get("marker_scale"))
        return _FakeFrameImage()

    monkeypatch.setattr(
        map_video_module, "render_frame_visual", fake_render_frame_visual
    )
    monkeypatch.setattr(
        map_video_module, "compose_frame_overlay", _passthrough_compose_frame_overlay
    )
    monkeypatch.setattr(
        map_video_module, "encode_frame_sequence", lambda *_a, **_k: None
    )

    fixes = (_fix(0, 59.300, 18.000), _fix(1, 59.302, 18.004))
    bbox = BoundingBox(min_lat=59.29, min_lon=17.99, max_lat=59.31, max_lon=18.01)

    render_map_video(
        fixes, roads=(), bbox=bbox, destination=tmp_path / "map.mp4", fps=2,
        marker_image_path=icon_path, zoom_meters=30.0,
    )

    # 30m vs the 120m default -> 4.0x raw, clamped to 3.0x - see
    # MARKER_ZOOM_SCALE_MAX. Combined with MARKER_IMAGE_SCALE (0.5),
    # a 128x64 source lands at 128*0.5*3.0 x 64*0.5*3.0 = 192x96.
    assert captured_sizes[0] == (192, 96)
    assert all(scale == 3.0 for scale in captured_marker_scales)


def test_render_map_video_hides_the_marker_before_the_first_real_fix(
    tmp_path, monkeypatch
):
    # Christer: the car shouldn't be seen before it gets real
    # coordinates for the first time - only the strict leading-gap
    # frames (before positioned[0].timestamp) should suppress the
    # marker.
    captured = []

    def fake_render_frame_visual(*_args, **kwargs):
        captured.append(kwargs.get("show_marker"))
        return _FakeFrameImage()

    monkeypatch.setattr(
        map_video_module, "render_frame_visual", fake_render_frame_visual
    )
    monkeypatch.setattr(
        map_video_module, "compose_frame_overlay", _passthrough_compose_frame_overlay
    )
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

    # Frame 0 (elapsed=0s) is before the first real fix (at 3s) -
    # marker hidden. A later frame, once the first fix is covered,
    # should show it again.
    assert captured[0] is False
    assert any(shown is True for shown in captured)


def test_render_map_video_still_shows_the_marker_during_a_trailing_gap(
    tmp_path, monkeypatch
):
    # Unlike the leading-gap case above, a trailing gap (no more GPS
    # data after the last fix) still shows the (clamped) marker -
    # Christer only asked for the car to be hidden before it *first*
    # gets real coordinates, not for every gap the satellite badge
    # itself goes dark for.
    captured = []

    def fake_render_frame_visual(*_args, **kwargs):
        captured.append(kwargs.get("show_marker"))
        return _FakeFrameImage()

    monkeypatch.setattr(
        map_video_module, "render_frame_visual", fake_render_frame_visual
    )
    monkeypatch.setattr(
        map_video_module, "compose_frame_overlay", _passthrough_compose_frame_overlay
    )
    monkeypatch.setattr(
        map_video_module, "encode_frame_sequence", lambda *_a, **_k: None
    )

    fixes = (_fix(0, 59.300, 18.000), _fix(1, 59.302, 18.004))
    bbox = BoundingBox(min_lat=59.29, min_lon=17.99, max_lat=59.31, max_lon=18.01)

    render_map_video(
        fixes, roads=(), bbox=bbox, destination=tmp_path / "map.mp4",
        fps=2, video_duration_seconds=5.0,
    )

    assert captured
    assert all(shown is True for shown in captured)


def test_render_map_video_still_shows_the_marker_during_a_mid_trip_signal_loss_gap(
    tmp_path, monkeypatch
):
    captured = []

    def fake_render_frame_visual(*_args, **kwargs):
        captured.append(kwargs.get("show_marker"))
        return _FakeFrameImage()

    monkeypatch.setattr(
        map_video_module, "render_frame_visual", fake_render_frame_visual
    )
    monkeypatch.setattr(
        map_video_module, "compose_frame_overlay", _passthrough_compose_frame_overlay
    )
    monkeypatch.setattr(
        map_video_module, "encode_frame_sequence", lambda *_a, **_k: None
    )

    wide_gap = MAX_LIVE_FIX_GAP_SECONDS + 20
    fixes = (
        _fix(0, 59.300, 18.000),
        _fix(2, 59.302, 18.004),
        _fix(2 + wide_gap, 59.400, 18.200),
        _fix(2 + wide_gap + 2, 59.402, 18.204),
    )
    bbox = BoundingBox(min_lat=59.29, min_lon=17.99, max_lat=59.5, max_lon=18.3)

    render_map_video(
        fixes, roads=(), bbox=bbox, destination=tmp_path / "map.mp4", fps=1,
    )

    # Every frame is after the first real fix, so show_marker is True
    # throughout even though some of those frames have the badge off
    # (a different, already-tested signal).
    assert captured
    assert all(shown is True for shown in captured)


def test_is_live_fix_false_before_the_first_fix():
    fixes = (_fix(0, 59.30, 18.00), _fix(10, 59.31, 18.02))

    assert _is_live_fix(fixes, fixes[0].timestamp - timedelta(seconds=1), 0) is False


def test_is_live_fix_false_after_the_last_fix():
    fixes = (_fix(0, 59.30, 18.00), _fix(10, 59.31, 18.02))

    assert _is_live_fix(fixes, fixes[-1].timestamp + timedelta(seconds=1), 1) is False


def test_is_live_fix_true_between_two_closely_spaced_fixes():
    fixes = (_fix(0, 59.30, 18.00), _fix(2, 59.31, 18.02))

    assert _is_live_fix(fixes, fixes[0].timestamp + timedelta(seconds=1), 0) is True


def test_is_live_fix_false_across_a_signal_loss_gap_mid_trip():
    # Two real fixes more than MAX_LIVE_FIX_GAP_SECONDS apart (e.g. a
    # tunnel) - both individual fixes are real, but the straight-line
    # interpolation between them isn't a live position.
    gap = MAX_LIVE_FIX_GAP_SECONDS + 30
    fixes = (_fix(0, 59.30, 18.00), _fix(gap, 59.40, 18.20))

    midpoint = fixes[0].timestamp + timedelta(seconds=gap / 2)
    assert _is_live_fix(fixes, midpoint, 0) is False


def test_is_live_fix_true_right_at_a_real_fix_even_with_a_wide_gap_ahead():
    # Exactly at the earlier fix's own timestamp, before any
    # interpolation across the wide gap has actually happened yet -
    # this instant itself is still a real, live fix.
    gap = MAX_LIVE_FIX_GAP_SECONDS + 30
    fixes = (_fix(0, 59.30, 18.00), _fix(gap, 59.40, 18.20))

    assert _is_live_fix(fixes, fixes[0].timestamp, 0) is True


def test_is_live_fix_true_at_exactly_the_gap_threshold():
    fixes = (_fix(0, 59.30, 18.00), _fix(MAX_LIVE_FIX_GAP_SECONDS, 59.31, 18.02))

    midpoint = fixes[0].timestamp + timedelta(seconds=MAX_LIVE_FIX_GAP_SECONDS / 2)
    assert _is_live_fix(fixes, midpoint, 0) is True


def test_render_map_video_omits_the_gps_badge_during_a_mid_trip_signal_loss_gap(
    tmp_path, monkeypatch
):
    captured = []

    def fake_compose_frame_overlay(_visual, **kwargs):
        captured.append(kwargs.get("show_gps_badge"))
        return _FakeFrameImage()

    monkeypatch.setattr(
        map_video_module, "compose_frame_overlay", fake_compose_frame_overlay
    )
    monkeypatch.setattr(
        map_video_module, "encode_frame_sequence", lambda *_a, **_k: None
    )

    # A real gap wider than MAX_LIVE_FIX_GAP_SECONDS between the 2nd
    # and 3rd fixes (e.g. a tunnel) - frames landing inside that gap
    # should have the badge off even though both bracketing fixes are
    # themselves real.
    wide_gap = MAX_LIVE_FIX_GAP_SECONDS + 20
    fixes = (
        _fix(0, 59.300, 18.000),
        _fix(2, 59.302, 18.004),
        _fix(2 + wide_gap, 59.400, 18.200),
        _fix(2 + wide_gap + 2, 59.402, 18.204),
    )
    bbox = BoundingBox(min_lat=59.29, min_lon=17.99, max_lat=59.5, max_lon=18.3)

    render_map_video(
        fixes, roads=(), bbox=bbox, destination=tmp_path / "map.mp4", fps=1,
    )

    # At least one frame is live before the gap, at least one frame
    # inside the gap has the badge off, and at least one frame after
    # the gap is live again.
    assert captured[0] is True
    assert any(shown is False for shown in captured[1:-1])
    assert captured[-1] is True


def test_render_map_video_omits_the_gps_badge_during_the_leading_gap(
    tmp_path, monkeypatch
):
    # Same leading-gap setup as test_render_map_video_video_start_
    # clamps_position_during_the_leading_gap above: frame 0 is before
    # the first real fix, so its position is clamped/frozen - the
    # satellite badge shouldn't claim that's a live fix.
    captured = []

    def fake_compose_frame_overlay(_visual, **kwargs):
        captured.append(kwargs.get("show_gps_badge"))
        return _FakeFrameImage()

    monkeypatch.setattr(
        map_video_module, "compose_frame_overlay", fake_compose_frame_overlay
    )
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

    # Frame 0 (elapsed=0s) is before the first real fix (at 3s) -
    # badge should be off. A later frame, once real fixes are covered,
    # should have it on.
    assert captured[0] is False
    assert any(shown is True for shown in captured)


def test_render_map_video_shows_the_gps_badge_for_every_frame_without_a_gap(
    tmp_path, monkeypatch
):
    # No video_start/video_duration_seconds given, so every frame's
    # timestamp falls within the fixes' own range by construction -
    # the badge should be on for every single frame.
    captured = []

    def fake_compose_frame_overlay(_visual, **kwargs):
        captured.append(kwargs.get("show_gps_badge"))
        return _FakeFrameImage()

    monkeypatch.setattr(
        map_video_module, "compose_frame_overlay", fake_compose_frame_overlay
    )
    monkeypatch.setattr(
        map_video_module, "encode_frame_sequence", lambda *_a, **_k: None
    )

    fixes = (_fix(0, 59.300, 18.000), _fix(2, 59.310, 18.020))
    bbox = BoundingBox(min_lat=59.29, min_lon=17.99, max_lat=59.32, max_lon=18.03)

    render_map_video(
        fixes, roads=(), bbox=bbox, destination=tmp_path / "map.mp4", fps=2,
    )

    assert len(captured) >= 2
    assert all(shown is True for shown in captured)


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


# --- render_intro_flyover() -------------------------------------------
#
# Christer, after importing a trip's KML into Google Earth Web and
# watching its own flyover tour there: "is it possible to extract that
# slide show and by an option be the introduction to the trip". Google
# Earth's flyover has no export API, so render_intro_flyover() builds
# an equivalent establishing shot natively from the same OSM road data
# map.mp4 already draws from - see the function's own docstring in
# map_video.py for the full design rationale.


def test_ease_out_cubic_starts_at_zero_and_ends_at_one():
    assert _ease_out_cubic(0.0) == 0.0
    assert _ease_out_cubic(1.0) == 1.0


def test_ease_out_cubic_is_monotonically_increasing():
    samples = [_ease_out_cubic(t / 100) for t in range(101)]
    assert all(a <= b for a, b in zip(samples, samples[1:]))


def test_ease_out_cubic_decelerates_rather_than_moving_at_a_constant_rate():
    # "Ease out" means most of the progress happens early - the
    # halfway point in time should already be well past halfway in
    # progress, unlike a plain linear ramp.
    assert _ease_out_cubic(0.5) > 0.5


def test_lerp_bbox_returns_the_start_box_at_t_zero():
    start = BoundingBox(min_lat=59.0, min_lon=18.0, max_lat=59.1, max_lon=18.1)
    end = BoundingBox(min_lat=59.02, min_lon=18.02, max_lat=59.08, max_lon=18.08)

    assert _lerp_bbox(start, end, 0.0) == start


def test_lerp_bbox_returns_the_end_box_at_t_one():
    start = BoundingBox(min_lat=59.0, min_lon=18.0, max_lat=59.1, max_lon=18.1)
    end = BoundingBox(min_lat=59.02, min_lon=18.02, max_lat=59.08, max_lon=18.08)

    assert _lerp_bbox(start, end, 1.0) == end


def test_lerp_bbox_interpolates_each_corner_independently_at_the_midpoint():
    start = BoundingBox(min_lat=59.0, min_lon=18.0, max_lat=59.1, max_lon=18.2)
    end = BoundingBox(min_lat=59.02, min_lon=18.04, max_lat=59.08, max_lon=18.16)

    result = _lerp_bbox(start, end, 0.5)

    # round() rather than exact equality - the midpoint is computed via
    # floating-point interpolation, not a literal, so it won't
    # necessarily be bit-exact to a hand-typed value.
    assert round(result.min_lat, 6) == 59.01
    assert round(result.min_lon, 6) == 18.02
    assert round(result.max_lat, 6) == 59.09
    assert round(result.max_lon, 6) == 18.18


def test_render_intro_flyover_returns_none_for_fewer_than_two_fixes(tmp_path):
    result = render_intro_flyover(
        (_fix(0, 59.30, 18.00),),
        roads=(),
        destination=tmp_path / "intro.mp4",
    )

    assert result is None
    assert not (tmp_path / "intro.mp4").exists()


def test_render_intro_flyover_returns_none_when_all_fixes_are_invalid(tmp_path):
    result = render_intro_flyover(
        (_fix(0, 59.30, 18.00, valid=False), _fix(10, 59.31, 18.02, valid=False)),
        roads=(),
        destination=tmp_path / "intro.mp4",
    )

    assert result is None


def test_render_intro_flyover_starts_on_the_wide_box_and_ends_on_the_trip_bbox(
    tmp_path, monkeypatch
):
    captured_bboxes = []
    captured_kwargs = []
    rasters = []

    def fake_render_frame_visual(bbox, *_args, **kwargs):
        captured_bboxes.append(bbox)
        captured_kwargs.append(kwargs)
        raster = _FakeFrameImage()
        rasters.append(raster)
        return raster

    monkeypatch.setattr(
        map_video_module, "render_frame_visual", fake_render_frame_visual
    )
    monkeypatch.setattr(
        map_video_module, "encode_frame_sequence", lambda *_a, **_k: None
    )

    fixes = (
        _fix(0, 59.300, 18.000),
        _fix(1, 59.302, 18.004),
        _fix(2, 59.304, 18.008),
    )

    render_intro_flyover(
        fixes, roads=(), destination=tmp_path / "intro.mp4",
        duration_seconds=2.0, fps=2,
    )

    width, height = map_video_module.DEFAULT_WIDTH, map_video_module.DEFAULT_HEIGHT
    end_bbox = bounding_box_for_fixes(fixes, aspect_ratio=width / height)
    center_lat = (end_bbox.min_lat + end_bbox.max_lat) / 2
    center_lon = (end_bbox.min_lon + end_bbox.max_lon) / 2
    half_lat = (end_bbox.max_lat - end_bbox.min_lat) / 2 * INTRO_ZOOM_START_MULTIPLIER
    half_lon = (end_bbox.max_lon - end_bbox.min_lon) / 2 * INTRO_ZOOM_START_MULTIPLIER
    expected_start_bbox = BoundingBox(
        min_lat=center_lat - half_lat, min_lon=center_lon - half_lon,
        max_lat=center_lat + half_lat, max_lon=center_lon + half_lon,
    )

    # Since the Ken-Burns rewrite (see render_intro_flyover's own
    # docstring), render_frame_visual() itself is only called once, for
    # the wide starting box - "starts wide, ends tight" now shows up as
    # the *crop rectangle* moving within that one raster, not as a
    # different bbox being rendered per frame.
    assert captured_bboxes == [expected_start_bbox]

    raster_width = captured_kwargs[0]["width"]
    raster_height = captured_kwargs[0]["height"]
    resize_calls = rasters[0].resize_calls

    # First frame (t=0, eased to 0) crops the whole raster - exactly
    # the wide starting box; last frame (t=1, eased to 1) crops down to
    # the pixel rectangle end_bbox occupies within that same raster -
    # the same box map.mp4's static overview frames itself with, so the
    # two videos line up if played back to back.
    assert resize_calls[0]["box"] == bbox_pixel_rect(
        expected_start_bbox, expected_start_bbox, raster_width, raster_height
    )
    assert resize_calls[-1]["box"] == bbox_pixel_rect(
        end_bbox, expected_start_bbox, raster_width, raster_height
    )


def test_render_intro_flyover_eases_rather_than_moving_at_a_constant_rate(
    tmp_path, monkeypatch
):
    captured_kwargs = []
    rasters = []

    def fake_render_frame_visual(_bbox, *_args, **kwargs):
        captured_kwargs.append(kwargs)
        raster = _FakeFrameImage()
        rasters.append(raster)
        return raster

    monkeypatch.setattr(
        map_video_module, "render_frame_visual", fake_render_frame_visual
    )
    monkeypatch.setattr(
        map_video_module, "encode_frame_sequence", lambda *_a, **_k: None
    )

    fixes = (
        _fix(0, 59.300, 18.000),
        _fix(1, 59.302, 18.004),
        _fix(2, 59.304, 18.008),
    )

    width, height = map_video_module.DEFAULT_WIDTH, map_video_module.DEFAULT_HEIGHT
    end_bbox = bounding_box_for_fixes(fixes, aspect_ratio=width / height)
    start_bbox = _scale_bbox_from_center(end_bbox, INTRO_ZOOM_START_MULTIPLIER)

    # duration_seconds=2, fps=2 -> frame_count=5, so the middle frame
    # (index 2) sits at plain linear progress t=0.5 - eased progress at
    # t=0.5 is 0.875 (see _ease_out_cubic tests above), so the mid
    # frame's crop box should already be much closer to the final crop
    # box than a linear halfway point would be. Since the Ken-Burns
    # rewrite, this now has to be checked via the crop rectangle
    # (resize()'s `box` kwarg) rather than via a bbox render_frame_visual()
    # was called with per frame - render_frame_visual() itself is only
    # called once now, for the wide starting box.
    render_intro_flyover(
        fixes, roads=(), destination=tmp_path / "intro.mp4",
        duration_seconds=2.0, fps=2,
    )

    raster_width = captured_kwargs[0]["width"]
    raster_height = captured_kwargs[0]["height"]
    resize_calls = rasters[0].resize_calls

    mid_box = resize_calls[2]["box"]
    end_crop_box = resize_calls[-1]["box"]

    linear_midpoint_bbox = _lerp_bbox(start_bbox, end_bbox, 0.5)
    linear_midpoint_box = bbox_pixel_rect(
        linear_midpoint_bbox, start_bbox, raster_width, raster_height
    )

    assert mid_box != linear_midpoint_box
    # Closer (in left-edge pixel terms) to the end crop than a linear
    # halfway point would be, since ease-out spends most of its motion
    # early.
    assert abs(mid_box[0] - end_crop_box[0]) < abs(
        linear_midpoint_box[0] - end_crop_box[0]
    )


def test_render_intro_flyover_zoom_start_multiplier_widens_the_starting_box(
    tmp_path, monkeypatch
):
    captured_bboxes = []

    def fake_render_frame_visual(bbox, *_args, **_kwargs):
        captured_bboxes.append(bbox)
        return _FakeFrameImage()

    monkeypatch.setattr(
        map_video_module, "render_frame_visual", fake_render_frame_visual
    )
    monkeypatch.setattr(
        map_video_module, "encode_frame_sequence", lambda *_a, **_k: None
    )

    fixes = (
        _fix(0, 59.300, 18.000),
        _fix(1, 59.302, 18.004),
        _fix(2, 59.304, 18.008),
    )

    render_intro_flyover(
        fixes, roads=(), destination=tmp_path / "intro.mp4",
        duration_seconds=1.0, fps=2, zoom_start_multiplier=20.0,
    )

    # render_frame_visual() is only called once (for the wide starting
    # box), so captured_bboxes[0] is that box regardless of index -
    # unlike before the Ken-Burns rewrite, captured_bboxes[-1] is *not*
    # the trip's own tight end_bbox anymore (there's only one call), so
    # end_bbox has to be computed independently here instead.
    start_bbox = captured_bboxes[0]
    lat_span = start_bbox.max_lat - start_bbox.min_lat
    lon_span = start_bbox.max_lon - start_bbox.min_lon

    width, height = map_video_module.DEFAULT_WIDTH, map_video_module.DEFAULT_HEIGHT
    end_bbox = bounding_box_for_fixes(fixes, aspect_ratio=width / height)
    default_start_span_lat = (
        (end_bbox.max_lat - end_bbox.min_lat) * INTRO_ZOOM_START_MULTIPLIER
    )

    # A 20x multiplier should produce a visibly wider starting box than
    # the default (8x) multiplier would for the same trip.
    assert lat_span > default_start_span_lat
    assert lon_span > 0


def test_render_intro_flyover_draws_the_whole_route_from_the_first_frame(
    tmp_path, monkeypatch
):
    # Unlike map.mp4's "route driven so far" line, this is a scene-
    # setting shot - the complete path should already be there on
    # frame 0, not built up progressively as the camera arrives.
    captured_routes = []

    def fake_render_frame_visual(_bbox, _roads, route_points, *_args, **_kwargs):
        captured_routes.append(route_points)
        return _FakeFrameImage()

    monkeypatch.setattr(
        map_video_module, "render_frame_visual", fake_render_frame_visual
    )
    monkeypatch.setattr(
        map_video_module, "encode_frame_sequence", lambda *_a, **_k: None
    )

    fixes = (
        _fix(0, 59.300, 18.000),
        _fix(1, 59.302, 18.004),
        _fix(2, 59.304, 18.008),
    )
    full_route = tuple((fix.latitude, fix.longitude) for fix in fixes)

    render_intro_flyover(
        fixes, roads=(), destination=tmp_path / "intro.mp4",
        duration_seconds=1.0, fps=2,
    )

    # render_frame_visual() is only called once now (the one-time
    # raster render), so there's exactly one captured route rather than
    # one per frame - but it's still the complete route, baked into the
    # single raster every frame crops from.
    assert captured_routes == [full_route]


def test_render_intro_flyover_keeps_the_marker_fixed_at_the_trips_first_fix(
    tmp_path, monkeypatch
):
    captured_positions = []
    captured_show_marker = []

    def fake_render_frame_visual(
        _bbox, _roads, _route, position, *_args, show_marker=None, **_kwargs
    ):
        captured_positions.append(position)
        captured_show_marker.append(show_marker)
        return _FakeFrameImage()

    monkeypatch.setattr(
        map_video_module, "render_frame_visual", fake_render_frame_visual
    )
    monkeypatch.setattr(
        map_video_module, "encode_frame_sequence", lambda *_a, **_k: None
    )

    fixes = (
        _fix(0, 59.300, 18.000),
        _fix(1, 59.302, 18.004),
        _fix(2, 59.304, 18.008),
    )

    render_intro_flyover(
        fixes, roads=(), destination=tmp_path / "intro.mp4",
        duration_seconds=1.0, fps=2,
    )

    # No "current position" concept here - just where the trip that's
    # about to play actually begins. render_frame_visual() is only
    # called once now (the one-time raster render, since the Ken-Burns
    # rewrite bakes the marker into that single raster rather than
    # redrawing it per frame - see render_intro_flyover's own
    # docstring), so there's exactly one captured position/show_marker
    # pair rather than one per frame.
    assert captured_positions == [(59.300, 18.000)]
    assert captured_show_marker == [True]


def test_render_intro_flyover_never_composes_a_timestamp_speed_overlay(
    tmp_path, monkeypatch
):
    # This is a clean establishing shot, not a position readout -
    # compose_frame_overlay() (timestamp/speed text/GPS badge) should
    # never be called at all.
    def fail_compose_frame_overlay(*_args, **_kwargs):
        raise AssertionError(
            "compose_frame_overlay() should not be called by render_intro_flyover()"
        )

    monkeypatch.setattr(
        map_video_module, "compose_frame_overlay", fail_compose_frame_overlay
    )
    monkeypatch.setattr(
        map_video_module, "render_frame_visual", lambda *_a, **_k: _FakeFrameImage()
    )
    monkeypatch.setattr(
        map_video_module, "encode_frame_sequence", lambda *_a, **_k: None
    )

    fixes = (_fix(0, 59.300, 18.000), _fix(1, 59.302, 18.004))

    render_intro_flyover(
        fixes, roads=(), destination=tmp_path / "intro.mp4",
        duration_seconds=1.0, fps=2,
    )


def test_render_intro_flyover_defaults_duration_and_fps(tmp_path, monkeypatch):
    rasters = []

    def fake_render_frame_visual(_bbox, *_args, **_kwargs):
        raster = _FakeFrameImage()
        rasters.append(raster)
        return raster

    monkeypatch.setattr(
        map_video_module, "render_frame_visual", fake_render_frame_visual
    )
    monkeypatch.setattr(
        map_video_module, "encode_frame_sequence", lambda *_a, **_k: None
    )

    fixes = (_fix(0, 59.300, 18.000), _fix(1, 59.302, 18.004))

    render_intro_flyover(fixes, roads=(), destination=tmp_path / "intro.mp4")

    expected_frame_count = int(DEFAULT_INTRO_SECONDS * map_video_module.DEFAULT_FPS) + 1
    # render_frame_visual() itself is only called once (the one-time
    # raster render - see render_intro_flyover's own Ken-Burns
    # docstring); the per-frame count now shows up as one resize() call
    # per output frame against that single raster instead.
    assert len(rasters) == 1
    assert len(rasters[0].resize_calls) == expected_frame_count


def test_render_intro_flyover_produces_a_real_video_at_the_requested_duration_and_size(
    tmp_path
):
    fixes = (
        _fix(0, 59.300, 18.000),
        _fix(1, 59.302, 18.004),
        _fix(2, 59.304, 18.008),
    )
    destination = tmp_path / "intro.mp4"

    result = render_intro_flyover(
        fixes, roads=(), destination=destination,
        duration_seconds=2.0, fps=10, width=320, height=240,
    )

    assert result == destination
    assert destination.exists()
    assert _video_dimensions(destination) == (320, 240)
    assert round(_video_duration_seconds(destination), 0) == 2


def test_render_intro_flyover_raises_for_a_missing_marker_image(tmp_path):
    fixes = (_fix(0, 59.300, 18.000), _fix(1, 59.302, 18.004))

    with pytest.raises(MediaToolError):
        render_intro_flyover(
            fixes,
            roads=(),
            destination=tmp_path / "intro.mp4",
            marker_image_path=tmp_path / "does-not-exist.png",
        )


# --- intro_start_bbox() ------------------------------------------------


def test_intro_start_bbox_returns_none_for_no_fixes():
    assert intro_start_bbox(()) is None


def test_intro_start_bbox_returns_a_degenerate_box_for_a_single_fix():
    # Unlike render_intro_flyover() itself (which separately requires
    # at least two positioned fixes to have a route worth drawing),
    # intro_start_bbox() is a thin wrapper around
    # bounding_box_for_fixes() alone - a single valid fix still yields
    # a real (zero-size before scaling) box, not None.
    bbox = intro_start_bbox((_fix(0, 59.300, 18.000),), width=640, height=640)

    assert bbox is not None


def test_intro_start_bbox_returns_none_when_all_fixes_are_invalid():
    fixes = (
        _fix(0, 59.300, 18.000, valid=False),
        _fix(1, 59.302, 18.004, valid=False),
    )

    assert intro_start_bbox(fixes) is None


def test_intro_start_bbox_widens_the_trips_own_bbox_by_the_multiplier():
    fixes = (
        _fix(0, 59.300, 18.000),
        _fix(1, 59.302, 18.004),
        _fix(2, 59.304, 18.008),
    )

    end_bbox = bounding_box_for_fixes(fixes, aspect_ratio=1.0)
    wide_bbox = intro_start_bbox(fixes, width=640, height=640)

    # Same center, INTRO_ZOOM_START_MULTIPLIER-x wider on both axes -
    # exactly what trip_export.py's _load_trip_roads() needs its OSM
    # fetch to actually cover (see intro_start_bbox()'s own docstring:
    # Christer's "started with the whole map showing").
    assert round((wide_bbox.max_lat - wide_bbox.min_lat), 6) == round(
        (end_bbox.max_lat - end_bbox.min_lat) * INTRO_ZOOM_START_MULTIPLIER, 6
    )
    assert round((wide_bbox.max_lon - wide_bbox.min_lon), 6) == round(
        (end_bbox.max_lon - end_bbox.min_lon) * INTRO_ZOOM_START_MULTIPLIER, 6
    )
    assert round((wide_bbox.min_lat + wide_bbox.max_lat) / 2, 6) == round(
        (end_bbox.min_lat + end_bbox.max_lat) / 2, 6
    )
    assert round((wide_bbox.min_lon + wide_bbox.max_lon) / 2, 6) == round(
        (end_bbox.min_lon + end_bbox.max_lon) / 2, 6
    )


def test_intro_start_bbox_respects_a_custom_zoom_start_multiplier():
    fixes = (_fix(0, 59.300, 18.000), _fix(1, 59.302, 18.004))

    end_bbox = bounding_box_for_fixes(fixes, aspect_ratio=1.0)
    wide_bbox = intro_start_bbox(fixes, width=640, height=640, zoom_start_multiplier=2.0)

    assert round((wide_bbox.max_lat - wide_bbox.min_lat), 6) == round(
        (end_bbox.max_lat - end_bbox.min_lat) * 2.0, 6
    )


def test_intro_start_bbox_matches_render_intro_flyovers_own_starting_box(
    tmp_path, monkeypatch
):
    # The whole reason intro_start_bbox() was factored out of
    # render_intro_flyover() rather than staying inline: trip_export.py
    # widens its OSM fetch against intro_start_bbox()'s own return
    # value, so the two have to agree exactly, or the widened fetch
    # wouldn't actually cover what render_intro_flyover() draws.
    captured_bboxes = []

    def fake_render_frame_visual(bbox, *_args, **_kwargs):
        captured_bboxes.append(bbox)
        return _FakeFrameImage()

    monkeypatch.setattr(
        map_video_module, "render_frame_visual", fake_render_frame_visual
    )
    monkeypatch.setattr(
        map_video_module, "encode_frame_sequence", lambda *_a, **_k: None
    )

    fixes = (
        _fix(0, 59.300, 18.000),
        _fix(1, 59.302, 18.004),
        _fix(2, 59.304, 18.008),
    )

    render_intro_flyover(
        fixes, roads=(), destination=tmp_path / "intro.mp4",
        duration_seconds=1.0, fps=2, width=320, height=240,
    )

    expected_start_bbox = intro_start_bbox(fixes, width=320, height=240)

    # The very first frame (t=0) is rendered on the unmodified starting
    # box, before any easing/interpolation moves it toward end_bbox.
    assert captured_bboxes[0] == expected_start_bbox


# --- render_intro_flyover() caption param -------------------------------


def test_render_intro_flyover_draws_no_caption_by_default(tmp_path, monkeypatch):
    def fail_draw_caption(*_args, **_kwargs):
        raise AssertionError(
            "draw_caption() should not be called when caption=None"
        )

    monkeypatch.setattr(map_video_module, "draw_caption", fail_draw_caption)
    monkeypatch.setattr(
        map_video_module, "render_frame_visual", lambda *_a, **_k: _FakeFrameImage()
    )
    monkeypatch.setattr(
        map_video_module, "encode_frame_sequence", lambda *_a, **_k: None
    )

    fixes = (_fix(0, 59.300, 18.000), _fix(1, 59.302, 18.004))

    render_intro_flyover(
        fixes, roads=(), destination=tmp_path / "intro.mp4",
        duration_seconds=1.0, fps=2,
    )


def test_render_intro_flyover_draws_the_caption_on_every_frame_when_given(
    tmp_path, monkeypatch
):
    captured_captions = []

    def fake_draw_caption(visual, text, **_kwargs):
        captured_captions.append(text)
        return visual

    monkeypatch.setattr(map_video_module, "draw_caption", fake_draw_caption)
    monkeypatch.setattr(
        map_video_module, "render_frame_visual", lambda *_a, **_k: _FakeFrameImage()
    )
    monkeypatch.setattr(
        map_video_module, "encode_frame_sequence", lambda *_a, **_k: None
    )

    fixes = (
        _fix(0, 59.300, 18.000),
        _fix(1, 59.302, 18.004),
        _fix(2, 59.304, 18.008),
    )

    render_intro_flyover(
        fixes, roads=(), destination=tmp_path / "intro.mp4",
        duration_seconds=1.0, fps=2, caption="Holiday_trip_20260715_133458",
    )

    assert len(captured_captions) >= 2
    assert all(text == "Holiday_trip_20260715_133458" for text in captured_captions)


def test_render_intro_flyover_burns_a_real_caption_bar_into_the_encoded_video(
    tmp_path
):
    # End-to-end check (no monkeypatching) that a real caption actually
    # changes the rendered pixels, matching Christer's ask: "with the
    # prefix and trip name on like subtitles".
    fixes = (
        _fix(0, 59.300, 18.000),
        _fix(1, 59.302, 18.004),
        _fix(2, 59.304, 18.008),
    )

    plain = tmp_path / "intro_plain.mp4"
    captioned = tmp_path / "intro_captioned.mp4"

    render_intro_flyover(
        fixes, roads=(), destination=plain,
        duration_seconds=1.0, fps=2, width=320, height=240,
    )
    render_intro_flyover(
        fixes, roads=(), destination=captioned,
        duration_seconds=1.0, fps=2, width=320, height=240,
        caption="Holiday_trip_20260715_133458",
    )

    assert plain.stat().st_size != captioned.stat().st_size
