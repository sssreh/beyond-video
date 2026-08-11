import subprocess
from pathlib import Path

import pytest

from blackvue.generate.media import MediaToolError
from blackvue.search_zoom import CropBox
from blackvue.search_zoom import DEFAULT_HORIZONTAL_FOV_DEGREES
from blackvue.search_zoom import MIN_SPEED_FOR_HEADING_KMH
from blackvue.search_zoom import _bearing_degrees
from blackvue.search_zoom import _crop_filter
from blackvue.search_zoom import _relative_bearing_degrees
from blackvue.search_zoom import compute_crop_box
from blackvue.search_zoom import render_zoom_clip
from blackvue.search_zoom import render_zoom_outputs
from blackvue.search_zoom import render_zoom_thumbnail


def test_bearing_degrees_due_east_is_90():
    assert _bearing_degrees(0.0, 0.0, 0.0, 0.001) == pytest.approx(90.0)


def test_bearing_degrees_due_south_is_180():
    assert _bearing_degrees(0.0, 0.0, -0.001, 0.0) == pytest.approx(180.0)


def test_bearing_degrees_due_west_is_270():
    assert _bearing_degrees(0.0, 0.0, 0.0, -0.001) == pytest.approx(270.0)


def test_relative_bearing_target_ahead_is_zero():
    assert _relative_bearing_degrees(90.0, 90.0) == pytest.approx(0.0)


def test_relative_bearing_target_to_the_right_is_positive():
    assert _relative_bearing_degrees(0.0, 45.0) == pytest.approx(45.0)


def test_relative_bearing_target_to_the_left_is_negative():
    assert _relative_bearing_degrees(0.0, 315.0) == pytest.approx(-45.0)


def test_relative_bearing_wraps_around_360():
    # Heading 350, target bearing 10 - only 20 degrees apart the short
    # way around, not 340 the long way.
    assert _relative_bearing_degrees(350.0, 10.0) == pytest.approx(20.0)


def test_compute_crop_box_returns_none_without_a_heading():
    result = compute_crop_box(
        car_lat=59.3293, car_lon=18.0686,
        heading=None, speed_kmh=30.0,
        target_lat=59.3295, target_lon=18.0692,
        distance_meters=20.0,
    )

    assert result is None


def test_compute_crop_box_returns_none_below_the_min_heading_speed():
    result = compute_crop_box(
        car_lat=59.3293, car_lon=18.0686,
        heading=0.0, speed_kmh=MIN_SPEED_FOR_HEADING_KMH - 0.1,
        target_lat=59.3295, target_lon=18.0692,
        distance_meters=20.0,
    )

    assert result is None


def test_compute_crop_box_returns_none_when_speed_is_missing():
    result = compute_crop_box(
        car_lat=59.3293, car_lon=18.0686,
        heading=0.0, speed_kmh=None,
        target_lat=59.3295, target_lon=18.0692,
        distance_meters=20.0,
    )

    assert result is None


def test_compute_crop_box_returns_none_when_target_is_behind_the_car():
    # Heading north, target due south - straight behind, nowhere near
    # the front camera's own forward field of view.
    result = compute_crop_box(
        car_lat=0.0, car_lon=0.0,
        heading=0.0, speed_kmh=30.0,
        target_lat=-0.001, target_lon=0.0,
        distance_meters=100.0,
    )

    assert result is None


def test_compute_crop_box_centers_when_target_is_straight_ahead():
    crop_box = compute_crop_box(
        car_lat=0.0, car_lon=0.0,
        heading=0.0, speed_kmh=30.0,
        target_lat=0.001, target_lon=0.0,
        distance_meters=50.0,
    )

    assert crop_box is not None
    assert crop_box.x_fraction == pytest.approx(0.5, abs=1e-6)
    assert crop_box.y_fraction == pytest.approx(0.5)


def test_compute_crop_box_shifts_right_for_a_target_to_the_right():
    crop_box = compute_crop_box(
        car_lat=59.3293, car_lon=18.0686,
        heading=0.0, speed_kmh=30.0,
        target_lat=59.3295, target_lon=18.0692,
        distance_meters=20.0,
    )

    assert crop_box is not None
    assert crop_box.x_fraction > 0.5


def test_compute_crop_box_shifts_left_for_a_target_to_the_left():
    crop_box = compute_crop_box(
        car_lat=59.3293, car_lon=18.0686,
        heading=0.0, speed_kmh=30.0,
        target_lat=59.3295, target_lon=18.0680,
        distance_meters=20.0,
    )

    assert crop_box is not None
    assert crop_box.x_fraction < 0.5


def test_compute_crop_box_width_fraction_is_clamped_to_a_sane_range():
    very_close = compute_crop_box(
        car_lat=0.0, car_lon=0.0,
        heading=0.0, speed_kmh=30.0,
        target_lat=0.0001, target_lon=0.0,
        distance_meters=0.5,
    )
    very_far = compute_crop_box(
        car_lat=0.0, car_lon=0.0,
        heading=0.0, speed_kmh=30.0,
        target_lat=0.0001, target_lon=0.0,
        distance_meters=5000.0,
    )

    assert very_close is not None
    assert very_far is not None
    assert 0.0 < very_close.width_fraction <= 0.9
    assert 0.0 < very_far.width_fraction <= 0.9
    assert very_close.width_fraction > very_far.width_fraction


