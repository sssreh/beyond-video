import json
import subprocess

import pytest
from PIL import Image

from blackvue.export import media as media_module
from blackvue.export.media import concatenate_media
from blackvue.export.media import encode_frame_sequence
from blackvue.export.media import encode_with_nvenc_fallback
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

    def fake_encode(codec_args, input_args, destination):
        captured.append(codec_args)

    monkeypatch.setattr(media_module, "_run_ffmpeg_encode", fake_encode)

    encode_frame_sequence(tmp_path, tmp_path / "out.mp4", fps=5)

    # No bitrate was requested, so the default quality target
    # (_DEFAULT_LIBX264_QUALITY_ARGS) is applied instead of leaving it
    # to libx264's own internal default - see
    # encode_with_nvenc_fallback()'s own docstring for why.
    assert captured == [
        ["-c:v", "libx264", "-pix_fmt", "yuv420p", "-crf", "19"]
    ]


def test_encode_frame_sequence_tries_nvenc_first_when_available(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(media_module, "_NVENC_AVAILABLE", True)
    captured = []

    def fake_encode(codec_args, input_args, destination):
        captured.append(codec_args)

    monkeypatch.setattr(media_module, "_run_ffmpeg_encode", fake_encode)

    encode_frame_sequence(tmp_path, tmp_path / "out.mp4", fps=5)

    # Only the (successful) NVENC attempt - no CPU fallback needed. No
    # bitrate was requested, so the default quality target
    # (_DEFAULT_NVENC_QUALITY_ARGS) is applied instead of leaving it to
    # nvenc's own internal default - see encode_with_nvenc_fallback()'s
    # own docstring for why.
    assert captured == [
        [
            "-c:v", "h264_nvenc", "-pix_fmt", "yuv420p",
            "-rc", "vbr", "-cq", "19", "-b:v", "0",
        ]
    ]


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


def test_encode_with_nvenc_fallback_applies_default_quality_when_unspecified(
    tmp_path, monkeypatch
):
    # Regression test for a real problem Christer found on his own
    # archive: with no --stitch-bitrate given, nvenc's own unset
    # -b:v default landed at a visibly grainy ~1.9Mbps for a real
    # stitch.mp4 (vs. ~23Mbps for an earlier, differently-composited
    # stitch also run with no bitrate given) - not something safe to
    # leave to the encoder's own internal heuristic. No extra_codec_args
    # at all here (the "nothing requested" case every caller besides
    # stitch.py's --stitch-bitrate path is in).
    monkeypatch.setattr(media_module, "_NVENC_AVAILABLE", True)
    captured = []

    def fake_encode(codec_args, input_args, destination):
        captured.append(codec_args)

    monkeypatch.setattr(media_module, "_run_ffmpeg_encode", fake_encode)

    encode_with_nvenc_fallback(["-i", "in.mp4"], tmp_path / "out.mp4")

    assert captured == [
        [
            "-c:v", "h264_nvenc", "-pix_fmt", "yuv420p",
            "-rc", "vbr", "-cq", "19", "-b:v", "0",
        ]
    ]


def test_encode_with_nvenc_fallback_applies_default_quality_to_libx264_too(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(media_module, "_NVENC_AVAILABLE", False)
    captured = []

    def fake_encode(codec_args, input_args, destination):
        captured.append(codec_args)

    monkeypatch.setattr(media_module, "_run_ffmpeg_encode", fake_encode)

    encode_with_nvenc_fallback(["-i", "in.mp4"], tmp_path / "out.mp4")

    assert captured == [
        ["-c:v", "libx264", "-pix_fmt", "yuv420p", "-crf", "19"]
    ]


def test_encode_with_nvenc_fallback_skips_default_quality_when_caller_sets_bitrate(
    tmp_path, monkeypatch
):
    # An explicit --stitch-bitrate (via stitch.py's own _bitrate_args())
    # arrives here as "-b:v", "256k", "-maxrate", "256k", "-bufsize",
    # "256k" - the caller's own explicit rate control must win outright,
    # not get a competing -cq/-crf target stacked on top of it.
    monkeypatch.setattr(media_module, "_NVENC_AVAILABLE", True)
    captured = []

    def fake_encode(codec_args, input_args, destination):
        captured.append(codec_args)

    monkeypatch.setattr(media_module, "_run_ffmpeg_encode", fake_encode)

    encode_with_nvenc_fallback(
        ["-i", "in.mp4"],
        tmp_path / "out.mp4",
        extra_codec_args=["-b:v", "256k", "-maxrate", "256k", "-bufsize", "256k"],
    )

    assert captured == [
        [
            "-c:v", "h264_nvenc", "-pix_fmt", "yuv420p",
            "-b:v", "256k", "-maxrate", "256k", "-bufsize", "256k",
        ]
    ]


def test_encode_with_nvenc_fallback_default_quality_survives_a_real_encode(
    tmp_path, monkeypatch
):
    # Not mocked - lets the real ffmpeg/libx264 (this sandbox has no
    # NVIDIA GPU, so the nvenc attempt genuinely fails and falls
    # through) actually run with the new default -crf 19, confirming
    # it's a flag ffmpeg accepts and produces a real, playable file
    # from, not just a string this project's own code expects.
    monkeypatch.setattr(media_module, "_NVENC_AVAILABLE", False)

    frame_dir = tmp_path / "frames"
    _make_frames(frame_dir)
    destination = tmp_path / "out.mp4"

    encode_frame_sequence(frame_dir, destination, fps=5)

    assert destination.exists()
    assert destination.stat().st_size > 0
