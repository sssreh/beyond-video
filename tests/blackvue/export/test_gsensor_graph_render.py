from datetime import timedelta
from pathlib import Path

from PIL import Image
from PIL import ImageDraw
from PIL import ImageFont

from blackvue.export import gsensor_graph_render as gsensor_graph_render_module
from blackvue.export.gsensor_graph_render import AXIS_COLOR
from blackvue.export.gsensor_graph_render import BACKGROUND_COLOR
from blackvue.export.gsensor_graph_render import DEFAULT_HEIGHT
from blackvue.export.gsensor_graph_render import DEFAULT_MARGIN_PX
from blackvue.export.gsensor_graph_render import DEFAULT_VERTICAL_HEIGHT
from blackvue.export.gsensor_graph_render import DEFAULT_VERTICAL_WIDTH
from blackvue.export.gsensor_graph_render import DEFAULT_WIDTH
from blackvue.export.gsensor_graph_render import LANE_DIVIDER_COLOR
from blackvue.export.gsensor_graph_render import LANE_GAP_PX
from blackvue.export.gsensor_graph_render import LEGEND_FONT_SIZE
from blackvue.export.gsensor_graph_render import LEGEND_LABELS
from blackvue.export.gsensor_graph_render import LEGEND_PADDING
from blackvue.export.gsensor_graph_render import LEGEND_ROW_HEIGHT
from blackvue.export.gsensor_graph_render import LEGEND_SWATCH_LENGTH
from blackvue.export.gsensor_graph_render import PLAYHEAD_COLOR
from blackvue.export.gsensor_graph_render import TRACE_LINE_WIDTH
from blackvue.export.gsensor_graph_render import X_COLOR
from blackvue.export.gsensor_graph_render import Y_COLOR
from blackvue.export.gsensor_graph_render import Z_COLOR
from blackvue.export.gsensor_graph_render import Z_LANE_FRACTION
from blackvue.export.gsensor_graph_render import _format_tick
from blackvue.export.gsensor_graph_render import _legend_reserve_px
from blackvue.export.gsensor_graph_render import _load_font
from blackvue.export.gsensor_graph_render import _nice_tick_interval
from blackvue.export.gsensor_graph_render import _plot_area
from blackvue.export.gsensor_graph_render import _split_lanes
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
    # that axis's own trace color. X/Y share main_lane; Z is plotted
    # into its own separate z_lane (see _split_lanes()) - Christer's
    # own request ("let the cluttered Z have its own line") - so its
    # expected position is computed against z_lane, not the full plot
    # area X/Y still share. show_z=True since this test is specifically
    # about Z's own trace/lane - Z is hidden by default (see
    # test_render_base_frame_hides_z_by_default()).
    baseline = (0.0, 0.0, 0.0)
    scale = 100.0
    total_seconds = 1.0
    samples = (_sample(0, 50, -30, 10), _sample(1000, 50, -30, 10))

    image = render_base_frame(samples, baseline, scale, total_seconds, show_z=True)
    left, top, right, bottom = _plot_area(
        DEFAULT_WIDTH, DEFAULT_HEIGHT, 32, 20, "horizontal",
        legend_reserve=_legend_reserve_px("horizontal", show_z=True),
    )
    x_pixel = round(_time_to_pos(0.0, total_seconds, left, right))

    (main_top, main_bottom), (z_top, z_bottom) = _split_lanes(top, bottom)
    x_y = round(_value_to_pos(50, scale, main_top, main_bottom))
    y_y = round(_value_to_pos(-30, scale, main_top, main_bottom))
    z_y = round(_value_to_pos(10, scale, z_top, z_bottom))

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
        DEFAULT_WIDTH, DEFAULT_HEIGHT, 32, 20, "horizontal",
        legend_reserve=_legend_reserve_px("horizontal"),
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
    # its left edge the way horizontal mode does. X/Y share main_lane;
    # Z is plotted into its own z_lane (see _split_lanes()), so its
    # expected position is computed against z_lane, not the full plot
    # area X/Y still share. show_z=True since this test is specifically
    # about Z's own trace/lane - Z is hidden by default (see
    # test_render_base_frame_hides_z_by_default()).
    baseline = (0.0, 0.0, 0.0)
    scale = 100.0
    total_seconds = 1.0
    samples = (_sample(0, 50, -30, 10), _sample(1000, 50, -30, 10))

    image = render_base_frame(
        samples, baseline, scale, total_seconds, orientation="vertical",
        show_z=True,
    )
    left, top, right, bottom = _plot_area(
        DEFAULT_VERTICAL_WIDTH, DEFAULT_VERTICAL_HEIGHT, 32, 44, "vertical",
        legend_reserve=_legend_reserve_px("vertical", show_z=True),
    )
    y_pixel = round(_time_to_pos(0.0, total_seconds, top, bottom))

    (main_left, main_right), (z_left, z_right) = _split_lanes(left, right)
    x_x = round(_value_to_pos(50, scale, main_left, main_right))
    y_x = round(_value_to_pos(-30, scale, main_left, main_right))
    z_x = round(_value_to_pos(10, scale, z_left, z_right))

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
        DEFAULT_VERTICAL_WIDTH, DEFAULT_VERTICAL_HEIGHT, 32, 44, "vertical",
        legend_reserve=_legend_reserve_px("vertical"),
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


