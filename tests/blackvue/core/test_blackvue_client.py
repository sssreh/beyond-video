from datetime import datetime
from pathlib import PurePosixPath
from urllib.error import HTTPError
from urllib.error import URLError

import pytest

from blackvue.core import blackvue_client as blackvue_client_module
from blackvue.core.blackvue_client import SNAPSHOT_WARMUP_FRAMES
from blackvue.core.blackvue_client import SNAPSHOT_WARMUP_FRAMES_INTERIOR_MULTIPLIER
from blackvue.core.blackvue_client import BlackVueClient
from blackvue.core.blackvue_client import NoGpsDataError
from blackvue.domain.vod_entry import VodEntry


def _entry(path: str) -> VodEntry:
    return VodEntry(
        timestamp=datetime(2026, 1, 1),
        path=PurePosixPath(path),
        fields={},
    )


class _FakeResponse:
    def __init__(self, data: bytes, headers=None):
        self._data = data
        self._offset = 0
        self.headers = headers or {}

    def read(self, size=-1):
        if size is None or size < 0:
            chunk = self._data[self._offset:]
            self._offset = len(self._data)
            return chunk

        chunk = self._data[self._offset:self._offset + size]
        self._offset += len(chunk)
        return chunk

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return False


def _fake_urlopen(content: bytes):
    """Build a fake urlopen() that serves `content` for every request:
    a plain string url (the _get() codepath), a HEAD Request (size()),
    or a GET Request with an optional Range header (chunked video
    download / resume)."""

    def urlopen(request_or_url, timeout=None):
        if isinstance(request_or_url, str):
            return _FakeResponse(content)

        if request_or_url.get_method() == "HEAD":
            return _FakeResponse(
                b"", headers={"Content-Length": str(len(content))}
            )

        range_header = request_or_url.get_header("Range")
        if range_header:
            start = int(range_header.split("=", 1)[1].rstrip("-"))
            return _FakeResponse(content[start:])

        return _FakeResponse(content)

    return urlopen


def test_download_video_reports_bytes_via_on_bytes(monkeypatch, tmp_path):
    # Bigger than one 64KB chunk so on_bytes fires more than once.
    video_bytes = b"x" * (64 * 1024 * 2 + 100)
    monkeypatch.setattr(
        blackvue_client_module, "urlopen", _fake_urlopen(video_bytes)
    )

    client = BlackVueClient("http://camera")
    entry = _entry("/Record/20260101_000000_NF.mp4")
    destination = tmp_path / "20260101_000000_NF.mp4"

    reported = []
    changed = client.download(entry, destination, on_bytes=reported.append)

    assert changed is True
    assert destination.read_bytes() == video_bytes
    assert len(reported) > 1
    assert sum(reported) == len(video_bytes)


def test_download_metadata_reports_bytes_via_on_bytes(monkeypatch, tmp_path):
    data = b"[123456]$GPRMC,..."
    monkeypatch.setattr(blackvue_client_module, "urlopen", _fake_urlopen(data))

    client = BlackVueClient("http://camera")
    entry = _entry("/Record/20260101_000000_N.gps")
    destination = tmp_path / "20260101_000000_N.gps"

    reported = []
    changed = client.download(entry, destination, on_bytes=reported.append)

    assert changed is True
    assert reported == [len(data)]


def test_download_without_on_bytes_still_works(monkeypatch, tmp_path):
    monkeypatch.setattr(
        blackvue_client_module, "urlopen", _fake_urlopen(b"hello")
    )

    client = BlackVueClient("http://camera")
    entry = _entry("/Record/20260101_000000_N.gps")
    destination = tmp_path / "20260101_000000_N.gps"

    changed = client.download(entry, destination)

    assert changed is True
    assert destination.read_bytes() == b"hello"


def test_download_skips_metadata_already_on_disk_without_reporting(
    monkeypatch, tmp_path
):
    monkeypatch.setattr(
        blackvue_client_module, "urlopen", _fake_urlopen(b"hello")
    )

    client = BlackVueClient("http://camera")
    entry = _entry("/Record/20260101_000000_N.gps")
    destination = tmp_path / "20260101_000000_N.gps"
    destination.write_bytes(b"already here")

    reported = []
    changed = client.download(entry, destination, on_bytes=reported.append)

    assert changed is False
    assert reported == []


