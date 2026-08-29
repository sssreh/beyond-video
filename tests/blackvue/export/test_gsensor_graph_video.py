import json
import subprocess
from datetime import timedelta

import pytest

from blackvue.export.gsensor_graph_video import render_gsensor_graph_video
from blackvue.export.media import ExportCancelled
from blackvue.telemetry.gsensor_reader import GSensorSample


def _sample(offset_ms, x, y, z=900):
    return GSensorSample(offset=timedelta(milliseconds=offset_ms), x=x, y=y, z=z)


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


def test_render_gsensor_graph_video_returns_none_for_fewer_than_two_samples(tmp_path):
    result = render_gsensor_graph_video(
        (_sample(0, 10, 20),), tmp_path / "gsensor_graph.mp4"
    )

    assert result is None
    assert not (tmp_path / "gsensor_graph.mp4").exists()


def test_render_gsensor_graph_video_returns_none_for_zero_duration(tmp_path):
    result = render_gsensor_graph_video(
        (_sample(0, 10, 20), _sample(0, 30, 40)), tmp_path / "gsensor_graph.mp4"
    )

    assert result is None


def test_render_gsensor_graph_video_raises_export_cancelled_when_should_continue_is_false(
    tmp_path,
):
    samples = (
        _sample(0, 0, 0),
        _sample(1000, 200, -100),
        _sample(2000, -150, 300),
    )

    with pytest.raises(ExportCancelled):
        render_gsensor_graph_video(
            samples,
            tmp_path / "gsensor_graph.mp4",
            should_continue=lambda: False,
        )

    assert not (tmp_path / "gsensor_graph.mp4").exists()


def test_render_gsensor_graph_video_produces_a_real_video_end_to_end(tmp_path):
    samples = (
        _sample(0, 0, 0),
        _sample(1000, 200, -100),
        _sample(2000, -150, 300),
    )
    destination = tmp_path / "gsensor_graph.mp4"

    result = render_gsensor_graph_video(samples, destination, fps=2)

    assert result == destination
    assert destination.exists()
    # 2 seconds of g-sensor data at 2fps -> roughly 2 seconds of video.
    assert round(_video_duration_seconds(destination)) == 2


def test_render_gsensor_graph_video_duration_seconds_extends_past_a_trailing_gap(
    tmp_path,
):
    # Same reasoning as render_gsensor_video()'s own equivalent test -
    # a recording with no g-sensor data at the trip's own end would
    # otherwise make the render stop short of the real video length.
    samples = (_sample(0, 10, 20), _sample(1000, 30, 40))
    destination = tmp_path / "gsensor_graph.mp4"

    result = render_gsensor_graph_video(
        samples, destination, fps=2, duration_seconds=5.0
    )

    assert result == destination
    assert _video_duration_seconds(destination) >= 4.5


def test_render_gsensor_graph_video_falls_back_to_samples_derived_duration_by_default(
    tmp_path,
):
    samples = (_sample(0, 0, 0), _sample(2000, 100, -100))
    destination = tmp_path / "gsensor_graph.mp4"

    result = render_gsensor_graph_video(samples, destination, fps=2)

    assert result == destination
    assert round(_video_duration_seconds(destination)) == 2


def test_render_gsensor_graph_video_renders_the_base_chart_only_once(
    tmp_path, monkeypatch
):
    # The whole point of the base/playhead split (see
    # gsensor_graph_render.py's own module docstring) is that the
    # static chart itself is only ever drawn once per export, not once
    # per frame - confirmed here by counting calls to
    # render_base_frame() across a render with several output frames.
    from blackvue.export import gsensor_graph_video as module

    call_count = {"base": 0}
    real_render_base_frame = module.render_base_frame

    def _counting_render_base_frame(*args, **kwargs):
        call_count["base"] += 1
        return real_render_base_frame(*args, **kwargs)

    monkeypatch.setattr(module, "render_base_frame", _counting_render_base_frame)

    samples = (_sample(0, 0, 0), _sample(2000, 100, -100))
    render_gsensor_graph_video(samples, tmp_path / "gsensor_graph.mp4", fps=5)

    assert call_count["base"] == 1


