import json
import subprocess

import pytest
from PIL import Image

from blackvue.export import media as media_module
from blackvue.export.media import concatenate_media
from blackvue.export.media import encode_frame_sequence
from blackvue.generate.media import MediaToolError


def _make_silent_audio(path, duration_seconds: float) -> None:
    subprocess.run(
        [
            "ffmpeg", "-y",
            "-f", "lavfi", "-i", f"anullsrc=r=8000:cl=mono",
            "-t", str(duration_seconds),
            str(path),
        ],
        capture_output=True,
        text=True,
        check=True,
    )


def _audio_duration_seconds(path) -> float:
    # generate.media.probe() assumes a video stream (-select_streams
    # v:0), which these audio-only fixtures don't have - query the
    # container duration directly instead.
    result = subprocess.run(
        [
            "ffprobe", "-v", "error",
            "-show_entries", "format=duration",
            "-of", "json",
            str(path),
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    return float(json.loads(result.stdout)["format"]["duration"])


def test_concatenate_media_joins_two_files_end_to_end(tmp_path):
    first = tmp_path / "first.aac"
    second = tmp_path / "second.aac"
    _make_silent_audio(first, 1.0)
    _make_silent_audio(second, 2.0)

    destination = tmp_path / "combined.aac"
    concatenate_media([first, second], destination)

    assert destination.exists()
    assert round(_audio_duration_seconds(destination)) == 3


def test_concatenate_media_does_nothing_for_empty_sources(tmp_path):
    destination = tmp_path / "combined.aac"

    concatenate_media([], destination)

    assert not destination.exists()


def test_concatenate_media_handles_a_single_source(tmp_path):
    first = tmp_path / "only.aac"
    _make_silent_audio(first, 1.0)

    destination = tmp_path / "combined.aac"
    concatenate_media([first], destination)

    assert destination.exists()
    assert round(_audio_duration_seconds(destination)) == 1


def test_concatenate_media_handles_paths_with_single_quotes(tmp_path):
    weird_dir = tmp_path / "trip's audio"
    weird_dir.mkdir()
    first = weird_dir / "clip.aac"
    _make_silent_audio(first, 1.0)

    destination = tmp_path / "combined.aac"
    concatenate_media([first], destination)

    assert destination.exists()


def _make_frames(frame_dir, count=2):
    frame_dir.mkdir(parents=True, exist_ok=True)
    for i in range(count):
        Image.new("RGB", (32, 32), (i * 40, 0, 0)).save(
            frame_dir / f"frame_{i:06d}.png"
        )


def test_nvenc_available_detects_h264_nvenc_in_the_encoder_list(monkeypatch):
    monkeypatch.setattr(media_module, "_NVENC_AVAILABLE", None)
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *_a, **_k: subprocess.CompletedProcess(
            [], 0, stdout="... h264_nvenc ...", stderr=""
        ),
    )

    assert media_module._nvenc_available() is True


def test_nvenc_available_returns_false_when_not_listed(monkeypatch):
    monkeypatch.setattr(media_module, "_NVENC_AVAILABLE", None)
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *_a, **_k: subprocess.CompletedProcess(
            [], 0, stdout="... libx264 ...", stderr=""
        ),
    )

    assert media_module._nvenc_available() is False


def test_nvenc_available_is_cached_after_the_first_call(monkeypatch):
    monkeypatch.setattr(media_module, "_NVENC_AVAILABLE", None)
    calls = []

    def fake_run(*args, **kwargs):
        calls.append(args)
        return subprocess.CompletedProcess([], 0, stdout="h264_nvenc", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)

    assert media_module._nvenc_available() is True
    assert media_module._nvenc_available() is True
    assert len(calls) == 1


def test_encode_frame_sequence_uses_libx264_directly_when_nvenc_unavailable(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(media_module, "_NVENC_AVAILABLE", False)
    captured = []

    def fake_encode(codec_args, frame_dir, destination, fps):
        captured.append(codec_args)

    monkeypatch.setattr(media_module, "_run_ffmpeg_encode", fake_encode)

    encode_frame_sequence(tmp_path, tmp_path / "out.mp4", fps=5)

    assert captured == [["-c:v", "libx264", "-pix_fmt", "yuv420p"]]


def test_encode_frame_sequence_tries_nvenc_first_when_available(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(media_module, "_NVENC_AVAILABLE", True)
    captured = []

    def fake_encode(codec_args, frame_dir, destination, fps):
        captured.append(codec_args)

    monkeypatch.setattr(media_module, "_run_ffmpeg_encode", fake_encode)

    encode_frame_sequence(tmp_path, tmp_path / "out.mp4", fps=5)

    # Only the (successful) NVENC attempt - no CPU fallback needed.
    assert captured == [["-c:v", "h264_nvenc", "-pix_fmt", "yuv420p"]]


def test_encode_frame_sequence_falls_back_to_libx264_when_nvenc_fails_for_real(
    tmp_path, monkeypatch
):
    # Force "NVENC is available" (this sandbox's ffmpeg build may or
    # may not actually list it) but let the real ffmpeg attempt run -
    # with no real NVIDIA GPU/driver here, the h264_nvenc attempt
    # genuinely fails, proving the fallback to libx264 isn't just
    # mocked but actually produces a working video.
    monkeypatch.setattr(media_module, "_NVENC_AVAILABLE", True)

    frame_dir = tmp_path / "frames"
    _make_frames(frame_dir)
    destination = tmp_path / "out.mp4"

    encode_frame_sequence(frame_dir, destination, fps=5)

    assert destination.exists()


def test_encode_frame_sequence_raises_when_the_cpu_encoder_also_fails(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(media_module, "_NVENC_AVAILABLE", False)

    # An empty frame_dir has no frame_%06d.png files for ffmpeg to
    # read, so even the libx264 fallback genuinely fails.
    empty_frame_dir = tmp_path / "empty"
    empty_frame_dir.mkdir()

    with pytest.raises(MediaToolError):
        encode_frame_sequence(empty_frame_dir, tmp_path / "out.mp4", fps=5)
