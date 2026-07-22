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
) -> tuple[SpeechSegment, ...]:
    trip_start = trip.start_timestamp
    segments: list[SpeechSegment] = []

    for recording in trip:
        subtitle_file = recording.file(asset)
        if subtitle_file is None:
            continue

        offset_seconds = (recording.id.timestamp - trip_start).total_seconds()
        text = subtitle_file.path.read_text(encoding="utf-8")

        segments.extend(
            _shift(segment, offset_seconds) for segment in parser(text)
        )

    return tuple(sorted(segments, key=lambda segment: segment.start))


def merge_srt(trip: Trip) -> str | None:
    """Merge every recording's .srt in the trip into one trip-relative
    SRT string, cues renumbered and sorted by start time. Returns None
    if no recording in the trip has an .srt."""

    segments = _merge_subtitle_segments(trip, Asset.SUBTITLES, parse_srt)
    if not segments:
        return None
    return format_srt(segments)


def merge_lrc(trip: Trip) -> str | None:
    """Merge every recording's .lrc in the trip into one trip-relative
    LRC string, sorted by start time. Returns None if no recording in
    the trip has an .lrc."""

    segments = _merge_subtitle_segments(trip, Asset.LYRICS, parse_lrc)
    if not segments:
        return None
    return format_lrc(segments)
