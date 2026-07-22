"""
Camera composition for bv-export --stitch: combines a trip's
front/rear footage into one video via ffmpeg's hstack/vstack filters.

This is the first --stitch building block - see WORKING_CONTEXT.md for
the full agreed spec. Only the two camera layouts that are a straight
stack of unmodified footage are built so far. rearview_mirror (flip +
scale + overlay), the map panel, the g-sensor overlay, subtitle
burn-in, and auto-picking a layout from the trip's own geometry all
come in later passes.

Copyright (C) 2026 Christer R. (sssreh)

SPDX-License-Identifier: GPL-3.0-or-later
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

from ..generate.media import MediaToolError
from .media import concatenate_media
from .media import encode_with_nvenc_fallback

# side_by_side places front and rear next to each other (ffmpeg
# hstack) - per the agreed --stitch spec, this is the layout a trip
# that runs mostly east-west will eventually auto-pick. top_down
# stacks them one above the other (vstack) - the north-south pick.
# Auto-picking from the trip's own geometry isn't built yet, so
# `layout` is always explicit for now.
STACK_LAYOUTS = {
    "side_by_side": "hstack",
    "top_down": "vstack",
}


def stitch_cameras(
    front: Path | None,
    rear: Path | None,
    destination: Path,
    *,
    layout: str,
) -> Path | None:
    """Compose a trip's front/rear footage into one video at
    `destination`.

    `layout` must be one of STACK_LAYOUTS's keys ('side_by_side' or
    'top_down' - 'rearview_mirror' isn't built yet). Only meaningful
    when both front and rear exist; a trip with just one of the two
    (the common single-front-camera case) falls back to a plain copy
    of whichever one is available, ignoring `layout` entirely - the
    same "don't fail, just do the sensible thing" convention the rest
    of bv-export follows for a missing optional input. Returns None if
    neither exists.

    No audio track is carried into the stitched video yet - trip-level
    audio already lives in its own audio.aac (see trip_export.py),
    muxing that back in is a later --stitch pass, not this one.
    """

    if front is not None and rear is not None:
        return _stack(front, rear, destination, layout=layout)

    only = front or rear
    if only is None:
        return None

    concatenate_media([only], destination)
    return destination


def _video_dimensions(path: Path) -> tuple[int, int]:
    try:
        result = subprocess.run(
            [
                "ffprobe", "-v", "error",
                "-select_streams", "v:0",
                "-show_entries", "stream=width,height",
                "-of", "json",
                str(path),
            ],
            capture_output=True,
            text=True,
            check=True,
        )
    except FileNotFoundError as exc:
        raise MediaToolError("ffprobe not found on PATH") from exc
    except subprocess.CalledProcessError as exc:
        raise MediaToolError(
            f"ffprobe failed for {path.name}: {exc.stderr.strip()}"
        ) from exc

    try:
        stream = json.loads(result.stdout)["streams"][0]
        return int(stream["width"]), int(stream["height"])
    except (KeyError, IndexError, ValueError) as exc:
        raise MediaToolError(
            f"could not parse ffprobe output for {path.name}"
        ) from exc


def _stack(front: Path, rear: Path, destination: Path, *, layout: str) -> Path:
    if layout not in STACK_LAYOUTS:
        raise ValueError(
            f"unknown stitch layout: {layout!r} "
            f"(expected one of {sorted(STACK_LAYOUTS)})"
        )

    filter_name = STACK_LAYOUTS[layout]

    # Front and rear cameras can differ in resolution (some BlackVue
    # setups pair a higher-res front with a lower-res rear) - hstack/
    # vstack both require matching dimensions on the non-stacked axis,
    # so rear is stretched to front's own width/height (probed
    # directly, rather than relying on ffmpeg's scale2ref filter,
    # whose "which input gets scaled to match which" semantics turned
    # out to be easy to get backwards - a plain probed scale=W:H is
    # simpler to reason about and get right). A full stretch rather
    # than a letterboxed fit: simpler, and worth revisiting only if it
    # actually looks wrong on a real mismatched front/rear pair.
    front_width, front_height = _video_dimensions(front)
    filter_complex = (
        f"[1:v]scale={front_width}:{front_height}[rear_scaled];"
        f"[0:v][rear_scaled]{filter_name}=inputs=2[v]"
    )

    encode_with_nvenc_fallback(
        [
            "-i", str(front),
            "-i", str(rear),
            "-filter_complex", filter_complex,
            "-map", "[v]",
        ],
        destination,
    )
    return destination
