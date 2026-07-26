import json
import subprocess
from datetime import timedelta

from blackvue.export.gsensor_graph_video import render_gsensor_graph_video
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
