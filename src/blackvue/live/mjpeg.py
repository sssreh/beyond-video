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
import threading
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
    render: Callable[[], Image.Image],
    fps: float,
    *,
    stop_event: threading.Event | None = None,
) -> Iterator[bytes]:
    """Yield encode_jpeg_part(render()) forever, at roughly `fps` -
    shared frame-loop-to-multipart plumbing for both of bv-live's own
    synthetic streams (see live/map_stream.py's live_map_frames() and
    live/gsensor_stream.py's live_gsensor_frames(), which each return
    the zero-argument `render` callable this expects).

    Runs until the caller stops iterating (e.g. the browser tab closes
    and Starlette stops pulling from the generator) *or* `stop_event`
    gets set, whichever happens first. `stop_event` exists for the
    latter case specifically - see live/app.py's own create_live_app()
    docstring on why relying on the former alone left `bv-live` hanging
    at uvicorn's "Waiting for connections to close" on Ctrl-C. Checked
    once per loop iteration (bounded by `interval` either way), not
    inside a blocking call, so it's always responsive within roughly
    one frame period.
    """

    interval = 1.0 / fps if fps > 0 else 0.0

    while stop_event is None or not stop_event.is_set():
        started = time.monotonic()
        yield encode_jpeg_part(render())
        elapsed = time.monotonic() - started
        remaining = interval - elapsed
        if remaining > 0:
            time.sleep(remaining)


# Bounds how many bytes relay_raw_stream() will read while trying to
# discard `discard_frames` complete frames before giving up and just
# relaying from wherever it's gotten to - same reasoning as
# core/blackvue_client.py's own SNAPSHOT_MAX_BYTES: if the camera's
# multipart framing ever doesn't look like what's expected (missing
# Content-Length, corrupt boundary), this stays a startup hiccup - a
# few stale frames still get through, same as before this feature
# existed - rather than the whole feed sitting with no image at all
# until a viewer gives up and reloads. Same 4MB size as
# SNAPSHOT_MAX_BYTES; the discard phase should only ever need a small
# fraction of that in practice (each JPEG frame is well under 4096
# bytes per SNAPSHOT_WARMUP_FRAMES's own comment).
DISCARD_MAX_BYTES = 4 * 1024 * 1024


def relay_raw_stream(
    response,
    *,
    chunk_size: int = 4096,
    stop_event: threading.Event | None = None,
    discard_frames: int = 0,
) -> Iterator[bytes]:
    """Relay `response` (an already-open BlackVueClient.open_stream()
    result) chunk by chunk, unmodified, until it stops yielding data,
    the caller stops iterating, or `stop_event` gets set - closes
    `response` in every case.

    Used for the camera's own front/rear MJPEG feed (see live/app.py's
    /stream/camera route): the camera's multipart framing/boundary is
    forwarded through byte-for-byte rather than decoded and
    re-encoded, since there's nothing to change about it - unlike
    rendered_frame_stream() above, which is building brand new frames
    from scratch every time, this is pure passthrough.

    `discard_frames`: drop this many complete multipart frames from
    the very start of the stream before relaying anything to the
    browser. bv-live's own camera feed opens a fresh connection per
    direction switch (see live/app.py's stream_camera()) - and, per
    core/blackvue_client.py's own SNAPSHOT_WARMUP_FRAMES comment, "the
    camera's shared video encoder apparently needs a moment to
    actually reconfigure to the requested lens even on a brand new
    connection", so the first frame(s) served can still be showing
    whatever direction was live just before this request. bv-snap/
    bv-gps --snap already discard SNAPSHOT_WARMUP_FRAMES frames before
    *capturing* one for exactly this reason (Christer, confirmed by
    using the feature); this is the same fix for bv-live's own
    continuous feed, which had no such warm-up of its own until
    Christer separately reported "more than 2 seconds before it switch
    picture" here too. Parses complete frames itself (same
    Content-Length-header approach as
    BlackVueClient._read_one_mjpeg_frame()) only during this discard
    phase, then falls back to pure byte passthrough for the rest of
    the stream's lifetime - unlike that bounded, single-frame capture,
    this needs to keep the connection open afterward and relay
    everything else unmodified, so it can't just delegate there.
    Bounded by DISCARD_MAX_BYTES so a stream that never satisfies the
    expected framing (see that constant's own comment) degrades to
    "some stale frames get through", not "no image ever appears".

    `stop_event` is checked once per chunk read, not while blocked
    inside response.read() itself - under normal operation the camera
    keeps sending frames continuously, so in practice this still
    notices a shutdown within about one frame's worth of delay, same
    as rendered_frame_stream() above, even though there's no hard
    guarantee if the camera itself ever stops sending data mid-stream.
    """

    try:
        buffer = b""
        frames_discarded = 0
        bytes_read_while_discarding = 0

        while frames_discarded < discard_frames:
            if stop_event is not None and stop_event.is_set():
                return
            chunk = response.read(chunk_size)
            if not chunk:
                return
            buffer += chunk
            bytes_read_while_discarding += len(chunk)
            if bytes_read_while_discarding >= DISCARD_MAX_BYTES:
                break

            while frames_discarded < discard_frames:
                header_end = buffer.find(b"\r\n\r\n")
                if header_end == -1:
                    break

                length = None
                header_text = buffer[:header_end].decode(
                    "ascii", errors="replace"
                )
                for line in header_text.splitlines():
                    if line.lower().startswith("content-length:"):
                        length = int(line.split(":", 1)[1].strip())
                        break

                if length is None:
                    break

                frame_end = header_end + 4 + length
                if len(buffer) < frame_end:
                    break

                buffer = buffer[frame_end:]
                frames_discarded += 1

        if buffer:
            yield buffer

        while stop_event is None or not stop_event.is_set():
            chunk = response.read(chunk_size)
            if not chunk:
                break
            yield chunk
    finally:
        response.close()