def test_compute_crop_box_width_and_height_fraction_match():
    # width_fraction == height_fraction is what keeps the crop's own
    # aspect ratio equal to the source frame's - see CropBox's own
    # docstring.
    crop_box = compute_crop_box(
        car_lat=0.0, car_lon=0.0,
        heading=0.0, speed_kmh=30.0,
        target_lat=0.001, target_lon=0.0,
        distance_meters=20.0,
    )

    assert crop_box is not None
    assert crop_box.width_fraction == crop_box.height_fraction


def test_compute_crop_box_respects_a_narrower_fov_override():
    # A target 60 degrees off-heading is inside the default 136-degree
    # FOV (half = 68) but outside a narrower 90-degree override
    # (half = 45).
    crop_box = compute_crop_box(
        car_lat=0.0, car_lon=0.0,
        heading=0.0, speed_kmh=30.0,
        target_lat=0.0005, target_lon=0.00087,
        distance_meters=20.0,
        fov_degrees=DEFAULT_HORIZONTAL_FOV_DEGREES,
    )
    narrow = compute_crop_box(
        car_lat=0.0, car_lon=0.0,
        heading=0.0, speed_kmh=30.0,
        target_lat=0.0005, target_lon=0.00087,
        distance_meters=20.0,
        fov_degrees=90.0,
    )

    assert crop_box is not None
    assert narrow is None


def test_crop_filter_stays_within_frame_bounds_at_the_edge():
    # x_fraction pinned to the far right edge - the crop box itself
    # must still fit entirely inside the frame, not run off it.
    crop_box = CropBox(
        x_fraction=1.0, y_fraction=0.5,
        width_fraction=0.5, height_fraction=0.5,
    )

    filter_string = _crop_filter(crop_box, 1000, 500)

    # crop=W:H:X:Y
    _, dims = filter_string.split("=")
    width, height, x, y = (int(v) for v in dims.split(":"))
    assert x >= 0
    assert x + width <= 1000
    assert y >= 0
    assert y + height <= 500


def test_crop_filter_dimensions_are_always_even():
    crop_box = CropBox(
        x_fraction=0.5, y_fraction=0.5,
        width_fraction=0.333, height_fraction=0.333,
    )

    filter_string = _crop_filter(crop_box, 641, 361)

    _, dims = filter_string.split("=")
    width, height, _x, _y = (int(v) for v in dims.split(":"))
    assert width % 2 == 0
    assert height % 2 == 0


def _make_test_video(path: Path, duration_seconds: float = 3.0) -> None:
    subprocess.run(
        [
            "ffmpeg", "-y",
            "-f", "lavfi",
            "-i", f"testsrc=size=320x240:rate=10:duration={duration_seconds}",
            "-pix_fmt", "yuv420p",
            str(path),
        ],
        capture_output=True,
        text=True,
        check=True,
    )


_CENTER_CROP = CropBox(
    x_fraction=0.5, y_fraction=0.5, width_fraction=0.4, height_fraction=0.4,
)


def test_render_zoom_thumbnail_produces_a_real_image_at_source_resolution(tmp_path):
    video = tmp_path / "front.mp4"
    _make_test_video(video)
    destination = tmp_path / "out.jpg"

    render_zoom_thumbnail(video, 1.0, _CENTER_CROP, destination)

    assert destination.exists()
    assert destination.stat().st_size > 0

    result = subprocess.run(
        [
            "ffprobe", "-v", "error",
            "-select_streams", "v:0",
            "-show_entries", "stream=width,height",
            "-of", "csv=p=0",
            str(destination),
        ],
        capture_output=True, text=True, check=True,
    )
    assert result.stdout.strip() == "320,240"


def test_render_zoom_thumbnail_raises_when_the_source_does_not_exist(tmp_path):
    with pytest.raises(MediaToolError):
        render_zoom_thumbnail(
            tmp_path / "missing.mp4", 1.0, _CENTER_CROP, tmp_path / "out.jpg"
        )


def test_render_zoom_clip_produces_a_real_video_without_audio(tmp_path):
    video = tmp_path / "front.mp4"
    _make_test_video(video)
    destination = tmp_path / "out.mp4"

    render_zoom_clip(video, 1.0, _CENTER_CROP, destination, clip_seconds=2.0)

    assert destination.exists()
    assert destination.stat().st_size > 0

    result = subprocess.run(
        [
            "ffprobe", "-v", "error",
            "-show_entries", "stream=codec_type",
            "-of", "csv=p=0",
            str(destination),
        ],
        capture_output=True, text=True, check=True,
    )
    streams = [line for line in result.stdout.splitlines() if line.strip()]
    assert streams == ["video"]


def test_render_zoom_clip_raises_when_the_source_does_not_exist(tmp_path):
    with pytest.raises(MediaToolError):
        render_zoom_clip(
            tmp_path / "missing.mp4", 1.0, _CENTER_CROP, tmp_path / "out.mp4"
        )


def test_render_zoom_outputs_produces_both_files_with_the_given_stem(tmp_path):
    video = tmp_path / "front.mp4"
    _make_test_video(video)

    outputs = render_zoom_outputs(
        video, 1.0, _CENTER_CROP, tmp_path, "20260715_120000_N"
    )

    assert outputs.thumbnail == tmp_path / "20260715_120000_N_zoom.jpg"
    assert outputs.clip == tmp_path / "20260715_120000_N_zoom.mp4"
    assert outputs.thumbnail.exists()
    assert outputs.clip.exists()
