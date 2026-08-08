"""
Trip-level SRT/LRC merging for bv-export.

Each recording already has its own .srt/.lrc (bv-generate --srt/--lrc,
timestamps relative to that recording's own start). This rebases every
recording's cues onto the trip's timeline - the same offset-rebasing
pattern trip_export.py already uses for g-sensor samples in .3gf - then
re-numbers/re-formats the combined cues as one trip.srt / trip.lrc, the
same way merge_text_assets() combines transcript.txt across a trip.

Copyright (C) 2026 Christer R. (sssreh)

SPDX-License-Identifier: GPL-3.0-or-later
"""

from __future__ import annotations

from collections.abc import Callable

from ..archive.asset import Asset
from ..archive.recording_id import RecordingId
from ..generate.speech import SpeechSegment
from ..generate.subtitles import format_lrc
from ..generate.subtitles import format_srt
from ..generate.subtitles import parse_lrc
from ..generate.subtitles import parse_srt
from ..trip.trip import Trip


def _shift(segment: SpeechSegment, offset_seconds: float) -> SpeechSegment:
    return SpeechSegment(
        start=segment.start + offset_seconds,
        end=segment.end + offset_seconds,
        text=segment.text,
    )


def _merge_subtitle_segments(
    trip: Trip,
    asset: Asset,
    parser: Callable[[str], tuple[SpeechSegment, ...]],
    video_offsets: dict[RecordingId, float] | None = None,
) -> tuple[SpeechSegment, ...]:
    """Merge every recording's own `asset` (SUBTITLES or LYRICS) into
    one trip-relative sequence of SpeechSegments.

    `video_offsets`, if given (see trip_export.py's
    `_recording_video_offsets()`), rebases a recording present in it
    by its own real position in the concatenated video - the same
    fix `_merge_gsensor()` already applies, for the same reason: a
    recording's ID-timestamp gap from the trip's first recording only
    matches its real video position when every earlier recording's
    own video duration exactly equals its own ID-timestamp gap, which
    a mid-trip Parking recording violates by a lot (its own wall
    -clock span is commonly far longer than the real video seconds it
    contributes - the entire premise `_recording_video_offsets()`'s
    own docstring covers). Confirmed on a real trip: with
    `--include-parking`, every subtitle cue for a recording *after*
    the Parking recording landed offset by the Parking segment's
    *wall-clock* duration rather than its much shorter real video
    duration - often placing them well past the actual video's end,
    which looks indistinguishable from "no subtitles at all" during
    playback. A recording absent from `video_offsets` (no
    `video_offsets` given at all, or the recording's own video
    couldn't be probed) falls back to the old ID-timestamp-gap
    rebase, same as `_merge_gsensor()`.
    """

    trip_start = trip.start_timestamp
    segments: list[SpeechSegment] = []

    for recording in trip:
        subtitle_file = recording.file(asset)
        if subtitle_file is None:
            continue

        if video_offsets is not None and recording.id in video_offsets:
            offset_seconds = video_offsets[recording.id]
        else:
            offset_seconds = (recording.id.timestamp - trip_start).total_seconds()

        text = subtitle_file.path.read_text(encoding="utf-8")

        segments.extend(
            _shift(segment, offset_seconds) for segment in parser(text)
        )

    return tuple(sorted(segments, key=lambda segment: segment.start))


def _pad_to_duration(
    segments: tuple[SpeechSegment, ...],
    total_duration_seconds: float | None,
) -> tuple[SpeechSegment, ...]:
    """Append an empty trailing cue so the subtitle timeline reaches
    total_duration_seconds, if it doesn't already.

    Whisper only ever emits segments for actual speech, so a trip
    where the last stretch is silent (nobody talking for the last
    couple of minutes, say) ends up with a merged subtitle file that
    stops well before the video does. A no-op if there's nothing to
    pad to, no segments to pad, or the real content already reaches
    (or exceeds) the video's length.
    """

    if total_duration_seconds is None or not segments:
        return segments

    last_end = max(segment.end for segment in segments)
    if last_end >= total_duration_seconds:
        return segments

    # Starts within the final second (but never before the last real
    # cue ends) rather than exactly at total_duration_seconds, so SRT
    # players that dislike a zero-duration cue still get a sane one -
    # and for LRC, whose format only has a start time, this puts the
    # empty marker right at the end of the video rather than
    # redundantly at the same spot as the last real line.
    padding_start = max(last_end, total_duration_seconds - 1.0)

    return segments + (
        SpeechSegment(start=padding_start, end=total_duration_seconds, text=""),
    )


def merge_srt(
    trip: Trip,
    *,
    total_duration_seconds: float | None = None,
    video_offsets: dict[RecordingId, float] | None = None,
) -> str | None:
    """Merge every recording's .srt in the trip into one trip-relative
    SRT string, cues renumbered and sorted by start time. Returns None
    if no recording in the trip has an .srt.

    If total_duration_seconds is given and the merged cues end before
    it, an empty trailing cue is appended so the subtitle file's
    length matches the actual video (see _pad_to_duration).

    `video_offsets`, if given, positions each recording's cues by its
    own real position in the concatenated video rather than its ID
    -timestamp gap from the trip's start - see
    `_merge_subtitle_segments()`'s own docstring for why that matters
    whenever a Parking recording sits mid-trip.
    """

    segments = _merge_subtitle_segments(
        trip, Asset.SUBTITLES, parse_srt, video_offsets
    )
    if not segments:
        return None
    segments = _pad_to_duration(segments, total_duration_seconds)
    return format_srt(segments)


def merge_lrc(
    trip: Trip,
    *,
    total_duration_seconds: float | None = None,
    video_offsets: dict[RecordingId, float] | None = None,
) -> str | None:
    """Merge every recording's .lrc in the trip into one trip-relative
    LRC string, sorted by start time. Returns None if no recording in
    the trip has an .lrc.

    If total_duration_seconds is given and the merged cues end before
    it, an empty trailing line is appended near the end of the video
    (see _pad_to_duration).

    `video_offsets`, if given, positions each recording's cues by its
    own real position in the concatenated video rather than its ID
    -timestamp gap from the trip's start - see
    `_merge_subtitle_segments()`'s own docstring for why that matters
    whenever a Parking recording sits mid-trip.
    """

    segments = _merge_subtitle_segments(
        trip, Asset.LYRICS, parse_lrc, video_offsets
    )
    if not segments:
        return None
    segments = _pad_to_duration(segments, total_duration_seconds)
    return format_lrc(segments)
