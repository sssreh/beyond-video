"""
SRT and LRC subtitle/lyric export.

Whisper's SpeechSegment and pyannote's SpeakerTurn already carry
start/end timestamps (see speech.py) - this module just formats them
into two standard, widely-supported sidecar formats instead of
beyond-video inventing its own timestamp notation:

  - SRT (SubRip): numbered cues with a start --> end range per line,
    the common video-subtitle format almost every player understands.
  - LRC: a single [mm:ss.xx] timestamp per line, the format karaoke/
    lyrics-sync players use - a lighter-weight alternative when you
    just want a per-line timestamp to scrub through a conversation,
    not full subtitle-file semantics.

Both formats optionally take a diarized speaker label as a
"[SPEAKER_XX] " prefix on the cue text, matching the convention
format_diarized_transcript already uses for plain-text output.

Copyright (C) 2026 Christer R. (sssreh)

SPDX-License-Identifier: GPL-3.0-or-later
"""

from __future__ import annotations

import re

from .speech import SpeakerTurn
from .speech import SpeechSegment
from .speech import speaker_for

_SRT_TIME_PATTERN = re.compile(
    r"(\d+):(\d{2}):(\d{2}),(\d{3})\s*-->\s*(\d+):(\d{2}):(\d{2}),(\d{3})"
)
_LRC_TIME_PATTERN = re.compile(r"^\[(\d+):(\d{2})\.(\d{2})\]\s?(.*)$")


def _cue_text(segment: SpeechSegment, turns: tuple[SpeakerTurn, ...] | None) -> str:
    if not turns:
        return segment.text

    speaker = speaker_for(segment, turns)
    label = speaker or "UNKNOWN"

    return f"[{label}] {segment.text}"


def _srt_timestamp(seconds: float) -> str:
    """Format seconds as SRT's HH:MM:SS,mmm timestamp."""

    total_ms = round(seconds * 1000)
    hours, remainder_ms = divmod(total_ms, 3_600_000)
    minutes, remainder_ms = divmod(remainder_ms, 60_000)
    secs, ms = divmod(remainder_ms, 1_000)

    return f"{hours:02d}:{minutes:02d}:{secs:02d},{ms:03d}"


def _lrc_timestamp(seconds: float) -> str:
    """Format seconds as LRC's [mm:ss.xx] timestamp."""

    total_hundredths = round(seconds * 100)
    minutes, remainder_hundredths = divmod(total_hundredths, 6_000)
    secs, hundredths = divmod(remainder_hundredths, 100)

    return f"[{minutes:02d}:{secs:02d}.{hundredths:02d}]"


def format_srt(
    segments: tuple[SpeechSegment, ...],
    turns: tuple[SpeakerTurn, ...] | None = None,
) -> str:
    """Format transcript segments as an SRT subtitle file.

    If turns is given, each cue is prefixed with the speaker
    attributed to that segment (see speaker_for()).
    """

    blocks = []

    for index, segment in enumerate(segments, start=1):
        blocks.append(
            f"{index}\n"
            f"{_srt_timestamp(segment.start)} --> {_srt_timestamp(segment.end)}\n"
            f"{_cue_text(segment, turns)}\n"
        )

    return "\n".join(blocks)


def format_lrc(
    segments: tuple[SpeechSegment, ...],
    turns: tuple[SpeakerTurn, ...] | None = None,
) -> str:
    """Format transcript segments as an LRC lyric/timestamp file - one
    [mm:ss.xx] line per segment, timestamped at the segment's start.

    If turns is given, each line is prefixed with the speaker
    attributed to that segment (see speaker_for()).
    """

    lines = [
        f"{_lrc_timestamp(segment.start)} {_cue_text(segment, turns)}"
        for segment in segments
    ]

    return "\n".join(lines)


def _seconds_from_srt_match(match: re.Match) -> tuple[float, float]:
    h1, m1, s1, ms1, h2, m2, s2, ms2 = match.groups()
    start = int(h1) * 3600 + int(m1) * 60 + int(s1) + int(ms1) / 1000
    end = int(h2) * 3600 + int(m2) * 60 + int(s2) + int(ms2) / 1000
    return start, end


def parse_srt(text: str) -> tuple[SpeechSegment, ...]:
    """Parse an SRT file's cues back into SpeechSegments.

    The inverse of format_srt() - any "[SPEAKER_XX] " prefix baked
    into a cue's text by a diarized export is left as part of
    segment.text rather than parsed back out, since format_srt()
    treats it as opaque text too (turns=None). Cue index numbers are
    discarded on read; format_srt() renumbers sequentially anyway, so
    they carry no information worth keeping.
    """

    segments = []

    for block in re.split(r"\r?\n\r?\n+", text.strip()):
        if not block.strip():
            continue

        lines = block.splitlines()
        timing_index = next(
            (i for i, line in enumerate(lines) if _SRT_TIME_PATTERN.search(line)),
            None,
        )
        if timing_index is None:
            continue

        match = _SRT_TIME_PATTERN.search(lines[timing_index])
        start, end = _seconds_from_srt_match(match)
        cue_text = "\n".join(lines[timing_index + 1:]).strip()

        segments.append(SpeechSegment(start=start, end=end, text=cue_text))

    return tuple(segments)


def parse_lrc(text: str) -> tuple[SpeechSegment, ...]:
    """Parse an LRC file's lines back into SpeechSegments.

    The inverse of format_lrc(). LRC has no explicit end time, so
    each segment's end is set equal to its start - format_lrc() only
    ever reads segment.start, so this round-trips cleanly; anything
    that does care about a duration should use SRT instead.
    """

    segments = []

    for line in text.splitlines():
        match = _LRC_TIME_PATTERN.match(line)
        if match is None:
            continue

        minutes, secs, hundredths, cue_text = match.groups()
        start = int(minutes) * 60 + int(secs) + int(hundredths) / 100

        segments.append(SpeechSegment(start=start, end=start, text=cue_text))

    return tuple(segments)
