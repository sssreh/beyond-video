"""
Transcription, diarization and translation.

Engines: faster-whisper (transcription), pyannote.audio (speaker
diarization), argos-translate (translation).

Copyright (C) 2026 Christer R. (sssreh)

SPDX-License-Identifier: GPL-3.0-or-later
"""

from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass
from dataclasses import field
from pathlib import Path

from .media import MediaToolError

# pyannote.audio's own audio loading (source given as a path/string)
# resamples to this rate internally too, so decoding straight to it
# ourselves means no extra resampling work happens on pyannote's side
# either.
_DIARIZATION_SAMPLE_RATE = 16000

_WHISPER_MODEL_CACHE: dict[str, object] = {}
_DIARIZATION_PIPELINE_CACHE: dict[str, object] = {}

#  pyannote.audio 4.0 (released Sep 2025) replaced the legacy
# speaker-diarization-3.1 pipeline with this one as its recommended
# default: better accuracy per pyannote's own benchmarks, and - unlike
# 3.1 under 4.0, which reached into speaker-diarization-community-1's
# repo for a shared file - it packages its own underlying models in
# one repo, so (as far as observed) it only needs its own license
# accepted, not a chain of separate gated dependencies.
DIARIZATION_MODEL = "pyannote/speaker-diarization-community-1"

# Other gated repos a diarization pipeline might reach into at load
# time, whose licenses would need accepting too - not documented
# anywhere in one place, so this is "known so far" from hitting real
# 403s in practice, not exhaustive. Empty for DIARIZATION_MODEL as of
# this writing (see comment above); kept around in case a future
# pyannote.audio version reintroduces a cross-repo dependency.
DEPENDENT_MODELS: tuple[str, ...] = ()

# Legacy pipeline beyond-video originally targeted, kept only for
# reference/backwards compatibility with anything importing this name
# directly - no longer used as a dependency of DIARIZATION_MODEL.
SEGMENTATION_MODEL = "pyannote/segmentation-3.0"