def test_live_gps_parses_a_gps_reading_from_the_stream(monkeypatch):
    stream = (
        b'--ptaboundary\r\nContent-Type: application/json\r\n\r\n'
        b'{"3G":{"FrontRear":1, "LeftRight":2, "UpperLower":3}}\r\n'
        b'--ptaboundary\r\nContent-Type: application/json\r\n\r\n'
        b'{"GPS":{"LATITUDE":59.334591, "LONGITUDE":18.063240}}\r\n'
    )
    monkeypatch.setattr(blackvue_client_module, "urlopen", _fake_urlopen(stream))

    client = BlackVueClient("http://camera")
    fix = client.live_gps()

    assert (fix.latitude, fix.longitude) == (59.334591, 18.063240)
    assert fix.has_fix is True


def test_live_gps_recovers_from_a_gps_object_split_across_reads(monkeypatch):
    # live_gps() reads in fixed 4096-byte chunks (see its own
    # docstring). Padding the stream well past that with 3G-only
    # noise before the GPS object forces _FakeResponse.read(4096) to
    # hand it back across more than one call, exercising live_gps()'s
    # own re-parse-a-growing-buffer loop exactly like a real chunked
    # socket read splitting a GPS object mid-way would.
    padding = b'{"3G":{"FrontRear":1, "LeftRight":2, "UpperLower":3}}' * 200
    stream = padding + b'{"GPS":{"LATITUDE":59.334591, "LONGITUDE":18.063240}}\r\n'
    assert len(padding) > 4096
    monkeypatch.setattr(blackvue_client_module, "urlopen", _fake_urlopen(stream))

    client = BlackVueClient("http://camera")
    fix = client.live_gps()

    assert (fix.latitude, fix.longitude) == (59.334591, 18.063240)


def test_live_gps_returns_a_zero_reading_as_no_fix_rather_than_erroring(
    monkeypatch,
):
    stream = b'{"GPS":{"LATITUDE":0.0, "LONGITUDE":0.0}}\r\n'
    monkeypatch.setattr(blackvue_client_module, "urlopen", _fake_urlopen(stream))

    client = BlackVueClient("http://camera")
    fix = client.live_gps()

    assert fix.has_fix is False


def test_live_gps_raises_when_the_stream_never_yields_a_gps_object(
    monkeypatch,
):
    stream = b'{"3G":{"FrontRear":1, "LeftRight":2, "UpperLower":3}}\r\n'
    monkeypatch.setattr(blackvue_client_module, "urlopen", _fake_urlopen(stream))

    client = BlackVueClient("http://camera")

    with pytest.raises(NoGpsDataError):
        client.live_gps()


def test_open_stream_returns_the_raw_response_for_reading_in_chunks(monkeypatch):
    data = b"chunk-one" + b"chunk-two"
    monkeypatch.setattr(blackvue_client_module, "urlopen", _fake_urlopen(data))

    client = BlackVueClient("http://camera")
    response = client.open_stream("/blackvue_livedata.cgi")

    first = response.read(9)
    second = response.read(9)

    assert first == b"chunk-one"
    assert second == b"chunk-two"


def test_open_stream_url_includes_the_given_path(monkeypatch):
    seen_urls = []

    def urlopen(request_or_url, timeout=None):
        seen_urls.append(request_or_url)
        return _FakeResponse(b"")

    monkeypatch.setattr(blackvue_client_module, "urlopen", urlopen)

    client = BlackVueClient("http://camera")
    client.open_stream("/blackvue_live.cgi?direction=R")

    assert seen_urls == ["http://camera/blackvue_live.cgi?direction=R"]


def test_probe_returns_true_on_success(monkeypatch):
    monkeypatch.setattr(
        blackvue_client_module, "urlopen", _fake_urlopen(b"some content")
    )

    client = BlackVueClient("http://camera")

    assert client.probe("/Record/20260101_000000_N.gps") is True


