from datetime import datetime
from pathlib import PurePosixPath
from urllib.error import HTTPError
from urllib.error import URLError

import pytest

from blackvue.core import blackvue_client as blackvue_client_module
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
# ---------------------------------------------------------------------------


def test_snapshot_default_directions_is_f_r_i():
    from blackvue.core.blackvue_client import SNAPSHOT_DIRECTIONS

    assert SNAPSHOT_DIRECTIONS == ("F", "R", "I")


def test_snapshot_fetches_every_default_direction(monkeypatch):
    seen_urls = []

    def urlopen(request_or_url, timeout=None):
        seen_urls.append(request_or_url)
        # Distinguish the three responses so per-direction dict keys
        # can be checked against genuinely different bytes, not just
        # three copies of the same fake JPEG.
        direction = request_or_url.rsplit("=", 1)[-1]
        return _FakeResponse(f"jpeg-bytes-{direction}".encode())

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


def test_snapshot_accepts_an_explicit_direction_subset(monkeypatch):
    monkeypatch.setattr(
        blackvue_client_module, "urlopen", _fake_urlopen(b"jpeg-bytes")
    )

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
    def urlopen(request_or_url, timeout=None):
        if request_or_url.endswith("direction=I"):
            raise HTTPError(request_or_url, 404, "Not Found", {}, None)
        return _FakeResponse(b"jpeg-bytes")

    monkeypatch.setattr(blackvue_client_module, "urlopen", urlopen)

    client = BlackVueClient("http://camera")
    result = client.snapshot()

    assert set(result.keys()) == {"F", "R"}


def test_snapshot_drops_a_direction_with_an_empty_body(monkeypatch):
    def urlopen(request_or_url, timeout=None):
        if request_or_url.endswith("direction=I"):
            return _FakeResponse(b"")
        return _FakeResponse(b"jpeg-bytes")

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
