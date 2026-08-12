"""
SRT subtitle export.

Whisper's SpeechSegment and pyannote's SpeakerTurn already carry
start/end timestamps (see speech.py) - this module just formats them
into SRT (SubRip): numbered cues with a start --> end range per line,
the common video-subtitle format almost every player understands,
instead of beyond-video inventing its own timestamp notation.

Optionally takes a diarized speaker label as a "[SPEAKER_XX] " prefix
on the cue text, matching the convention format_diarized_transcript
already uses for plain-text output.

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