_MISSING_TOKEN_MESSAGE = (
    "speaker diarization needs a HuggingFace access token:\n"
    "  1. Create one at https://huggingface.co/settings/tokens "
    "(read access is enough)\n"
    f"  2. Accept the model license at "
    f"https://huggingface.co/{DIARIZATION_MODEL}\n"
    + "".join(
        f"  {step}. Also accept https://huggingface.co/{model} "
        "(a model it depends on)\n"
        for step, model in enumerate(DEPENDENT_MODELS, start=3)
    )
    + "  If you still get a 403 for some other gated repo after that, "
    "accept its license too - the error names the exact repo each "
    "time.\n"
    f"  {len(DEPENDENT_MODELS) + 3}. Pass --hf-token TOKEN, or set the "
    "HF_TOKEN environment variable"
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

        # faster-whisper decodes in fixed-size chunks internally, so
        # the last segment's end timestamp can land slightly past the
        # audio's real length (info.duration, which faster-whisper
        # measures from the same decode) - clamp both ends so nothing
        # downstream (trip.srt in particular) ever runs longer than
        # the source it was transcribed from.
        segments = tuple(
            SpeechSegment(
                start=min(raw_segment.start, info.duration),
                end=min(raw_segment.end, info.duration),
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


def _load_pipeline(pipeline_class, token: str):
    """Call Pipeline.from_pretrained() with whichever token keyword
    the installed pyannote.audio/huggingface_hub version accepts.

    huggingface_hub renamed `use_auth_token` to `token` a while back,
    and newer pyannote.audio releases (installed via
    `pip install pyannote.audio` today) only accept the new name -
    passing the old one raises `TypeError: unexpected keyword argument
    'use_auth_token'`. Try the current name first and fall back to the
    old one, so this works whichever version ends up installed instead
    of pinning one exactly.
    """

    try:
        return pipeline_class.from_pretrained(DIARIZATION_MODEL, token=token)
    except TypeError:
        return pipeline_class.from_pretrained(
            DIARIZATION_MODEL, use_auth_token=token
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
            pipeline = _load_pipeline(Pipeline, token)
        except Exception as exc:
            known_models = ", ".join(
                f"https://huggingface.co/{model}"
                for model in (DIARIZATION_MODEL,) + DEPENDENT_MODELS
            )
            raise MediaToolError(
                f"could not load diarization model: {exc} - if this "
                "mentions a gated/restricted repo, visit the URL it "
                "names and accept its access conditions (pyannote "
                "pipelines can depend on more than one gated model - "
                f"known ones so far: {known_models})"
            ) from exc

        _DIARIZATION_PIPELINE_CACHE[cache_key] = pipeline

    return _DIARIZATION_PIPELINE_CACHE[cache_key]


def _load_waveform_via_ffmpeg(source: Path) -> dict:
    """Decode source to a mono waveform at _DIARIZATION_SAMPLE_RATE
    using ffmpeg directly, and return it in the in-memory
    {'waveform', 'sample_rate'} form pyannote.audio's pipelines accept.

    pyannote.audio 4.x normally reads audio itself via torchcodec,
    which needs its native DLL built against the exact installed
    ffmpeg/torch version combination - a fragile pairing that's easy
    to get wrong (particularly on Windows) and outside beyond-video's
    control. Since this project already shells out to ffmpeg directly
    everywhere else (extract_audio, probe), decoding here the same way
    sidesteps torchcodec's DLL matching entirely rather than depending
    on it working.
    """

    try:
        import numpy as np
    except ImportError as exc:
        raise MediaToolError(
            "numpy is not installed (pip install numpy)"
        ) from exc

    try:
        import torch
    except ImportError as exc:
        raise MediaToolError(
            "torch is not installed (pip install torch)"
        ) from exc

    try:
        result = subprocess.run(
            [
                "ffmpeg",
                "-v", "error",
                "-i", str(source),
                "-f", "f32le",
                "-ac", "1",
                "-ar", str(_DIARIZATION_SAMPLE_RATE),
                "-",
            ],
            capture_output=True,
            check=True,
        )
    except FileNotFoundError as exc:
        raise MediaToolError("ffmpeg not found on PATH") from exc
    except subprocess.CalledProcessError as exc:
        raise MediaToolError(
            f"ffmpeg failed to decode {source.name} for diarization: "
            f"{exc.stderr.decode(errors='replace').strip()}"
        ) from exc

    samples = np.frombuffer(result.stdout, dtype=np.float32)
    waveform = torch.from_numpy(samples.copy()).unsqueeze(0)  # (channel, time)

    return {"waveform": waveform, "sample_rate": _DIARIZATION_SAMPLE_RATE}


def diarize(
    source: Path,
    *,
    hf_token: str | None = None,
) -> tuple[SpeakerTurn, ...]:
    """Return who-spoke-when speaker turns for source.

    If source is a container format pyannote.audio's loader cannot
    read directly, extract audio first (--extract-audio) and
    diarize the .aac file instead. Audio is decoded via ffmpeg
    directly (see _load_waveform_via_ffmpeg) rather than handed to
    pyannote.audio as a path, to avoid depending on torchcodec.
    """

    pipeline = _get_diarization_pipeline(hf_token)
    audio_input = _load_waveform_via_ffmpeg(source)

    try:
        output = pipeline(audio_input)
    except Exception as exc:
        raise MediaToolError(
            f"diarization failed for {source.name}: {exc}"
        ) from exc

    # The legacy speaker-diarization-3.1 pipeline returns the
    # Annotation directly. DIARIZATION_MODEL (speaker-diarization-
    # community-1) instead returns a wrapper object exposing it as
    # .speaker_diarization. Support both, so this keeps working
    # whichever pipeline is actually configured.
    annotation = getattr(output, "speaker_diarization", output)

    return tuple(
        SpeakerTurn(start=turn.start, end=turn.end, speaker=speaker)
        for turn, _, speaker in annotation.itertracks(yield_label=True)
    )


def speaker_for(
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
        speaker = speaker_for(segment, turns)

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
