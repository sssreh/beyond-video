import shutil
from pathlib import Path

import pytest

from blackvue.archive.asset import Asset
from blackvue.archive.asset_file import AssetFile
from blackvue.archive.recording import Recording
from blackvue.archive.recording_id import RecordingId
from blackvue.generate import media as media_module
from blackvue.generate.media import MediaInfo
from blackvue.generate.media import MediaToolError
from blackvue.generate.media import compute_span
from blackvue.generate.media import get_span
from blackvue.generate.media import load_or_compute_duration
from blackvue.generate.media import read_duration_seconds
from blackvue.generate.media import select_source
from blackvue.generate.mp4_box_reader import Mp4Info
from test_mp4_box_reader import _audio_trak_with_garbage
from test_mp4_box_reader import _build_mp4
from test_mp4_box_reader import _mvhd_v0
from test_mp4_box_reader import _video_trak


def make_recording(id_value: str, *assets: Asset) -> Recording:
    recording = Recording(id=RecordingId(id_value))

    for asset in assets:
        recording.assets[asset] = AssetFile(
            asset=asset,
            path=Path(f"/archive/{id_value}.file"),
        )

    return recording


def test_select_source_prefers_front():
    recording = make_recording(
        "20260715_133255_N", Asset.FRONT, Asset.REAR
    )

    assert select_source(recording) is recording.file(Asset.FRONT)


def test_select_source_falls_back_to_rear():
    recording = make_recording("20260715_133255_N", Asset.REAR)

    assert select_source(recording) is recording.file(Asset.REAR)


def test_select_source_returns_none_without_video():
    recording = make_recording("20260715_133255_N", Asset.GPS)

    assert select_source(recording) is None


def test_compute_span_normal_recording_matches_playback_duration():
    recording_id = RecordingId("20260715_133255_N")
    info = MediaInfo(duration_seconds=300.0, frame_rate=30.0)

    assert compute_span(recording_id, info) == 300


def test_compute_span_parking_recording_multiplies_by_frame_rate():
    # A 1-minute file at 30fps, where each frame represents one real
    # second, spans 30 minutes (1800s) of real elapsed time.
    recording_id = RecordingId("20260715_133255_P")
    info = MediaInfo(duration_seconds=60.0, frame_rate=30.0)

    assert compute_span(recording_id, info) == 1800


def test_compute_span_rounds_to_nearest_second():
    recording_id = RecordingId("20260715_133255_N")
    info = MediaInfo(duration_seconds=59.6, frame_rate=30.0)

    assert compute_span(recording_id, info) == 60


def test_compute_span_event_recording_is_not_treated_as_timelapse():
    recording_id = RecordingId("20260715_133255_E")
    info = MediaInfo(duration_seconds=60.0, frame_rate=30.0)

    assert compute_span(recording_id, info) == 60


def test_read_duration_seconds_reads_a_valid_file(tmp_path):
    duration_path = tmp_path / "20260715_133255_N.duration.txt"
    duration_path.write_text("125\n", encoding="utf-8")

    recording = Recording(id=RecordingId("20260715_133255_N"))
    recording.assets[Asset.DURATION] = AssetFile(
        asset=Asset.DURATION, path=duration_path
    )

    assert read_duration_seconds(recording) == 125


def test_read_duration_seconds_returns_none_without_the_asset():
    recording = Recording(id=RecordingId("20260715_133255_N"))

    assert read_duration_seconds(recording) is None


def test_read_duration_seconds_returns_none_for_unreadable_file(tmp_path):
    recording = Recording(id=RecordingId("20260715_133255_N"))
    recording.assets[Asset.DURATION] = AssetFile(
        asset=Asset.DURATION, path=tmp_path / "missing.duration.txt"
    )

    assert read_duration_seconds(recording) is None


def test_read_duration_seconds_returns_none_for_malformed_content(tmp_path):
    duration_path = tmp_path / "20260715_133255_N.duration.txt"
    duration_path.write_text("not-a-number\n", encoding="utf-8")

    recording = Recording(id=RecordingId("20260715_133255_N"))
    recording.assets[Asset.DURATION] = AssetFile(
        asset=Asset.DURATION, path=duration_path
    )

    assert read_duration_seconds(recording) is None


def test_load_or_compute_duration_returns_the_cached_value_without_probing(
    tmp_path, monkeypatch
):
    duration_path = tmp_path / "20260715_133255_N.duration.txt"
    duration_path.write_text("125\n", encoding="utf-8")

    recording = Recording(id=RecordingId("20260715_133255_N"))
    recording.assets[Asset.DURATION] = AssetFile(
        asset=Asset.DURATION, path=duration_path
    )

    def fail_get_span(*_args, **_kwargs):
        raise AssertionError("get_span() should not be called for a cache hit")

    monkeypatch.setattr(media_module, "get_span", fail_get_span)

    assert load_or_compute_duration(recording) == 125


def test_load_or_compute_duration_computes_and_writes_a_missing_file(
    tmp_path, monkeypatch
):
    front_path = tmp_path / "20260715_133255_N_front.mp4"
    front_path.write_bytes(b"")

    recording = Recording(id=RecordingId("20260715_133255_N"))
    recording.assets[Asset.FRONT] = AssetFile(asset=Asset.FRONT, path=front_path)

    monkeypatch.setattr(media_module, "get_span", lambda _id, _path: 342)

    result = load_or_compute_duration(recording)

    assert result == 342
    written = tmp_path / "20260715_133255_N.duration.txt"
    assert written.read_text(encoding="utf-8") == "342\n"


