from datetime import timedelta

from blackvue.export.gsensor_graph_render import BACKGROUND_COLOR
from blackvue.export.gsensor_graph_render import DEFAULT_HEIGHT
from blackvue.export.gsensor_graph_render import DEFAULT_MARGIN_PX
from blackvue.export.gsensor_graph_render import DEFAULT_VERTICAL_HEIGHT
from blackvue.export.gsensor_graph_render import DEFAULT_VERTICAL_WIDTH
from blackvue.export.gsensor_graph_render import DEFAULT_WIDTH
from blackvue.export.gsensor_graph_render import LEGEND_LABELS
from blackvue.export.gsensor_graph_render import LEGEND_PADDING
from blackvue.export.gsensor_graph_render import LEGEND_ROW_HEIGHT
from blackvue.export.gsensor_graph_render import PLAYHEAD_COLOR
from blackvue.export.gsensor_graph_render import TRACE_LINE_WIDTH
from blackvue.export.gsensor_graph_render import X_COLOR
from blackvue.export.gsensor_graph_render import Y_COLOR
from blackvue.export.gsensor_graph_render import Z_COLOR
from blackvue.export.gsensor_graph_render import _format_tick
from blackvue.export.gsensor_graph_render import _nice_tick_interval
from blackvue.export.gsensor_graph_render import _plot_area
from blackvue.export.gsensor_graph_render import _smoothed
from blackvue.export.gsensor_graph_render import _time_to_pos
from blackvue.export.gsensor_graph_render import _value_to_pos
from blackvue.export.gsensor_graph_render import baseline_for_samples
from blackvue.export.gsensor_graph_render import render_base_frame
from blackvue.export.gsensor_graph_render import render_frame
from blackvue.export.gsensor_graph_render import scale_for_samples
from blackvue.export.gsensor_render import BACKGROUND_COLOR as DOT_GAUGE_BACKGROUND_COLOR
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


def test_render_base_frame_background_is_a_flat_light_color():
    # This panel is never chroma-keyed/composited over footage (unlike
    # the dot-gauge overlay) - stitch.py just hstacks/vstacks it
    # straight onto the --stitch composite, so there's no reason for
    # its own BACKGROUND_COLOR to be chroma-key green (Christer's own
    # feedback: "awful green background"). Checked at a far corner,
    # well outside where any trace/tick/legend could land.
    samples = (_sample(0, 0, 0, 0), _sample(1000, 10, -10, 5))
    image = render_base_frame(samples, (0.0, 0.0, 0.0), 100.0, 1.0)

    assert image.getpixel((0, 0)) == BACKGROUND_COLOR


def test_graph_background_color_is_decoupled_from_the_dot_gauges_chroma_key_green():
    # The dot-gauge overlay (gsensor_render.py) still needs to stay
    # chroma-key green for its own real compositing use - this strip
    # chart's own BACKGROUND_COLOR must not be tied to it any more.
    assert BACKGROUND_COLOR != DOT_GAUGE_BACKGROUND_COLOR
    assert DOT_GAUGE_BACKGROUND_COLOR == (0, 255, 0)


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
    # be computed directly via the same _time_to_pos/_value_to_pos
    # helpers the renderer itself uses, and should land exactly on
    # that axis's own trace color.
    baseline = (0.0, 0.0, 0.0)
    scale = 100.0
    total_seconds = 1.0
    samples = (_sample(0, 50, -30, 10), _sample(1000, 50, -30, 10))

    image = render_base_frame(samples, baseline, scale, total_seconds)
    left, top, right, bottom = _plot_area(
        DEFAULT_WIDTH, DEFAULT_HEIGHT, 32, 20, "horizontal"
    )
    x_pixel = round(_time_to_pos(0.0, total_seconds, left, right))

    x_y = round(_value_to_pos(50, scale, top, bottom))
    y_y = round(_value_to_pos(-30, scale, top, bottom))
    z_y = round(_value_to_pos(10, scale, top, bottom))

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
    left, top, right, bottom = _plot_area(
        DEFAULT_WIDTH, DEFAULT_HEIGHT, 32, 20, "horizontal"
    )
    playhead_x = round(_time_to_pos(0.5, 1.0, left, right))

    assert frame.getpixel((playhead_x, round(top))) == PLAYHEAD_COLOR


