import json
import subprocess
from datetime import datetime
from datetime import timedelta

from blackvue.export.gsensor_video import interpolate_sample
from blackvue.export.gsensor_video import render_gsensor_video
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


def test_interpolate_sample_returns_exact_sample_at_its_own_offset():
    samples = (_sample(0, 10, 20), _sample(10000, 30, 40))

    x, y, z = interpolate_sample(samples, samples[0].offset)

    assert (x, y, z) == (10.0, 20.0, 900.0)


def test_interpolate_sample_interpolates_midpoint():
    samples = (_sample(0, 0, 0, z=800), _sample(10000, 100, -200, z=1000))

    x, y, z = interpolate_sample(samples, timedelta(seconds=5))

    assert (x, y, z) == (50.0, -100.0, 900.0)


def test_interpolate_sample_clamps_before_first_sample():
    samples = (_sample(1000, 10, 20), _sample(2000, 30, 40))

    x, y, z = interpolate_sample(samples, timedelta(seconds=-5))

    assert (x, y, z) == (10.0, 20.0, 900.0)


def test_interpolate_sample_clamps_after_last_sample():
    samples = (_sample(0, 10, 20), _sample(1000, 30, 40))

    x, y, z = interpolate_sample(samples, timedelta(seconds=5))

    assert (x, y, z) == (30.0, 40.0, 900.0)


def test_render_gsensor_video_returns_none_for_fewer_than_two_samples(tmp_path):
    result = render_gsensor_video((_sample(0, 10, 20),), tmp_path / "gsensor.mp4")

    assert result is None
    assert not (tmp_path / "gsensor.mp4").exists()


def test_render_gsensor_video_returns_none_for_zero_duration(tmp_path):
    result = render_gsensor_video(
        (_sample(0, 10, 20), _sample(0, 30, 40)), tmp_path / "gsensor.mp4"
    )

    assert result is None


def test_render_gsensor_video_produces_a_real_video_end_to_end(tmp_path):
    samples = (
        _sample(0, 0, 0),
        _sample(1000, 200, -100),
        _sample(2000, -150, 300),
    )
    destination = tmp_path / "gsensor.mp4"

    result = render_gsensor_video(samples, destination, fps=2)

    assert result == destination
    assert destination.exists()
    # 2 seconds of g-sensor data at 2fps -> roughly 2 seconds of video.
    assert round(_video_duration_seconds(destination)) == 2


def test_render_gsensor_video_includes_a_wall_clock_caption_when_given(
    tmp_path,
):
    # Just confirms passing start_timestamp doesn't crash and still
    # produces a video - the caption text itself isn't pixel-checked
    # (see gsensor_render tests for that kind of assertion).
    samples = (_sample(0, 0, 0), _sample(1000, 100, 100))
    destination = tmp_path / "gsensor.mp4"

    result = render_gsensor_video(
        samples,
        destination,
        fps=2,
        start_timestamp=datetime(2026, 7, 20, 10, 0, 0),
    )

    assert result == destination
    assert destination.exists()
