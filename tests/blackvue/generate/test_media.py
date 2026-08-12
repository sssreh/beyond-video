import shutil
import subprocess
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
from blackvue.generate.media import extract_audio
from blackvue.generate.media import get_span
from blackvue.generate.media import is_audio_silent
from blackvue.generate.media import load_or_compute_duration
from blackvue.generate.media import probe_audio_codec
from blackvue.generate.media import probe_audio_format
from blackvue.generate.media import probe_video_codec
from blackvue.generate.media import read_duration_seconds
from blackvue.generate.media import select_source
from blackvue.generate.mp4_box_reader import Mp4Info
from .test_mp4_box_reader import _audio_trak_with_garbage
from .test_mp4_box_reader import _build_mp4
from .test_mp4_box_reader import _mvhd_v0
from .test_mp4_box_reader import _video_trak


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


def test_probe_audio_codec_returns_the_codec_name(monkeypatch, tmp_path):
    def fake_run(*_args, **_kwargs):
        return subprocess.CompletedProcess([], 0, stdout="aac\n", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)

    assert probe_audio_codec(tmp_path / "x.mp4") == "aac"


def test_probe_audio_codec_returns_none_without_an_audio_stream(
    monkeypatch, tmp_path
):
    def fake_run(*_args, **_kwargs):
        return subprocess.CompletedProcess([], 0, stdout="", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)

    assert probe_audio_codec(tmp_path / "x.mp4") is None


def test_probe_audio_codec_raises_when_ffprobe_is_missing(monkeypatch, tmp_path):
    def fake_run(*_args, **_kwargs):
        raise FileNotFoundError()

    monkeypatch.setattr(subprocess, "run", fake_run)

    with pytest.raises(MediaToolError):
        probe_audio_codec(tmp_path / "x.mp4")


def test_probe_audio_codec_raises_when_ffprobe_fails(monkeypatch, tmp_path):
    def fake_run(*_args, **_kwargs):
        raise subprocess.CalledProcessError(1, ["ffprobe"], stderr="broken file")

    monkeypatch.setattr(subprocess, "run", fake_run)

    with pytest.raises(MediaToolError):
        probe_audio_codec(tmp_path / "x.mp4")


def test_probe_video_codec_returns_the_codec_name(monkeypatch, tmp_path):
    def fake_run(*_args, **_kwargs):
        return subprocess.CompletedProcess([], 0, stdout="hevc\n", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)

    assert probe_video_codec(tmp_path / "x.mp4") == "hevc"


def test_probe_video_codec_returns_none_without_a_video_stream(
    monkeypatch, tmp_path
):
    def fake_run(*_args, **_kwargs):
        return subprocess.CompletedProcess([], 0, stdout="", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)

    assert probe_video_codec(tmp_path / "x.mp4") is None


def test_probe_video_codec_raises_when_ffprobe_is_missing(monkeypatch, tmp_path):
    def fake_run(*_args, **_kwargs):
        raise FileNotFoundError()

    monkeypatch.setattr(subprocess, "run", fake_run)

    with pytest.raises(MediaToolError):
        probe_video_codec(tmp_path / "x.mp4")


def test_probe_video_codec_raises_when_ffprobe_fails(monkeypatch, tmp_path):
    def fake_run(*_args, **_kwargs):
        raise subprocess.CalledProcessError(1, ["ffprobe"], stderr="broken file")

    monkeypatch.setattr(subprocess, "run", fake_run)

    with pytest.raises(MediaToolError):
        probe_video_codec(tmp_path / "x.mp4")


def test_probe_audio_format_returns_sample_rate_and_channels(monkeypatch, tmp_path):
    def fake_run(*_args, **_kwargs):
        return subprocess.CompletedProcess([], 0, stdout="48000,2\n", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)

    assert probe_audio_format(tmp_path / "x.aac") == (48000, 2)


def test_probe_audio_format_returns_none_without_an_audio_stream(
    monkeypatch, tmp_path
):
    def fake_run(*_args, **_kwargs):
        return subprocess.CompletedProcess([], 0, stdout="", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)

    assert probe_audio_format(tmp_path / "x.mp4") is None


