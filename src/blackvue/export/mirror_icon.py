"""
Segments a photo of a physical rearview mirror (--stitch-mirror-icon)
into a frame overlay and a glass clipping mask, so real rear-camera
footage can be composited into the photo's own glass area and read as
footage playing inside that actual mirror - see stitch.py's
rearview_mirror layout and its own `mirror_icon` handling in _stack().

The approach (flood fill from a plain product-photo-style source, not
a general-purpose image segmentation model): a photo like this is
almost always three regions - a dark frame/bezel/mount, a light
"glass" interior enclosed by that frame, and a light background
outside it. Distinguishing "light and enclosed" (glass) from "light
and reachable from the image's own border without crossing the dark
frame" (background) only needs a threshold plus a flood fill, no ML
and no user-drawn mask required. Confirmed against a real product
photo (a wide rearview mirror with a suction-cup mount, white
background) during this feature's own design/mockup phase - a sharp
bimodal luminance histogram with almost nothing in the 100-150 range,
comfortably separating "frame" from "everything else."

Copyright (C) 2026 Christer R. (sssreh)

SPDX-License-Identifier: GPL-3.0-or-later
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from pathlib import Path

from PIL import Image

from ..generate.media import MediaToolError

# Bundled alongside this module (see pyproject.toml's package-data
# entry for "blackvue.export") so it's available wherever bv-export
# actually runs - a plain repo-relative path like "images/mirror.png"
# would break under Dockerfile.cli's build (only pyproject.toml/
# README.md/src/ are copied in) and any pip-installed, non-checkout
# setup. Same Path(__file__).parent-relative convention
# blackvue.web.app.py's TEMPLATES_DIR already uses for its own bundled
# assets. This is Christer's own reference photo of his physical
# rearview mirror (see this module's own history in WORKING_CONTEXT.md
# - "--stitch-mirror-icon: composite a real mirror photo as the inset
# frame"), now the default --stitch-mirror-icon instead of an
# opt-in-only path: see bv_export.py's own handling of this constant
# for the "omit the flag -> use this; pass the literal string 'none'
# -> fall back to the plain procedural inset instead" convention.
DEFAULT_MIRROR_ICON_PATH = Path(__file__).parent / "assets" / "mirror.png"

# Luminance (0-255, average of R/G/B) below which a pixel counts as
# "dark" - the mirror's own frame/bezel/mount - rather than "light"
# -background or glass, both of which read as bright in a normal
# product photo. Picked from a real photo's own histogram: a sharp
# bimodal split (~20k dark pixels clustered under 64, ~330k light
# pixels clustered over 200) with almost nothing in between - 120 sits
# comfortably in that gap. A photo with a much darker or busier
# background than a plain product shot could confuse this threshold;
# not attempting to handle that case - see load_mirror_frame()'s own
# docstring note.
_DARK_LUMINANCE_THRESHOLD = 120


@dataclass(frozen=True)
class MirrorFrame:
    """The two derived assets from a --stitch-mirror-icon photo, both
    already cropped to the icon's own *content* bounding box (frame +
    mount together, background margin removed) - so --stitch-mirror
    -size's percentage sizes the real mirror shape, not the source
    photo's own arbitrary canvas including wasted white space around
    it.

    `frame_overlay`: RGBA, same size as `glass_mask`. Opaque
    (original color, full alpha) wherever the source was "dark" (the
    frame/bezel/mount) - fully transparent everywhere else (both the
    glass interior and the background margin, indistinguishable here
    on purpose since both should show whatever's *behind* this layer
    once composited, be that the clipped rear footage inside the
    glass or the front camera's own footage everywhere outside the
    mirror's silhouette).

    `glass_mask`: single-channel (mode "L"). White (255) wherever the
    source was "light and enclosed by the frame" (the glass) - black
    (0) everywhere else, including the background margin. Used to
    alpha-clip the rear camera's footage into exactly the glass's own
    silhouette (see stitch.py's `is_mirror` + `mirror_icon` handling)
    - without this, a plain rectangular rear-footage layer would
    poke out past the mirror's rounded silhouette into the
    background-margin corners.

    `glass_bbox`: (x0, y0, x1, y1), the glass region's own bounding
    box within `frame_overlay`'s/`glass_mask`'s shared coordinate
    space (inclusive of both edges, i.e. width = x1 - x0 + 1) - the
    exact sub-rectangle rear footage needs to be scaled and positioned
    into before the alpha-clip against `glass_mask` trims it down to
    the glass's own non-rectangular silhouette. Needed as its own
    field (not re-derived from `glass_mask` at every call site)
    because stitch.py's caller has to size/position the rear video
    *before* any masking happens - ffmpeg's `alphamerge` filter clips
    whatever shape it's given, but doesn't know or care how well the
    video underneath was actually framed to the glass's own bounding
    box in the first place; getting that placement right is this
    field's whole job.
    """

    frame_overlay: Image.Image
    glass_mask: Image.Image
    glass_bbox: tuple[int, int, int, int]


def _largest_connected_component(
    mask: list[list[bool]], width: int, height: int
) -> list[list[bool]]:
    """Return a copy of `mask` (a 2D grid of booleans) with every True
    region except the single largest 4-connected one cleared to
    False - see load_mirror_frame()'s own note on why this matters
    for a real product photo (a small enclosed light spot elsewhere
    in the frame, e.g. a logo, shouldn't be treated as part of the
    mirror's actual glass).
    """

    visited = [[False] * width for _ in range(height)]
    best_component: list[tuple[int, int]] = []

    for start_y in range(height):
        for start_x in range(width):
            if not mask[start_y][start_x] or visited[start_y][start_x]:
                continue

            component: list[tuple[int, int]] = []
            queue: deque[tuple[int, int]] = deque([(start_x, start_y)])
            visited[start_y][start_x] = True
            while queue:
                x, y = queue.popleft()
                component.append((x, y))
                for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1)):
                    nx, ny = x + dx, y + dy
                    if (
                        0 <= nx < width and 0 <= ny < height
                        and mask[ny][nx] and not visited[ny][nx]
                    ):
                        visited[ny][nx] = True
                        queue.append((nx, ny))

            if len(component) > len(best_component):
                best_component = component

    result = [[False] * width for _ in range(height)]
    for x, y in best_component:
        result[y][x] = True
    return result


def load_mirror_frame(icon_path: Path) -> MirrorFrame:
    """Load and segment `icon_path` (a --stitch-mirror-icon photo)
    into a MirrorFrame - see its own docstring for what the two
    images mean.

    Raises MediaToolError if `icon_path` doesn't exist, isn't a
    readable image, or segments into an empty glass region (a
    plain-colored image with no enclosed light area at all - nothing
    to clip rear footage into) - stitch.py's own caller degrades this
    to a `warnings` entry and falls back to the plain procedural
    rounded-rectangle inset, the same "don't fail the whole stitch
    over an optional cosmetic input" convention `map_icon` already
    follows for --map/--map-zoom's own custom marker image.

    Not attempting general-purpose background removal - this assumes
    a plain-photo-style source (a mostly-uniform light background, a
    clearly darker frame, matching the "product photo on white"
    style Christer's own reference image used). A photo with a
    textured/patterned/dark background, or an already-transparent PNG
    someone hand-prepared, isn't handled specially - the flood fill
    would either mis-segment it or (for an already-transparent image,
    flattened to RGB by the `.convert("RGB")` below) ignore the
    existing alpha entirely. Good enough for the feature's own actual
    use case; not a general image-processing tool.
    """

    try:
        image = Image.open(icon_path).convert("RGB")
    except (FileNotFoundError, OSError) as exc:
        raise MediaToolError(
            f"could not read mirror icon {icon_path.name}: {exc}"
        ) from exc

    width, height = image.size
    pixels = image.load()

    # "dark" = frame/bezel/mount. Computed once into a plain 2D list
    # (not a numpy array - this project has no numpy dependency
    # elsewhere, and a few hundred thousand pixels is fast enough in
    # pure Python for a once-per-stitch-run image, not a per-frame
    # cost) so the flood fill below can test membership by simple
    # indexing.
    dark = [
        [
            (sum(pixels[x, y]) / 3) < _DARK_LUMINANCE_THRESHOLD
            for x in range(width)
        ]
        for y in range(height)
    ]

    # BFS flood fill from every border pixel, through "light" (not
    # dark) territory only - marks every light pixel reachable from
    # the image's own edge without crossing the dark frame as
    # "background." Whatever's left over (light, but never reached)
    # is "glass" - light territory the frame fully encloses.
    background = [[False] * width for _ in range(height)]
    queue: deque[tuple[int, int]] = deque()

    def _seed(x: int, y: int) -> None:
        if not dark[y][x] and not background[y][x]:
            background[y][x] = True
            queue.append((x, y))

    for x in range(width):
        _seed(x, 0)
        _seed(x, height - 1)
    for y in range(height):
        _seed(0, y)
        _seed(width - 1, y)

    while queue:
        x, y = queue.popleft()
        for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1)):
            nx, ny = x + dx, y + dy
            if (
                0 <= nx < width and 0 <= ny < height
                and not dark[ny][nx] and not background[ny][nx]
            ):
                background[ny][nx] = True
                queue.append((nx, ny))

    glass = [
        [not dark[y][x] and not background[y][x] for x in range(width)]
        for y in range(height)
    ]

    # A real photo's frame/mount can enclose small light spots that
    # aren't the mirror's own glass at all - e.g. a reflective logo
    # engraved on the mount, light enough to flood-fill as "glass" by
    # the same light-and-enclosed rule. Confirmed on the actual
    # reference photo: a tiny cluster near the mount's own label.
    # Keeping only the largest connected glass component discards
    # these as noise - the real glass area is, by a wide margin, the
    # biggest enclosed light region in a plain mirror photo.
    glass = _largest_connected_component(glass, width, height)

    # Content bounding box: the smallest rectangle containing every
    # non-background pixel (dark frame/mount OR glass) - this is what
    # gets cropped to, discarding the source photo's own surrounding
    # white margin.
    content_xs = [
        x for y in range(height) for x in range(width)
        if dark[y][x] or glass[y][x]
    ]
    content_ys = [
        y for y in range(height) for x in range(width)
        if dark[y][x] or glass[y][x]
    ]
    # NOTE: `content_xs` covers dark frame pixels too, not just glass
    # - an image that's *entirely* dark (no light pixels anywhere, so
    # nothing was ever eligible to become "glass" in the first place)
    # still has non-empty `content_xs`, but zero glass pixels. Caught
    # again, more specifically, once `glass_xs` is built below - this
    # first check only rules out the "totally blank, nothing dark or
    # light-and-enclosed at all" case.
    if not content_xs:
        raise MediaToolError(
            f"mirror icon {icon_path.name} has no enclosed glass "
            "area to composite footage into - is this a plain photo "
            "of a mirror on a light background?"
        )

    x0, x1 = min(content_xs), max(content_xs)
    y0, y1 = min(content_ys), max(content_ys)
    content_width, content_height = x1 - x0 + 1, y1 - y0 + 1

    frame_overlay = Image.new("RGBA", (content_width, content_height), (0, 0, 0, 0))
    glass_mask = Image.new("L", (content_width, content_height), 0)
    frame_pixels = frame_overlay.load()
    mask_pixels = glass_mask.load()
    source_pixels = image.load()

    glass_xs = []
    glass_ys = []
    for y in range(y0, y1 + 1):
        for x in range(x0, x1 + 1):
            out_x, out_y = x - x0, y - y0
            if dark[y][x]:
                r, g, b = source_pixels[x, y]
                frame_pixels[out_x, out_y] = (r, g, b, 255)
            elif glass[y][x]:
                mask_pixels[out_x, out_y] = 255
                glass_xs.append(out_x)
                glass_ys.append(out_y)

    if not glass_xs:
        # content_xs (checked above) only rules out a totally blank
        # image - an image that's entirely dark (e.g. a --stitch
        # -mirror-icon photo of a mirror re-fed one of its own
        # already-segmented outputs back in as input: frame_overlay's
        # own transparent "glass" pixels flatten to solid black once
        # reloaded and stripped of alpha) has plenty of content but
        # zero light-and-enclosed pixels, so glass_xs/glass_ys stay
        # empty even though content_xs didn't.
        raise MediaToolError(
            f"mirror icon {icon_path.name} has no enclosed glass "
            "area to composite footage into - is this a plain photo "
            "of a mirror on a light background? (if this file is "
            "itself a --stitch-mirror-icon output, e.g. a saved "
            "frame_overlay preview, point --stitch-mirror-icon at "
            "the original source photo instead)"
        )

    glass_bbox = (min(glass_xs), min(glass_ys), max(glass_xs), max(glass_ys))

    return MirrorFrame(
        frame_overlay=frame_overlay, glass_mask=glass_mask,
        glass_bbox=glass_bbox,
    )
