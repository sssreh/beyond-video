"""
Media probing and extraction (ffprobe / ffmpeg).

Copyright (C) 2026 Christer R. (sssreh)

SPDX-License-Identifier: GPL-3.0-or-later
"""

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass
from pathlib import Path

from ..archive.asset import Asset
from ..archive.asset_file import AssetFile
from ..archive.recording import Recording
from ..archive.recording_id import RecordingId


class MediaToolError(RuntimeError):
    """Raised when ffmpeg/ffprobe is missing or fails."""


@dataclass(frozen=True)
class MediaInfo:
    """Probed properties of a video file."""

    duration_seconds: float
    frame_rate: float


def select_source(recording: Recording) -> AssetFile | None:
    """Return the recording's front video, or its rear video if there
    is no front video.

    Returns None if the recording has neither.
    """

    return recording.file(Asset.FRONT) or recording.file(Asset.REAR)


def _parse_frame_rate(value: str) -> float:
    """Parse an ffprobe frame rate string such as '30000/1001' or '30/1'."""

    if "/" in value:
        numerator, _, denominator = value.partition("/")
        denominator_value = float(denominator)

        if denominator_value == 0:
            return 0.0

        return float(numerator) / denominator_value

    return float(value)


def probe(path: Path) -> MediaInfo:
    """Probe a video file's duration and frame rate using ffprobe."""

    try:
        result = subprocess.run(
            [
                "ffprobe",
                "-v", "error",
                "-select_streams", "v:0",
                "-show_entries", "stream=avg_frame_rate:format=duration",
                "-of", "json",
                str(path),
            ],
            capture_output=True,
            text=True,
            check=True,
        )
    except FileNotFoundError as exc:
        raise MediaToolError("ffprobe not found on PATH") from exc
    except subprocess.CalledProcessError as exc:
        raise MediaToolError(
            f"ffprobe failed for {path.name}: {exc.stderr.strip()}"
        ) from exc

    try:
        data = json.loads(result.stdout)
        duration_seconds = float(data["format"]["duration"])
        frame_rate = _parse_frame_rate(data["streams"][0]["avg_frame_rate"])
    except (KeyError, IndexError, ValueError) as exc:
        raise MediaToolError(
            f"could not parse ffprobe output for {path.name}"
        ) from exc

    return MediaInfo(
        duration_seconds=duration_seconds,
        frame_rate=frame_rate,
    )


def compute_span(recording_id: RecordingId, info: MediaInfo) -> int:
    """Return the real-world elapsed time of a recording, in seconds.

    Parking-mode (P) recordings are timelapses captured at one frame
    per second: each frame represents one real second of elapsed
    time, but the file is encoded (and reported by ffprobe) at the
    normal playback frame rate. A 30-minute parking event can end up
    as a file that only plays for one minute. For every other kind,
    playback duration already equals real elapsed time.
    """

    if recording_id.is_parking:
        return round(info.duration_seconds * info.frame_rate)

    return round(info.duration_seconds)


def get_span(recording_id: RecordingId, path: Path) -> int:
    """Return the real-world span in seconds for a recording, in
    seconds - trying ffprobe first, falling back to reading the
    MP4's box structure directly if ffprobe can't open the file.

    Some dashcam recordings (parking-mode ones in particular) carry
    a broken, vestigial audio track that trips ffmpeg's strict
    container validation even though the video track itself is
    intact. When that happens, fall back to a minimal, tolerant MP4
    box reader that only ever looks at the video track. For parking
    mode specifically, the fallback uses the video track's raw frame
    count directly (1 frame = 1 real second), which sidesteps the
    duration x frame-rate math - and its floating-point rounding -
    entirely.
    """

    try:
        info = probe(path)
    except MediaToolError:
        return _estimate_span_from_boxes(recording_id, path)

    return compute_span(recording_id, info)


def _estimate_span_from_boxes(recording_id: RecordingId, path: Path) -> int:
    """The get_span() fallback path - see get_span()'s docstring."""

    from .mp4_box_reader import read_mp4_info

    info = read_mp4_info(path)

    if recording_id.is_parking and info.frame_count is not None:
        return info.frame_count

    return round(info.duration_seconds)


def extract_audio(source: Path, destination: Path) -> None:
    """Extract the audio track from source into destination via ffmpeg.

    The audio stream is copied without re-encoding.
    """

    destination.parent.mkdir(parents=True, exist_ok=True)

    try:
        subprocess.run(
            [
                "ffmpeg",
                "-y",
                "-i", str(source),
                "-vn",
                "-acodec", "copy",
                str(destination),
            ],
            capture_output=True,
            text=True,
            check=True,
        )
    except FileNotFoundError as exc:
        raise MediaToolError("ffmpeg not found on PATH") from exc
    except subprocess.CalledProcessError as exc:
        raise MediaToolError(
            f"ffmpeg failed for {source.name}: {exc.stderr.strip()}"
        ) from exc
