from blackvue.export.gsensor_render import BACKGROUND_COLOR
from blackvue.export.gsensor_render import RING_LINE_WIDTH
from blackvue.export.gsensor_render import baseline_for_samples
from blackvue.export.gsensor_render import render_frame
from blackvue.export.gsensor_render import scale_for_samples
from blackvue.telemetry.gsensor_reader import GSensorSample
from datetime import timedelta


def _sample(offset_ms, x, y, z=900):
    return GSensorSample(offset=timedelta(milliseconds=offset_ms), x=x, y=y, z=z)


def test_render_frame_returns_image_of_requested_size():
    image = render_frame(
        1.0, trail_points=(), position=None, width=320, height=240
    )

    assert image.size == (320, 240)
    assert image.mode == "RGB"


def test_render_frame_draws_something_when_trail_and_position_given():
    background = render_frame(1.0, trail_points=(), position=None)

    with_content = render_frame(
        1.0,
        trail_points=((0.1, 0.2), (0.3, 0.4), (0.5, 0.6)),
        position=(0.5, 0.6),
    )

    # Not a pixel-exact check (font rendering/AA can vary across
    # environments) - just confirms drawing actually changed pixels
    # relative to a blank background of the same size.
    assert list(background.getdata()) != list(with_content.getdata())


def test_render_frame_background_is_a_flat_chroma_key_green():
    # gsensor.mp4 is meant to be composited over the front/rear
    # footage later (--stitch, future), so its background needs to be
    # a single flat color a chroma-key filter can match exactly -
    # confirmed here by checking a far corner (well outside the gauge
    # circle) is exactly BACKGROUND_COLOR, not some other tone.
    image = render_frame(1.0, trail_points=(), position=None)

    assert image.getpixel((0, 0)) == BACKGROUND_COLOR
    assert BACKGROUND_COLOR == (0, 255, 0)


def test_render_frame_ring_lines_are_at_least_two_pixels_thick():
    # A single-pixel outline reads fine in this raw frame, but doesn't
    # survive --stitch's own downscale-to-overlay-size + H.264 encode +
    # colorkey by the time it's actually watched (confirmed on a real
    # export: at a realistic overlay size, a 1px ring line came through
    # at ~0% of its own pixels - Christer saw the dot but not the rings
    # around it). RING_LINE_WIDTH is the fix; this walks outward along
    # a ray from the gauge's center (avoiding the axis lines - offset
    # 30 degrees off horizontal) and confirms the outermost ring is at
    # least RING_LINE_WIDTH pixels thick, not just one.
    import math

    image = render_frame(1.0, trail_points=(), position=None)
    width, height = image.size
    cx, cy = width / 2, height / 2
    radius = min(width, height) / 2 - 40  # DEFAULT_MARGIN_PX

    angle = math.radians(30)
    dx, dy = math.cos(angle), math.sin(angle)

    def _is_ring_pixel(distance):
        x = round(cx + dx * distance)
        y = round(cy + dy * distance)
        return image.getpixel((x, y)) != BACKGROUND_COLOR

    # Scan a window around the outer ring's own radius for the longest
    # run of consecutive non-background pixels.
    run = 0
    longest_run = 0
    for distance in range(int(radius) - 5, int(radius) + 6):
        if _is_ring_pixel(distance):
            run += 1
            longest_run = max(longest_run, run)
        else:
            run = 0

    assert longest_run >= RING_LINE_WIDTH


def test_render_frame_handles_a_single_trail_point_without_crashing():
    # len(trail_points) < 2 means draw.line() would be skipped -
    # exercised here to make sure a trip with only one sample doesn't
    # crash frame rendering.
    image = render_frame(1.0, trail_points=((0.1, 0.2),), position=(0.1, 0.2))

    assert image.size == (480, 480)


def test_scale_for_samples_floors_at_minimum_for_flat_data():
    # A parked/idling trip: zero x/y everywhere shouldn't produce a
    # zero (divide-by-zero-prone) scale.
    samples = (_sample(0, 0, 0), _sample(100, 0, 0))

    assert scale_for_samples(samples, minimum=1.0) == 1.0


def test_scale_for_samples_scales_to_the_observed_peak_with_padding():
    samples = (_sample(0, 100, -50), _sample(100, -300, 200))

    # Largest |x| or |y| across both samples is 300 (from x=-300).
    assert scale_for_samples(samples, padding=1.2, minimum=1.0) == 360.0


def test_scale_for_samples_measures_deviation_from_a_given_baseline():
    # Same samples as above, but now centered on a baseline of
    # (500, 500): deviations are (-400, -550) and (-800, -300), so the
    # largest is 800, not 300 (what raw (0, 0)-relative would give).
    samples = (_sample(0, 100, -50), _sample(100, -300, 200))

    scale = scale_for_samples(
        samples, baseline=(500.0, 500.0), padding=1.0, minimum=1.0
    )

    assert scale == 800.0


def test_baseline_for_samples_is_the_median_x_and_median_y():
    samples = (
        _sample(0, 10, 100),
        _sample(100, 20, 300),
        _sample(200, 30, 200),
    )

    assert baseline_for_samples(samples) == (20.0, 200.0)


def test_baseline_for_samples_averages_the_two_middle_values_for_even_counts():
    samples = (
        _sample(0, 0, 0),
        _sample(100, 10, 10),
        _sample(200, 20, 20),
        _sample(300, 30, 30),
    )

    assert baseline_for_samples(samples) == (15.0, 15.0)


def test_baseline_for_samples_returns_origin_for_no_samples():
    assert baseline_for_samples(()) == (0.0, 0.0)