def test_plot_area_horizontal_legend_reserve_shifts_the_left_edge():
    # The legend now gets its own dedicated column to the left of the
    # chart (Christer: "legend should not be on top of lines ... a
    # little space to the left on horizontal") - legend_reserve adds
    # onto the plain margin on the left edge only; every other edge is
    # unaffected.
    left, top, right, bottom = _plot_area(960, 220, 32, 20, "horizontal")
    left_with_legend, top2, right2, bottom2 = _plot_area(
        960, 220, 32, 20, "horizontal", legend_reserve=100.0
    )

    assert left_with_legend == left + 100.0
    assert (top2, right2, bottom2) == (top, right, bottom)


def test_plot_area_vertical_legend_reserve_shifts_the_top_edge():
    # Same idea, rotated: vertical mode's own legend gets a dedicated
    # row above the chart (Christer: "...at the top for vertical") -
    # legend_reserve adds onto the plain margin on the top edge only.
    left, top, right, bottom = _plot_area(220, 960, 32, 44, "vertical")
    left2, top_with_legend, right2, bottom2 = _plot_area(
        220, 960, 32, 44, "vertical", legend_reserve=60.0
    )

    assert top_with_legend == top + 60.0
    assert (left2, right2, bottom2) == (left, right, bottom)


def test_split_lanes_covers_the_full_original_range():
    # main_lane's own start and z_lane's own end should be the exact
    # ends of the range that was split - no space at either extreme
    # goes unclaimed by either lane.
    (main_start, main_end), (z_start, z_end) = _split_lanes(100.0, 400.0)

    assert main_start == 100.0
    assert z_end == 400.0


def test_split_lanes_leaves_a_gap_between_the_two_lanes():
    # X/Y's main_lane and Z's own z_lane shouldn't touch - a small gap
    # (plus a divider line drawn in it, see render_base_frame()) is
    # what visually separates them into two distinct regions rather
    # than one plot with a kink in it.
    (main_start, main_end), (z_start, z_end) = _split_lanes(100.0, 400.0)

    assert z_start - main_end == LANE_GAP_PX


def test_split_lanes_gives_z_the_smaller_share():
    # Z is one trace against X/Y's two, so it gets proportionally less
    # of the panel (Christer's own pick: ~1/3 for Z, over an equal
    # 50/50 split) - Z_LANE_FRACTION applies to the space actually
    # available for the two lanes, i.e. after LANE_GAP_PX is set aside.
    value_start, value_end = 100.0, 400.0
    (main_start, main_end), (z_start, z_end) = _split_lanes(value_start, value_end)

    available = (value_end - value_start) - LANE_GAP_PX
    expected_z_span = available * Z_LANE_FRACTION
    expected_main_span = available - expected_z_span

    # Computed through several chained float operations inside
    # _split_lanes() itself (subtraction, addition, subtraction again),
    # so the result can be a couple of ulps off an independently
    # computed expectation even though both are mathematically the
    # same value - a tiny tolerance avoids a false failure over that,
    # not a real one.
    assert abs((z_end - z_start) - expected_z_span) < 1e-9
    assert abs((main_end - main_start) - expected_main_span) < 1e-9
    assert (z_end - z_start) < (main_end - main_start)


