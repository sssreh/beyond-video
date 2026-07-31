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
