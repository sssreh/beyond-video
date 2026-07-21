from datetime import datetime
from pathlib import PurePosixPath

from blackvue.core.blackvue_camera import BlackVueCamera
from blackvue.domain.recording import Recording
from blackvue.domain.vod_entry import VodEntry


def _entry(path: str) -> VodEntry:
    return VodEntry(
        timestamp=datetime(2026, 1, 1),
        path=PurePosixPath(path),
        fields={},
    )


class _FakeClient:
    def __init__(self):
        self.calls = []

    def download(self, entry, destination, *, on_bytes=None):
        self.calls.append((entry, destination, on_bytes))
        if on_bytes is not None:
            on_bytes(123)
        return True


def test_download_passes_on_bytes_through_to_every_entry(tmp_path):
    client = _FakeClient()
    camera = BlackVueCamera(client)

    recording = Recording(
        id="20260101_000000_N",
        entries=[
            _entry("/Record/20260101_000000_NF.mp4"),
            _entry("/Record/20260101_000000_N.gps"),
        ],
    )

    reported = []
    changed = camera.download(
        recording, tmp_path, on_bytes=reported.append
    )

    assert changed is True
    assert len(client.calls) == 2
    assert reported == [123, 123]


def test_download_on_bytes_is_optional(tmp_path):
    client = _FakeClient()
    camera = BlackVueCamera(client)

    recording = Recording(
        id="20260101_000000_N",
        entries=[_entry("/Record/20260101_000000_NF.mp4")],
    )

    changed = camera.download(recording, tmp_path)

    assert changed is True
    assert client.calls[0][2] is None
