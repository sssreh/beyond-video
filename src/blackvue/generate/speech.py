"""
Transcription, diarization and translation.

Engines: faster-whisper (transcription), pyannote.audio (speaker
diarization), argos-translate (translation).

Copyright (C) 2026 Christer R. (sssreh)

SPDX-License-Identifier: GPL-3.0-or-later
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from dataclasses import field
from pathlib import Path

from .media import MediaToolError

_WHISPER_MODEL_CACHE: dict[str, object] = {}
_DIARIZATION_PIPELINE_CACHE: dict[str, object] = {}

DIARIZATION_MODEL = "pyannote/speaker-diarization-3.1"

# The diarization pipeline above pulls this model in as a dependency
# at load time - its license needs accepting too, separately from the
# diarization model's own license, or Pipeline.from_pretrained() fails
# even with a valid, license-accepted token.
SEGMENTATION_MODEL = "pyannote/segmentation-3.0"

_MISSING_TOKEN_MESSAGE = (
    "speaker diarization needs a HuggingFace access token:\n"
    "  1. Create one at https://huggingface.co/settings/tokens "
    "(read access is enough)\n"
    f"  2. Accept the model license at "
    f"https://huggingface.co/{DIARIZATION_MODEL}\n"
    f"  3. Also accept https://huggingface.co/{SEGMENTATION_MODEL} "
    "(the diarization model depends on it)\n"
    "  4. Pass --hf-token TOKEN, or set the HF_TOKEN environment variable"
)


@dataclass(frozen=True)
class SpeechSegment:
    """One transcribed segment, with timing."""

    start: float
    end: float
    text: str


@dataclass(frozen=True)
class Transcript:
    """The result of transcribing a media file."""

    text: str
    language: str
    segments: tuple[SpeechSegment, ...] = field(default_factory=tuple)


@dataclass(frozen=True)
class SpeakerTurn:
    """One diarized speaker turn."""

    start: float
    end: float
    speaker: str


def _get_whisper_model(model_size: str):
    """Return a cached faster-whisper model, loading it if needed."""

    if model_size not in _WHISPER_MODEL_CACHE:
        try:
            from faster_whisper import WhisperModel
        except ImportError as exc:
            raise MediaToolError(
                "faster-whisper is not installed "
                "(pip install faster-whisper)"
            ) from exc

        _WHISPER_MODEL_CACHE[model_size] = WhisperModel(
            model_size,
            device="cpu",
            compute_type="int8",
        )

    return _WHISPER_MODEL_CACHE[model_size]


def detect_language(source: Path, *, model_size: str = "small") -> str:
    """Cheaply detect the spoken language of source.

    Whisper detects the language from roughly the first 30 seconds
    of audio before transcribing the rest, so this only pays for
    that short window - not a full decode of the file. Useful to
    know the language up front (e.g. for naming an output file)
    without committing to a full transcription.
    """

    model = _get_whisper_model(model_size)

    try:
        _, info = model.transcribe(str(source))
    except Exception as exc:
        raise MediaToolError(
            f"language detection failed for {source.name}: {exc}"
        ) from exc

    return info.language


def transcribe(
    source: Path,
    *,
    language: str | None = None,
    model_size: str = "small",
) -> Transcript:
    """Transcribe the audio track of source using faster-whisper.

    source may be a video file or an already-extracted audio file -
    faster-whisper reads either directly via ffmpeg. If language is
    None, the spoken language is auto-detected.
    """

    model = _get_whisper_model(model_size)

    try:
        raw_segments, info = model.transcribe(str(source), language=language)

        segments = tuple(
            SpeechSegment(
                start=raw_segment.start,
                end=raw_segment.end,
                text=raw_segment.text.strip(),
            )
            for raw_segment in raw_segments
        )

        text = " ".join(segment.text for segment in segments).strip()
    except Exception as exc:  # faster-whisper/ffmpeg failures
        raise MediaToolError(
            f"transcription failed for {source.name}: {exc}"
        ) from exc

    return Transcript(
        text=text,
        language=info.language,
        segments=segments,
    )


def _get_diarization_pipeline(hf_token: str | None):
    """Return a cached pyannote.audio diarization pipeline."""

    cache_key = hf_token or ""

    if cache_key not in _DIARIZATION_PIPELINE_CACHE:
        try:
            from pyannote.audio import Pipeline
        except ImportError as exc:
            raise MediaToolError(
                "pyannote.audio is not installed "
                "(pip install pyannote.audio)"
            ) from exc

        token = (
            hf_token
            or os.environ.get("HF_TOKEN")
            or os.environ.get("HUGGINGFACE_TOKEN")
        )

        if not token:
            raise MediaToolError(_MISSING_TOKEN_MESSAGE)

        try:
            pipeline = Pipeline.from_pretrained(
                DIARIZATION_MODEL,
                use_auth_token=token,
            )
        except Exception as exc:
            raise MediaToolError(
                f"could not load diarization model: {exc} - if you have a "
                "token but haven't accepted both model licenses yet, "
                f"visit https://huggingface.co/{DIARIZATION_MODEL} and "
                f"https://huggingface.co/{SEGMENTATION_MODEL}"
            ) from exc

        _DIARIZATION_PIPELINE_CACHE[cache_key] = pipeline

    return _DIARIZATION_PIPELINE_CACHE[cache_key]


def diarize(
    source: Path,
    *,
    hf_token: str | None = None,
) -> tuple[SpeakerTurn, ...]:
    """Return who-spoke-when speaker turns for source.

    If source is a container format pyannote.audio's loader cannot
    read directly, extract audio first (--extract-audio) and
    diarize the .aac file instead.
    """

    pipeline = _get_diarization_pipeline(hf_token)

    try:
        annotation = pipeline(str(source))
    except Exception as exc:
        raise MediaToolError(
            f"diarization failed for {source.name}: {exc}"
        ) from exc

    return tuple(
        SpeakerTurn(start=turn.start, end=turn.end, speaker=speaker)
        for turn, _, speaker in annotation.itertracks(yield_label=True)
    )


def _speaker_for(
    segment: SpeechSegment,
    turns: tuple[SpeakerTurn, ...],
) -> str | None:
    """Return the speaker whose turn overlaps segment the most."""

    midpoint = (segment.start + segment.end) / 2

    for turn in turns:
        if turn.start <= midpoint <= turn.end:
            return turn.speaker

    best_speaker: str | None = None
    best_overlap = 0.0

    for turn in turns:
        overlap = max(
            0.0,
            min(segment.end, turn.end) - max(segment.start, turn.start),
        )

        if overlap > best_overlap:
            best_overlap = overlap
            best_speaker = turn.speaker

    return best_speaker


def format_diarized_transcript(
    segments: tuple[SpeechSegment, ...],
    turns: tuple[SpeakerTurn, ...],
) -> str:
    """Merge Whisper segments with diarized turns into labeled lines.

    Consecutive segments attributed to the same speaker are grouped
    onto one line, e.g.:

        [SPEAKER_00] Hello, how's the drive going?
        [SPEAKER_01] Not bad, traffic's light today.
    """

    lines: list[str] = []
    current_speaker: str | None = None
    current_words: list[str] = []

    def _flush() -> None:
        if current_words:
            label = current_speaker or "UNKNOWN"
            lines.append(f"[{label}] {' '.join(current_words).strip()}")

    for segment in segments:
        speaker = _speaker_for(segment, turns)

        if speaker != current_speaker:
            _flush()
            current_speaker = speaker
            current_words = [segment.text]
        else:
            current_words.append(segment.text)

    _flush()

    return "\n".join(lines)


def translate(
    text: str,
    *,
    source_language: str,
    target_language: str,
) -> str:
    """Translate text from source_language to target_language.

    Uses whatever argos-translate language packages are already
    installed on this machine. Nothing is downloaded automatically -
    this project stays offline and private by default. Install a
    package yourself (see the argos-translate documentation) if the
    pair you need is missing.
    """

    try:
        import argostranslate.translate
    except ImportError as exc:
        raise MediaToolError(
            "argostranslate is not installed (pip install argostranslate)"
        ) from exc

    languages = argostranslate.translate.get_installed_languages()

    source = next(
        (lang for lang in languages if lang.code == source_language),
        None,
    )
    target = next(
        (lang for lang in languages if lang.code == target_language),
        None,
    )

    if source is None or target is None:
        raise MediaToolError(
            "no argos-translate language installed for "
            f"{source_language!r} -> {target_language!r} - install it "
            f"with 'bv-lang install {source_language} {target_language}'"
        )

    translation = source.get_translation(target)

    if translation is None:
        raise MediaToolError(
            "argos-translate has no installed package for "
            f"{source_language!r} -> {target_language!r} - install it "
            f"with 'bv-lang install {source_language} {target_language}'"
        )

    return translation.translate(text)
