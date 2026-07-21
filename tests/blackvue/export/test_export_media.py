import json
import subprocess

from blackvue.export.media import concatenate_media


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
