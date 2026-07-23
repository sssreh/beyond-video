import json
import subprocess
import time
from datetime import timedelta

from blackvue.export import gsensor_video as gsensor_video_module
from blackvue.export.gsensor_video import _advance_search_index
from blackvue.export.gsensor_video import _interpolate_from_index
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


def test_advance_and_interpolate_from_index_matches_exact_offset():
    samples = (_sample(0, 10, 20), _sample(10000, 30, 40))

    index = _advance_search_index(samples, samples[0].offset, 0)
    x, y, z = _interpolate_from_index(samples, samples[0].offset, index)

    assert (x, y, z) == (10.0, 20.0, 900.0)


def test_advance_and_interpolate_from_index_matches_midpoint():
    samples = (_sample(0, 0, 0, z=800), _sample(10000, 100, -200, z=1000))

    index = _advance_search_index(samples, timedelta(seconds=5), 0)
    x, y, z = _interpolate_from_index(samples, timedelta(seconds=5), index)

    assert (x, y, z) == (50.0, -100.0, 900.0)


def test_advance_and_interpolate_from_index_clamps_before_first_sample():
    samples = (_sample(1000, 10, 20), _sample(2000, 30, 40))

    index = _advance_search_index(samples, timedelta(seconds=-5), 0)
    x, y, z = _interpolate_from_index(samples, timedelta(seconds=-5), index)

    assert (x, y, z) == (10.0, 20.0, 900.0)


def test_advance_and_interpolate_from_index_clamps_after_last_sample():
    samples = (_sample(0, 10, 20), _sample(1000, 30, 40))

    index = _advance_search_index(samples, timedelta(seconds=5), 0)
    x, y, z = _interpolate_from_index(samples, timedelta(seconds=5), index)

    assert (x, y, z) == (30.0, 40.0, 900.0)


def test_advance_and_interpolate_from_index_matches_interpolate_sample_over_a_monotonic_sweep():
    # The exact usage shape render_gsensor_video()'s own frame loop
    # relies on: elapsed only ever increases, and the index returned
    # from one call is fed straight back in as the next call's
    # starting point. Every result along the way should match
    # interpolate_sample()'s own (slower, full-rescan) answer exactly.
    samples = tuple(
        _sample(ms, ms % 300, -(ms % 200)) for ms in range(0, 20000, 137)
    )

    index = 0
    for ms in range(-500, 21000, 53):
        elapsed = timedelta(milliseconds=ms)
        index = _advance_search_index(samples, elapsed, index)
        fast = _interpolate_from_index(samples, elapsed, index)
        slow = interpolate_sample(samples, elapsed)
        assert fast == slow


def test_render_gsensor_video_interpolation_stays_fast_for_a_large_sample_count():
    # Regression guard for the O(samples x frames) bug interpolate_
    # sample()'s full rescan-per-frame produced (see gsensor_video.py's
    # _advance_search_index()/_interpolate_from_index() docstrings) - a
    # real multi-hour trip at g-sensor's ~10Hz native rate produced
    # tens of thousands of samples and frames, and looked from the
    # outside like bv-export had hung. Simulates just the interpolation
    # cost of render_gsensor_video()'s frame loop directly (not the
    # PIL/ffmpeg parts, which have their own real, expected cost at
    # this frame count) for a synthetic 4-hour trip at the native
    # ~10Hz rate - the old O(n^2) path would be on the order of 2*10^8
    # inner-loop iterations here; the fixed O(n) path should finish in
    # well under a second.
    samples = tuple(
        _sample(ms, (ms % 400) - 200, (ms % 300) - 150)
        for ms in range(0, 4 * 60 * 60 * 1000, 100)  # 4 hours at 10Hz
    )
    total_seconds = samples[-1].offset.total_seconds()
    fps = 10
    frame_count = int(total_seconds * fps) + 1

    start = time.monotonic()
    index = 0
    for frame_number in range(frame_count):
        elapsed = timedelta(seconds=min(frame_number / fps, total_seconds))
        index = _advance_search_index(samples, elapsed, index)
        _interpolate_from_index(samples, elapsed, index)
    elapsed_wall = time.monotonic() - start

    assert elapsed_wall < 5.0


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


def test_render_gsensor_video_centers_positions_on_the_trips_median_reading(
    tmp_path, monkeypatch
):
    positions = []

    class _FakeImage:
        def save(self, _path):
            pass

    def _fake_render_frame(_scale, _trail, position, **_kwargs):
        positions.append(position)
        return _FakeImage()

    monkeypatch.setattr(gsensor_video_module, "render_frame", _fake_render_frame)
    monkeypatch.setattr(
        gsensor_video_module, "encode_frame_sequence", lambda *_a, **_k: None
    )

    # A constant offset baked into every sample - a dashcam mounted at
    # an angle, say. Median x/y across these three samples is
    # (500, -300).
    samples = (
        _sample(0, 500, -300),
        _sample(500, 700, -100),
        _sample(1000, 300, -500),
    )

    render_gsensor_video(samples, tmp_path / "gsensor.mp4", fps=2)

    # The first sample is an exact match for the median baseline, so
    # it should render at the gauge's center (0, 0) - not at its raw
    # (500, -300) reading.
    assert positions[0] == (0.0, 0.0)