def test_probe_audio_format_raises_when_ffprobe_is_missing(monkeypatch, tmp_path):
    def fake_run(*_args, **_kwargs):
        raise FileNotFoundError()

    monkeypatch.setattr(subprocess, "run", fake_run)

    with pytest.raises(MediaToolError):
        probe_audio_format(tmp_path / "x.mp4")


def test_probe_audio_format_raises_when_ffprobe_fails(monkeypatch, tmp_path):
    def fake_run(*_args, **_kwargs):
        raise subprocess.CalledProcessError(1, ["ffprobe"], stderr="broken file")

    monkeypatch.setattr(subprocess, "run", fake_run)

    with pytest.raises(MediaToolError):
        probe_audio_format(tmp_path / "x.mp4")


def test_extract_audio_stream_copies_when_source_is_already_aac(
    monkeypatch, tmp_path
):
    monkeypatch.setattr(media_module, "probe_audio_codec", lambda _path: "aac")

    calls = []

    def fake_run(cmd, **_kwargs):
        calls.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)

    extract_audio(tmp_path / "source.mp4", tmp_path / "out.aac")

    assert len(calls) == 1
    assert "copy" in calls[0]
    assert "aac" not in calls[0]


def test_extract_audio_transcodes_when_source_is_not_aac(monkeypatch, tmp_path):
    # This is the Elite 10 case: MP3 audio can't be stream-copied into
    # the .aac destination's ADTS container, so it must be transcoded.
    monkeypatch.setattr(media_module, "probe_audio_codec", lambda _path: "mp3")

    calls = []

    def fake_run(cmd, **_kwargs):
        calls.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)

    extract_audio(tmp_path / "source.mp4", tmp_path / "out.aac")

    assert len(calls) == 1
    assert "copy" not in calls[0]
    assert "aac" in calls[0]


def test_extract_audio_transcodes_when_source_has_no_recognized_codec(
    monkeypatch, tmp_path
):
    # None (no audio stream detected by ffprobe) isn't "aac" either -
    # falls into the same transcode branch, and ffmpeg itself is the
    # one that ultimately reports "no audio stream" if that's really
    # the case.
    monkeypatch.setattr(media_module, "probe_audio_codec", lambda _path: None)

    calls = []

    def fake_run(cmd, **_kwargs):
        calls.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)

    extract_audio(tmp_path / "source.mp4", tmp_path / "out.aac")

    assert "aac" in calls[0]
    assert "copy" not in calls[0]


def test_extract_audio_wraps_ffmpeg_failure(monkeypatch, tmp_path):
    monkeypatch.setattr(media_module, "probe_audio_codec", lambda _path: "aac")

    def fake_run(cmd, **_kwargs):
        raise subprocess.CalledProcessError(1, cmd, stderr="boom")

    monkeypatch.setattr(subprocess, "run", fake_run)

    with pytest.raises(MediaToolError):
        extract_audio(tmp_path / "source.mp4", tmp_path / "out.aac")


def test_extract_audio_removes_partial_output_on_ffmpeg_failure(
    monkeypatch, tmp_path
):
    """ffmpeg opens (and truncates) its output file before it can
    fail to write anything into it - confirmed for real against the
    exact "adts muxer" failure this bug was found from, which left a
    genuine 0-byte .aac on disk even though the whole command errored
    out. A leftover empty file looks like a completed extraction to
    every downstream caller that only checks "does the file exist" -
    bv-generate's own cached-audio reuse got poisoned by exactly this
    - so a failed extract_audio() must not leave anything behind."""

    monkeypatch.setattr(media_module, "probe_audio_codec", lambda _path: "aac")

    destination = tmp_path / "out.aac"

    def fake_run(cmd, **_kwargs):
        # Mimic ffmpeg's real behavior: the output file gets created/
        # truncated before the command fails.
        destination.write_bytes(b"")
        raise subprocess.CalledProcessError(1, cmd, stderr="boom")

    monkeypatch.setattr(subprocess, "run", fake_run)

    with pytest.raises(MediaToolError):
        extract_audio(tmp_path / "source.mp4", destination)

    assert not destination.exists()


