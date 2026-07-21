from datetime import datetime
from pathlib import PurePosixPath

from blackvue.core import blackvue_client as blackvue_client_module
from blackvue.core.blackvue_client import BlackVueClient
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