def test_load_or_compute_duration_prefers_front_falls_back_to_rear(
    tmp_path, monkeypatch
):
    rear_path = tmp_path / "20260715_133255_N_rear.mp4"
    rear_path.write_bytes(b"")

    recording = Recording(id=RecordingId("20260715_133255_N"))
    recording.assets[Asset.REAR] = AssetFile(asset=Asset.REAR, path=rear_path)

    probed_paths = []

    def fake_get_span(_id, path):
        probed_paths.append(path)
        return 88

    monkeypatch.setattr(media_module, "get_span", fake_get_span)

    assert load_or_compute_duration(recording) == 88
    assert probed_paths == [rear_path]


def test_load_or_compute_duration_returns_none_without_front_or_rear(monkeypatch):
    recording = Recording(id=RecordingId("20260715_133255_N"))

    def fail_get_span(*_args, **_kwargs):
        raise AssertionError("get_span() should not be called with no source")

    monkeypatch.setattr(media_module, "get_span", fail_get_span)

    assert load_or_compute_duration(recording) is None


def test_load_or_compute_duration_returns_none_when_get_span_fails(
    tmp_path, monkeypatch
):
    front_path = tmp_path / "20260715_133255_N_front.mp4"
    front_path.write_bytes(b"")

    recording = Recording(id=RecordingId("20260715_133255_N"))
    recording.assets[Asset.FRONT] = AssetFile(asset=Asset.FRONT, path=front_path)

    def fail_get_span(*_args, **_kwargs):
        raise MediaToolError("ffprobe not found")

    monkeypatch.setattr(media_module, "get_span", fail_get_span)

    assert load_or_compute_duration(recording) is None
    assert not (tmp_path / "20260715_133255_N.duration.txt").exists()


def test_load_or_compute_duration_still_returns_the_value_if_writing_fails(
    tmp_path, monkeypatch
):
    # A read-only archive, a full disk, etc. - the caller still gets
    # the real answer it asked for even if persisting the cache back
    # out didn't work, same "never worth failing over" convention as
    # everything else in this module.
    front_path = tmp_path / "20260715_133255_N_front.mp4"
    front_path.write_bytes(b"")

    recording = Recording(id=RecordingId("20260715_133255_N"))
    recording.assets[Asset.FRONT] = AssetFile(asset=Asset.FRONT, path=front_path)

    monkeypatch.setattr(media_module, "get_span", lambda _id, _path: 99)

    real_write_text = Path.write_text

    def fake_write_text(self, *args, **kwargs):
        if self.name.endswith(".duration.txt"):
            raise OSError("disk full")
        return real_write_text(self, *args, **kwargs)

    monkeypatch.setattr(Path, "write_text", fake_write_text)

    assert load_or_compute_duration(recording) == 99


def test_get_span_uses_ffprobe_result_when_probe_succeeds(
    monkeypatch, tmp_path
):
    monkeypatch.setattr(
        media_module,
        "probe",
        lambda path: MediaInfo(duration_seconds=10.0, frame_rate=30.0),
    )

    span = get_span(RecordingId("20260715_133255_N"), tmp_path / "x.mp4")

    assert span == 10


def test_get_span_falls_back_to_box_reader_when_probe_fails(
    monkeypatch, tmp_path
):
    def fake_probe(path):
        raise MediaToolError("ffprobe failed")

    def fake_read_mp4_info(path):
        return Mp4Info(duration_seconds=10.0, frame_count=600)

    monkeypatch.setattr(media_module, "probe", fake_probe)
    monkeypatch.setattr(
        "blackvue.generate.mp4_box_reader.read_mp4_info", fake_read_mp4_info
    )

    # Parking mode: the fallback uses the raw frame count directly.
    span_p = get_span(RecordingId("20260715_133255_P"), tmp_path / "x.mp4")
    assert span_p == 600

    # Normal mode: falls back to mvhd duration.
    span_n = get_span(RecordingId("20260715_133255_N"), tmp_path / "x.mp4")
    assert span_n == 10


@pytest.mark.skipif(
    shutil.which("ffprobe") is None, reason="ffprobe not installed"
)
def test_get_span_end_to_end_on_a_genuinely_broken_file(tmp_path):
    """Build an MP4 real ffprobe refuses to open (broken audio trak,
    same shape as the real-world dashcam files this was written
    for), and confirm get_span() still produces the right answer via
    the fallback - with no mocking at all."""

    data = _build_mp4(
        _mvhd_v0(timescale=30, duration=60),
        _video_trak(frame_count=1800),
        _audio_trak_with_garbage(),
    )
    path = tmp_path / "20260715_133255_PF.mp4"
    path.write_bytes(data)

    with pytest.raises(MediaToolError):
        media_module.probe(path)

    # The fallback isn't gated on recording kind - it kicks in for any
    # kind whenever ffprobe can't open the file. Only the *parking*
    # multiplier inside the fallback is kind-specific.
    assert get_span(RecordingId("20260715_133255_P"), path) == 1800
    assert get_span(RecordingId("20260715_133255_N"), path) == 2
    assert get_span(RecordingId("20260715_133255_E"), path) == 2
    assert get_span(RecordingId("20260715_133255_M"), path) == 2