def test_render_base_frame_vertical_uses_the_vertical_defaults_when_unsized():
    samples = (_sample(0, 0, 0, 0), _sample(1000, 10, -10, 5))
    image = render_base_frame(
        samples, (0.0, 0.0, 0.0), 100.0, 1.0, orientation="vertical"
    )

    assert image.size == (DEFAULT_VERTICAL_WIDTH, DEFAULT_VERTICAL_HEIGHT)


def _color_near(image, x, y, expected):
    # TRACE_LINE_WIDTH is now 1px (thinner - see module docstring), so
    # a fractional pixel position like 148.8 can rasterize to either
    # its floor or its round() depending on PIL's own sub-pixel
    # placement - a single exact-pixel check is too fragile at width 1.
    # Accept the expected color landing on any of the pixel's own
    # immediate neighbors instead of demanding one exact column.
    return any(
        image.getpixel((x + dx, y)) == expected for dx in (-1, 0, 1)
    )


def test_render_base_frame_vertical_runs_time_top_to_bottom():
    # Two samples produce one straight-line segment per axis - in
    # vertical mode, elapsed=0 should land at the plot area's own top
    # edge (time runs top to bottom - see the module docstring), not
    # its left edge the way horizontal mode does.
    baseline = (0.0, 0.0, 0.0)
    scale = 100.0
    total_seconds = 1.0
    samples = (_sample(0, 50, -30, 10), _sample(1000, 50, -30, 10))

    image = render_base_frame(
        samples, baseline, scale, total_seconds, orientation="vertical"
    )
    left, top, right, bottom = _plot_area(
        DEFAULT_VERTICAL_WIDTH, DEFAULT_VERTICAL_HEIGHT, 32, 44, "vertical"
    )
    y_pixel = round(_time_to_pos(0.0, total_seconds, top, bottom))

    x_x = round(_value_to_pos(50, scale, left, right))
    y_x = round(_value_to_pos(-30, scale, left, right))
    z_x = round(_value_to_pos(10, scale, left, right))

    assert _color_near(image, x_x, y_pixel, X_COLOR)
    assert _color_near(image, y_x, y_pixel, Y_COLOR)
    assert _color_near(image, z_x, y_pixel, Z_COLOR)


def test_render_frame_vertical_playhead_moves_down_not_across():
    samples = (_sample(0, 0, 0, 0), _sample(1000, 10, -10, 5))
    base = render_base_frame(
        samples, (0.0, 0.0, 0.0), 100.0, 1.0, orientation="vertical"
    )

    start = render_frame(base, 0.0, 1.0, orientation="vertical")
    middle = render_frame(base, 0.5, 1.0, orientation="vertical")

    assert list(start.getdata()) != list(middle.getdata())

    left, top, right, bottom = _plot_area(
        DEFAULT_VERTICAL_WIDTH, DEFAULT_VERTICAL_HEIGHT, 32, 44, "vertical"
    )
    playhead_y = round(_time_to_pos(0.5, 1.0, top, bottom))

    assert middle.getpixel((round(left), playhead_y)) == PLAYHEAD_COLOR


def test_plot_area_vertical_reserves_space_on_the_left_not_the_bottom():
    # Vertical mode keeps tick labels upright in a left-side margin
    # (see the module docstring) rather than rotating the whole chart
    # - the plot area's own left edge should sit further in than a
    # plain margin would put it, while its bottom edge is just the
    # plain margin (no reserve there in vertical mode).
    left, top, right, bottom = _plot_area(220, 960, 32, 44, "vertical")

    assert left == 32 + 44
    assert bottom == 960 - 32


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


