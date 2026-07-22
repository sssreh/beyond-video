import json
import subprocess

import pytest
from PIL import Image

from blackvue.export import stitch as stitch_module
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


def _make_solid_video(path, width, height, color, duration_seconds=1.0):
    subprocess.run(
        [
            "ffmpeg", "-y",
            "-f", "lavfi",
            "-i", f"color=c={color}:size={width}x{height}:rate=10",
            "-t", str(duration_seconds),
            str(path),
        ],
        capture_output=True,
        text=True,
        check=True,
    )


def _extract_first_frame(video_path, png_path) -> Image.Image:
    subprocess.run(
        ["ffmpeg", "-y", "-i", str(video_path), "-frames:v", "1", str(png_path)],
        capture_output=True,
        text=True,
        check=True,
    )
    return Image.open(png_path).convert("RGB")


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


def test_stitch_cameras_scales_the_stacked_output_to_a_requested_resolution(
    tmp_path
):
    front = tmp_path / "front.mp4"
    rear = tmp_path / "rear.mp4"
    _make_video(front, 640, 480)
    _make_video(rear, 640, 480)

    destination = tmp_path / "stitch.mp4"
    stitch_cameras(
        front, rear, destination,
        layout="side_by_side", resolution=(320, 240),
    )

    # Without resolution this would come out 1280x480 (double front's
    # width) - the resolution override replaces that entirely, not
    # scaling relative to it.
    assert _video_size(destination) == (320, 240)


def test_stitch_cameras_letterboxes_instead_of_distorting_a_mismatched_aspect(
    tmp_path
):
    # Two 640x480 (4:3) clips side by side stack to 1280x480 - a
    # 2.667:1 shape. Fitting that into a 320x240 (4:3, 1.333:1) box
    # without distorting it leaves black bars top/bottom rather than
    # squishing the picture to fill the whole frame - this is the bug
    # Christer hit on his real archive (two 16:9 cameras stacked side
    # by side, forced into an unrelated aspect ratio, came out visibly
    # squeezed).
    front = tmp_path / "front.mp4"
    rear = tmp_path / "rear.mp4"
    _make_solid_video(front, 640, 480, "red")
    _make_solid_video(rear, 640, 480, "red")

    destination = tmp_path / "stitch.mp4"
    stitch_cameras(
        front, rear, destination,
        layout="side_by_side", resolution=(320, 240),
    )

    image = _extract_first_frame(destination, tmp_path / "frame.png")

    top_bar = image.getpixel((160, 5))
    bottom_bar = image.getpixel((160, 234))
    center = image.getpixel((160, 120))

    assert sum(top_bar) < 60
    assert sum(bottom_bar) < 60
    assert center[0] > 150 and center[1] < 80 and center[2] < 80


def test_stitch_cameras_preserves_a_mismatched_rears_own_aspect_ratio(
    tmp_path
):
    # A rear camera with a genuinely different aspect ratio than
    # front (not just a different resolution at the same ratio, like
    # the earlier mismatched-resolution test) - front 640x480 (4:3),
    # rear 640x360 (16:9). hstack only requires matching heights, so
    # rear should scale to height 480 while keeping its own 16:9
    # shape (853x480), not get force-stretched into 4:3.
    front = tmp_path / "front.mp4"
    rear = tmp_path / "rear.mp4"
    _make_video(front, 640, 480)
    _make_video(rear, 640, 360)

    destination = tmp_path / "stitch.mp4"
    stitch_cameras(front, rear, destination, layout="side_by_side")

    width, height = _video_size(destination)
    assert height == 480
    # front's own 640 plus rear scaled to keep 16:9 at height 480
    # (640 * (480/360) rounded to even) = 853 or 854.
    assert width in (640 + 853, 640 + 854)


def test_stitch_cameras_scales_a_single_camera_when_resolution_given(tmp_path):
    front = tmp_path / "front.mp4"
    _make_video(front, 640, 480)

    destination = tmp_path / "stitch.mp4"
    stitch_cameras(
        front, None, destination,
        layout="side_by_side", resolution=(320, 240),
    )

    assert _video_size(destination) == (320, 240)


def test_stitch_cameras_passes_bitrate_through_to_the_encoder(
    tmp_path, monkeypatch
):
    captured = {}

    def fake_encode(input_args, destination, extra_codec_args=None):
        captured["extra_codec_args"] = extra_codec_args
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(b"")

    monkeypatch.setattr(
        stitch_module, "encode_with_nvenc_fallback", fake_encode
    )

    front = tmp_path / "front.mp4"
    rear = tmp_path / "rear.mp4"
    _make_video(front, 320, 240)
    _make_video(rear, 320, 240)

    stitch_cameras(
        front, rear, tmp_path / "stitch.mp4",
        layout="side_by_side", bitrate="256k",
    )

    assert captured["extra_codec_args"] == [
        "-b:v", "256k", "-maxrate", "256k", "-bufsize", "256k",
    ]


def test_stitch_cameras_raises_media_tool_error_on_a_bad_source(tmp_path):
    front = tmp_path / "front.mp4"
    front.write_text("not a real video")
    rear = tmp_path / "rear.mp4"
    rear.write_text("also not a real video")

    with pytest.raises(MediaToolError):
        stitch_cameras(
            front, rear, tmp_path / "stitch.mp4", layout="side_by_side"
        )


