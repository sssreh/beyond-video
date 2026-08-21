import threading

from PIL import Image

from blackvue.live.mjpeg import BOUNDARY
from blackvue.live.mjpeg import encode_jpeg_part
from blackvue.live.mjpeg import relay_raw_stream
from blackvue.live.mjpeg import rendered_frame_stream


def test_encode_jpeg_part_starts_with_the_boundary_marker():
    image = Image.new("RGB", (4, 4), (255, 0, 0))

    part = encode_jpeg_part(image)

    assert part.startswith(f"--{BOUNDARY}\r\n".encode("ascii"))
    assert b"Content-Type: image/jpeg\r\n" in part
    assert part.endswith(b"\r\n")


def test_encode_jpeg_part_content_length_matches_the_actual_jpeg_bytes():
    image = Image.new("RGB", (8, 8), (0, 255, 0))

    part = encode_jpeg_part(image)

    header, _, rest = part.partition(b"\r\n\r\n")
    length_line = [
        line for line in header.split(b"\r\n") if line.startswith(b"Content-Length")
    ][0]
    declared_length = int(length_line.split(b":")[1].strip())

    # rest is "<jpeg bytes>\r\n" - the trailing CRLF isn't part of the
    # declared Content-Length.
    jpeg_bytes = rest[:-2]
    assert len(jpeg_bytes) == declared_length


def test_rendered_frame_stream_yields_encoded_frames():
    calls = {"count": 0}

    def render():
        calls["count"] += 1
        return Image.new("RGB", (2, 2), (0, 0, 255))

    stream = rendered_frame_stream(render, fps=1000.0)  # fast, no real sleep needed

    first = next(stream)
    second = next(stream)

    assert first.startswith(f"--{BOUNDARY}\r\n".encode("ascii"))
    assert second.startswith(f"--{BOUNDARY}\r\n".encode("ascii"))
    assert calls["count"] == 2


class _FakeUpstream:
    def __init__(self, chunks):
        self._chunks = list(chunks)
        self.closed = False

    def read(self, size):
        if not self._chunks:
            return b""
        return self._chunks.pop(0)

    def close(self):
        self.closed = True


def test_relay_raw_stream_yields_every_chunk_then_closes():
    upstream = _FakeUpstream([b"one", b"two", b""])

    chunks = list(relay_raw_stream(upstream))

    assert chunks == [b"one", b"two"]
    assert upstream.closed is True


def test_relay_raw_stream_closes_upstream_even_if_iteration_stops_early():
    upstream = _FakeUpstream([b"one", b"two", b"three", b""])

    generator = relay_raw_stream(upstream)
    next(generator)
    generator.close()

    assert upstream.closed is True


def test_rendered_frame_stream_stops_once_stop_event_is_set():
    # Regression test for the bv-live Ctrl-C hang Christer reported
    # ("The only way to take it down is to use task manager") - with
    # no stop_event at all, this generator ran forever regardless of
    # server shutdown. Sets the event from inside `render()` itself
    # (simulating the app's lifespan shutdown firing mid-stream) and
    # confirms the generator ends on its own right after, rather than
    # yielding one more frame or looping forever.
    stop_event = threading.Event()
    calls = {"count": 0}

    def render():
        calls["count"] += 1
        if calls["count"] == 2:
            stop_event.set()
        return Image.new("RGB", (2, 2), (0, 0, 255))

    frames = list(
        rendered_frame_stream(render, fps=1000.0, stop_event=stop_event)
    )

    assert calls["count"] == 2
    assert len(frames) == 2


def test_rendered_frame_stream_stops_immediately_if_stop_event_already_set():
    stop_event = threading.Event()
    stop_event.set()
    calls = {"count": 0}

    def render():
        calls["count"] += 1
        return Image.new("RGB", (2, 2), (0, 0, 255))

    frames = list(
        rendered_frame_stream(render, fps=1000.0, stop_event=stop_event)
    )

    assert calls["count"] == 0
    assert frames == []


def test_relay_raw_stream_stops_once_stop_event_is_set():
    upstream = _FakeUpstream([b"one", b"two", b"three", b""])
    stop_event = threading.Event()

    def stopping_iteration():
        chunks = []
        for chunk in relay_raw_stream(upstream, stop_event=stop_event):
            chunks.append(chunk)
            if chunk == b"one":
                stop_event.set()
        return chunks

    chunks = stopping_iteration()

    assert chunks == [b"one"]
    assert upstream.closed is True


def test_relay_raw_stream_stops_immediately_if_stop_event_already_set():
    upstream = _FakeUpstream([b"one", b"two", b""])
    stop_event = threading.Event()
    stop_event.set()

    chunks = list(relay_raw_stream(upstream, stop_event=stop_event))

    assert chunks == []
    assert upstream.closed is True