def test_render_gsensor_graph_video_forwards_orientation_and_size_to_the_render(
    tmp_path
):
    # --stitch's own --stitch-graph panel (see stitch.py's
    # _render_graph_panel()) needs the video encoded at an exact pixel
    # size and orientation to hstack/vstack cleanly alongside the
    # camera composite - this confirms those three params make it all
    # the way through to the actual encoded output, not just to
    # gsensor_graph_render.py's own already-tested render functions.
    samples = (
        _sample(0, 0, 0),
        _sample(1000, 200, -100),
        _sample(2000, -150, 300),
    )
    destination = tmp_path / "gsensor_graph.mp4"

    result = render_gsensor_graph_video(
        samples, destination, fps=2,
        orientation="vertical", width=220, height=800,
    )

    assert result == destination

    probe = subprocess.run(
        [
            "ffprobe", "-v", "error",
            "-select_streams", "v:0",
            "-show_entries", "stream=width,height",
            "-of", "json",
            str(destination),
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    stream = json.loads(probe.stdout)["streams"][0]
    assert (stream["width"], stream["height"]) == (220, 800)


def test_render_gsensor_graph_video_defaults_to_hiding_z(tmp_path, monkeypatch):
    # Christer: "Z is just not useful, unless you hit a giant pothole,
    # but then the video probably got that and the reaction of the
    # driver" - Z (Up/down) is now the axis that reasoning describes,
    # under the letters' BlackVue-convention rotation (see
    # gsensor_reader.py's own module docstring for the full story).
    # Confirms show_z's own default (False) actually reaches
    # render_base_frame()/render_frame(), not just that
    # gsensor_graph_render.py's own default does the right thing in
    # isolation.
    from blackvue.export import gsensor_graph_video as module

    calls = {"base": [], "frame": []}
    real_render_base_frame = module.render_base_frame
    real_render_frame = module.render_frame

    def _capturing_render_base_frame(*args, **kwargs):
        calls["base"].append(kwargs.get("show_z"))
        return real_render_base_frame(*args, **kwargs)

    def _capturing_render_frame(*args, **kwargs):
        calls["frame"].append(kwargs.get("show_z"))
        return real_render_frame(*args, **kwargs)

    monkeypatch.setattr(module, "render_base_frame", _capturing_render_base_frame)
    monkeypatch.setattr(module, "render_frame", _capturing_render_frame)

    samples = (_sample(0, 0, 0), _sample(1000, 200, -100))
    render_gsensor_graph_video(samples, tmp_path / "gsensor_graph.mp4", fps=2)

    assert calls["base"] == [False]
    assert calls["frame"] and all(value is False for value in calls["frame"])


def test_render_gsensor_graph_video_forwards_show_z_true(tmp_path, monkeypatch):
    from blackvue.export import gsensor_graph_video as module

    calls = {"base": [], "frame": []}
    real_render_base_frame = module.render_base_frame
    real_render_frame = module.render_frame

    def _capturing_render_base_frame(*args, **kwargs):
        calls["base"].append(kwargs.get("show_z"))
        return real_render_base_frame(*args, **kwargs)

    def _capturing_render_frame(*args, **kwargs):
        calls["frame"].append(kwargs.get("show_z"))
        return real_render_frame(*args, **kwargs)

    monkeypatch.setattr(module, "render_base_frame", _capturing_render_base_frame)
    monkeypatch.setattr(module, "render_frame", _capturing_render_frame)

    samples = (_sample(0, 0, 0), _sample(1000, 200, -100))
    render_gsensor_graph_video(
        samples, tmp_path / "gsensor_graph.mp4", fps=2, show_z=True
    )

    assert calls["base"] == [True]
    assert calls["frame"] and all(value is True for value in calls["frame"])


def test_render_gsensor_graph_video_falls_back_to_one_chart_when_shorter_than_the_window(
    tmp_path, monkeypatch
):
    # window_seconds=None (the standalone --gsensor-graph-video output's
    # own default) always renders one whole-trip chart - this confirms
    # a *given* window_seconds still falls back to that same single-
    # chart path when the trip itself doesn't exceed the window, per
    # Christer's own "fall back to whole trip" pick for short trips.
    from blackvue.export import gsensor_graph_video as module

    call_count = {"base": 0}
    real_render_base_frame = module.render_base_frame

    def _counting_render_base_frame(*args, **kwargs):
        call_count["base"] += 1
        return real_render_base_frame(*args, **kwargs)

    monkeypatch.setattr(module, "render_base_frame", _counting_render_base_frame)

    samples = (_sample(0, 0, 0), _sample(5000, 100, -100))
    render_gsensor_graph_video(
        samples, tmp_path / "gsensor_graph.mp4", fps=1,
        duration_seconds=5.0, window_seconds=600.0,
    )

    assert call_count["base"] == 1


def test_render_gsensor_graph_video_paginates_when_longer_than_the_window(
    tmp_path, monkeypatch
):
    # A trip longer than window_seconds gets one base chart per fixed
    # chunk (0..window_seconds, window_seconds..2*window_seconds, ...,
    # the leftover remainder) - this confirms the chunk boundaries
    # themselves, computed from a 25s trip against a 10s window (three
    # chunks: 0-10, 10-20, 20-25 - the last one shorter, not padded
    # back out to a full 10s).
    from blackvue.export import gsensor_graph_video as module

    windows = []
    real_render_base_frame = module.render_base_frame

    def _capturing_render_base_frame(*args, **kwargs):
        windows.append((kwargs.get("window_start"), kwargs.get("window_end")))
        return real_render_base_frame(*args, **kwargs)

    monkeypatch.setattr(module, "render_base_frame", _capturing_render_base_frame)

    samples = tuple(_sample(i * 1000, i, -i) for i in range(26))
    render_gsensor_graph_video(
        samples, tmp_path / "gsensor_graph.mp4", fps=1,
        duration_seconds=25.0, window_seconds=10.0, width=64, height=32,
    )

    assert windows == [(0.0, 10.0), (10.0, 20.0), (20.0, 25.0)]


def test_render_gsensor_graph_video_playhead_gets_chunk_relative_elapsed_and_total(
    tmp_path, monkeypatch
):
    # Each output frame's playhead is composited via render_frame() -
    # this confirms the caller translates the frame's real absolute
    # elapsed_seconds into the *current chunk's own* elapsed/total
    # before calling it (render_frame() itself is untouched/unaware of
    # windowing - see its own docstring), so the playhead keeps
    # sweeping left-to-right across one page at a time rather than
    # drifting based on the whole trip's total_seconds.
    from blackvue.export import gsensor_graph_video as module

    calls = []
    real_render_frame = module.render_frame

    def _capturing_render_frame(base_image, elapsed_seconds, total_seconds, **kwargs):
        calls.append((elapsed_seconds, total_seconds))
        return real_render_frame(base_image, elapsed_seconds, total_seconds, **kwargs)

    monkeypatch.setattr(module, "render_frame", _capturing_render_frame)

    samples = tuple(_sample(i * 1000, i, -i) for i in range(26))
    render_gsensor_graph_video(
        samples, tmp_path / "gsensor_graph.mp4", fps=1,
        duration_seconds=25.0, window_seconds=10.0, width=64, height=32,
    )

    # frame_count = max(2, int(25 * 1) + 1) = 26 frames (index 0..25),
    # elapsed_seconds = min(frame_number / fps, total_seconds).
    assert calls[9] == (9.0, 10.0)  # last frame of chunk 0 (0-10)
    assert calls[10] == (0.0, 10.0)  # first frame of chunk 1 (10-20)
    assert calls[20] == (0.0, 5.0)  # first frame of chunk 2 (20-25, 5s long)
    assert calls[25] == (5.0, 5.0)  # last frame overall, end of chunk 2


def test_render_gsensor_graph_video_defaults_to_horizontal_orientation(tmp_path):
    samples = (_sample(0, 0, 0), _sample(1000, 200, -100))
    destination = tmp_path / "gsensor_graph.mp4"

    from blackvue.export.gsensor_graph_render import DEFAULT_HEIGHT
    from blackvue.export.gsensor_graph_render import DEFAULT_WIDTH

    render_gsensor_graph_video(samples, destination, fps=2)

    probe = subprocess.run(
        [
            "ffprobe", "-v", "error",
            "-select_streams", "v:0",
            "-show_entries", "stream=width,height",
            "-of", "json",
            str(destination),
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    stream = json.loads(probe.stdout)["streams"][0]
    assert (stream["width"], stream["height"]) == (DEFAULT_WIDTH, DEFAULT_HEIGHT)
