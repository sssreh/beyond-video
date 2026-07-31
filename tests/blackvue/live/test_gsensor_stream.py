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
        samples, 60.0, max_deviation=2.0, width=300, height=150,
    )

    assert image.size == (300, 150)


def test_render_live_gsensor_frame_handles_a_single_sample_without_crashing():
    # len(samples) >= 2 gates the trace-drawing branch - a single
    # sample (right after bv-live starts) should still render cleanly,
    # just with no traces yet, rather than raising.
    samples = (GSensorSample(0.0, 1.0, 1.0, 1.0),)

    image = render_live_gsensor_frame(samples, 60.0)

    assert image is not None


def test_render_live_gsensor_frame_draws_a_trace_with_no_startup_delay():
    # No calibration step anymore (removed at Christer's own request -
    # see WORKING_CONTEXT.md) - a trace should draw the moment there
    # are at least two samples, not wait for any warm-up period. Two
    # samples is enough on its own; compared against the fewer-than-
    # two-samples case (background + zero axis line only) to confirm a
    # trace/legend is actually drawn on top.
    samples = (
        GSensorSample(0.0, 1.0, -1.0, 0.5),
        GSensorSample(1.0, 2.0, -2.0, 0.2),
    )
    single = (samples[0],)

    with_trace = render_live_gsensor_frame(samples, 60.0, max_deviation=2.0)
    without_trace = render_live_gsensor_frame(single, 60.0)

    assert list(with_trace.getdata()) != list(without_trace.getdata())


def test_render_live_gsensor_frame_plots_raw_readings_against_raw_zero():
    # No baseline subtraction anymore - a sample sitting exactly on raw
    # zero should draw flat at the strip's own zero line. Regression
    # guard for the calibration-removal follow-up (see
    # WORKING_CONTEXT.md): plotting used to subtract a calibrated
    # baseline first; now it plots sample.front_rear/left_right/
    # upper_lower directly.
    samples = (
        GSensorSample(0.0, 0.0, 0.0, 0.0),
        GSensorSample(1.0, 0.0, 0.0, 0.0),
    )

    image = render_live_gsensor_frame(
        samples, 60.0, max_deviation=1.0, width=100, height=60, margin=10,
    )

    zero_y = (10 + (60 - 10)) / 2  # margin=10, height=60
    # A flat trace at raw zero draws every trace color on top of the
    # zero_y row itself (the last-drawn trace, Upper/Lower's own
    # color, ends up the visible one wherever they all overlap) - and
    # nowhere *else* in that same column, since there's no other row
    # any line touches with a perfectly flat trace. Checked at
    # x=margin specifically (the plot's own left edge, one pixel left
    # of where _draw_legend() starts drawing its swatches/text) so the
    # legend doesn't contaminate this check.
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
    # samples, only max_deviation differs - the tight scale (matching
    # the samples' own small peak) should draw a visibly larger
    # -amplitude trace than the wide scale (a much bigger historical
    # peak the current samples don't come close to).
    samples = (
        GSensorSample(0.0, 1.0, 0.0, 0.0),
        GSensorSample(1.0, -1.0, 0.0, 0.0),
    )

    tight = render_live_gsensor_frame(
        samples, 60.0, max_deviation=1.0, width=200, height=100,
    )
    wide = render_live_gsensor_frame(
        samples, 60.0, max_deviation=100.0, width=200, height=100,
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


def test_live_gsensor_frames_draws_a_trace_immediately_with_no_calibration_delay():
    # No calibration step anymore (removed at Christer's own request -
    # see WORKING_CONTEXT.md) - a real trace should be ready as soon as
    # there are two samples, with no warm-up wait.
    state = TelemetryState()
    state.add_gsensor(1.0, 1.0, 1.0)
    state.add_gsensor(3.0, 1.0, 1.0)  # a real deviation

    render = live_gsensor_frames(state, window_seconds=DEFAULT_WINDOW_SECONDS)
    image = render()

    assert state.gsensor_max_deviation() == 3.0
    assert image is not None
