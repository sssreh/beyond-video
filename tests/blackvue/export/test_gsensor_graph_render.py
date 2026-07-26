from datetime import timedelta

from blackvue.export.gsensor_graph_render import DEFAULT_HEIGHT
from blackvue.export.gsensor_graph_render import DEFAULT_WIDTH
from blackvue.export.gsensor_graph_render import PLAYHEAD_COLOR
from blackvue.export.gsensor_graph_render import X_COLOR
from blackvue.export.gsensor_graph_render import Y_COLOR
from blackvue.export.gsensor_graph_render import Z_COLOR
from blackvue.export.gsensor_graph_render import _format_tick
from blackvue.export.gsensor_graph_render import _nice_tick_interval
from blackvue.export.gsensor_graph_render import _plot_area
from blackvue.export.gsensor_graph_render import _time_to_x
from blackvue.export.gsensor_graph_render import _value_to_y
from blackvue.export.gsensor_graph_render import baseline_for_samples
from blackvue.export.gsensor_graph_render import render_base_frame
from blackvue.export.gsensor_graph_render import render_frame
from blackvue.export.gsensor_graph_render import scale_for_samples
from blackvue.export.gsensor_render import BACKGROUND_COLOR
from blackvue.telemetry.gsensor_reader import GSensorSample


def _sample(offset_ms, x, y, z):
    return GSensorSample(offset=timedelta(milliseconds=offset_ms), x=x, y=y, z=z)


def test_render_base_frame_returns_image_of_requested_size():
    samples = (_sample(0, 0, 0, 0), _sample(1000, 10, -10, 5))
    image = render_base_frame(
        samples, (0.0, 0.0, 0.0), 100.0, 1.0, width=320, height=180
    )

    assert image.size == (320, 180)
    assert image.mode == "RGB"


def test_render_base_frame_background_is_a_flat_chroma_key_green():
    # Same reasoning as gsensor_render.py's own equivalent test - this
    # strip chart is meant to be composited over front/rear footage
    # later too, so a chroma-key filter needs a single flat background
    # color to match exactly. Checked at a far corner, well outside
    # where any trace/tick could land.
    samples = (_sample(0, 0, 0, 0), _sample(1000, 10, -10, 5))
    image = render_base_frame(samples, (0.0, 0.0, 0.0), 100.0, 1.0)

    assert image.getpixel((0, 0)) == BACKGROUND_COLOR
    assert BACKGROUND_COLOR == (0, 255, 0)


def test_render_base_frame_handles_fewer_than_two_samples_without_crashing():
    # A trip with only one g-sensor sample has nothing to draw a line
    # between - render_base_frame() should still produce a valid
    # (blank-of-traces) image rather than crashing on draw.line() with
    # a single point.
    samples = (_sample(0, 0, 0, 0),)
    image = render_base_frame(samples, (0.0, 0.0, 0.0), 1.0, 1.0)

    assert image.size == (DEFAULT_WIDTH, DEFAULT_HEIGHT)


def test_render_base_frame_draws_each_axis_in_its_own_color():
    # Two samples produce one exact straight-line segment per axis -
    # the endpoint pixel at the strip's own left edge (elapsed=0) can
    # be computed directly via the same _time_to_x/_value_to_y helpers
    # the renderer itself uses, and should land exactly on that axis's
    # own trace color.
    baseline = (0.0, 0.0, 0.0)
    scale = 100.0
    total_seconds = 1.0
    samples = (_sample(0, 50, -30, 10), _sample(1000, 50, -30, 10))

    image = render_base_frame(samples, baseline, scale, total_seconds)
    left, top, right, bottom = _plot_area(
        DEFAULT_WIDTH, DEFAULT_HEIGHT, 32, 20
    )
    x_pixel = round(_time_to_x(0.0, total_seconds, left, right))

    x_y = round(_value_to_y(50, scale, top, bottom))
    y_y = round(_value_to_y(-30, scale, top, bottom))
    z_y = round(_value_to_y(10, scale, top, bottom))

    assert image.getpixel((x_pixel, x_y)) == X_COLOR
    assert image.getpixel((x_pixel, y_y)) == Y_COLOR
    assert image.getpixel((x_pixel, z_y)) == Z_COLOR


def test_render_frame_draws_a_playhead_that_moves_with_elapsed_seconds():
    samples = (_sample(0, 0, 0, 0), _sample(1000, 10, -10, 5))
    base = render_base_frame(samples, (0.0, 0.0, 0.0), 100.0, 1.0)

    start = render_frame(base, 0.0, 1.0)
    middle = render_frame(base, 0.5, 1.0)

    assert list(start.getdata()) != list(middle.getdata())
    assert start.size == base.size


def test_render_frame_playhead_uses_the_playhead_color():
    samples = (_sample(0, 0, 0, 0), _sample(1000, 10, -10, 5))
    base = render_base_frame(samples, (0.0, 0.0, 0.0), 100.0, 1.0)

    frame = render_frame(base, 0.5, 1.0)
    left, top, right, bottom = _plot_area(DEFAULT_WIDTH, DEFAULT_HEIGHT, 32, 20)
    playhead_x = round(_time_to_x(0.5, 1.0, left, right))

    assert frame.getpixel((playhead_x, round(top))) == PLAYHEAD_COLOR


def test_scale_for_samples_floors_at_minimum_for_flat_data():
    samples = (_sample(0, 0, 0, 0), _sample(100, 0, 0, 0))

    assert scale_for_samples(samples, minimum=1.0) == 1.0


def test_scale_for_samples_scales_to_the_observed_peak_across_all_three_axes():
    samples = (_sample(0, 100, -50, 10), _sample(100, -300, 200, -20))

    # Largest deviation across x/y/z is 300 (from x=-300).
    assert scale_for_samples(samples, padding=1.2, minimum=1.0) == 360.0


def test_scale_for_samples_measures_deviation_from_a_given_baseline():
    samples = (_sample(0, 100, -50, 0), _sample(100, -300, 200, 0))

    scale = scale_for_samples(
        samples, baseline=(500.0, 500.0, 0.0), padding=1.0, minimum=1.0
    )

    # Deviations: (-400, -550, 0) and (-800, -300, 0) -> largest is 800.
    assert scale == 800.0


def test_baseline_for_samples_is_the_median_of_each_axis():
    samples = (
        _sample(0, 10, 100, 1),
        _sample(100, 20, 300, 2),
        _sample(200, 30, 200, 3),
    )

    assert baseline_for_samples(samples) == (20.0, 200.0, 2.0)


def test_baseline_for_samples_averages_the_two_middle_values_for_even_counts():
    samples = (
        _sample(0, 0, 0, 0),
        _sample(100, 10, 10, 10),
        _sample(200, 20, 20, 20),
        _sample(300, 30, 30, 30),
    )

    assert baseline_for_samples(samples) == (15.0, 15.0, 15.0)


def test_baseline_for_samples_returns_origin_for_no_samples():
    assert baseline_for_samples(()) == (0.0, 0.0, 0.0)


def test_nice_tick_interval_picks_a_round_number_giving_roughly_the_target_count():
    # 120 seconds over a target of 6 ticks -> 20s/tick raw, rounds up
    # to the next candidate, 30.
    assert _nice_tick_interval(120.0, target_tick_count=6) == 30


def test_nice_tick_interval_handles_a_very_short_trip():
    assert _nice_tick_interval(3.0, target_tick_count=6) == 1


def test_format_tick_formats_as_mm_ss():
    assert _format_tick(0) == "00:00"
    assert _format_tick(52) == "00:52"
    assert _format_tick(125) == "02:05"
