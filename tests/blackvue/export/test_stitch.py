import json
import subprocess
from datetime import datetime
from datetime import timedelta
from pathlib import Path

import pytest
from PIL import Image

from blackvue.export import stitch as stitch_module
from blackvue.export.stitch import ALL_LAYOUTS
from blackvue.export.stitch import AUTO_LAYOUT
from blackvue.export.stitch import STACK_LAYOUTS
from blackvue.export.stitch import _escape_subtitles_filename
from blackvue.export.stitch import _map_panel_dimensions
from blackvue.export.stitch import parse_gsensor_position
from blackvue.export.stitch import pick_stitch_layout
from blackvue.export.stitch import stitch_cameras
from blackvue.generate.media import MediaToolError
from blackvue.telemetry.gps_reader import GpsFix


def _fix(offset_seconds, lat, lon):
    return GpsFix(
        timestamp=datetime(2026, 7, 15, 13, 0, 0) + timedelta(seconds=offset_seconds),
        valid=True,
        latitude=lat,
        longitude=lon,
        speed_kmh=50.0,
        course=45.0,
    )


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


def _make_gsensor_fake(path, size=480, box_size=200, duration_seconds=1.0):
    # A flat chroma-key green background with a big red box in the
    # middle - a stand-in for gsensor_render.py's real gauge (also
    # pure green background, RGB(0,255,0)), just simpler to reason
    # about pixel colors for a colorkey/overlay smoke test. The box is
    # deliberately large (not gsensor.mp4's actual thin rings/dot) so
    # it's still trivially samplable after being scaled down to a
    # small overlay and re-encoded.
    subprocess.run(
        [
            "ffmpeg", "-y",
            "-f", "lavfi",
            "-i", f"color=c=0x00ff00:size={size}x{size}:rate=5",
            "-vf",
            f"drawbox=x=iw/2-{box_size // 2}:y=ih/2-{box_size // 2}:"
            f"w={box_size}:h={box_size}:color=red:t=fill",
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


def _make_audio(path, duration_seconds=1.0):
    subprocess.run(
        [
            "ffmpeg", "-y",
            "-f", "lavfi",
            "-i", f"sine=frequency=440:duration={duration_seconds}",
            "-c:a", "aac",
            str(path),
        ],
        capture_output=True,
        text=True,
        check=True,
    )


def _audio_stream(path) -> dict | None:
    result = subprocess.run(
        [
            "ffprobe", "-v", "error",
            "-select_streams", "a:0",
            "-show_entries", "stream=codec_name",
            "-of", "json",
            str(path),
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    streams = json.loads(result.stdout)["streams"]
    return streams[0] if streams else None


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
            front, rear, tmp_path / "stitch.mp4", layout="diagonal"
        )


def test_stack_layouts_has_the_two_hstack_vstack_layouts():
    # rearview_mirror is deliberately NOT in STACK_LAYOUTS - it isn't a
    # plain hstack/vstack of two full-size cameras (see ALL_LAYOUTS for
    # the full set stitch_cameras() itself accepts).
    assert set(STACK_LAYOUTS) == {"side_by_side", "top_down"}


def test_all_layouts_includes_rearview_mirror():
    assert set(ALL_LAYOUTS) == {"side_by_side", "top_down", "rearview_mirror"}


def test_pick_stitch_layout_picks_side_by_side_for_an_east_west_trip():
    fixes = (_fix(0, 59.30, 18.000), _fix(2, 59.301, 18.100))
    assert pick_stitch_layout(fixes) == "side_by_side"


def test_pick_stitch_layout_picks_top_down_for_a_north_south_trip():
    fixes = (_fix(0, 59.00, 18.000), _fix(2, 60.00, 18.001))
    assert pick_stitch_layout(fixes) == "top_down"


def test_pick_stitch_layout_returns_none_for_no_gps_data():
    assert pick_stitch_layout(()) is None


def test_stitch_cameras_rejects_auto_layout_directly(tmp_path):
    # AUTO_LAYOUT is a trip_export.py/CLI-level sentinel resolved
    # *before* ever reaching stitch_cameras() - it should never be a
    # valid `layout` here, same as any other made-up name.
    front = tmp_path / "front.mp4"
    rear = tmp_path / "rear.mp4"
    _make_video(front, 160, 120)
    _make_video(rear, 160, 120)

    with pytest.raises(ValueError):
        stitch_cameras(front, rear, tmp_path / "stitch.mp4", layout=AUTO_LAYOUT)


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


def test_stitch_cameras_scale_shrinks_the_output_preserving_aspect_ratio(
    tmp_path
):
    # Christer: a native side_by_side composite with no --stitch
    # -resolution/--stitch-bitrate given came out 3.5GB at 5422x4320,
    # 20 minutes to render - --stitch-scale is a padding-free way to
    # shrink it that always keeps the natural aspect ratio, unlike an
    # exact --stitch-resolution WxH pair (which can introduce
    # letterbox/pillarbox bars for a size that doesn't happen to match).
    front = tmp_path / "front.mp4"
    rear = tmp_path / "rear.mp4"
    _make_video(front, 320, 240)
    _make_video(rear, 320, 240)

    destination = tmp_path / "stitch.mp4"
    stitch_cameras(
        front, rear, destination, layout="side_by_side", scale=50,
    )

    # Natural size is 640x240 (two 320x240 hstacked) - scale=50 halves
    # both dimensions, so the aspect ratio (640/240 == 320/120) is
    # exactly preserved, not just "smaller".
    width, height = _video_size(destination)
    assert (width, height) == (320, 120)


def test_stitch_cameras_scale_100_is_a_no_op(tmp_path):
    front = tmp_path / "front.mp4"
    rear = tmp_path / "rear.mp4"
    _make_video(front, 320, 240)
    _make_video(rear, 320, 240)

    destination = tmp_path / "stitch.mp4"
    stitch_cameras(front, rear, destination, layout="side_by_side", scale=100)

    assert _video_size(destination) == (640, 240)


def test_stitch_cameras_max_width_caps_width_without_upscaling(tmp_path):
    front = tmp_path / "front.mp4"
    rear = tmp_path / "rear.mp4"
    _make_video(front, 320, 240)
    _make_video(rear, 320, 240)

    destination = tmp_path / "stitch.mp4"
    # Natural width is 640 (two 320-wide cameras hstacked) - capped at
    # 400, so both dimensions scale down together (never up) to fit.
    stitch_cameras(
        front, rear, destination, layout="side_by_side", max_width=400,
    )

    width, height = _video_size(destination)
    assert width <= 400
    # Aspect ratio (640/240 == 2.667) preserved, not distorted.
    assert abs(width / height - 640 / 240) < 0.05


def test_stitch_cameras_max_width_larger_than_natural_size_is_a_no_op(
    tmp_path
):
    front = tmp_path / "front.mp4"
    rear = tmp_path / "rear.mp4"
    _make_video(front, 320, 240)
    _make_video(rear, 320, 240)

    destination = tmp_path / "stitch.mp4"
    # Natural width is 640 - a cap above that should never upscale.
    stitch_cameras(
        front, rear, destination, layout="side_by_side", max_width=2000,
    )

    assert _video_size(destination) == (640, 240)


def test_stitch_cameras_max_height_caps_height_without_upscaling(tmp_path):
    front = tmp_path / "front.mp4"
    rear = tmp_path / "rear.mp4"
    _make_video(front, 320, 240)
    _make_video(rear, 320, 240)

    destination = tmp_path / "stitch.mp4"
    # Natural height is 240 - capped at 100.
    stitch_cameras(
        front, rear, destination, layout="side_by_side", max_height=100,
    )

    width, height = _video_size(destination)
    assert height <= 100
    assert abs(width / height - 640 / 240) < 0.05


def test_stitch_cameras_scale_and_max_width_combine_tightest_wins(tmp_path):
    front = tmp_path / "front.mp4"
    rear = tmp_path / "rear.mp4"
    _make_video(front, 320, 240)
    _make_video(rear, 320, 240)

    destination = tmp_path / "stitch.mp4"
    # Natural width 640: scale=90 alone would ask for ~576, but
    # max_width=200 is the tighter cap and should win instead - no
    # validation/error needed, they just combine as independent bounds.
    stitch_cameras(
        front, rear, destination, layout="side_by_side",
        scale=90, max_width=200,
    )

    width, _height = _video_size(destination)
    # scale=90 alone would land near 576 (90% of the natural 640) -
    # max_width=200 being the tighter cap should win instead. A couple
    # of pixels' slack accounts for ffmpeg's own even-number rounding
    # on the auto-derived width (only the target height is set
    # explicitly - see _stack()'s own note on why one `scale=-2:H`
    # filter is enough for either bound).
    assert width <= 202


def test_stitch_cameras_scale_includes_the_map_panel(tmp_path):
    # The scale/max_width/max_height shrink applies to the *whole*
    # final frame, not just the camera portion - confirmed here by
    # checking the scaled-down output still has a map panel's worth of
    # extra width beyond a plain camera-only stitch at the same scale.
    front = tmp_path / "front.mp4"
    rear = tmp_path / "rear.mp4"
    _make_video(front, 320, 240)
    _make_video(rear, 320, 240)

    fixes = (_fix(0, 59.30, 18.000), _fix(2, 59.34, 18.005))

    camera_only = tmp_path / "camera_only.mp4"
    stitch_cameras(
        front, rear, camera_only, layout="side_by_side", scale=50,
    )
    camera_only_width, _h1 = _video_size(camera_only)

    with_map = tmp_path / "with_map.mp4"
    stitch_cameras(
        front, rear, with_map, layout="side_by_side", scale=50,
        map_mode="map", map_side="right", map_fixes=fixes, map_roads=(),
    )
    with_map_width, _h2 = _video_size(with_map)

    assert with_map_width > camera_only_width


def test_stitch_cameras_scale_shrinks_decode_time_scaling_not_just_the_final_pass(
    tmp_path, monkeypatch
):
    # Christer: "rear, front, panel and stitch are still slow even
    # with --stitch-scale 10" - the first version of this feature only
    # applied scale/max_width/max_height as a trailing filter on the
    # already-fully-built final frame, so front/rear still decoded and
    # re-encoded at full native size regardless. Fixed by deriving an
    # equivalent `effective_resolution` from the natural (pre-decode
    # -probed) size and feeding it through the same decode-time
    # -scaling path --stitch-resolution already uses. Confirmed here
    # the same way test_stack_scales_both_cameras_toward_the_target_
    # resolution_not_native_size does: front's own native height
    # (2160) must not show up in either scale filter.
    monkeypatch.setattr(stitch_module, "_NVDEC_AVAILABLE", False)

    scale_filters = {}

    def fake_decode_camera(source, destination, *, scale_filter, debug=False):
        # Copies the real source through rather than writing an empty
        # placeholder (unlike test_stack_scales_both_cameras_toward_
        # the_target_resolution_not_native_size's own fake) - `scale`
        # (unlike a bare `resolution`) also makes _stack() probe the
        # *decoded* intermediates afterward (see content_width/
        # comp_width below _decode_camera's own call site), which
        # needs a real, ffprobe-able file to succeed against.
        import shutil

        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, destination)
        scale_filters[destination.name] = scale_filter

    def fake_encode(input_args, destination, extra_codec_args=None):
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(b"")

    monkeypatch.setattr(stitch_module, "_decode_camera", fake_decode_camera)
    monkeypatch.setattr(stitch_module, "encode_with_nvenc_fallback", fake_encode)

    front = tmp_path / "front.mp4"
    rear = tmp_path / "rear.mp4"
    _make_video(front, 3840, 2160, duration_seconds=0.1)
    _make_video(rear, 3840, 2160, duration_seconds=0.1)

    stitch_cameras(
        front, rear, tmp_path / "stitch.mp4",
        layout="side_by_side", scale=10,
    )

    for name, scale_filter in scale_filters.items():
        assert "2160" not in scale_filter, (
            f"{name}'s scale filter ({scale_filter!r}) still targets "
            "front's full native height - --stitch-scale isn't "
            "reducing decode-time work"
        )


def test_stitch_cameras_scale_shrinks_the_map_panels_own_render_size(
    tmp_path, monkeypatch
):
    # Same complaint, the map-panel half: rendering the panel at full
    # native size regardless of --stitch-scale was real, wasted PIL
    # -rendering work (map.mp4's own per-frame road-drawing cost -
    # see map_video.py/map_render.py) - not just a bigger final file.
    captured_sizes = []
    original_render_map_panel = stitch_module._render_map_panel

    def _capture_render_map_panel(*args, **kwargs):
        captured_sizes.append((kwargs.get("width"), kwargs.get("height")))
        return original_render_map_panel(*args, **kwargs)

    monkeypatch.setattr(
        stitch_module, "_render_map_panel", _capture_render_map_panel
    )

    front = tmp_path / "front.mp4"
    rear = tmp_path / "rear.mp4"
    _make_video(front, 640, 480)
    _make_video(rear, 640, 480)

    fixes = (_fix(0, 59.30, 18.000), _fix(2, 59.34, 18.005))

    stitch_cameras(
        front, rear, tmp_path / "full.mp4",
        layout="side_by_side",
        map_mode="map", map_fixes=fixes, map_roads=(),
    )
    stitch_cameras(
        front, rear, tmp_path / "scaled.mp4",
        layout="side_by_side", scale=25,
        map_mode="map", map_fixes=fixes, map_roads=(),
    )

    assert len(captured_sizes) == 2
    full_width, full_height = captured_sizes[0]
    scaled_width, scaled_height = captured_sizes[1]
    assert scaled_width < full_width
    assert scaled_height < full_height


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


def test_run_reencode_single_with_hw_decode_pins_the_input_to_a_shared_device(
    tmp_path, monkeypatch
):
    # A controlled test on Christer's real archive found decoding
    # front and rear concurrently in one ffmpeg process with two
    # *unshared* -hwaccel cuda inputs cost ~5x the sum of decoding each
    # alone (two separate processes, run at the same time, each cost
    # barely more than solo) - pointing at ffmpeg opening two
    # independent CUDA contexts rather than real GPU decoder
    # contention. The fix: one -init_hw_device up front, every input
    # pinned to it via -hwaccel_device. Asserted here on the args this
    # module actually builds.
    captured = {}

    def fake_encode(input_args, destination, extra_codec_args=None):
        captured["input_args"] = input_args
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(b"")

    monkeypatch.setattr(stitch_module, "encode_with_nvenc_fallback", fake_encode)

    stitch_module._run_reencode_single(
        tmp_path / "front.mp4", tmp_path / "out.mp4",
        resolution=None, bitrate=None, hw_decode=True,
    )

    input_args = captured["input_args"]
    assert input_args[:3] == ["-init_hw_device", "cuda=cu:0", "-hwaccel"]
    assert "-hwaccel_device" in input_args
    assert input_args[input_args.index("-hwaccel_device") + 1] == "cu"


def test_run_reencode_single_with_hw_decode_and_no_resolution_builds_valid_filter(
    tmp_path, monkeypatch
):
    # Regression test: with resolution=None, hw_decode=True takes the
    # "elif hw_decode" branch, which builds f"[0:v]{predecode}[v]" -
    # predecode ("hwdownload,format=nv12,") ends with a trailing comma
    # meant to separate it from a following filter, but there's no
    # following filter here, so the un-stripped version produced
    # "[0:v]hwdownload,format=nv12,[v]" - a dangling comma right
    # before the output label, which ffmpeg rejects instantly. Found
    # for real on Christer's archive (this exact branch had never been
    # exercised before - every prior real run always passed
    # --stitch-resolution, which takes the other branch).
    captured = {}

    def fake_encode(input_args, destination, extra_codec_args=None):
        captured["input_args"] = input_args
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(b"")

    monkeypatch.setattr(stitch_module, "encode_with_nvenc_fallback", fake_encode)

    stitch_module._run_reencode_single(
        tmp_path / "front.mp4", tmp_path / "out.mp4",
        resolution=None, bitrate=None, hw_decode=True,
    )

    input_args = captured["input_args"]
    filter_complex = input_args[input_args.index("-filter_complex") + 1]
    assert filter_complex == "[0:v]hwdownload,format=nv12[v]"


def test_run_decode_camera_with_hw_decode_pins_the_input_to_a_shared_device(
    tmp_path, monkeypatch
):
    # _run_decode_camera() is the per-camera decode call the two-camera
    # _stack() path now uses (one process per camera, run concurrently
    # - see _decode_camera()'s docstring) - it still uses the shared
    # CUDA device convention for its own single input, consistent with
    # _run_reencode_single()'s single-camera path.
    captured = {}

    def fake_encode(input_args, destination, extra_codec_args=None):
        captured["input_args"] = input_args
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(b"")

    monkeypatch.setattr(stitch_module, "encode_with_nvenc_fallback", fake_encode)

    stitch_module._run_decode_camera(
        tmp_path / "front.mp4", tmp_path / "out.mp4",
        scale_filter=None, hw_decode=True,
    )

    input_args = captured["input_args"]
    assert input_args[:3] == ["-init_hw_device", "cuda=cu:0", "-hwaccel"]
    assert "-hwaccel_device" in input_args
    assert input_args[input_args.index("-hwaccel_device") + 1] == "cu"


def test_run_decode_camera_with_hw_decode_and_no_scale_filter_builds_valid_filter(
    tmp_path, monkeypatch
):
    # The same trailing-comma regression as
    # test_run_reencode_single_with_hw_decode_and_no_resolution_builds_valid_filter,
    # in _run_decode_camera()'s equivalent branch - this is the one
    # that actually fired on Christer's real archive: front doesn't
    # need a scale_filter (only rear does, to match front - see
    # _stack()), so front's decode call hits exactly this branch.
    captured = {}

    def fake_encode(input_args, destination, extra_codec_args=None):
        captured["input_args"] = input_args
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(b"")

    monkeypatch.setattr(stitch_module, "encode_with_nvenc_fallback", fake_encode)

    stitch_module._run_decode_camera(
        tmp_path / "front.mp4", tmp_path / "out.mp4",
        scale_filter=None, hw_decode=True,
    )

    input_args = captured["input_args"]
    filter_complex = input_args[input_args.index("-filter_complex") + 1]
    assert filter_complex == "[0:v]hwdownload,format=nv12[v]"


def test_stack_scales_both_cameras_toward_the_target_resolution_not_native_size(
    tmp_path, monkeypatch
):
    # Regression test for the second real-archive finding: matching
    # rear to front's full NATIVE height before the final downscale
    # wasted a lot of time encoding an intermediate far bigger than
    # the eventual output needed (rear upscaled from 1080p to front's
    # native ~2160p, just to immediately shrink back to 720p two steps
    # later - measured at ~100s on Christer's archive for that
    # unnecessary size). Neither scale filter should reference front's
    # native height (2160) at all.
    monkeypatch.setattr(stitch_module, "_NVDEC_AVAILABLE", False)

    scale_filters = {}

    def fake_decode_camera(source, destination, *, scale_filter, debug=False):
        scale_filters[destination.name] = scale_filter
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(b"")

    def fake_encode(input_args, destination, extra_codec_args=None):
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(b"")

    monkeypatch.setattr(stitch_module, "_decode_camera", fake_decode_camera)
    monkeypatch.setattr(stitch_module, "encode_with_nvenc_fallback", fake_encode)

    front = tmp_path / "front.mp4"
    rear = tmp_path / "rear.mp4"
    # Front is 4K, rear is 1080p - a real mismatch like Christer's
    # archive - front's native height (2160) must NOT show up in
    # either scale filter below.
    _make_video(front, 3840, 2160, duration_seconds=0.1)
    _make_video(rear, 1920, 1080, duration_seconds=0.1)

    stitch_cameras(
        front, rear, tmp_path / "stitch.mp4",
        layout="side_by_side", resolution=(1280, 720),
    )

    # Both same 16:9 aspect ratio, so the ideal shared height splits
    # the target width evenly between them: 1280 / (16/9 + 16/9) = 360
    # -> 640-wide each, combined width exactly 1280. Not out_height
    # (720) directly - that combines to 2560 wide, double what's
    # needed, which is the bug this test originally caught before
    # _ideal_shared_dimension() existed (Christer worked out the
    # correct ~half-of-target number by hand and asked whether that
    # was right - it was).
    assert scale_filters["front.mp4"] == "scale=-2:360"
    assert scale_filters["rear.mp4"] == "scale=-2:360"
    assert "2160" not in scale_filters["front.mp4"]
    assert "2160" not in scale_filters["rear.mp4"]
    assert "720" not in scale_filters["front.mp4"]
    assert "720" not in scale_filters["rear.mp4"]


def test_ideal_shared_dimension_hstack_makes_combined_width_match_target():
    # Two same-aspect-ratio (16:9) cameras split the target width
    # evenly - see the test above for the real-archive numbers this
    # traces back to.
    shared = stitch_module._ideal_shared_dimension(
        3840, 2160, 1920, 1080,
        filter_name="hstack", out_width=1280, out_height=720,
    )
    assert shared == 360


def test_ideal_shared_dimension_vstack_makes_combined_height_match_target():
    # Mirror of the hstack case: shared width instead of shared
    # height, solving so the combined *height* (front's height on top
    # of rear's) matches out_height instead of out_width.
    shared = stitch_module._ideal_shared_dimension(
        3840, 2160, 1920, 1080,
        filter_name="vstack", out_width=1280, out_height=720,
    )
    assert shared == 640


def test_ideal_shared_dimension_caps_at_the_other_target_dimension():
    # Two very narrow/tall cameras (aspect ratio 1:4) side by side:
    # solving purely for "combined width == out_width" would ask for a
    # height taller than the target frame itself (each camera's own
    # width contribution is small, so a lot of height is needed to use
    # up the target width) - capped at out_height instead, since
    # nothing should ever be scaled bigger than the final frame.
    shared = stitch_module._ideal_shared_dimension(
        100, 400, 100, 400,
        filter_name="hstack", out_width=1280, out_height=720,
    )
    assert shared == 720


def test_stack_decodes_front_and_rear_as_separate_ffmpeg_calls(
    tmp_path, monkeypatch
):
    # The core of the two-process-decode redesign: front and rear must
    # each get their own ffmpeg call (their own single -i) rather than
    # one ffmpeg process handling both hardware-decoded inputs at once
    # - see _decode_camera()'s docstring for why (a real, measured ~5x
    # slowdown from combining them). Confirmed here by counting
    # encode_with_nvenc_fallback calls (front decode, rear decode,
    # final combine = 3): the two decode calls each have exactly one
    # -i; the final combine call legitimately has two (it reads the
    # two already-decoded, CPU-readable intermediates - no hwaccel on
    # either, so no contention there).
    monkeypatch.setattr(stitch_module, "_NVDEC_AVAILABLE", False)

    calls = []

    def fake_encode(input_args, destination, extra_codec_args=None):
        calls.append(input_args)
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(b"")

    monkeypatch.setattr(stitch_module, "encode_with_nvenc_fallback", fake_encode)

    front = tmp_path / "front.mp4"
    rear = tmp_path / "rear.mp4"
    _make_video(front, 320, 240)
    _make_video(rear, 320, 240)

    stitch_cameras(front, rear, tmp_path / "stitch.mp4", layout="side_by_side")

    assert len(calls) == 3
    i_counts = sorted(call.count("-i") for call in calls)
    assert i_counts == [1, 1, 2]


def test_stack_applies_bitrate_only_to_the_final_combine_call(
    tmp_path, monkeypatch
):
    # The two decode calls produce intermediates, not the final
    # output - a bitrate cap only makes sense on the last (combine)
    # call, which is what the user actually asked to constrain.
    monkeypatch.setattr(stitch_module, "_NVDEC_AVAILABLE", False)

    captured_extra_codec_args = []

    def fake_encode(input_args, destination, extra_codec_args=None):
        captured_extra_codec_args.append(extra_codec_args)
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(b"")

    monkeypatch.setattr(stitch_module, "encode_with_nvenc_fallback", fake_encode)

    front = tmp_path / "front.mp4"
    rear = tmp_path / "rear.mp4"
    _make_video(front, 320, 240)
    _make_video(rear, 320, 240)

    stitch_cameras(
        front, rear, tmp_path / "stitch.mp4",
        layout="side_by_side", bitrate="256k",
    )

    assert captured_extra_codec_args.count(None) == 2
    assert captured_extra_codec_args.count(
        ["-b:v", "256k", "-maxrate", "256k", "-bufsize", "256k"]
    ) == 1


def test_parse_bitrate_bps_handles_k_and_m_suffixes():
    assert stitch_module._parse_bitrate_bps("256k") == 256_000
    assert stitch_module._parse_bitrate_bps("256K") == 256_000
    assert stitch_module._parse_bitrate_bps("2M") == 2_000_000
    assert stitch_module._parse_bitrate_bps("2m") == 2_000_000
    assert stitch_module._parse_bitrate_bps("1500000") == 1_500_000


def test_parse_bitrate_bps_returns_none_for_unparseable_values():
    assert stitch_module._parse_bitrate_bps("not-a-number") is None
    assert stitch_module._parse_bitrate_bps("") is None


def test_video_bitrate_reads_a_real_files_container_bit_rate(tmp_path):
    video = tmp_path / "video.mp4"
    _make_video(video, 320, 240)

    bit_rate = stitch_module._video_bitrate(video)

    assert bit_rate is not None
    assert bit_rate > 0


def test_video_bitrate_returns_none_for_an_unreadable_file(tmp_path):
    not_a_video = tmp_path / "not_a_video.mp4"
    not_a_video.write_text("garbage")

    assert stitch_module._video_bitrate(not_a_video) is None


def _fake_intermediate_bitrates(front_bps, rear_bps):
    def fake_video_bitrate(path):
        return {"front.mp4": front_bps, "rear.mp4": rear_bps}[path.name]

    return fake_video_bitrate


def test_stack_caps_bitrate_to_the_sum_of_the_two_intermediates_bitrates(
    tmp_path, monkeypatch
):
    # The whole point of the cap: a requested bitrate way above what
    # the two (already resolution/bitrate-reduced) intermediates
    # actually carry can't recover detail that isn't there anymore -
    # capped to the sum of their real bitrates instead, with a warning
    # explaining why.
    monkeypatch.setattr(stitch_module, "_NVDEC_AVAILABLE", False)
    monkeypatch.setattr(
        stitch_module, "_video_bitrate",
        _fake_intermediate_bitrates(500_000, 300_000),
    )

    captured_extra_codec_args = []

    def fake_encode(input_args, destination, extra_codec_args=None):
        captured_extra_codec_args.append(extra_codec_args)
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(b"")

    monkeypatch.setattr(stitch_module, "encode_with_nvenc_fallback", fake_encode)

    front = tmp_path / "front.mp4"
    rear = tmp_path / "rear.mp4"
    _make_video(front, 320, 240)
    _make_video(rear, 320, 240)

    warnings: list[str] = []
    stitch_cameras(
        front, rear, tmp_path / "stitch.mp4",
        layout="side_by_side", bitrate="5M", warnings=warnings,
    )

    # 500_000 + 300_000 = 800_000 bps ceiling, well under the
    # requested 5_000_000 (5M) - the final combine call should be
    # capped to the sum, not the original request.
    final_call = captured_extra_codec_args[-1]
    assert final_call == [
        "-b:v", "800000", "-maxrate", "800000", "-bufsize", "800000",
    ]
    assert len(warnings) == 1
    assert "5M" in warnings[0]
    assert "800k" in warnings[0]


def test_stack_does_not_cap_bitrate_when_already_below_the_ceiling(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(stitch_module, "_NVDEC_AVAILABLE", False)
    monkeypatch.setattr(
        stitch_module, "_video_bitrate",
        _fake_intermediate_bitrates(500_000, 300_000),
    )

    captured_extra_codec_args = []

    def fake_encode(input_args, destination, extra_codec_args=None):
        captured_extra_codec_args.append(extra_codec_args)
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(b"")

    monkeypatch.setattr(stitch_module, "encode_with_nvenc_fallback", fake_encode)

    front = tmp_path / "front.mp4"
    rear = tmp_path / "rear.mp4"
    _make_video(front, 320, 240)
    _make_video(rear, 320, 240)

    warnings: list[str] = []
    stitch_cameras(
        front, rear, tmp_path / "stitch.mp4",
        layout="side_by_side", bitrate="256k", warnings=warnings,
    )

    final_call = captured_extra_codec_args[-1]
    assert final_call == [
        "-b:v", "256k", "-maxrate", "256k", "-bufsize", "256k",
    ]
    assert warnings == []


def test_stack_skips_the_bitrate_cap_when_intermediate_bitrate_unknown(
    tmp_path, monkeypatch
):
    # Never worth failing (or even warning about) the export over -
    # if either intermediate's bitrate can't be determined, the
    # requested bitrate just flows through untouched.
    monkeypatch.setattr(stitch_module, "_NVDEC_AVAILABLE", False)
    monkeypatch.setattr(stitch_module, "_video_bitrate", lambda path: None)

    captured_extra_codec_args = []

    def fake_encode(input_args, destination, extra_codec_args=None):
        captured_extra_codec_args.append(extra_codec_args)
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(b"")

    monkeypatch.setattr(stitch_module, "encode_with_nvenc_fallback", fake_encode)

    front = tmp_path / "front.mp4"
    rear = tmp_path / "rear.mp4"
    _make_video(front, 320, 240)
    _make_video(rear, 320, 240)

    warnings: list[str] = []
    stitch_cameras(
        front, rear, tmp_path / "stitch.mp4",
        layout="side_by_side", bitrate="5M", warnings=warnings,
    )

    final_call = captured_extra_codec_args[-1]
    assert final_call == ["-b:v", "5M", "-maxrate", "5M", "-bufsize", "5M"]
    assert warnings == []


def test_stack_skips_bitrate_probing_when_no_bitrate_requested(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(stitch_module, "_NVDEC_AVAILABLE", False)

    probe_calls = []

    def fake_video_bitrate(path):
        probe_calls.append(path)
        return 1

    monkeypatch.setattr(stitch_module, "_video_bitrate", fake_video_bitrate)

    def fake_encode(input_args, destination, extra_codec_args=None):
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(b"")

    monkeypatch.setattr(stitch_module, "encode_with_nvenc_fallback", fake_encode)

    front = tmp_path / "front.mp4"
    rear = tmp_path / "rear.mp4"
    _make_video(front, 320, 240)
    _make_video(rear, 320, 240)

    stitch_cameras(front, rear, tmp_path / "stitch.mp4", layout="side_by_side")

    assert probe_calls == []


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


# --- --stitch-map panel tests ---


def test_map_panel_dimensions_matches_shared_axis_for_left_right():
    # A north-south trip (tall real-world shape) placed left/right -
    # panel height must match the composite exactly.
    fixes = (_fix(0, 59.30, 18.000), _fix(10, 59.34, 18.001))

    size = _map_panel_dimensions(1000, 400, side="left", fixes=fixes)

    assert size is not None
    width, height = size
    assert height == 400
    assert 0 < width <= 1000


def test_map_panel_dimensions_matches_shared_axis_for_top_down():
    fixes = (_fix(0, 59.30, 18.000), _fix(10, 59.34, 18.001))

    size = _map_panel_dimensions(1000, 400, side="down", fixes=fixes)

    assert size is not None
    width, height = size
    assert width == 1000
    assert 0 < height <= 400


def test_map_panel_dimensions_clamps_the_free_dimension_to_the_fraction_range():
    # An extremely tall, near-straight-line trip would otherwise ask
    # for a razor-thin (or huge) panel - clamped to
    # [_MIN_MAP_PANEL_FRACTION, _MAX_MAP_PANEL_FRACTION] of the
    # composite's own corresponding dimension instead.
    fixes = (_fix(0, 0.0, 0.0), _fix(10, 10.0, 0.0001))

    width, _height = _map_panel_dimensions(1000, 400, side="left", fixes=fixes)

    assert round(width / 1000, 2) >= stitch_module._MIN_MAP_PANEL_FRACTION
    assert round(width / 1000, 2) <= stitch_module._MAX_MAP_PANEL_FRACTION


def test_map_panel_dimensions_returns_none_for_no_gps_data():
    assert _map_panel_dimensions(1000, 400, side="left", fixes=()) is None


def test_map_panel_dimensions_size_fraction_overrides_the_geography_sizing():
    # A near-straight-line trip (same shape as the "clamps" test above)
    # would auto-size right at the 20% floor - --stitch-map-size
    # (size_fraction, a raw 0-1 fraction here) bypasses that entirely,
    # for a trip a user wants a bigger panel for than the auto sizing
    # would ever give them.
    fixes = (_fix(0, 0.0, 0.0), _fix(10, 10.0, 0.0001))

    width, height = _map_panel_dimensions(
        1000, 400, side="left", fixes=fixes, size_fraction=0.7,
    )

    assert width == 700
    assert height == 400


def test_map_panel_dimensions_size_fraction_is_not_clamped_to_the_auto_range():
    # Deliberately outside [_MIN_MAP_PANEL_FRACTION,
    # _MAX_MAP_PANEL_FRACTION] (0.2-0.5) - an explicit size_fraction is
    # an override, not a suggestion the auto-sizing clamp should still
    # apply to. Range validation belongs at the CLI layer (see
    # bv_export.py's _parse_map_size()/MIN_/MAX_MAP_SIZE_PERCENT), not
    # here.
    fixes = (_fix(0, 0.0, 0.0), _fix(10, 10.0, 0.0001))

    width, _height = _map_panel_dimensions(
        1000, 400, side="top", fixes=fixes, size_fraction=0.05,
    )

    assert width == 1000
    assert round(_map_panel_dimensions(
        1000, 400, side="top", fixes=fixes, size_fraction=0.05,
    )[1] / 400, 3) == 0.05


def test_map_panel_dimensions_size_fraction_still_requires_gps_data():
    assert _map_panel_dimensions(
        1000, 400, side="left", fixes=(), size_fraction=0.7,
    ) is None


def test_stitch_cameras_map_panel_defaults_to_down_for_side_by_side(tmp_path):
    front = tmp_path / "front.mp4"
    rear = tmp_path / "rear.mp4"
    _make_video(front, 160, 120)
    _make_video(rear, 160, 120)

    fixes = (_fix(0, 59.30, 18.000), _fix(2, 59.34, 18.005))
    warnings = []
    destination = tmp_path / "stitch.mp4"

    stitch_cameras(
        front, rear, destination, layout="side_by_side",
        map_mode="map", map_fixes=fixes, map_roads=(), warnings=warnings,
    )

    assert warnings == []
    width, height = _video_size(destination)
    # side_by_side alone would be 320x120 (two 160x120 hstacked) - the
    # default 'down' side vstacks the panel below, so width is
    # unchanged and height grows.
    assert width == 320
    assert height > 120


def test_stitch_cameras_map_panel_defaults_to_left_for_top_down(tmp_path):
    front = tmp_path / "front.mp4"
    rear = tmp_path / "rear.mp4"
    _make_video(front, 160, 120)
    _make_video(rear, 160, 120)

    fixes = (_fix(0, 59.30, 18.000), _fix(2, 59.34, 18.005))
    warnings = []
    destination = tmp_path / "stitch.mp4"

    stitch_cameras(
        front, rear, destination, layout="top_down",
        map_mode="map", map_fixes=fixes, map_roads=(), warnings=warnings,
    )

    assert warnings == []
    width, height = _video_size(destination)
    # top_down alone would be 160x240 (two 160x120 vstacked) - the
    # default 'left' side hstacks the panel on the left, so height is
    # unchanged and width grows.
    assert height == 240
    assert width > 160


def test_stitch_cameras_map_panel_render_is_silent_by_default(tmp_path, capsys):
    front = tmp_path / "front.mp4"
    rear = tmp_path / "rear.mp4"
    _make_video(front, 160, 120)
    _make_video(rear, 160, 120)

    fixes = (_fix(0, 59.30, 18.000), _fix(2, 59.34, 18.005))
    destination = tmp_path / "stitch.mp4"

    stitch_cameras(
        front, rear, destination, layout="side_by_side",
        map_mode="map", map_fixes=fixes, map_roads=(),
    )

    assert capsys.readouterr().err == ""


def test_stitch_cameras_map_panel_debug_reports_render_timing(tmp_path, capsys):
    # Christer: "but it doesnt report time for the map video build" -
    # the panel is rendered fresh inside _stack() (see the design
    # decision recorded in WORKING_CONTEXT.md's --stitch-map section:
    # reused files would risk visible stretching, so a fresh render
    # was chosen instead), but its own cost was entirely invisible,
    # folded into the overall "stitch phase took Xs" line with no way
    # to tell how much of that was the panel specifically.
    front = tmp_path / "front.mp4"
    rear = tmp_path / "rear.mp4"
    _make_video(front, 160, 120)
    _make_video(rear, 160, 120)

    fixes = (_fix(0, 59.30, 18.000), _fix(2, 59.34, 18.005))
    destination = tmp_path / "stitch.mp4"

    stitch_cameras(
        front, rear, destination, layout="side_by_side",
        map_mode="map", map_fixes=fixes, map_roads=(), debug=True,
    )

    err = capsys.readouterr().err
    assert "stitch: map panel render took" in err


def test_stitch_cameras_map_panel_side_can_be_overridden(tmp_path):
    front = tmp_path / "front.mp4"
    rear = tmp_path / "rear.mp4"
    _make_video(front, 160, 120)
    _make_video(rear, 160, 120)

    fixes = (_fix(0, 59.30, 18.000), _fix(2, 59.34, 18.005))
    warnings = []
    destination = tmp_path / "stitch.mp4"

    stitch_cameras(
        front, rear, destination, layout="side_by_side",
        map_mode="map", map_side="right",
        map_fixes=fixes, map_roads=(), warnings=warnings,
    )

    assert warnings == []
    width, height = _video_size(destination)
    assert height == 120
    assert width > 320


def test_stitch_cameras_map_size_overrides_the_auto_panel_width(tmp_path):
    front = tmp_path / "front.mp4"
    rear = tmp_path / "rear.mp4"
    _make_video(front, 160, 120)
    _make_video(rear, 160, 120)

    # A near-straight-line trip - the same shape that hits the auto
    # -sizing's 20% floor in _map_panel_dimensions()'s own unit tests.
    fixes = (_fix(0, 0.0, 0.0), _fix(10, 10.0, 0.0001))
    warnings = []
    destination = tmp_path / "stitch.mp4"

    stitch_cameras(
        front, rear, destination, layout="top_down",
        map_mode="map", map_fixes=fixes, map_roads=(), map_size=40.0,
        warnings=warnings,
    )

    assert warnings == []
    width, height = _video_size(destination)
    # top_down's camera composite is 160 wide (two 160x120 stacked
    # vertically) - 40% of that, rounded to an even pixel count.
    assert height == 240
    assert width == 160 + max(2, round(160 * 0.40 / 2) * 2)


def test_stitch_cameras_map_panel_zoom_requires_a_radius(tmp_path):
    front = tmp_path / "front.mp4"
    rear = tmp_path / "rear.mp4"
    _make_video(front, 160, 120)
    _make_video(rear, 160, 120)

    fixes = (_fix(0, 59.30, 18.000), _fix(2, 59.34, 18.005))
    warnings = []
    destination = tmp_path / "stitch.mp4"

    stitch_cameras(
        front, rear, destination, layout="side_by_side",
        map_mode="zoom", map_fixes=fixes, map_roads=(), warnings=warnings,
    )

    # No radius given - the panel is skipped, camera composite alone
    # comes through untouched, and the warning names the missing flag.
    width, height = _video_size(destination)
    assert (width, height) == (320, 120)
    assert len(warnings) == 1
    assert "zoom requires" in warnings[0]


def test_stitch_cameras_map_panel_zoom_with_a_radius_adds_the_panel(tmp_path):
    front = tmp_path / "front.mp4"
    rear = tmp_path / "rear.mp4"
    _make_video(front, 160, 120)
    _make_video(rear, 160, 120)

    fixes = (_fix(0, 59.30, 18.000), _fix(2, 59.34, 18.005))
    warnings = []
    destination = tmp_path / "stitch.mp4"

    stitch_cameras(
        front, rear, destination, layout="side_by_side",
        map_mode="zoom", map_zoom_meters=50.0,
        map_fixes=fixes, map_roads=(), warnings=warnings,
    )

    assert warnings == []
    width, height = _video_size(destination)
    assert width == 320
    assert height > 120


def test_stitch_cameras_map_panel_skipped_without_gps_data(tmp_path):
    front = tmp_path / "front.mp4"
    rear = tmp_path / "rear.mp4"
    _make_video(front, 160, 120)
    _make_video(rear, 160, 120)

    warnings = []
    destination = tmp_path / "stitch.mp4"

    stitch_cameras(
        front, rear, destination, layout="side_by_side",
        map_mode="map", map_fixes=(), map_roads=(), warnings=warnings,
    )

    # map_fixes is empty - map_mode is a documented no-op in that
    # case, not a failure worth a warning (there's simply nothing to
    # draw a map from, e.g. a trip with no GPS at all).
    width, height = _video_size(destination)
    assert (width, height) == (320, 120)
    assert warnings == []


def test_stitch_cameras_map_panel_combines_with_a_requested_resolution(tmp_path):
    front = tmp_path / "front.mp4"
    rear = tmp_path / "rear.mp4"
    _make_video(front, 160, 120)
    _make_video(rear, 160, 120)

    fixes = (_fix(0, 59.30, 18.000), _fix(2, 59.34, 18.005))
    warnings = []
    destination = tmp_path / "stitch.mp4"

    stitch_cameras(
        front, rear, destination, layout="side_by_side",
        resolution=(240, 135),
        map_mode="map", map_fixes=fixes, map_roads=(), warnings=warnings,
    )

    assert warnings == []
    width, height = _video_size(destination)
    # The camera portion is fit-and-padded to exactly the requested
    # resolution first; the map panel then adds to that - so the
    # composite's own width (240) is preserved, and total height grows
    # past the requested 135.
    assert width == 240
    assert height > 135


def test_stitch_cameras_map_panel_ignored_for_single_camera_fallback(tmp_path):
    front = tmp_path / "front.mp4"
    _make_video(front, 160, 120)

    fixes = (_fix(0, 59.30, 18.000), _fix(2, 59.34, 18.005))
    warnings = []
    destination = tmp_path / "stitch.mp4"

    stitch_cameras(
        front, None, destination, layout="side_by_side",
        map_mode="map", map_fixes=fixes, map_roads=(), warnings=warnings,
    )

    # Single-camera fallback doesn't support a map panel yet (see
    # stitch_cameras()'s docstring) - it's silently ignored, same as
    # `layout` is for this path, not a warning-worthy failure.
    width, height = _video_size(destination)
    assert (width, height) == (160, 120)
    assert warnings == []


# --- --stitch-gsensor overlay tests ---


def test_parse_gsensor_position_parses_named_combinations():
    assert parse_gsensor_position("top-right") == ("right", "top")
    assert parse_gsensor_position("down-left") == ("left", "down")
    assert parse_gsensor_position("left") == ("left", "center")
    assert parse_gsensor_position("top") == ("center", "top")
    assert parse_gsensor_position("center") == ("center", "center")


def test_parse_gsensor_position_is_case_insensitive():
    assert parse_gsensor_position("Top-Right") == ("right", "top")


def test_parse_gsensor_position_rejects_contradictory_horizontal_tokens():
    with pytest.raises(ValueError) as exc_info:
        parse_gsensor_position("left-right")
    assert "left and right" in str(exc_info.value)


def test_parse_gsensor_position_rejects_contradictory_vertical_tokens():
    with pytest.raises(ValueError) as exc_info:
        parse_gsensor_position("top-down")
    assert "top and down" in str(exc_info.value)


def test_parse_gsensor_position_rejects_unknown_tokens():
    with pytest.raises(ValueError) as exc_info:
        parse_gsensor_position("bottom")
    assert "unknown position token" in str(exc_info.value)


def test_stitch_cameras_gsensor_overlay_keys_out_the_green_background(tmp_path):
    front = tmp_path / "front.mp4"
    rear = tmp_path / "rear.mp4"
    _make_solid_video(front, 160, 120, "blue")
    _make_solid_video(rear, 160, 120, "blue")
    gsensor = tmp_path / "gsensor.mp4"
    _make_gsensor_fake(gsensor)

    warnings = []
    destination = tmp_path / "stitch.mp4"
    stitch_cameras(
        front, rear, destination, layout="side_by_side",
        gsensor_video=gsensor, gsensor_pos="top-right",
        warnings=warnings,
    )

    assert warnings == []
    assert _video_size(destination) == (320, 120)

    image = _extract_first_frame(destination, tmp_path / "frame.png")

    # comp 320x120, default size 15% -> overlay ~48x48, margin ~(6, 2).
    overlay_x0, overlay_y0 = 320 - 48 - 6, 2
    corner = image.getpixel((overlay_x0 + 2, overlay_y0 + 2))
    center = image.getpixel((overlay_x0 + 24, overlay_y0 + 24))
    far = image.getpixel((10, 100))

    # Far from the overlay: plain blue footage, untouched.
    assert far[2] > far[0] and far[2] > far[1]
    # Near the overlay's own corner (outside the fake gauge's red
    # box): the green background was keyed out, so the blue footage
    # underneath shows through - not green.
    assert not (corner[1] > 200 and corner[0] < 60 and corner[2] < 60)
    # The overlay's own center (inside the fake gauge's red box):
    # still red - the overlay's actual content survived the key.
    assert center[0] > 120 and center[1] < 100


def test_stitch_cameras_gsensor_overlay_defaults_to_top_right(tmp_path):
    front = tmp_path / "front.mp4"
    rear = tmp_path / "rear.mp4"
    _make_solid_video(front, 160, 120, "blue")
    _make_solid_video(rear, 160, 120, "blue")
    gsensor = tmp_path / "gsensor.mp4"
    _make_gsensor_fake(gsensor)

    warnings = []
    destination = tmp_path / "stitch.mp4"
    # No gsensor_pos/gsensor_xy given at all.
    stitch_cameras(
        front, rear, destination, layout="side_by_side",
        gsensor_video=gsensor, warnings=warnings,
    )

    assert warnings == []
    image = _extract_first_frame(destination, tmp_path / "frame.png")
    overlay_x0, overlay_y0 = 320 - 48 - 6, 2
    center = image.getpixel((overlay_x0 + 24, overlay_y0 + 24))
    assert center[0] > 120 and center[1] < 100


def test_stitch_cameras_gsensor_overlay_explicit_xy_places_it_exactly(tmp_path):
    front = tmp_path / "front.mp4"
    rear = tmp_path / "rear.mp4"
    _make_solid_video(front, 160, 120, "blue")
    _make_solid_video(rear, 160, 120, "blue")
    gsensor = tmp_path / "gsensor.mp4"
    _make_gsensor_fake(gsensor)

    warnings = []
    destination = tmp_path / "stitch.mp4"
    stitch_cameras(
        front, rear, destination, layout="side_by_side",
        gsensor_video=gsensor, gsensor_xy=(50.0, 50.0),
        warnings=warnings,
    )

    assert warnings == []
    image = _extract_first_frame(destination, tmp_path / "frame.png")
    # comp 320x120 -> x=round(320*0.5)=160, y=round(120*0.5)=60, no
    # margin applied to an explicit xy override.
    center = image.getpixel((160 + 24, 60 + 24))
    assert center[0] > 120 and center[1] < 100


def test_stitch_cameras_gsensor_overlay_combines_with_a_map_panel(tmp_path):
    front = tmp_path / "front.mp4"
    rear = tmp_path / "rear.mp4"
    _make_solid_video(front, 160, 120, "blue")
    _make_solid_video(rear, 160, 120, "blue")
    gsensor = tmp_path / "gsensor.mp4"
    _make_gsensor_fake(gsensor)
    fixes = (_fix(0, 59.30, 18.000), _fix(2, 59.34, 18.005))

    warnings = []
    destination = tmp_path / "stitch.mp4"
    stitch_cameras(
        front, rear, destination, layout="side_by_side",
        gsensor_video=gsensor, gsensor_pos="top-right",
        map_mode="map", map_fixes=fixes, map_roads=(),
        warnings=warnings,
    )

    # Correct input-index bookkeeping between the two extra inputs
    # (gsensor at index 2, map panel at index 3) is the real thing
    # being tested here - a wrong index would either fail outright or
    # silently combine the wrong stream.
    assert warnings == []
    width, height = _video_size(destination)
    assert width == 320
    assert height > 120


def test_stitch_cameras_gsensor_overlay_lands_on_real_footage_not_resolution_padding(
    tmp_path
):
    # Regression test for a real issue Christer found: a
    # --stitch-resolution whose aspect ratio doesn't match the camera
    # composite's own (a 320x180 landscape box here, vs. top_down's
    # portrait 160x240 stack) makes _fit_and_pad() pillarbox the
    # footage - the overlay used to be sized/positioned against the
    # *padded* box's own full width/height, landing "top-right" deep
    # in the black bars instead of anywhere near the real footage.
    front = tmp_path / "front.mp4"
    rear = tmp_path / "rear.mp4"
    _make_solid_video(front, 160, 120, "blue")
    _make_solid_video(rear, 160, 120, "blue")
    gsensor = tmp_path / "gsensor.mp4"
    _make_gsensor_fake(gsensor)

    warnings = []
    destination = tmp_path / "stitch.mp4"
    stitch_cameras(
        front, rear, destination, layout="top_down",
        resolution=(320, 180),
        gsensor_video=gsensor, gsensor_pos="top-right",
        warnings=warnings,
    )

    assert warnings == []
    assert _video_size(destination) == (320, 180)

    image = _extract_first_frame(destination, tmp_path / "frame.png")

    def _has_red(x_range, y_range):
        return any(
            image.getpixel((x, y))[0] > 120 and image.getpixel((x, y))[1] < 100
            for x in x_range for y in y_range
        )

    # 160x240 content scaled by 0.75 (height-constrained: 240*0.75=180
    # exactly) into the 320x180 box, centered with 100px of black
    # pillarbox on each side (320-120)/2=100 - real footage occupies
    # x in [100, 220). The pre-pad overlay (comp=160x240, default 15%
    # size/2% margin) lands its fake gauge's red box around x=[140,
    # 150), y=[12,22) *before* that same 0.75 scale+offset is applied,
    # landing around x=[205,213), y=[9,17) - comfortably inside the
    # real footage, near its right edge.
    assert _has_red(range(203, 216, 2), range(6, 20, 2))

    # Where the *old*, buggy math (sized/positioned against the padded
    # 320x180 box directly) would have placed it instead - deep in the
    # right pillarbox, nowhere near the real footage at all.
    assert not _has_red(range(275, 305, 3), range(15, 40, 3))


def test_stitch_cameras_gsensor_overlay_ignored_for_single_camera_fallback(
    tmp_path
):
    front = tmp_path / "front.mp4"
    _make_video(front, 160, 120)
    gsensor = tmp_path / "gsensor.mp4"
    _make_gsensor_fake(gsensor)

    warnings = []
    destination = tmp_path / "stitch.mp4"
    stitch_cameras(
        front, None, destination, layout="side_by_side",
        gsensor_video=gsensor, warnings=warnings,
    )

    # Same "not built for the single-camera path yet" convention as
    # the map panel.
    assert _video_size(destination) == (160, 120)
    assert warnings == []


def test_stitch_cameras_without_gsensor_video_leaves_footage_untouched(
    tmp_path
):
    front = tmp_path / "front.mp4"
    rear = tmp_path / "rear.mp4"
    _make_video(front, 160, 120)
    _make_video(rear, 160, 120)

    warnings = []
    destination = tmp_path / "stitch.mp4"
    stitch_cameras(
        front, rear, destination, layout="side_by_side", warnings=warnings,
    )

    assert _video_size(destination) == (320, 120)
    assert warnings == []


def test_stitch_cameras_muxes_the_trip_audio_into_the_two_camera_output(
    tmp_path
):
    front = tmp_path / "front.mp4"
    rear = tmp_path / "rear.mp4"
    audio = tmp_path / "audio.aac"
    _make_video(front, 160, 120)
    _make_video(rear, 160, 120)
    _make_audio(audio)

    destination = tmp_path / "stitch.mp4"
    stitch_cameras(
        front, rear, destination, layout="side_by_side", audio_path=audio,
    )

    stream = _audio_stream(destination)
    assert stream is not None
    # Stream-copied, not re-encoded - the muxed track should still be
    # the same codec the source .aac already was.
    assert stream["codec_name"] == "aac"


def test_stitch_cameras_without_audio_path_produces_no_audio_stream(tmp_path):
    front = tmp_path / "front.mp4"
    rear = tmp_path / "rear.mp4"
    _make_video(front, 160, 120)
    _make_video(rear, 160, 120)

    destination = tmp_path / "stitch.mp4"
    stitch_cameras(front, rear, destination, layout="side_by_side")

    assert _audio_stream(destination) is None


def test_stitch_cameras_audio_path_ignored_for_single_camera_fallback(
    tmp_path
):
    front = tmp_path / "front.mp4"
    audio = tmp_path / "audio.aac"
    _make_video(front, 160, 120)
    _make_audio(audio)

    destination = tmp_path / "stitch.mp4"
    # Same "not built for the single-camera path yet" convention as the
    # map panel/gsensor overlay - documented as a known gap, not a bug.
    stitch_cameras(
        front, None, destination, layout="side_by_side", audio_path=audio,
    )

    assert destination.exists()


def test_escape_subtitles_filename_converts_backslashes_and_escapes_colons():
    # A Windows-style absolute path (the real shape of every path this
    # project actually produces, since bv-export runs on Christer's
    # Windows machine) - `\` becomes `/` (sidesteps its own meaning as
    # an escape character rather than trying to double-escape it), and
    # the drive-letter `:` is escaped as `\:` since ffmpeg's
    # filtergraph parser would otherwise read it as the `subtitles`
    # filter's own option separator and truncate the path at `C`.
    windows_path = Path("C:\\Users\\christer\\trip\\trip.srt")
    assert (
        _escape_subtitles_filename(windows_path)
        == "C\\:/Users/christer/trip/trip.srt"
    )


def _average_brightness(image, x_range, y_range):
    pixels = [
        image.getpixel((x, y))
        for x in x_range
        for y in y_range
    ]
    return sum(sum(p) / 3 for p in pixels) / len(pixels)


def _dark_pixel_fraction(image, x_range, y_range, threshold=150):
    pixels = [image.getpixel((x, y)) for x in x_range for y in y_range]
    dark = sum(1 for p in pixels if sum(p) / 3 < threshold)
    return dark / len(pixels)


def test_stitch_cameras_subtitles_background_bar_darkens_more_than_without(
    tmp_path
):
    front = tmp_path / "front.mp4"
    rear = tmp_path / "rear.mp4"
    _make_solid_video(front, 320, 240, "0xdddddd")
    _make_solid_video(rear, 320, 240, "0xdddddd")
    srt = tmp_path / "trip.srt"
    srt.write_text(
        "1\n00:00:00,000 --> 00:00:02,000\nHello world subtitle test\n\n"
    )

    fractions = {}
    for background in (True, False):
        destination = tmp_path / f"stitch_{background}.mp4"
        warnings = []
        stitch_cameras(
            front, rear, destination, layout="side_by_side",
            subtitles_path=srt, subtitles_background=background,
            warnings=warnings,
        )
        assert warnings == []

        image = _extract_first_frame(
            destination, tmp_path / f"frame_{background}.png"
        )
        width, height = image.size
        fractions[background] = _dark_pixel_fraction(
            image, range(0, width, 2), range(height - 60, height - 5, 2),
        )

    # Both variants have *some* dark pixels near the bottom (libass's
    # default outline-only style already draws a thin dark outline
    # around the glyphs even with no force_style override at all) -
    # the real thing distinguishing "background bar on" is a solid box
    # spanning the whole text line, which darkens a much bigger
    # fraction of the sampled region than bare outlined text does.
    assert fractions[True] > fractions[False] * 1.5


def test_stitch_cameras_subtitles_leaves_the_top_of_the_frame_untouched(
    tmp_path
):
    front = tmp_path / "front.mp4"
    rear = tmp_path / "rear.mp4"
    _make_solid_video(front, 320, 240, "0xdddddd")
    _make_solid_video(rear, 320, 240, "0xdddddd")
    srt = tmp_path / "trip.srt"
    srt.write_text(
        "1\n00:00:00,000 --> 00:00:02,000\nHello world subtitle test\n\n"
    )

    warnings = []
    destination = tmp_path / "stitch.mp4"
    stitch_cameras(
        front, rear, destination, layout="side_by_side",
        subtitles_path=srt, warnings=warnings,
    )
    assert warnings == []

    image = _extract_first_frame(destination, tmp_path / "frame.png")
    width, _ = image.size
    # libass's default placement is centered, near the bottom - the
    # top of the frame should still be the plain 0xdddddd (221,221,221)
    # footage, completely unaffected by either the text or (if on) its
    # background bar.
    top_brightness = _average_brightness(
        image, range(0, width, 4), range(5, 30, 2)
    )
    assert top_brightness > 210


def test_stitch_cameras_subtitles_combines_with_gsensor_and_a_map_panel(
    tmp_path
):
    front = tmp_path / "front.mp4"
    rear = tmp_path / "rear.mp4"
    _make_solid_video(front, 160, 120, "blue")
    _make_solid_video(rear, 160, 120, "blue")
    gsensor = tmp_path / "gsensor.mp4"
    _make_gsensor_fake(gsensor)
    srt = tmp_path / "trip.srt"
    srt.write_text("1\n00:00:00,000 --> 00:00:02,000\nHello\n\n")
    fixes = (_fix(0, 59.30, 18.000), _fix(2, 59.34, 18.005))

    warnings = []
    destination = tmp_path / "stitch.mp4"
    stitch_cameras(
        front, rear, destination, layout="side_by_side",
        gsensor_video=gsensor, gsensor_pos="top-right",
        map_mode="map", map_fixes=fixes, map_roads=(),
        subtitles_path=srt,
        warnings=warnings,
    )

    # Correct bookkeeping across all three extra pieces (gsensor input,
    # map panel input, and the subtitle burn-in - applied to the
    # camera composite alone, *before* the map panel is combined in;
    # see test_stitch_cameras_subtitles_are_confined_to_the_camera_
    # region_not_the_map_panel below for that scoping itself) is the
    # real thing being tested here - a wrong label/index anywhere in
    # this chain would fail the ffmpeg call outright rather than
    # silently produce a wrong image.
    assert warnings == []
    width, height = _video_size(destination)
    assert width == 320
    assert height > 120


def test_stitch_cameras_subtitles_are_confined_to_the_camera_region_not_the_map_panel(
    tmp_path
):
    # Confirms a real issue found on Christer's own --stitch-map
    # export: subtitles used to be burned onto the *final* composed
    # frame (camera + map panel combined), so a full-width subtitle
    # bar stretched underneath the map panel too, with nothing to do
    # with it. Fixed by applying the subtitle burn-in to the camera
    # composite alone, before the map panel is ever hstacked/vstacked
    # alongside it - same "confined to the footage region" scoping the
    # gsensor overlay already had.
    front = tmp_path / "front.mp4"
    rear = tmp_path / "rear.mp4"
    _make_solid_video(front, 160, 120, "0xdddddd")
    _make_solid_video(rear, 160, 120, "0xdddddd")
    srt = tmp_path / "trip.srt"
    srt.write_text(
        "1\n00:00:00,000 --> 00:00:02,000\nHello world subtitle\n\n"
    )
    fixes = (_fix(0, 59.30, 18.000), _fix(2, 59.34, 18.005))

    warnings = []
    destination = tmp_path / "stitch.mp4"
    stitch_cameras(
        front, rear, destination, layout="top_down",
        map_mode="map", map_fixes=fixes, map_roads=(), map_size=30.0,
        subtitles_path=srt, subtitles_background=True,
        warnings=warnings,
    )
    assert warnings == []

    # top_down's default map panel side is "left"; front/rear are
    # already both 160 wide (vstack matches width, so no rescale is
    # needed), and --stitch-map-size=30 forces the panel to exactly
    # 30% of that camera width, rounded to the nearest even pixel
    # count (same convention _map_panel_dimensions() itself uses).
    width, height = _video_size(destination)
    map_width = max(2, round(160 * 0.30 / 2) * 2)
    assert width == 160 + map_width

    image = _extract_first_frame(destination, tmp_path / "frame.png")

    # The map panel's own bottom strip - its light off-white
    # map_render.BACKGROUND_COLOR, ~(247, 244, 238) - should be
    # completely unaffected by the subtitle burn-in, which was applied
    # before the map panel was ever combined in.
    map_bottom_brightness = _average_brightness(
        image,
        range(2, max(4, map_width - 2), 2),
        range(height - 40, height - 5, 2),
    )
    assert map_bottom_brightness > 210

    # The camera region's own bottom strip should show *some* dark
    # pixels from the subtitle's background bar - confirming the
    # subtitle still rendered at all, just confined to this region.
    camera_dark_fraction = _dark_pixel_fraction(
        image, range(map_width, width, 2), range(height - 40, height - 5, 2),
    )
    assert camera_dark_fraction > 0.05


def test_stitch_cameras_subtitles_ignored_for_single_camera_fallback(
    tmp_path
):
    front = tmp_path / "front.mp4"
    _make_video(front, 160, 120)
    srt = tmp_path / "trip.srt"
    srt.write_text("1\n00:00:00,000 --> 00:00:02,000\nHello\n\n")

    warnings = []
    destination = tmp_path / "stitch.mp4"
    stitch_cameras(
        front, None, destination, layout="side_by_side",
        subtitles_path=srt, warnings=warnings,
    )

    # Same "not built for the single-camera path yet" convention as
    # the map panel/gsensor overlay.
    assert _video_size(destination) == (160, 120)
    assert warnings == []


def test_stitch_cameras_without_subtitles_path_leaves_footage_untouched(
    tmp_path
):
    front = tmp_path / "front.mp4"
    rear = tmp_path / "rear.mp4"
    _make_video(front, 160, 120)
    _make_video(rear, 160, 120)

    warnings = []
    destination = tmp_path / "stitch.mp4"
    stitch_cameras(
        front, rear, destination, layout="side_by_side", warnings=warnings,
    )

    assert _video_size(destination) == (320, 120)
    assert warnings == []


def _make_rear_flip_probe(path, size=320, duration_seconds=1.0):
    # Red on the left half, green on the right half - hflip should
    # swap which color ends up on which side, giving an unambiguous
    # real-render check that the mirror inset is actually flipped, not
    # just scaled and placed.
    subprocess.run(
        [
            "ffmpeg", "-y",
            "-f", "lavfi",
            "-i", f"color=c=black:size={size}x{size}:rate=5",
            "-vf",
            f"drawbox=x=0:y=0:w={size // 2}:h={size}:color=red:t=fill,"
            f"drawbox=x={size // 2}:y=0:w={size // 2}:h={size}:"
            "color=lime:t=fill",
            "-t", str(duration_seconds),
            str(path),
        ],
        capture_output=True,
        text=True,
        check=True,
    )


def test_stitch_cameras_rearview_mirror_flips_and_places_the_inset_top_center(
    tmp_path
):
    front = tmp_path / "front.mp4"
    rear = tmp_path / "rear.mp4"
    _make_solid_video(front, 640, 480, "blue")
    _make_rear_flip_probe(rear, size=320)

    warnings = []
    destination = tmp_path / "stitch.mp4"
    stitch_cameras(
        front, rear, destination, layout="rearview_mirror", warnings=warnings,
    )

    assert warnings == []
    # Front stays full-frame - rearview_mirror never crops/pads it
    # absent a --stitch-resolution request.
    assert _video_size(destination) == (640, 480)

    image = _extract_first_frame(destination, tmp_path / "frame.png")
    # Default mirror_size=25% of 640 -> inset 160 wide, 120 tall
    # (rear's own 1:1 aspect preserved). margin_y = round(480*0.02) = 10.
    inset_x0, inset_y0 = (640 - 160) // 2, 10

    # Pre-flip, red was on rear's LEFT half and green on the right -
    # after hflip, green should now be on the inset's own left side and
    # red on its right.
    left = image.getpixel((inset_x0 + 20, inset_y0 + 60))
    right = image.getpixel((inset_x0 + 140, inset_y0 + 60))
    assert left[1] > 150 and left[0] < 100  # green-ish
    assert right[0] > 150 and right[1] < 100  # red-ish

    # Far from the inset: plain blue front footage, untouched.
    far = image.getpixel((10, 470))
    assert far[2] > far[0] and far[2] > far[1]


def test_stitch_cameras_rearview_mirror_mirror_size_is_configurable(tmp_path):
    front = tmp_path / "front.mp4"
    rear = tmp_path / "rear.mp4"
    _make_solid_video(front, 640, 480, "blue")
    _make_solid_video(rear, 320, 320, "red")

    warnings = []
    destination = tmp_path / "stitch.mp4"
    stitch_cameras(
        front, rear, destination, layout="rearview_mirror",
        mirror_size=40.0, warnings=warnings,
    )

    assert warnings == []
    image = _extract_first_frame(destination, tmp_path / "frame.png")
    # 40% of 640 = 256 wide (even already).
    inset_x0 = (640 - 256) // 2
    inside = image.getpixel((inset_x0 + 128, 10 + 60))
    outside = image.getpixel((inset_x0 - 10, 10 + 60))
    assert inside[0] > 150 and inside[1] < 100  # inside the red inset
    assert not (outside[0] > 150 and outside[1] < 100)  # blue front


def test_stitch_cameras_rearview_mirror_scales_to_a_requested_resolution(
    tmp_path
):
    front = tmp_path / "front.mp4"
    rear = tmp_path / "rear.mp4"
    _make_video(front, 640, 480)
    _make_video(rear, 320, 240)

    warnings = []
    destination = tmp_path / "stitch.mp4"
    stitch_cameras(
        front, rear, destination, layout="rearview_mirror",
        resolution=(320, 240), warnings=warnings,
    )

    assert warnings == []
    assert _video_size(destination) == (320, 240)


def test_stitch_cameras_rearview_mirror_scale_shrinks_front_decode_time_scaling(
    tmp_path, monkeypatch
):
    # Same parity check as
    # test_stitch_cameras_scale_shrinks_decode_time_scaling_not_just_the_final_pass
    # (hstack/vstack), but for rearview_mirror - task #84's fix.
    # Christer's report ("front, rear, panel and stitch are still slow
    # even with --stitch-scale 10") was against a rearview_mirror
    # export specifically, and the first version of this feature
    # (task #83) explicitly excluded is_mirror from decode-time
    # scaling, so this exact scenario stayed slow even after that fix.
    monkeypatch.setattr(stitch_module, "_NVDEC_AVAILABLE", False)
    scale_filters = {}

    def fake_decode_camera(source, destination, *, scale_filter, debug=False):
        import shutil
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, destination)
        scale_filters[destination.name] = scale_filter

    def fake_encode(input_args, destination, extra_codec_args=None):
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(b"")

    monkeypatch.setattr(stitch_module, "_decode_camera", fake_decode_camera)
    monkeypatch.setattr(stitch_module, "encode_with_nvenc_fallback", fake_encode)

    front = tmp_path / "front.mp4"
    rear = tmp_path / "rear.mp4"
    _make_video(front, 3840, 2160, duration_seconds=0.1)
    _make_video(rear, 3840, 2160, duration_seconds=0.1)

    stitch_cameras(
        front, rear, tmp_path / "stitch.mp4",
        layout="rearview_mirror", scale=10,
    )

    # front: decode-time scale filter should target the small 10%
    # size, not the native 3840x2160.
    assert scale_filters["front.mp4"] is not None
    assert "2160" not in scale_filters["front.mp4"]
    # rear: always decoded straight to its own small inset size
    # (mirror_size% of front, default 25%) and flipped, regardless of
    # whether --stitch-scale was even given - see the unconditional
    # rear_scale_filter note in _stack().
    assert "hflip" in scale_filters["rear.mp4"]
    assert "2160" not in scale_filters["rear.mp4"]


def test_stitch_cameras_rearview_mirror_rear_is_always_decoded_pre_scaled(
    tmp_path, monkeypatch
):
    # Even with no --stitch-scale/--stitch-resolution at all (today's
    # existing full-native-quality default), rear should never be
    # decoded at full native size just to be shrunk down to
    # `mirror_size` percent afterward - that was wasted decode+encode
    # work on detail immediately discarded, and part of what Christer's
    # "rear ... still slow" report was pointing at.
    monkeypatch.setattr(stitch_module, "_NVDEC_AVAILABLE", False)
    scale_filters = {}

    def fake_decode_camera(source, destination, *, scale_filter, debug=False):
        import shutil
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, destination)
        scale_filters[destination.name] = scale_filter

    def fake_encode(input_args, destination, extra_codec_args=None):
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(b"")

    monkeypatch.setattr(stitch_module, "_decode_camera", fake_decode_camera)
    monkeypatch.setattr(stitch_module, "encode_with_nvenc_fallback", fake_encode)

    front = tmp_path / "front.mp4"
    rear = tmp_path / "rear.mp4"
    _make_video(front, 640, 480, duration_seconds=0.1)
    _make_video(rear, 640, 480, duration_seconds=0.1)

    stitch_cameras(
        front, rear, tmp_path / "stitch.mp4", layout="rearview_mirror",
    )

    assert scale_filters["front.mp4"] is None
    # Default mirror_size=25% of front's 640 -> 160.
    assert scale_filters["rear.mp4"] == "scale=160:-2,hflip"


def test_stitch_cameras_rearview_mirror_radius_zero_leaves_square_corners(
    tmp_path
):
    front = tmp_path / "front.mp4"
    rear = tmp_path / "rear.mp4"
    _make_solid_video(front, 640, 480, "blue")
    _make_solid_video(rear, 320, 320, "red")

    warnings = []
    destination = tmp_path / "stitch.mp4"
    stitch_cameras(
        front, rear, destination, layout="rearview_mirror",
        mirror_size=40.0, mirror_radius=0.0, warnings=warnings,
    )

    assert warnings == []
    image = _extract_first_frame(destination, tmp_path / "frame.png")
    # 40% of 640 = 256 wide/tall (rear is square). inset top-left is at
    # (192, 10) - a pixel right at that corner should still be solidly
    # red (mirror_radius=0, the unchanged default, leaves corners
    # square).
    corner = image.getpixel((192 + 2, 10 + 2))
    assert corner[0] > 150 and corner[1] < 100


def test_stitch_cameras_rearview_mirror_radius_rounds_the_corners(tmp_path):
    front = tmp_path / "front.mp4"
    rear = tmp_path / "rear.mp4"
    _make_solid_video(front, 640, 480, "blue")
    _make_solid_video(rear, 320, 320, "red")

    warnings = []
    destination = tmp_path / "stitch.mp4"
    stitch_cameras(
        front, rear, destination, layout="rearview_mirror",
        mirror_size=40.0, mirror_radius=100.0, warnings=warnings,
    )

    assert warnings == []
    image = _extract_first_frame(destination, tmp_path / "frame.png")
    # Same 256x256 square inset at (192, 10) as the zero-radius test
    # above. mirror_radius=100 rounds each corner to a quarter-circle
    # of radius min(256,256)/2=128 - for a square inset that's a full
    # inscribed circle, so a pixel right at the corner should now be
    # transparent (front's blue showing through instead of rear's red).
    corner = image.getpixel((192 + 2, 10 + 2))
    assert corner[2] > corner[0] and corner[2] > corner[1]
    # The inset's own center should still be fully opaque red - the
    # rounding only carves away the four corners, not the whole shape.
    center = image.getpixel((192 + 128, 10 + 128))
    assert center[0] > 150 and center[1] < 100


def _make_rear_zoom_probe(path, size=320, border=20, duration_seconds=1.0):
    # A yellow border around a solid blue center - zooming in (cropping
    # toward the center before the inset is scaled/flipped) should
    # eventually crop the yellow border away entirely, leaving the
    # inset's own edge solid blue instead.
    subprocess.run(
        [
            "ffmpeg", "-y",
            "-f", "lavfi",
            "-i", f"color=c=yellow:size={size}x{size}:rate=5",
            "-vf",
            f"drawbox=x={border}:y={border}:w={size - 2 * border}:"
            f"h={size - 2 * border}:color=blue:t=fill",
            "-t", str(duration_seconds),
            str(path),
        ],
        capture_output=True,
        text=True,
        check=True,
    )


def test_stitch_cameras_rearview_mirror_zoom_zero_is_a_no_op(tmp_path):
    front = tmp_path / "front.mp4"
    rear = tmp_path / "rear.mp4"
    _make_solid_video(front, 640, 480, "black")
    _make_rear_zoom_probe(rear, size=320, border=20)

    warnings = []
    destination = tmp_path / "stitch.mp4"
    stitch_cameras(
        front, rear, destination, layout="rearview_mirror",
        mirror_zoom=0.0, warnings=warnings,
    )

    assert warnings == []
    image = _extract_first_frame(destination, tmp_path / "frame.png")
    # Default mirror_size=25% of 640 -> 160x160 inset (rear's 1:1
    # aspect preserved) at (240, 10). A pixel right at its own edge
    # should still show the source's yellow border, unchanged from
    # today's existing (pre-mirror_zoom) behavior.
    edge = image.getpixel((240 + 2, 10 + 80))
    assert edge[0] > 150 and edge[1] > 150 and edge[2] < 100


def test_stitch_cameras_rearview_mirror_zoom_crops_toward_the_center(tmp_path):
    front = tmp_path / "front.mp4"
    rear = tmp_path / "rear.mp4"
    _make_solid_video(front, 640, 480, "black")
    _make_rear_zoom_probe(rear, size=320, border=20)

    warnings = []
    destination = tmp_path / "stitch.mp4"
    stitch_cameras(
        front, rear, destination, layout="rearview_mirror",
        mirror_zoom=30.0, warnings=warnings,
    )

    assert warnings == []
    image = _extract_first_frame(destination, tmp_path / "frame.png")
    # mirror_zoom=30 crops 30% off each edge before scaling (keep
    # fraction 0.7 of 320 = 224, i.e. 48px removed from each side) -
    # comfortably past the source's own 20px yellow border, so the
    # inset's own edge should now be solid blue instead.
    edge = image.getpixel((240 + 2, 10 + 80))
    assert edge[2] > edge[0] and edge[2] > edge[1]


def test_stitch_cameras_rearview_mirror_map_panel_is_capped_at_30_percent(
    tmp_path
):
    front = tmp_path / "front.mp4"
    rear = tmp_path / "rear.mp4"
    _make_solid_video(front, 640, 480, "blue")
    _make_solid_video(rear, 320, 240, "black")
    # A sharply north-south trip - with the general 50% clamp this
    # would ask for a much taller panel than 30% of 480 (144px); the
    # agreed spec caps rearview_mirror specifically at 30%, so the
    # actual added height should land exactly there instead.
    fixes = (_fix(0, 59.00, 18.000), _fix(2, 60.00, 18.001))

    warnings = []
    destination = tmp_path / "stitch.mp4"
    stitch_cameras(
        front, rear, destination, layout="rearview_mirror",
        map_mode="map", map_fixes=fixes, map_roads=(), warnings=warnings,
    )

    assert warnings == []
    width, height = _video_size(destination)
    assert width == 640
    # comp_height (480) * 0.3 = 144, already even.
    assert height == 480 + 144


def test_stitch_cameras_rearview_mirror_falls_back_to_plain_copy_for_single_camera(
    tmp_path
):
    front = tmp_path / "front.mp4"
    _make_video(front, 160, 120)

    warnings = []
    destination = tmp_path / "stitch.mp4"
    stitch_cameras(
        front, None, destination, layout="rearview_mirror", warnings=warnings,
    )

    # Same "not built for the single-camera path" convention as every
    # other rearview_mirror-specific/optional piece.
    assert _video_size(destination) == (160, 120)
    assert warnings == []
