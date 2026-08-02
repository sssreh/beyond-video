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
    def __init__(self, probe_results: dict[str, bool] | None = None):
        self.calls = []
        self.probe_calls = []
        self._probe_results = probe_results or {}

    def download(self, entry, destination, *, on_bytes=None):
        self.calls.append((entry, destination, on_bytes))
        if on_bytes is not None:
            on_bytes(123)
        return True

    def probe(self, path: str) -> bool:
        self.probe_calls.append(path)
        return self._probe_results.get(path, False)


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


def test_probe_missing_sidecars_adds_entries_the_camera_actually_serves():
    client = _FakeClient(
        probe_results={
            "/Record/20260101_000000_N.gps": True,
            "/Record/20260101_000000_N.3gf": True,
        }
    )
    camera = BlackVueCamera(client)

    recording = Recording(
        id="20260101_000000_N",
        entries=[
            _entry("/Record/20260101_000000_NF.mp4"),
            _entry("/Record/20260101_000000_NR.mp4"),
        ],
    )

    found = camera.probe_missing_sidecars(recording)

    assert {entry.path.as_posix() for entry in found} == {
        "/Record/20260101_000000_N.gps",
        "/Record/20260101_000000_N.3gf",
    }
    # Mutated in place too - the caller's own entries list grew.
    assert len(recording.entries) == 4


def test_probe_missing_sidecars_skips_extensions_the_camera_does_not_serve():
    client = _FakeClient(probe_results={})
    camera = BlackVueCamera(client)

    recording = Recording(
        id="20260101_000000_N",
        entries=[_entry("/Record/20260101_000000_NF.mp4")],
    )

    found = camera.probe_missing_sidecars(recording)

    assert found == []
    assert len(recording.entries) == 1
    assert set(client.probe_calls) == {
        "/Record/20260101_000000_N.gps",
        "/Record/20260101_000000_N.3gf",
        "/Record/20260101_000000_NF.thm",
    }


def test_probe_missing_sidecars_is_a_no_op_when_already_listed():
    """For a camera/firmware combination that does list everything
    (video, .gps, .3gf, and thumbnails) in blackvue_vod.cgi's own
    response, this should cost zero extra network calls."""

    client = _FakeClient()
    camera = BlackVueCamera(client)

    recording = Recording(
        id="20260101_000000_N",
        entries=[
            _entry("/Record/20260101_000000_NF.mp4"),
            _entry("/Record/20260101_000000_N.gps"),
            _entry("/Record/20260101_000000_N.3gf"),
            _entry("/Record/20260101_000000_NF.thm"),
        ],
    )

    found = camera.probe_missing_sidecars(recording)

    assert found == []
    assert client.probe_calls == []
    assert len(recording.entries) == 4


def test_probe_missing_sidecars_only_probes_extensions_actually_missing():
    client = _FakeClient(probe_results={"/Record/20260101_000000_N.3gf": True})
    camera = BlackVueCamera(client)

    recording = Recording(
        id="20260101_000000_N",
        entries=[
            _entry("/Record/20260101_000000_NF.mp4"),
            _entry("/Record/20260101_000000_N.gps"),
        ],
    )

    found = camera.probe_missing_sidecars(recording)

    assert [entry.path.as_posix() for entry in found] == [
        "/Record/20260101_000000_N.3gf"
    ]
    # The thumbnail probe still fires too (no thumbnail entry listed),
    # it just isn't served (not in probe_results) so nothing's added.
    assert client.probe_calls == [
        "/Record/20260101_000000_N.3gf",
        "/Record/20260101_000000_NF.thm",
    ]


def test_probe_missing_sidecars_adds_thumbnails_for_every_direction_with_video():
    # blackvue_vod.cgi has, across every camera model confirmed so
    # far, only ever listed video files - thumbnails need the same
    # opportunistic probing .gps/.3gf already get, per-direction since
    # thumbnails (unlike .gps/.3gf) are one-per-camera-direction.
    client = _FakeClient(
        probe_results={
            "/Record/20260101_000000_NF.thm": True,
            "/Record/20260101_000000_NR.thm": True,
        }
    )
    camera = BlackVueCamera(client)

    recording = Recording(
        id="20260101_000000_N",
        entries=[
            _entry("/Record/20260101_000000_NF.mp4"),
            _entry("/Record/20260101_000000_NR.mp4"),
        ],
    )

    found = camera.probe_missing_sidecars(recording)

    assert {entry.path.as_posix() for entry in found} == {
        "/Record/20260101_000000_NF.thm",
        "/Record/20260101_000000_NR.thm",
    }
    assert len(recording.entries) == 4


def test_probe_missing_sidecars_only_probes_thumbnails_for_directions_with_video():
    # No rear video here - a rear thumbnail wouldn't exist on the
    # camera either, so it shouldn't even be probed for. .gps/.3gf
    # already listed here so only the thumbnail probing is exercised.
    client = _FakeClient(probe_results={"/Record/20260101_000000_NF.thm": True})
    camera = BlackVueCamera(client)

    recording = Recording(
        id="20260101_000000_N",
        entries=[
            _entry("/Record/20260101_000000_NF.mp4"),
            _entry("/Record/20260101_000000_N.gps"),
            _entry("/Record/20260101_000000_N.3gf"),
        ],
    )

    found = camera.probe_missing_sidecars(recording)

    assert [entry.path.as_posix() for entry in found] == [
        "/Record/20260101_000000_NF.thm"
    ]
    assert client.probe_calls == ["/Record/20260101_000000_NF.thm"]


def test_probe_missing_sidecars_skips_a_thumbnail_already_listed():
    client = _FakeClient()
    camera = BlackVueCamera(client)

    recording = Recording(
        id="20260101_000000_N",
        entries=[
            _entry("/Record/20260101_000000_NF.mp4"),
            _entry("/Record/20260101_000000_N.gps"),
            _entry("/Record/20260101_000000_N.3gf"),
            _entry("/Record/20260101_000000_NF.thm"),
        ],
    )

    found = camera.probe_missing_sidecars(recording)

    assert found == []
    assert client.probe_calls == []
    assert len(recording.entries) == 4


def test_probe_missing_sidecars_skips_a_thumbnail_the_camera_does_not_serve():
    client = _FakeClient(probe_results={})
    camera = BlackVueCamera(client)

    recording = Recording(
        id="20260101_000000_N",
        entries=[
            _entry("/Record/20260101_000000_NF.mp4"),
            _entry("/Record/20260101_000000_N.gps"),
            _entry("/Record/20260101_000000_N.3gf"),
        ],
    )

    found = camera.probe_missing_sidecars(recording)

    assert found == []
    assert client.probe_calls == ["/Record/20260101_000000_NF.thm"]


def test_probe_missing_sidecars_probes_an_interior_thumbnail_too():
    client = _FakeClient(probe_results={"/Record/20260101_000000_NI.thm": True})
    camera = BlackVueCamera(client)

    recording = Recording(
        id="20260101_000000_N",
        entries=[
            _entry("/Record/20260101_000000_NI.mp4"),
            _entry("/Record/20260101_000000_N.gps"),
            _entry("/Record/20260101_000000_N.3gf"),
        ],
    )

    found = camera.probe_missing_sidecars(recording)

    assert [entry.path.as_posix() for entry in found] == [
        "/Record/20260101_000000_NI.thm"
    ]