def test_render_base_frame_draws_a_divider_between_the_two_lanes():
    # A real rendering-level check (not just the _split_lanes() geometry
    # unit tests above) that the divider actually gets drawn where the
    # two lanes meet - the visual cue that they're deliberately two
    # separate regions, not a plot with a kink in it. Only drawn when
    # show_z (no lane split at all otherwise - see
    # test_render_base_frame_hides_z_by_default()).
    samples = (_sample(0, 0, 0, 0), _sample(1000, 10, -10, 5))
    image = render_base_frame(samples, (0.0, 0.0, 0.0), 100.0, 1.0, show_z=True)

    left, top, right, bottom = _plot_area(
        DEFAULT_WIDTH, DEFAULT_HEIGHT, 32, 20, "horizontal",
        legend_reserve=_legend_reserve_px("horizontal", show_z=True),
    )
    (main_top, main_bottom), (z_top, z_bottom) = _split_lanes(top, bottom)
    divider_y = round((main_bottom + z_top) / 2)

    assert image.getpixel((round(left) + 5, divider_y)) == LANE_DIVIDER_COLOR


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
    # LEGEND_ROW_HEIGHT-tall row per axis. show_z=True to check all
    # three rows including Z's own - Z is hidden (and so is its legend
    # row) by default, see test_render_base_frame_hides_z_by_default().
    samples = (_sample(0, 0, 0, 0), _sample(1000, 10, -10, 5))
    image = render_base_frame(samples, (0.0, 0.0, 0.0), 100.0, 1.0, show_z=True)

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


def test_render_base_frame_vertical_centers_the_legend_block():
    # The narrow vertical side-panel doesn't have room to left-anchor
    # the legend the way the wide horizontal panel does - "Y — Forward/
    # back" alone runs past the plot area's own right edge from there.
    # Christer's own request: center it instead. Expected x computed
    # the same way _draw_legend() itself does, independently here
    # rather than importing the private helper, so this test would
    # actually catch a regression in the centering math.
    samples = (_sample(0, 0, 0, 0), _sample(1000, 10, -10, 5))
    image = render_base_frame(
        samples, (0.0, 0.0, 0.0), 100.0, 1.0, orientation="vertical"
    )

    font = _load_font(LEGEND_FONT_SIZE)
    measuring_draw = ImageDraw.Draw(Image.new("RGB", (1, 1)))
    widest_text_width = max(
        measuring_draw.textbbox((0, 0), f"{axis} — {meaning}", font=font)[2]
        for axis, meaning in LEGEND_LABELS
    )
    block_width = LEGEND_SWATCH_LENGTH + 4 + widest_text_width
    expected_x = round((DEFAULT_VERTICAL_WIDTH - block_width) / 2)

    top = DEFAULT_MARGIN_PX
    row_mid = round(top + LEGEND_PADDING + LEGEND_ROW_HEIGHT / 2)

    assert image.getpixel((expected_x + 5, row_mid)) == X_COLOR
    # And it isn't just coincidentally sitting at the old left-anchored
    # position - centering should have actually moved it.
    old_left_anchored_x = DEFAULT_MARGIN_PX + 44 + LEGEND_PADDING
    assert expected_x != old_left_anchored_x


def test_vertical_legend_block_fits_within_the_panels_own_canvas_width():
    # The whole point of centering against canvas_width (not just the
    # narrower plot area) is that the block no longer runs off the
    # right edge - assert the math itself guarantees that, independent
    # of any single pixel check.
    font = _load_font(LEGEND_FONT_SIZE)
    measuring_draw = ImageDraw.Draw(Image.new("RGB", (1, 1)))
    widest_text_width = max(
        measuring_draw.textbbox((0, 0), f"{axis} — {meaning}", font=font)[2]
        for axis, meaning in LEGEND_LABELS
    )
    block_width = LEGEND_SWATCH_LENGTH + 4 + widest_text_width

    assert block_width <= DEFAULT_VERTICAL_WIDTH


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
    # Went 1px -> 2px -> 1px -> 2px again. The back-and-forth was
    # really about the traces crossing each other (fixed by giving Z
    # its own lane, see Z_LANE_FRACTION) and the legend overlapping
    # the traces (fixed by _legend_reserve_px()), not line width in
    # isolation - once those two were fixed, Christer picked the
    # bolder 2px width back.
    assert TRACE_LINE_WIDTH == 2