def test_probe_returns_false_on_http_error(monkeypatch):
    def urlopen(request_or_url, timeout=None):
        raise HTTPError("http://camera/x", 404, "Not Found", {}, None)

    monkeypatch.setattr(blackvue_client_module, "urlopen", urlopen)

    client = BlackVueClient("http://camera")

    assert client.probe("/Record/20260101_000000_N.gps") is False


def test_probe_does_not_swallow_network_level_errors(monkeypatch):
    def urlopen(request_or_url, timeout=None):
        raise URLError("connection refused")

    monkeypatch.setattr(blackvue_client_module, "urlopen", urlopen)

    client = BlackVueClient("http://camera")

    with pytest.raises(URLError):
        client.probe("/Record/20260101_000000_N.gps")


# ---------------------------------------------------------------------------
# snapshot() - Christer: "I would like to have a snap function that takes
# 1 snapshot for camera F, R and I."
#
# blackvue_live.cgi is a never-closing multipart/x-mixed-replace MJPEG
# stream (same shape as blackvue_livedata.cgi's own GPS feed - see
# live_gps()'s tests above), so a realistic fake response here has to be
# framed as one multipart part (boundary/Content-Type/Content-Length
# headers, a blank line, then the image bytes) - a bare byte string with
# no framing at all (what these tests originally used) doesn't exercise
# _read_one_mjpeg_frame()'s actual parsing, and let a real hang (Christer:
# "looks like both commands hang") ship undetected.
# ---------------------------------------------------------------------------


def _mjpeg_part(data: bytes) -> bytes:
    """Build one multipart/x-mixed-replace part the way blackvue_live.cgi
    itself does - boundary line, headers (including the Content-Length
    _read_one_mjpeg_frame() parses to know where the frame ends), a blank
    line, then the raw image bytes and a trailing CRLF."""

    header = (
        f"--bvcamboundary\r\n"
        "Content-Type: image/jpeg\r\n"
        f"Content-Length: {len(data)}\r\n\r\n"
    ).encode("ascii")
    return header + data + b"\r\n"


def _mjpeg_stream(*frames: bytes) -> bytes:
    """Concatenate several _mjpeg_part() frames into one fake stream -
    snapshot() discards SNAPSHOT_WARMUP_FRAMES frames before capturing
    (see that constant's own comment: a camera's shared encoder can
    keep serving the previous direction for a moment after a switch,
    Christer: "I know sometimes when you switch direction it shows
    the previous direction for a short while"), so a realistic fake
    response needs more than one frame available."""

    return b"".join(frames)


def _warmup_frames(count: int = SNAPSHOT_WARMUP_FRAMES) -> tuple[bytes, ...]:
    """`count` throwaway parts to prepend before the "real" frame in a
    fake stream that goes through snapshot() itself (as opposed to
    calling _read_one_mjpeg_frame() directly with an explicit
    discard=). Defaults to SNAPSHOT_WARMUP_FRAMES - built from the
    live constant rather than a hardcoded count so these tests don't
    silently go stale the next time Christer reports duplicates and
    the default gets retuned again (it already went 2 -> 8 once -
    Christer: "i still get duplicates, well almost duplicates, i can
    se a small difference"). Pass an explicit `count` for direction
    "I", which discards SNAPSHOT_WARMUP_FRAMES_INTERIOR_MULTIPLIER
    times as many (see that constant's own comment - Christer's own
    2-camera setup kept landing a near-duplicate of "R" for "I" even
    after the 2 -> 8 raise)."""

    return tuple(_mjpeg_part(f"warmup-{i}".encode()) for i in range(count))


def _warmup_frames_for(direction: str) -> tuple[bytes, ...]:
    """Same discard-count logic snapshot() itself uses per direction -
    see _warmup_frames()'s own docstring for why "I" needs more."""

    count = SNAPSHOT_WARMUP_FRAMES
    if direction == "I":
        count *= SNAPSHOT_WARMUP_FRAMES_INTERIOR_MULTIPLIER
    return _warmup_frames(count)


def test_snapshot_default_directions_is_f_r_i():
    from blackvue.core.blackvue_client import SNAPSHOT_DIRECTIONS

    assert SNAPSHOT_DIRECTIONS == ("F", "R", "I")


