from blackvue.live.gsensor_stream import BACKGROUND_COLOR
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

    image = render_live_gsensor_frame(
        samples, 60.0, baseline=(0.0, 0.0, 0.0), max_deviation=2.0,
        width=300, height=150,
    )

    assert image.size == (300, 150)


def test_render_live_gsensor_frame_handles_a_single_sample_without_crashing():
    # len(samples) >= 2 gates the trace-drawing branch - a single
    # sample (right after bv-live starts) should still render cleanly,
    # just with no traces yet, rather than raising.
    samples = (GSensorSample(0.0, 1.0, 1.0, 1.0),)

    image = render_live_gsensor_frame(samples, 60.0, baseline=(0.0, 0.0, 0.0))

    assert image is not None


def test_render_live_gsensor_frame_draws_no_trace_before_calibration_finishes():
    # baseline=None (the default) is how the caller signals calibration
    # hasn't finished yet - regression test for Christer parked,
    # watching the strip sit offset from its own zero line: no trace
    # should render at all until there's a real baseline to draw
    # against, even with plenty of real samples on hand.
    samples = (
        GSensorSample(0.0, 1.0, -1.0, 0.5),
        GSensorSample(1.0, 2.0, -2.0, 0.2),
        GSensorSample(2.0, 0.5, -0.5, 0.1),
    )

    with_baseline = render_live_gsensor_frame(
        samples, 60.0, baseline=(0.0, 0.0, 0.0), max_deviation=2.0,
    )
    without_baseline = render_live_gsensor_frame(samples, 60.0)

    # Both render *something* (the background + zero axis line), but
    # only the calibrated one actually draws a trace/legend on top -
    # the two images should differ.
    assert list(with_baseline.getdata()) != list(without_baseline.getdata())


def test_render_live_gsensor_frame_subtracts_the_baseline_before_plotting():
    # A "parked" sample sitting exactly on the baseline should draw
    # flat at the zero line - regression test for Christer's own
    # report: "The car is parked right now, but the lines shows an
    # offset from zero." Two identical samples equal to the baseline
    # should trace a perfectly flat line at the strip's own zero_y,
    # not off to one side of it.
    baseline = (5.0, -3.0, 1.0)
    samples = (
        GSensorSample(0.0, 5.0, -3.0, 1.0),
        GSensorSample(1.0, 5.0, -3.0, 1.0),
    )

    image = render_live_gsensor_frame(
        samples, 60.0, baseline=baseline, max_deviation=1.0,
        width=100, height=60, margin=10,
    )

    zero_y = (10 + (60 - 10)) / 2  # margin=10, height=60
    # A flat trace at baseline draws every trace color on top of the
    # zero_y row itself (the last-drawn trace, Upper/Lower's own
    # color, ends up the visible one wherever they all overlap) - and
    # nowhere *else* in that same column, since there's no other row
    # any line touches with a perfectly flat, on-baseline trace.
    # Checked at x=margin specifically (the plot's own left edge, one
    # pixel left of where _draw_legend() starts drawing its swatches/
    # text) so the legend doesn't contaminate this check.
    non_background_rows = {
        y for y in range(image.height)
        if image.getpixel((10, y)) != BACKGROUND_COLOR
    }
    assert non_background_rows == {int(zero_y)}


def test_render_live_gsensor_frame_uses_max_deviation_not_the_windows_own_peak():
    # Core of Christer's ask: "when newer data comes in and are
    # greater than the previous max value, we scale down the lines to
    # match the new max value" - the scale must come from the
    # session's own peak-so-far (max_deviation), not be recomputed
    # fresh from whatever's currently in the display window. Same
    # samples, same baseline, only max_deviation differs - the tight
    # scale (matching the samples' own small peak) should draw a
    # visibly larger-amplitude trace than the wide scale (a much
    # bigger historical peak the current samples don't come close to).
    baseline = (0.0, 0.0, 0.0)
    samples = (
        GSensorSample(0.0, 1.0, 0.0, 0.0),
        GSensorSample(1.0, -1.0, 0.0, 0.0),
    )

    tight = render_live_gsensor_frame(
        samples, 60.0, baseline=baseline, max_deviation=1.0,
        width=200, height=100,
    )
    wide = render_live_gsensor_frame(
        samples, 60.0, baseline=baseline, max_deviation=100.0,
        width=200, height=100,
    )

    assert list(tight.getdata()) != list(wide.getdata())


def test_live_gsensor_frames_returns_a_callable_that_renders_from_state():
    state = TelemetryState()
    state.add_gsensor(1.0, 2.0, 3.0)
    state.add_gsensor(4.0, 5.0, 6.0)

    render = live_gsensor_frames(state, window_seconds=DEFAULT_WINDOW_SECONDS)
    image = render()

    assert image is not None
    assert image.size[0] > 0 and image.size[1] > 0


def test_live_gsensor_frames_draws_a_trace_once_calibration_finishes(monkeypatch):
    import blackvue.live.telemetry as telemetry_module

    fake_time = [0.0]
    monkeypatch.setattr(telemetry_module.time, "monotonic", lambda: fake_time[0])

    state = TelemetryState()
    state.add_gsensor(1.0, 1.0, 1.0)

    fake_time[0] = 10.0  # past GSENSOR_CALIBRATION_SECONDS (3.0)
    state.add_gsensor(1.0, 1.0, 1.0)
    state.add_gsensor(3.0, 1.0, 1.0)  # a real deviation, post-calibration

    render = live_gsensor_frames(state, window_seconds=DEFAULT_WINDOW_SECONDS)
    image = render()

    assert state.gsensor_baseline() is not None
    assert image is not None