def test_font_candidates_lists_the_bundled_font_first():
    # Same fix as map_render.py's own _load_font() (where this bug was
    # first diagnosed, for map street-name labels): the bundled copy
    # under assets/ has to be tried before the two old system-path
    # candidates (a Linux-only path absent on Christer's Windows
    # machine and the ffmpeg-only Docker image, and a bare filename
    # that only resolves from the current working directory), or a
    # real install would still silently fall through to the tiny
    # glyph-less default font PIL uses as its last resort.
    expected = Path(gsensor_graph_render_module.__file__).parent / "assets" / "DejaVuSans-Bold.ttf"
    assert gsensor_graph_render_module._FONT_CANDIDATES[0] == str(expected)


def test_bundled_font_file_exists_on_disk():
    assert Path(gsensor_graph_render_module._FONT_CANDIDATES[0]).is_file()


def test_bundled_font_loads_as_a_real_truetype_font(monkeypatch):
    monkeypatch.setattr(gsensor_graph_render_module, "_CACHED_FONT_BY_SIZE", {})

    font = _load_font()

    assert isinstance(font, ImageFont.FreeTypeFont)


def test_bundled_font_renders_swedish_letters_with_nonzero_width(monkeypatch):
    # Mirrors map_render.py's own version of this test: PIL's
    # ImageFont.load_default() fallback (reached when every
    # _FONT_CANDIDATES path fails) has no å/ä/ö glyphs and draws them
    # as blank/tofu boxes - a real DejaVu font renders noticeably
    # wider text for the same string. This module's own text (tick
    # labels, legend) is ASCII-only today, but the font is shared
    # infrastructure with map_render.py/parking_transition.py, so it's
    # worth confirming it actually works for non-ASCII text here too.
    monkeypatch.setattr(gsensor_graph_render_module, "_CACHED_FONT_BY_SIZE", {})

    font = _load_font(24)
    draw = ImageDraw.Draw(Image.new("RGB", (1, 1)))
    left, top, right, bottom = draw.textbbox((0, 0), "Åkergatan äö", font=font)

    assert (right - left) > 150
    assert (bottom - top) > 15


def test_render_base_frame_hides_z_by_default():
    # Christer, after seeing Z get its own dedicated lane: "Z is just
    # not useful, unless you hit a giant pothole, but then the video
    # probably got that and the reaction of the driver" - so Z is now
    # hidden entirely unless explicitly asked for via show_z=True. This
    # checks the *pixel-level* consequence: with two samples whose X/Y
    # are flat at the shared zero-line (so X/Y's own zero-line trace
    # pixels don't accidentally coincide with where Z would be) but
    # whose Z value is large, no pixel in the image should be Z_COLOR
    # at all when show_z is left at its default.
    samples = (_sample(0, 0, 0, 80), _sample(1000, 0, 0, 80))
    image = render_base_frame(samples, (0.0, 0.0, 0.0), 100.0, 1.0)

    assert Z_COLOR not in set(image.getdata())


def test_render_base_frame_x_y_reclaim_the_full_axis_when_z_is_hidden():
    # Christer's own explicit pick ("X/Y reclaim the space") over
    # leaving z_lane's own share of the panel empty when Z is hidden -
    # X's trace should land at the position _value_to_pos() computes
    # against the *entire* plot area's own value axis, not against
    # main_lane (_split_lanes()'s smaller ~2/3 share), confirming X/Y
    # aren't still confined to their old lane once Z is gone.
    baseline = (0.0, 0.0, 0.0)
    scale = 100.0
    total_seconds = 1.0
    samples = (_sample(0, 50, 0, 0), _sample(1000, 50, 0, 0))

    image = render_base_frame(samples, baseline, scale, total_seconds)
    left, top, right, bottom = _plot_area(
        DEFAULT_WIDTH, DEFAULT_HEIGHT, 32, 20, "horizontal",
        legend_reserve=_legend_reserve_px("horizontal"),
    )
    x_pixel = round(_time_to_pos(0.0, total_seconds, left, right))
    full_axis_y = round(_value_to_pos(50, scale, top, bottom))

    assert image.getpixel((x_pixel, full_axis_y)) == X_COLOR