def test_snapshot_fetches_every_default_direction(monkeypatch):
    seen_urls = []

    def urlopen(request_or_url, timeout=None):
        seen_urls.append(request_or_url)
        # Distinguish the three responses so per-direction dict keys
        # can be checked against genuinely different bytes, not just
        # three copies of the same fake JPEG. _warmup_frames_for()
        # matches snapshot()'s own per-direction discard count ("I"
        # needs more - see SNAPSHOT_WARMUP_FRAMES_INTERIOR_MULTIPLIER's
        # own comment).
        direction = request_or_url.rsplit("=", 1)[-1]
        stream = _mjpeg_stream(
            *_warmup_frames_for(direction),
            _mjpeg_part(f"jpeg-bytes-{direction}".encode()),
        )
        return _FakeResponse(stream)

    monkeypatch.setattr(blackvue_client_module, "urlopen", urlopen)

    client = BlackVueClient("http://camera")
    result = client.snapshot()

    assert set(result.keys()) == {"F", "R", "I"}
    assert result["F"] == b"jpeg-bytes-F"
    assert result["R"] == b"jpeg-bytes-R"
    assert result["I"] == b"jpeg-bytes-I"
    assert seen_urls == [
        "http://camera/blackvue_live.cgi?direction=F",
        "http://camera/blackvue_live.cgi?direction=R",
        "http://camera/blackvue_live.cgi?direction=I",
    ]


def test_snapshot_never_hangs_on_a_stream_that_keeps_sending_more_frames(
    monkeypatch,
):
    """Regression test for the actual bug shipped in the first cut of
    this feature (Christer: "looks like both commands hang"):
    blackvue_live.cgi never closes its connection, so a fake response
    that keeps yielding data past the frame snapshot() actually wants
    (simulating the camera moving on to yet more frames) must still
    return once that frame's been captured, not block waiting for the
    stream to end."""

    kept_frame = _mjpeg_part(b"jpeg-bytes-F")
    # SNAPSHOT_WARMUP_FRAMES warm-up frames (discarded) ahead of the
    # one that's kept, then one more appended after it - a real
    # never-closing stream would keep going forever; snapshot() must
    # never read this far.
    stream = _mjpeg_stream(
        *_warmup_frames(),
        kept_frame,
        _mjpeg_part(b"jpeg-bytes-F-extra"),
    )

    monkeypatch.setattr(blackvue_client_module, "urlopen", _fake_urlopen(stream))

    client = BlackVueClient("http://camera")
    result = client.snapshot(("F",))

    assert result["F"] == b"jpeg-bytes-F"


def test_snapshot_discards_warmup_frames_before_capturing(monkeypatch):
    """Direct regression test for Christer's own report after trying
    the feature: "I know sometimes when you switch direction it shows
    the previous direction for a short while." A fake stream whose
    first SNAPSHOT_WARMUP_FRAMES frames are clearly the "wrong"
    (previous-direction) image and whose last is the real one -
    snapshot() must return the last, not the first."""

    stale_frames = tuple(
        _mjpeg_part(f"stale-previous-direction-frame-{i}".encode())
        for i in range(SNAPSHOT_WARMUP_FRAMES)
    )
    stream = _mjpeg_stream(
        *stale_frames,
        _mjpeg_part(b"real-frame-for-this-direction"),
    )

    monkeypatch.setattr(blackvue_client_module, "urlopen", _fake_urlopen(stream))

    client = BlackVueClient("http://camera")
    result = client.snapshot(("R",))

    assert result["R"] == b"real-frame-for-this-direction"


def test_snapshot_accepts_an_explicit_direction_subset(monkeypatch):
    stream = _mjpeg_stream(*_warmup_frames(), _mjpeg_part(b"jpeg-bytes"))
    monkeypatch.setattr(blackvue_client_module, "urlopen", _fake_urlopen(stream))

    client = BlackVueClient("http://camera")
    result = client.snapshot(("F",))

    assert set(result.keys()) == {"F"}


