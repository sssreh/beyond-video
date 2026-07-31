"""
Shared multipart/x-mixed-replace MJPEG streaming helpers for bv-live.

Both the camera passthrough (proxying blackvue_live.cgi's own existing
MJPEG stream unchanged) and the two synthetic streams this project
renders itself (the live map, the live g-sensor strip) end up serving
the same content-type shape to the browser - a plain <img> tag
understands multipart/x-mixed-replace natively, with no client-side JS
needed to keep it updating (the same reasoning behind the original
bv-watch discussion this feature grew out of - see WORKING_CONTEXT.md).

Copyright (C) 2026 Christer R. (sssreh)

SPDX-License-Identifier: GPL-3.0-or-later
"""

from __future__ import annotations

import io
import time
from collections.abc import Callable
from collections.abc import Iterator

from PIL import Image

# Our own synthetic streams' boundary marker - arbitrary, just needs
# to match between CONTENT_TYPE (the header we tell the browser) and
# encode_jpeg_part() (what we actually write between frames). The
# camera's own MJPEG stream (relay_raw_stream()) uses whatever
# boundary *it* already sends, forwarded through unchanged - see
# live/app.py's /stream/camera route, which reads the upstream
# response's own Content-Type header rather than this constant.
BOUNDARY = "bvliveboundary"
CONTENT_TYPE = f"multipart/x-mixed-replace; boundary={BOUNDARY}"

JPEG_QUALITY = 80


def encode_jpeg_part(image: Image.Image, *, quality: int = JPEG_QUALITY) -> bytes:
    """Encode `image` as one multipart part: boundary line, headers,
    the JPEG bytes themselves, and a trailing CRLF - ready to write
    straight onto an HTTP response body using CONTENT_TYPE above."""

    buffer = io.BytesIO()
    image.convert("RGB").save(buffer, format="JPEG", quality=quality)
    data = buffer.getvalue()

    header = (
        f"--{BOUNDARY}\r\n"
        "Content-Type: image/jpeg\r\n"
        f"Content-Length: {len(data)}\r\n\r\n"
    ).encode("ascii")

    return header + data + b"\r\n"


def rendered_frame_stream(
    render: Callable[[], Image.Image], fps: float
) -> Iterator[bytes]:
    """Yield encode_jpeg_part(render()) forever, at roughly `fps` -
    shared frame-loop-to-multipart plumbing for both of bv-live's own
    synthetic streams (see live/map_stream.py's live_map_frames() and
    live/gsensor_stream.py's live_gsensor_frames(), which each return
    the zero-argument `render` callable this expects).

    Runs forever until the caller stops iterating (e.g. the browser
    tab closes and Starlette stops pulling from the generator) -
    there's no separate "stop" signal here, matching how
    live/app.py's routes are wired (one generator per HTTP request/
    connection, torn down when that connection ends).
    """

    interval = 1.0 / fps if fps > 0 else 0.0

    while True:
        started = time.monotonic()
        yield encode_jpeg_part(render())
        elapsed = time.monotonic() - started
        remaining = interval - elapsed
        if remaining > 0:
            time.sleep(remaining)


def relay_raw_stream(response, *, chunk_size: int = 4096) -> Iterator[bytes]:
    """Relay `response` (an already-open BlackVueClient.open_stream()
    result) chunk by chunk, unmodified, until it stops yielding data or
    the caller stops iterating - closes `response` either way.

    Used for the camera's own front/rear MJPEG feed (see live/app.py's
    /stream/camera route): the camera's multipart framing/boundary is
    forwarded through byte-for-byte rather than decoded and
    re-encoded, since there's nothing to change about it - unlike
    rendered_frame_stream() above, which is building brand new frames
    from scratch every time, this is pure passthrough.
    """

    try:
        while True:
            chunk = response.read(chunk_size)
            if not chunk:
                break
            yield chunk
    finally:
        response.close()
