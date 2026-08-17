"""
Tests for archive/container_gps.py - _probe_container_location(),
container_location_fix().

Christer's real report, verbatim, from a real ffprobe dump on a
stock/downloaded clip mixed into his GoPro archive:

    TAG:location-{=+05.0448-073.7965/
    TAG:location=+05.0448-073.7965/

"This looks like gps coordinates ... not found by bv-generate." These
tests write a real `location` tag via a real ffmpeg subprocess
(`-metadata location=...`) and read it back through the module's real
ffprobe-based parsing, rather than mocking subprocess - matching this
codebase's established real-fixture-over-mocking test style (see
test_folder_adapter.py's real-ffmpeg creation_time fixtures for the
same pattern already used for the closely-related timestamp-resolution
code path).
"""

import subprocess
from datetime import datetime
from pathlib import Path

from blackvue.archive.container_gps import _probe_container_location
from blackvue.archive.container_gps import container_location_fix


def _make_video_with_location(path: Path, location: str) -> None:
    subprocess.run(
        [
            "ffmpeg", "-y",
            "-f", "lavfi", "-i", "testsrc=size=64x64:rate=10",
            "-t", "1",
            "-c:v", "libx264",
            "-metadata", f"location={location}",
            str(path),
        ],
        capture_output=True,
        text=True,
        check=True,
    )


def _make_plain_video(path: Path) -> None:
    subprocess.run(
        [
            "ffmpeg", "-y",
            "-f", "lavfi", "-i", "testsrc=size=64x64:rate=10",
            "-t", "1",
            "-c:v", "libx264",
            str(path),
        ],
        capture_output=True,
        text=True,
        check=True,
    )


def test_probe_container_location_reads_christers_real_tag_shape(tmp_path):
    """Exactly the tag shape from Christer's real ffprobe dump - no
    altitude, a negative longitude."""

    video = tmp_path / "clip.mp4"
    _make_video_with_location(video, "+05.0448-073.7965/")

    result = _probe_container_location(video)

    assert result == (5.0448, -73.7965)


def test_probe_container_location_handles_altitude_field(tmp_path):
    video = tmp_path / "clip.mp4"
    _make_video_with_location(video, "+27.5916+086.5640+8850/")

    result = _probe_container_location(video)

    assert result == (27.5916, 86.564)


def test_probe_container_location_returns_none_without_a_tag(tmp_path):
    video = tmp_path / "clip.mp4"
    _make_plain_video(video)

    assert _probe_container_location(video) is None


def test_probe_container_location_returns_none_for_unreadable_file(tmp_path):
    bad_path = tmp_path / "not_a_video.mp4"
    bad_path.write_bytes(b"not a real video file")

    assert _probe_container_location(bad_path) is None


def test_container_location_fix_builds_a_valid_single_point_gpsfix(tmp_path):
    video = tmp_path / "clip.mp4"
    _make_video_with_location(video, "+05.0448-073.7965/")
    timestamp = datetime(2026, 8, 16, 14, 41, 30)

    fix = container_location_fix(video, timestamp=timestamp)

    assert fix is not None
    assert fix.timestamp == timestamp
    assert fix.valid is True
    assert fix.latitude == 5.0448
    assert fix.longitude == -73.7965
    # A container location tag is a single static point, not a track -
    # same framing as archive/exif.py's exif_gps_fix() for a photo.
    assert fix.speed_kmh is None
    assert fix.course is None


def test_container_location_fix_returns_none_without_a_tag(tmp_path):
    video = tmp_path / "clip.mp4"
    _make_plain_video(video)

    assert container_location_fix(video, timestamp=datetime(2026, 8, 16)) is None