def test_snapshot_drops_a_direction_that_errors_rather_than_failing_the_call(
    monkeypatch,
):
    # Christer's own firmware-endpoint scan found direction=I returns
    # a "Valid" HTTP response but never actually displayed a real
    # image for it on his hardware - snapshot() has to tolerate a
    # direction erroring (here: I) without losing F/R.
    stream = _mjpeg_stream(*_warmup_frames(), _mjpeg_part(b"jpeg-bytes"))

    def urlopen(request_or_url, timeout=None):
        if request_or_url.endswith("direction=I"):
            raise HTTPError(request_or_url, 404, "Not Found", {}, None)
        return _FakeResponse(stream)

    monkeypatch.setattr(blackvue_client_module, "urlopen", urlopen)

    client = BlackVueClient("http://camera")
    result = client.snapshot()

    assert set(result.keys()) == {"F", "R"}


def test_snapshot_drops_a_direction_with_an_empty_body(monkeypatch):
    # I's own three-frame stream ends on an empty frame (still a
    # complete, parseable part - just zero image bytes); F/R end on
    # real data.
    empty_stream = _mjpeg_stream(*_warmup_frames(), _mjpeg_part(b""))
    real_stream = _mjpeg_stream(*_warmup_frames(), _mjpeg_part(b"jpeg-bytes"))

    def urlopen(request_or_url, timeout=None):
        if request_or_url.endswith("direction=I"):
            return _FakeResponse(empty_stream)
        return _FakeResponse(real_stream)

    monkeypatch.setattr(blackvue_client_module, "urlopen", urlopen)

    client = BlackVueClient("http://camera")
    result = client.snapshot()

    assert set(result.keys()) == {"F", "R"}


def test_snapshot_drops_a_direction_that_never_sends_a_complete_frame(
    monkeypatch,
):
    # A response that never includes a Content-Length header (or gets
    # cut off before delivering that many bytes) at all - simulates a
    # direction the camera doesn't really support answering with
    # something other than a real MJPEG part.
    real_stream = _mjpeg_stream(*_warmup_frames(), _mjpeg_part(b"jpeg-bytes"))

    def urlopen(request_or_url, timeout=None):
        if request_or_url.endswith("direction=I"):
            return _FakeResponse(b"not a real mjpeg part")
        return _FakeResponse(real_stream)

    monkeypatch.setattr(blackvue_client_module, "urlopen", urlopen)

    client = BlackVueClient("http://camera")
    result = client.snapshot()

    assert set(result.keys()) == {"F", "R"}


def test_snapshot_returns_an_empty_dict_when_every_direction_fails(monkeypatch):
    def urlopen(request_or_url, timeout=None):
        raise HTTPError(request_or_url, 404, "Not Found", {}, None)

    monkeypatch.setattr(blackvue_client_module, "urlopen", urlopen)

    client = BlackVueClient("http://camera")
    result = client.snapshot()

    assert result == {}


def test_snapshot_uses_snapshot_warmup_frames_as_its_discard_count(monkeypatch):
    from blackvue.core.blackvue_client import SNAPSHOT_WARMUP_FRAMES

    seen_discard = []
    real_read_one = BlackVueClient._read_one_mjpeg_frame

    def spy(self, path, *, discard=0):
        seen_discard.append(discard)
        return real_read_one(self, path, discard=discard)

    monkeypatch.setattr(BlackVueClient, "_read_one_mjpeg_frame", spy)
    monkeypatch.setattr(
        blackvue_client_module,
        "urlopen",
        _fake_urlopen(
            _mjpeg_stream(*[_mjpeg_part(b"x")] * (SNAPSHOT_WARMUP_FRAMES + 1))
        ),
    )

    client = BlackVueClient("http://camera")
    client.snapshot(("F",))

    assert seen_discard == [SNAPSHOT_WARMUP_FRAMES]