def _frame(body: bytes) -> bytes:
    """Build one complete multipart frame in the same shape
    encode_jpeg_part() produces (and the discard-phase parser in
    relay_raw_stream() expects) - used to simulate the camera's own
    MJPEG framing in the discard_frames tests below."""

    header = (
        "--fakeboundary\r\n"
        "Content-Type: image/jpeg\r\n"
        f"Content-Length: {len(body)}\r\n\r\n"
    ).encode("ascii")
    return header + body + b"\r\n"


def test_relay_raw_stream_discards_the_requested_frames_then_relays_the_rest():
    # Regression test for Christer's report - "it sometimes can take
    # more than 2 seconds before it switch picture" when changing
    # ?direction= on bv-live's own camera feed. discard_frames=2 here
    # stands in for SNAPSHOT_WARMUP_FRAMES worth of stale frames from
    # the *previous* direction that a fresh connection can still be
    # serving - relay_raw_stream() should drop exactly that many
    # complete frames and then relay everything after byte-for-byte,
    # untouched.
    frames = [_frame(b"AAA"), _frame(b"BBB"), _frame(b"CCC")]
    tail = b"TAILDATA-not-a-frame-at-all"
    upstream = _FakeUpstream([b"".join(frames) + tail, b""])

    chunks = list(relay_raw_stream(upstream, discard_frames=2))

    # The discard parser cuts each dropped frame right after its body,
    # leaving that frame's own trailing "\r\n" attached ahead of
    # whatever comes next - harmless (multipart parts are
    # conventionally CRLF-prefixed anyway, per RFC 2046), but part of
    # the real observable output, so assert it explicitly rather than
    # assuming it gets stripped.
    assert b"".join(chunks) == b"\r\n" + _frame(b"CCC") + tail
    assert upstream.closed is True


def test_relay_raw_stream_discard_frames_zero_relays_everything_unchanged():
    # discard_frames defaults to 0 for every pre-existing caller of
    # relay_raw_stream() - confirms the new parameter is a true no-op
    # in that case, even when the data looks like real frames.
    frames = [_frame(b"AAA"), _frame(b"BBB")]
    upstream = _FakeUpstream([b"".join(frames), b""])

    chunks = list(relay_raw_stream(upstream, discard_frames=0))

    assert b"".join(chunks) == b"".join(frames)
    assert upstream.closed is True


def test_relay_raw_stream_handles_frames_split_across_multiple_reads():
    frame = _frame(b"split-me-across-two-chunks")
    midpoint = len(frame) // 2
    upstream = _FakeUpstream([frame[:midpoint], frame[midpoint:], b"AFTER", b""])

    chunks = list(relay_raw_stream(upstream, discard_frames=1))

    # Same leftover-CRLF behavior as above - the discarded frame's own
    # trailing "\r\n" precedes "AFTER".
    assert b"".join(chunks) == b"\r\nAFTER"
    assert upstream.closed is True


def test_relay_raw_stream_discard_gives_up_if_the_stream_ends_early():
    # Camera closes the connection before ever completing
    # discard_frames worth of frames - relay_raw_stream() should just
    # end cleanly (no image, same as before this feature existed)
    # rather than hanging.
    upstream = _FakeUpstream([_frame(b"AAA"), b""])

    chunks = list(relay_raw_stream(upstream, discard_frames=3))

    assert chunks == []
    assert upstream.closed is True


def test_relay_raw_stream_discard_stops_immediately_if_stop_event_already_set():
    upstream = _FakeUpstream([_frame(b"AAA"), b""])
    stop_event = threading.Event()
    stop_event.set()

    chunks = list(
        relay_raw_stream(upstream, discard_frames=2, stop_event=stop_event)
    )

    assert chunks == []
    assert upstream.closed is True


def test_relay_raw_stream_discard_gives_up_after_max_bytes_and_still_relays(
    monkeypatch,
):
    # Safety bound for malformed/unexpected framing (see
    # DISCARD_MAX_BYTES's own comment) - shrink the threshold so the
    # test doesn't need to push multiple real megabytes through a fake
    # stream. Garbage with no blank-line header terminator can never
    # satisfy the discard parser, so this exercises the bound itself
    # rather than a real camera response.
    monkeypatch.setattr("blackvue.live.mjpeg.DISCARD_MAX_BYTES", 10)
    garbage = b"no header terminator anywhere in here"
    upstream = _FakeUpstream([garbage, b"more-after", b""])

    chunks = list(relay_raw_stream(upstream, discard_frames=5))

    assert b"".join(chunks) == garbage + b"more-after"
    assert upstream.closed is True
