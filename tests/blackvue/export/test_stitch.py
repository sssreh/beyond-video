import json
import subprocess

import pytest

from blackvue.export.stitch import STACK_LAYOUTS
from blackvue.export.stitch import stitch_cameras
from blackvue.generate.media import MediaToolError


def _make_video(path, width, height, duration_seconds=1.0):
    subprocess.run(
        [
            "ffmpeg", "-y",
            "-f", "lavfi",
            "-i", f"testsrc=size={width}x{height}:rate=10",
            "-t", str(duration_seconds),
            str(path),
        ],
        capture_output=True,
        text=True,
        check=True,
    )


def _video_size(path) -> tuple[int, int]:
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


def test_stitch_cameras_side_by_side_doubles_the_width(tmp_path):
    front = tmp_path / "front.mp4"
    rear = tmp_path / "rear.mp4"
    _make_video(front, 320, 240)
    _make_video(rear, 320, 240)

    destination = tmp_path / "stitch.mp4"
    result = stitch_cameras(front, rear, destination, layout="side_by_side")

    assert result == destination
    assert destination.exists()
    assert _video_size(destination) == (640, 240)


def test_stitch_cameras_top_down_doubles_the_height(tmp_path):
    front = tmp_path / "front.mp4"
    rear = tmp_path / "rear.mp4"
    _make_video(front, 320, 240)
    _make_video(rear, 320, 240)

    destination = tmp_path / "stitch.mp4"
    result = stitch_cameras(front, rear, destination, layout="top_down")

    assert result == destination
    assert _video_size(destination) == (320, 480)


def test_stitch_cameras_scales_a_mismatched_rear_resolution_to_match_front(
    tmp_path
):
    front = tmp_path / "front.mp4"
    rear = tmp_path / "rear.mp4"
    _make_video(front, 640, 480)
    # A lower-res rear camera, a real BlackVue front/rear pairing.
    _make_video(rear, 320, 240)

    destination = tmp_path / "stitch.mp4"
    stitch_cameras(front, rear, destination, layout="side_by_side")

    # rear gets scaled to front's own 640x480 before stacking, so the
    # result is exactly double front's width, not some mismatched size
    # ffmpeg would otherwise refuse to hstack at all.
    assert _video_size(destination) == (1280, 480)


def test_stitch_cameras_falls_back_to_a_plain_copy_for_front_only(tmp_path):
    front = tmp_path / "front.mp4"
    _make_video(front, 320, 240)

    destination = tmp_path / "stitch.mp4"
    result = stitch_cameras(front, None, destination, layout="side_by_side")

    assert result == destination
    assert destination.exists()
    # A plain copy - not stacked with anything, so front's own
    # resolution is unchanged.
    assert _video_size(destination) == (320, 240)


def test_stitch_cameras_falls_back_to_a_plain_copy_for_rear_only(tmp_path):
    rear = tmp_path / "rear.mp4"
    _make_video(rear, 320, 240)

    destination = tmp_path / "stitch.mp4"
    result = stitch_cameras(None, rear, destination, layout="top_down")

    assert result == destination
    assert destination.exists()


def test_stitch_cameras_returns_none_for_neither_camera(tmp_path):
    result = stitch_cameras(
        None, None, tmp_path / "stitch.mp4", layout="side_by_side"
    )

    assert result is None
    assert not (tmp_path / "stitch.mp4").exists()


def test_stitch_cameras_rejects_an_unknown_layout(tmp_path):
    front = tmp_path / "front.mp4"
    rear = tmp_path / "rear.mp4"
    _make_video(front, 320, 240)
    _make_video(rear, 320, 240)

    with pytest.raises(ValueError):
        stitch_cameras(
            front, rear, tmp_path / "stitch.mp4", layout="rearview_mirror"
        )


def test_stack_layouts_has_the_two_currently_supported_layouts():
    assert set(STACK_LAYOUTS) == {"side_by_side", "top_down"}


def test_stitch_cameras_raises_media_tool_error_on_a_bad_source(tmp_path):
    front = tmp_path / "front.mp4"
    front.write_text("not a real video")
    rear = tmp_path / "rear.mp4"
    rear.write_text("also not a real video")

    with pytest.raises(MediaToolError):
        stitch_cameras(
            front, rear, tmp_path / "stitch.mp4", layout="side_by_side"
        )