def test_snapshot_doubles_warmup_frames_for_interior_direction(monkeypatch):
    """Christer, after raising SNAPSHOT_WARMUP_FRAMES to 8 didn't fully
    fix things on his 2-camera setup: "R and I are almost identical,
    just a little different since it differs a few seconds" - and
    asked to double the warm-up specifically before "I". Regression
    test for that: "I" alone should discard
    SNAPSHOT_WARMUP_FRAMES_INTERIOR_MULTIPLIER times as many frames as
    every other direction, not the shared default."""

    seen_discard = []
    real_read_one = BlackVueClient._read_one_mjpeg_frame

    def spy(self, path, *, discard=0):
        seen_discard.append(discard)
        return real_read_one(self, path, discard=discard)

    monkeypatch.setattr(BlackVueClient, "_read_one_mjpeg_frame", spy)
    monkeypatch.setattr(
        blackvue_client_module,
        "urlopen",
        _fake_urlopen(
            _mjpeg_stream(
                *[_mjpeg_part(b"x")]
                * (SNAPSHOT_WARMUP_FRAMES * SNAPSHOT_WARMUP_FRAMES_INTERIOR_MULTIPLIER + 1)
            )
        ),
    )

    client = BlackVueClient("http://camera")
    client.snapshot(("I",))

    assert seen_discard == [
        SNAPSHOT_WARMUP_FRAMES * SNAPSHOT_WARMUP_FRAMES_INTERIOR_MULTIPLIER
    ]


# ---------------------------------------------------------------------------
# _read_one_mjpeg_frame()'s own discard= parameter, tested directly rather
# than only through snapshot() - see SNAPSHOT_WARMUP_FRAMES's own comment.
# ---------------------------------------------------------------------------


def test_read_one_mjpeg_frame_discard_zero_returns_the_first_frame(monkeypatch):
    stream = _mjpeg_stream(_mjpeg_part(b"first"), _mjpeg_part(b"second"))
    monkeypatch.setattr(blackvue_client_module, "urlopen", _fake_urlopen(stream))

    client = BlackVueClient("http://camera")
    result = client._read_one_mjpeg_frame("/blackvue_live.cgi?direction=F", discard=0)

    assert result == b"first"


def test_read_one_mjpeg_frame_discard_skips_that_many_frames(monkeypatch):
    stream = _mjpeg_stream(
        _mjpeg_part(b"discard-1"),
        _mjpeg_part(b"discard-2"),
        _mjpeg_part(b"keep-this-one"),
        _mjpeg_part(b"never-read"),
    )
    monkeypatch.setattr(blackvue_client_module, "urlopen", _fake_urlopen(stream))

    client = BlackVueClient("http://camera")
    result = client._read_one_mjpeg_frame("/blackvue_live.cgi?direction=F", discard=2)

    assert result == b"keep-this-one"


def test_read_one_mjpeg_frame_discard_parses_frames_delivered_one_byte_at_a_time(
    monkeypatch,
):
    """The frame-discarding loop has to drain every complete frame
    already sitting in the buffer before asking the network for more
    (see _read_one_mjpeg_frame()'s own comment on why) - a response
    that trickles in one byte per read() call is the worst case for
    that, and is exactly the shape that first exposed the bug in this
    fix during development (a naive implementation returned "no
    complete frame received" even though the wanted frame had already
    fully arrived)."""

    class _ByteAtATimeResponse(_FakeResponse):
        def read(self, size=-1):
            return super().read(1) if size != 0 else b""

    stream = _mjpeg_stream(
        _mjpeg_part(b"discard-1"),
        _mjpeg_part(b"discard-2"),
        _mjpeg_part(b"keep-this-one"),
    )

    def urlopen(request_or_url, timeout=None):
        return _ByteAtATimeResponse(stream)

    monkeypatch.setattr(blackvue_client_module, "urlopen", urlopen)

    client = BlackVueClient("http://camera")
    result = client._read_one_mjpeg_frame("/blackvue_live.cgi?direction=F", discard=2)

    assert result == b"keep-this-one"


def test_read_one_mjpeg_frame_raises_when_stream_never_reaches_discard_count(
    monkeypatch,
):
    # Only one frame ever arrives, but discard=2 needs three.
    stream = _mjpeg_part(b"only-one")
    monkeypatch.setattr(blackvue_client_module, "urlopen", _fake_urlopen(stream))

    client = BlackVueClient("http://camera")

    with pytest.raises(RuntimeError, match="no complete frame received"):
        client._read_one_mjpeg_frame("/blackvue_live.cgi?direction=F", discard=2)