def test_stitch_cameras_falls_back_to_cpu_decode_when_nvdec_fails_for_real(
    tmp_path, monkeypatch
):
    # Force "NVDEC is available" (this sandbox has no real NVIDIA GPU)
    # and let the real ffmpeg attempt run - the -hwaccel cuda attempt
    # genuinely fails here, proving the fallback to plain CPU decode
    # isn't just mocked but actually produces a correct, working
    # stitched video. Same pattern as
    # test_encode_frame_sequence_falls_back_to_libx264_when_nvenc_fails_for_real
    # in test_export_media.py, one level up: that test forces the
    # encode-side NVENC/libx264 fallback, this one forces the
    # decode-side NVDEC/CPU fallback stitch.py adds on top of it.
    monkeypatch.setattr(stitch_module, "_NVDEC_AVAILABLE", True)

    front = tmp_path / "front.mp4"
    rear = tmp_path / "rear.mp4"
    _make_video(front, 320, 240)
    _make_video(rear, 320, 240)

    destination = tmp_path / "stitch.mp4"
    result = stitch_cameras(front, rear, destination, layout="side_by_side")

    assert result == destination
    assert destination.exists()
    assert _video_size(destination) == (640, 240)


def test_stitch_cameras_single_camera_falls_back_to_cpu_decode_when_nvdec_fails(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(stitch_module, "_NVDEC_AVAILABLE", True)

    front = tmp_path / "front.mp4"
    _make_video(front, 640, 480)

    destination = tmp_path / "stitch.mp4"
    stitch_cameras(
        front, None, destination,
        layout="side_by_side", resolution=(320, 240),
    )

    assert _video_size(destination) == (320, 240)


def test_stitch_cameras_is_silent_by_default(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(stitch_module, "_NVDEC_AVAILABLE", False)

    front = tmp_path / "front.mp4"
    rear = tmp_path / "rear.mp4"
    _make_video(front, 320, 240)
    _make_video(rear, 320, 240)

    stitch_cameras(front, rear, tmp_path / "stitch.mp4", layout="side_by_side")

    assert capsys.readouterr().err == ""


def test_stitch_cameras_prints_decode_timing_when_debug_is_true(
    tmp_path, monkeypatch, capsys
):
    monkeypatch.setattr(stitch_module, "_NVDEC_AVAILABLE", False)

    front = tmp_path / "front.mp4"
    rear = tmp_path / "rear.mp4"
    _make_video(front, 320, 240)
    _make_video(rear, 320, 240)

    stitch_cameras(
        front, rear, tmp_path / "stitch.mp4", layout="side_by_side", debug=True
    )

    err = capsys.readouterr().err
    assert "decode=cpu" in err
    assert "succeeded in" in err


def test_fit_and_pad_places_the_prefix_after_the_input_label(tmp_path):
    # A regression test for a real bug: an earlier version built
    # `predecode + _fit_and_pad(...)`, which put "hwdownload,
    # format=nv12," *before* the "[0:v]" label instead of after it -
    # ffmpeg requires the label first in a filter-chain link, so that
    # produced a malformed filter_complex string ffmpeg rejected
    # instantly (looked like "NVDEC unavailable", but was actually a
    # syntax error - real NVDEC decode was never attempted). The fix
    # was a `prefix` parameter inserted *inside* the bracketed label
    # reference, asserted here directly on the string this function
    # builds.
    result = stitch_module._fit_and_pad(
        "0:v", "v", 320, 240, prefix="hwdownload,format=nv12,"
    )

    assert result.startswith("[0:v]hwdownload,format=nv12,scale=")


def test_run_reencode_single_with_hw_decode_builds_a_valid_filter_string(
    tmp_path, monkeypatch
):
    # Exercises the actual call site (_run_reencode_single) rather
    # than just _fit_and_pad in isolation - the earlier bug was in how
    # the two were combined at the call site, not in either function
    # alone. Fakes the encoder so this doesn't need a real source
    # file or real ffmpeg - it only checks the filter_complex string
    # handed to the encoder is well-formed.
    captured = {}

    def fake_encode(input_args, destination, extra_codec_args=None):
        captured["input_args"] = input_args
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(b"")

    monkeypatch.setattr(stitch_module, "encode_with_nvenc_fallback", fake_encode)

    stitch_module._run_reencode_single(
        tmp_path / "front.mp4", tmp_path / "out.mp4",
        resolution=(320, 240), bitrate=None, hw_decode=True,
    )

    input_args = captured["input_args"]
    filter_complex = input_args[input_args.index("-filter_complex") + 1]
    assert filter_complex.startswith("[0:v]hwdownload,format=nv12,scale=")


def test_nvdec_available_checks_ffmpeg_hwaccels_output(monkeypatch):
    monkeypatch.setattr(stitch_module, "_NVDEC_AVAILABLE", None)

    captured = {}

    class FakeResult:
        stdout = "Hardware acceleration methods:\ncuda\nqsv\n"

    def fake_run(command, **kwargs):
        captured["command"] = command
        return FakeResult()

    monkeypatch.setattr(stitch_module.subprocess, "run", fake_run)

    assert stitch_module._nvdec_available() is True
    assert captured["command"] == ["ffmpeg", "-hide_banner", "-hwaccels"]

    # Cached after the first call - a second call shouldn't shell out
    # again.
    captured.clear()
    assert stitch_module._nvdec_available() is True
    assert captured == {}
