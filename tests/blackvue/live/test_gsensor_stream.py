from blackvue.live.gsensor_stream import DEFAULT_WINDOW_SECONDS
from blackvue.live.gsensor_stream import live_gsensor_frames
from blackvue.live.gsensor_stream import render_live_gsensor_frame
from blackvue.live.telemetry import GSensorSample
from blackvue.live.telemetry import TelemetryState


def test_render_live_gsensor_frame_returns_the_requested_size_with_no_samples():
    image = render_live_gsensor_frame((), 60.0, width=200, height=100)

    assert image.size == (200, 100)


def test_render_live_gsensor_frame_returns_the_requested_size_with_real_samples():
    samples = (
        GSensorSample(0.0, 1.0, -1.0, 0.5),
        GSensorSample(1.0, 2.0, -2.0, 0.2),
        GSensorSample(2.0, 0.5, -0.5, 0.1),
    )

    image = render_live_gsensor_frame(samples, 60.0, width=300, height=150)

    assert image.size == (300, 150)


def test_render_live_gsensor_frame_handles_a_single_sample_without_crashing():
    # len(samples) >= 2 gates the trace-drawing branch - a single
    # sample (right after bv-live starts) should still render cleanly,
    # just with no traces yet, rather than raising.
    samples = (GSensorSample(0.0, 1.0, 1.0, 1.0),)

    image = render_live_gsensor_frame(samples, 60.0)

    assert image is not None


def test_live_gsensor_frames_returns_a_callable_that_renders_from_state():
    state = TelemetryState()
    state.add_gsensor(1.0, 2.0, 3.0)
    state.add_gsensor(4.0, 5.0, 6.0)

    render = live_gsensor_frames(state, window_seconds=DEFAULT_WINDOW_SECONDS)
    image = render()

    assert image is not None
    assert image.size[0] > 0 and image.size[1] > 0