def test_render_base_frame_omits_the_divider_when_z_is_hidden():
    # No second lane, nothing to divide - only one zero-line should be
    # drawn across the value axis when Z is hidden, not the two (plus a
    # LANE_DIVIDER_COLOR line between them) show_z=True draws (see
    # test_render_base_frame_draws_a_divider_between_the_two_lanes).
    # Counts AXIS_COLOR rows in a single column rather than checking
    # "LANE_DIVIDER_COLOR absent anywhere in the image" - that plain
    # mid-gray (210, 210, 210) can coincidentally appear in anti
    # -aliased tick-label text elsewhere, which isn't the divider and
    # isn't a bug.
    samples = (_sample(0, 0, 0, 0), _sample(1000, 10, -10, 5))
    image = render_base_frame(samples, (0.0, 0.0, 0.0), 100.0, 1.0)

    left, top, right, bottom = _plot_area(
        DEFAULT_WIDTH, DEFAULT_HEIGHT, 32, 20, "horizontal",
        legend_reserve=_legend_reserve_px("horizontal"),
    )
    scan_x = round(left) + 5
    axis_color_rows = [
        y for y in range(round(top), round(bottom))
        if image.getpixel((scan_x, y)) == AXIS_COLOR
    ]

    assert len(axis_color_rows) == 1


def test_legend_has_two_rows_when_z_is_hidden():
    # Only X/Y's own rows - no "Z — Up/down" row for a trace that isn't
    # actually drawn (see _draw_legend()'s own docstring).
    samples = (_sample(0, 0, 0, 0), _sample(1000, 10, -10, 5))
    image = render_base_frame(samples, (0.0, 0.0, 0.0), 100.0, 1.0)

    left = top = DEFAULT_MARGIN_PX
    swatch_x = left + LEGEND_PADDING + 5

    for row, expected_color in enumerate((X_COLOR, Y_COLOR)):
        row_mid = round(
            top + LEGEND_PADDING + row * LEGEND_ROW_HEIGHT + LEGEND_ROW_HEIGHT / 2
        )
        assert image.getpixel((swatch_x, row_mid)) == expected_color

    # A third row, where Z's would have gone, should just be background
    # - nothing drawn there at all.
    third_row_mid = round(
        top + LEGEND_PADDING + 2 * LEGEND_ROW_HEIGHT + LEGEND_ROW_HEIGHT / 2
    )
    assert image.getpixel((swatch_x, third_row_mid)) == BACKGROUND_COLOR


def test_legend_reserve_is_smaller_when_z_is_hidden():
    # Two rows need less reserved space than three - checked in
    # vertical mode since that reserve is a simple fixed row count
    # (horizontal mode's reserve also depends on text width, which
    # doesn't change between 2 and 3 rows since Y's own row is already
    # the widest either way).
    assert _legend_reserve_px("vertical", show_z=False) < _legend_reserve_px(
        "vertical", show_z=True
    )


def test_render_frame_playhead_still_aligns_when_z_is_hidden():
    # render_frame() has to compute the exact same legend_reserve (and
    # therefore the exact same plot area) render_base_frame() used, or
    # the playhead drifts out of alignment with the base chart's own
    # traces - see render_frame()'s own docstring. Both default to
    # show_z=False now, so this should just work without either side
    # passing show_z explicitly.
    samples = (_sample(0, 0, 0, 0), _sample(1000, 10, -10, 5))
    base = render_base_frame(samples, (0.0, 0.0, 0.0), 100.0, 1.0)

    frame = render_frame(base, 0.5, 1.0)
    left, top, right, bottom = _plot_area(
        DEFAULT_WIDTH, DEFAULT_HEIGHT, 32, 20, "horizontal",
        legend_reserve=_legend_reserve_px("horizontal"),
    )
    playhead_x = round(_time_to_pos(0.5, 1.0, left, right))

    assert frame.getpixel((playhead_x, round(top))) == PLAYHEAD_COLOR
