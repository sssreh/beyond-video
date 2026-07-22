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
