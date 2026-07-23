from pathlib import Path

import pytest
from PIL import Image
from PIL import ImageDraw

from blackvue.export.mirror_icon import _largest_connected_component
from blackvue.export.mirror_icon import load_mirror_frame
from blackvue.generate.media import MediaToolError


def _make_frame_with_a_stray_logo_dot(path):
    # Canvas: a plain white background, with two separate dark blocks.
    #
    # Block A (the "real" frame/mount): a 20x20 solid black square at
    # (2,2)-(21,21) with a 12x12 white "glass" hole cut into its
    # center at (6,6)-(17,17) - this is the frame's own real glass
    # area, the one --stitch-mirror-icon should composite rear
    # footage into.
    #
    # Block B (a stray "logo"): a separate, unconnected 9x9 solid
    # black square at (25,8)-(33,16), with a tiny 3x3 white dot
    # enclosed in its own center at (28,11)-(30,13) - mimics a small
    # reflective logo on a real mirror's mount that segments as its
    # own tiny enclosed "glass" blob, purely by the same light
    # -and-enclosed rule, but is not the real glass and should be
    # discarded as noise (see _largest_connected_component).
    image = Image.new("RGB", (40, 24), (255, 255, 255))
    draw = ImageDraw.Draw(image)
    draw.rectangle((2, 2, 21, 21), fill=(0, 0, 0))
    draw.rectangle((6, 6, 17, 17), fill=(255, 255, 255))
    draw.rectangle((25, 8, 33, 16), fill=(0, 0, 0))
    draw.rectangle((28, 11, 30, 13), fill=(255, 255, 255))
    image.save(path)


def test_load_mirror_frame_crops_to_the_content_bounding_box(tmp_path):
    icon_path = tmp_path / "mirror.png"
    _make_frame_with_a_stray_logo_dot(icon_path)

    frame = load_mirror_frame(icon_path)

    # Content = the smallest rectangle spanning both dark blocks' own
    # bounding boxes: x from 2 (Block A's left edge) to 33 (Block B's
    # right edge), y from 2 (Block A's top edge) to 21 (Block A's own
    # bottom edge, taller than Block B's) - the surrounding white
    # margin (everything outside both blocks, reachable from the
    # image's own border) is cropped away entirely.
    assert frame.frame_overlay.size == (32, 20)
    assert frame.glass_mask.size == (32, 20)


def test_load_mirror_frame_keeps_only_the_largest_glass_component(tmp_path):
    icon_path = tmp_path / "mirror.png"
    _make_frame_with_a_stray_logo_dot(icon_path)

    frame = load_mirror_frame(icon_path)

    # The main 12x12 interior (in source coordinates (6,6)-(17,17), or
    # (4,4)-(15,15) once cropped to the content bbox starting at
    # (2,2)) is by far the larger enclosed-light region, so it wins.
    assert frame.glass_bbox == (4, 4, 15, 15)

    # The stray 3x3 logo dot ((28,11)-(30,13) in source coordinates,
    # (3,3)-(5,5) relative to Block B's own (25,8) corner, i.e.
    # (26,9)-(28,11) relative to the shared content origin (2,2)) must
    # NOT appear as white in the final glass mask - it was the
    # smaller of the two components and got discarded as noise.
    mask_pixels = frame.glass_mask.load()
    assert mask_pixels[27, 10] == 0


def test_load_mirror_frame_paints_dark_pixels_opaque_in_the_frame_overlay(
    tmp_path
):
    icon_path = tmp_path / "mirror.png"
    _make_frame_with_a_stray_logo_dot(icon_path)

    frame = load_mirror_frame(icon_path)

    frame_pixels = frame.frame_overlay.load()
    # A pixel inside the main frame's own black ring (source (3,3),
    # i.e. (1,1) relative to the content origin) should be fully
    # opaque black.
    assert frame_pixels[1, 1] == (0, 0, 0, 255)
    # A pixel inside the stray logo block's own black square (source
    # (26,9), i.e. (24,7) relative to the content origin) should also
    # be opaque black - it's still "dark," even though its own
    # enclosed light dot got filtered out as noise.
    assert frame_pixels[24, 7] == (0, 0, 0, 255)
    # A pixel inside the real glass area should stay fully transparent
    # (nothing drawn there - rear footage shows through in the real
    # stitch pipeline, see stitch.py's own is_mirror/mirror_icon
    # handling).
    assert frame_pixels[10, 10][3] == 0


def test_load_mirror_frame_marks_the_real_glass_area_white_in_the_mask(
    tmp_path
):
    icon_path = tmp_path / "mirror.png"
    _make_frame_with_a_stray_logo_dot(icon_path)

    frame = load_mirror_frame(icon_path)

    mask_pixels = frame.glass_mask.load()
    # Center of the real glass area (source (11,11), i.e. (9,9)
    # relative to the content origin) should be white (255) - part of
    # the glass to clip rear footage into.
    assert mask_pixels[9, 9] == 255


def test_load_mirror_frame_raises_a_clear_error_on_a_missing_file(tmp_path):
    with pytest.raises(MediaToolError):
        load_mirror_frame(tmp_path / "does-not-exist.png")


def test_load_mirror_frame_raises_when_there_is_no_enclosed_glass_area(
    tmp_path
):
    # A plain, uniformly light image - nothing dark at all, so
    # everything is background reachable from the border and nothing
    # is ever "enclosed." Nothing for --stitch-mirror-icon to
    # composite rear footage into.
    icon_path = tmp_path / "blank.png"
    Image.new("RGB", (20, 20), (255, 255, 255)).save(icon_path)

    with pytest.raises(MediaToolError) as excinfo:
        load_mirror_frame(icon_path)
    assert "no enclosed glass" in str(excinfo.value)


def test_load_mirror_frame_raises_a_clear_error_when_the_image_is_entirely_dark(
    tmp_path
):
    # Reproduces a real bad-input case: feeding one of load_mirror
    # _frame()'s own outputs (a saved frame_overlay.png, RGBA with a
    # fully transparent "glass" region) back in as a --stitch-mirror
    # -icon source. Image.open(...).convert("RGB") strips alpha and
    # keeps the underlying RGB, which for a transparent region created
    # via Image.new("RGBA", ..., (0, 0, 0, 0)) is solid black - so the
    # whole reloaded image reads as "dark," with content (the earlier
    # `not content_xs` check doesn't catch this) but zero glass pixels
    # (a later, separate check has to catch it instead - this used to
    # crash with an unguarded ValueError from min() on an empty list).
    icon_path = tmp_path / "all_dark.png"
    Image.new("RGB", (20, 20), (0, 0, 0)).save(icon_path)

    with pytest.raises(MediaToolError) as excinfo:
        load_mirror_frame(icon_path)
    assert "no enclosed glass" in str(excinfo.value)


def test_largest_connected_component_keeps_only_the_biggest_blob():
    # A 5x5 grid: a 2x2 block of True in the top-left (4 cells) and a
    # single lone True cell in the bottom-right corner (1 cell,
    # 4-connected to nothing else) - the top-left block should survive
    # and the lone cell should be cleared.
    mask = [[False] * 5 for _ in range(5)]
    for y in (0, 1):
        for x in (0, 1):
            mask[y][x] = True
    mask[4][4] = True

    result = _largest_connected_component(mask, 5, 5)

    assert result[0][0] and result[0][1] and result[1][0] and result[1][1]
    assert not result[4][4]