@pytest.mark.skipif(
    shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None,
    reason="ffmpeg/ffprobe not installed",
)
def test_extract_audio_end_to_end_transcodes_mp3_source_to_playable_aac(
    tmp_path,
):
    """Reproduces the real Elite 10 failure with no mocking at all: an
    MP4 whose audio track is MP3 (not AAC) used to make ffmpeg's ADTS
    muxer reject the stream-copy outright. Confirm the fix produces a
    genuinely playable AAC file instead."""

    source = tmp_path / "source.mp4"
    subprocess.run(
        [
            "ffmpeg", "-y",
            "-f", "lavfi", "-i", "testsrc=size=64x64:rate=10",
            "-f", "lavfi", "-i", "sine=frequency=440",
            "-t", "1",
            "-c:v", "libx264",
            "-c:a", "mp3",
            str(source),
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    assert probe_audio_codec(source) == "mp3"

    destination = tmp_path / "out.aac"
    extract_audio(source, destination)

    assert destination.exists()
    assert probe_audio_codec(destination) == "aac"


def test_is_audio_silent_returns_true_below_threshold(monkeypatch, tmp_path):
    def fake_run(cmd, **_kwargs):
        return subprocess.CompletedProcess(
            cmd, 0, stdout="",
            stderr="[Parsed_volumedetect_0 @ 0x0] mean_volume: -70.0 dB\n",
        )

    monkeypatch.setattr(subprocess, "run", fake_run)

    assert is_audio_silent(tmp_path / "x.aac") is True


def test_is_audio_silent_returns_false_above_threshold(monkeypatch, tmp_path):
    def fake_run(cmd, **_kwargs):
        return subprocess.CompletedProcess(
            cmd, 0, stdout="",
            stderr="[Parsed_volumedetect_0 @ 0x0] mean_volume: -18.4 dB\n",
        )

    monkeypatch.setattr(subprocess, "run", fake_run)

    assert is_audio_silent(tmp_path / "x.aac") is False


def test_is_audio_silent_treats_exact_threshold_as_silent(monkeypatch, tmp_path):
    # <= threshold, not strictly less than - a track sitting exactly at
    # the cutoff should be skipped, not kept "just in case".
    def fake_run(cmd, **_kwargs):
        return subprocess.CompletedProcess(
            cmd, 0, stdout="",
            stderr="mean_volume: -50.0 dB\n",
        )

    monkeypatch.setattr(subprocess, "run", fake_run)

    assert is_audio_silent(tmp_path / "x.aac") is True


def test_is_audio_silent_honors_a_custom_threshold(monkeypatch, tmp_path):
    def fake_run(cmd, **_kwargs):
        return subprocess.CompletedProcess(
            cmd, 0, stdout="", stderr="mean_volume: -30.0 dB\n",
        )

    monkeypatch.setattr(subprocess, "run", fake_run)

    assert is_audio_silent(tmp_path / "x.aac", threshold_db=-50.0) is False
    assert is_audio_silent(tmp_path / "x.aac", threshold_db=-20.0) is True


def test_is_audio_silent_returns_false_when_mean_volume_is_unparseable(
    monkeypatch, tmp_path
):
    # If ffmpeg's output doesn't contain the line we expect (a
    # different ffmpeg version, an unexpected failure that still
    # exits 0, etc.), err toward "not silent" rather than throwing
    # away audio that might actually have something on it.
    def fake_run(cmd, **_kwargs):
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)

    assert is_audio_silent(tmp_path / "x.aac") is False


def test_is_audio_silent_raises_when_ffmpeg_is_missing(monkeypatch, tmp_path):
    def fake_run(*_args, **_kwargs):
        raise FileNotFoundError()

    monkeypatch.setattr(subprocess, "run", fake_run)

    with pytest.raises(MediaToolError):
        is_audio_silent(tmp_path / "x.aac")