def test_render_base_frame_draws_a_legend_swatch_for_each_axis_color():
    # No indication anywhere else of which trace is which color
    # (Christer: "nothing explaining the colors") - a small legend is
    # drawn in the plot area's own top-left corner. Exact pixel
    # positions derived the same way _plot_area()'s own margin is
    # used elsewhere in this file: default margin puts the plot area's
    # own top-left at (DEFAULT_MARGIN_PX, DEFAULT_MARGIN_PX), and the
    # legend itself starts LEGEND_PADDING further in, one
    # LEGEND_ROW_HEIGHT-tall row per axis.
    samples = (_sample(0, 0, 0, 0), _sample(1000, 10, -10, 5))
    image = render_base_frame(samples, (0.0, 0.0, 0.0), 100.0, 1.0)

    left = top = DEFAULT_MARGIN_PX
    swatch_x = left + LEGEND_PADDING + 5
    for row, expected_color in enumerate((X_COLOR, Y_COLOR, Z_COLOR)):
        row_mid = round(
            top + LEGEND_PADDING + row * LEGEND_ROW_HEIGHT + LEGEND_ROW_HEIGHT / 2
        )
        assert image.getpixel((swatch_x, row_mid)) == expected_color


def test_render_base_frame_skips_the_legend_for_fewer_than_two_samples():
    # Nothing to plot with only one sample - the legend would be
    # labeling traces that were never drawn, so it's skipped too (same
    # "if len(samples) >= 2" guard the traces themselves use).
    samples = (_sample(0, 0, 0, 0),)
    image = render_base_frame(samples, (0.0, 0.0, 0.0), 1.0, 1.0)

    left = top = DEFAULT_MARGIN_PX
    swatch_x = left + LEGEND_PADDING + 5
    row_mid = round(top + LEGEND_PADDING + LEGEND_ROW_HEIGHT / 2)

    assert image.getpixel((swatch_x, row_mid)) == BACKGROUND_COLOR


def test_smoothed_returns_the_same_length_list():
    values = [1.0, 2.0, 3.0, 4.0, 5.0, 6.0, 7.0]

    assert len(_smoothed(values)) == len(values)


def test_smoothed_leaves_flat_data_unchanged():
    values = [5.0] * 10

    assert _smoothed(values) == values


def test_smoothed_flattens_a_single_sample_spike():
    # A single jittery outlier surrounded by flat, identical
    # neighbors - the smoothed value at the spike should land well
    # short of the spike's own raw value, and every neighbor covered
    # by the same averaging window should also move at least a little
    # (proving the window is centered on each point, not just
    # replacing the spike in isolation).
    values = [0.0, 0.0, 0.0, 0.0, 100.0, 0.0, 0.0, 0.0, 0.0]

    smoothed = _smoothed(values, window=5)

    assert smoothed[4] < 100.0
    assert smoothed[4] > 0.0
    assert smoothed[2] > 0.0  # pulled up slightly by the spike two steps away


def test_smoothed_shrinks_the_window_at_the_edges_rather_than_padding():
    # The first/last values only have themselves and their real
    # neighbors to average over - no padding with zeros or repeated
    # edge values, which would drag the smoothed line toward an
    # artificial reading right where the trip starts/ends.
    values = [10.0, 0.0, 0.0, 0.0, 0.0]

    smoothed = _smoothed(values, window=5)

    # First point: window covers indices 0..2 -> (10+0+0)/3.
    assert smoothed[0] == (10.0 + 0.0 + 0.0) / 3


def test_smoothed_is_a_no_op_for_a_window_of_one_or_less():
    values = [1.0, 5.0, 2.0, 9.0]

    assert _smoothed(values, window=1) == values
    assert _smoothed(values, window=0) == values


def test_playhead_color_is_distinct_from_background_axis_and_all_three_traces():
    from blackvue.export.gsensor_graph_render import AXIS_COLOR

    others = (BACKGROUND_COLOR, AXIS_COLOR, X_COLOR, Y_COLOR, Z_COLOR)
    assert PLAYHEAD_COLOR not in others


def test_legend_labels_spell_out_what_each_axis_physically_means():
    # Christer's own explicit wording, not just "X"/"Y"/"Z" - see
    # _draw_legend()/the module docstring for why this is kept
    # identical in both orientations even though it runs past the
    # narrow vertical panel's own plot-area edge there.
    assert LEGEND_LABELS == (
        ("X", "Left/right"),
        ("Y", "Forward/back"),
        ("Z", "Up/down"),
    )


def test_trace_line_width_is_2px():
    # Tried at 1px and 2px side by side - Christer picked 2px back
    # (1px read as too faint once traces crossed each other).
    assert TRACE_LINE_WIDTH == 2
