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


def read_duration_seconds(recording: Recording) -> int | None:
    """Return a recording's previously-computed real-world span in
    seconds, read from its .duration.txt file (see get_span(), which
    is what computes and writes the value bv-generate --get-duration
    persists there).

    Returns None if bv-generate --get-duration hasn't been run for
    this recording yet (no Asset.DURATION file), or if the file can't
    be read/parsed.

    Unlike get_span(), this never touches ffprobe/ffmpeg - it just
    reads whatever's already on disk, so it's safe to call from code
    (like TripBuilder) that shouldn't carry a hard ffmpeg dependency
    or the cost of probing video files for every recording.
    """

    duration_file = recording.file(Asset.DURATION)
    if duration_file is None:
        return None

    try:
        text = duration_file.path.read_text(encoding="utf-8").strip()
        return int(text)
    except (OSError, ValueError):
        return None


def load_or_compute_duration(recording: Recording) -> int | None:
    """Return a recording's real-world span in seconds, self-healing
    the cache read_duration_seconds() alone only ever reads: an
    existing .duration.txt is read and returned as-is (same as
    read_duration_seconds()), but a *missing* one is computed via
    get_span() and written out right here, the same
    load-if-cached-else-fetch-and-cache pattern
    osm_roads.load_or_fetch_roads() already uses for OSM road/area
    data - so a trip's own recordings don't need a separate upfront
    `bv-generate --get-duration` pass before something that needs real
    elapsed time (TripBuilder's max_parking_duration cap, the
    duration-aware trip-gap calculation, trip_stats) can use it. Once
    written, the file persists in the archive like any other asset -
    free on every later call, including from an entirely separate
    bv-export/bv-ls run.

    Unlike read_duration_seconds(), this does touch ffprobe/ffmpeg (via
    get_span()) whenever the cache is missing - a real, one-time cost
    per recording. Callers that must stay ffmpeg-free (e.g. anything
    that can't carry a hard ffmpeg dependency) should keep using
    read_duration_seconds() directly instead.

    Returns None - without writing anything - if the recording has
    neither a front nor rear video to probe (select_source() finds
    nothing) or if get_span() itself raises MediaToolError. Also
    returns the freshly computed value even if writing the cache file
    back out fails (a read-only archive, a full disk) - never worth
    losing the answer the caller actually asked for over a failed
    write, same "never worth failing over" convention this module's
    other cache-adjacent functions already follow.
    """

    cached = read_duration_seconds(recording)
    if cached is not None:
        return cached

    source_file = select_source(recording)
    if source_file is None:
        return None

    try:
        span = get_span(recording.id, source_file.path)
    except MediaToolError:
        return None

    destination = source_file.path.parent / f"{recording.id}.duration.txt"
    try:
        destination.write_text(f"{span}\n", encoding="utf-8")
    except OSError:
        pass

    return span


def probe_audio_codec(path: Path) -> str | None:
    """Return the source's first audio stream's codec name (e.g.
    "aac", "mp3"), or None if it has no audio stream at all.

    Raises MediaToolError if ffprobe itself is missing or fails
    outright (as opposed to just finding no audio stream, which is a
    normal outcome reported as None rather than an error).
    """

    try:
        result = subprocess.run(
            [
                "ffprobe",
                "-v", "error",
                "-select_streams", "a:0",
                "-show_entries", "stream=codec_name",
                "-of", "csv=p=0",
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

    codec_name = result.stdout.strip()
    return codec_name or None


def extract_audio(source: Path, destination: Path) -> None:
    """Extract the audio track from source into destination (always
    written as AAC/ADTS, per the archive's own `.aac` convention -
    see archive_reader.py) via ffmpeg.

    The audio stream is copied without re-encoding when it's already
    AAC (the common case, and effectively free). Otherwise it's
    transcoded to AAC: some camera models don't record AAC audio at
    all (confirmed: the BlackVue Elite 10 records MP3), and ADTS -
    the container implied by the `.aac` destination - can only hold
    AAC, so copying a non-AAC stream into it fails outright ("adts
    muxer supports only codec aac for type audio"). The extracted
    audio is only ever consumed for speech-to-text (transcription/
    diarization/translation), where the small quality cost of
    transcoding is not a practical concern.

    On failure, any partially-written destination file is removed
    before raising. ffmpeg opens (and truncates) its output file
    before it can fail to write anything into it - confirmed with the
    exact "adts muxer" failure above, which left a genuine 0-byte
    `.aac` on disk even though the whole command errored out. Left in
    place, that empty file would look like a completed extraction to
    every downstream caller that only checks "does the `.aac` already
    exist" (bv-generate's cached-audio reuse, its own retry on a
    later run) - failing loudly here and cleaning up after ourselves
    is what makes a retry actually retry instead of quietly reusing
    the broken leftover.
    """

    destination.parent.mkdir(parents=True, exist_ok=True)

    codec_name = probe_audio_codec(source)
    audio_args = (
        ["-acodec", "copy"] if codec_name == "aac" else ["-acodec", "aac"]
    )

    try:
        subprocess.run(
            [
                "ffmpeg",
                "-y",
                "-i", str(source),
                "-vn",
                *audio_args,
                str(destination),
            ],
            capture_output=True,
            text=True,
            check=True,
        )
    except FileNotFoundError as exc:
        raise MediaToolError("ffmpeg not found on PATH") from exc
    except subprocess.CalledProcessError as exc:
        destination.unlink(missing_ok=True)
        raise MediaToolError(
            f"ffmpeg failed for {source.name}: {exc.stderr.strip()}"
        ) from exc
