"""
Generated assets.

Copyright (C) 2026 Christer R. (sssreh)

SPDX-License-Identifier: GPL-3.0-or-later
"""

from .language_codes import normalize_language
from .language_codes import short_code
from .media import SILENCE_THRESHOLD_DB
from .media import MediaInfo
from .media import MediaToolError
from .media import compute_span
from .media import extract_audio
from .media import extract_video_thumbnail
from .media import get_span
from .media import is_audio_silent
from .media import probe
from .media import probe_audio_codec
from .media import select_source
from .mp4_box_reader import Mp4Info
from .mp4_box_reader import read_mp4_info
from .scene import DEFAULT_MODEL as SCENE_DEFAULT_MODEL
from .scene import SceneOptions
from .scene import describe_scene
from .scene import extract_description_section
from .scene import summarize_trip
from .scene import unload_scene_model
from .scene import vision_gpu_available
from .speech import DEPENDENT_MODELS
from .speech import DIARIZATION_MODEL
from .speech import SEGMENTATION_MODEL
from .speech import SpeakerTurn
from .speech import SpeechSegment
from .speech import Transcript
from .speech import detect_language
from .speech import diarize
from .speech import format_diarized_transcript
from .speech import gpu_available
from .speech import speaker_for
from .speech import transcribe
from .speech import translate
from .subtitles import format_srt
from .subtitles import parse_srt

__all__ = [
    "DEPENDENT_MODELS",
    "DIARIZATION_MODEL",
    "SCENE_DEFAULT_MODEL",
    "SEGMENTATION_MODEL",
    "SILENCE_THRESHOLD_DB",
    "MediaInfo",
    "MediaToolError",
    "Mp4Info",
    "SceneOptions",
    "SpeakerTurn",
    "SpeechSegment",
    "Transcript",
    "compute_span",
    "describe_scene",
    "detect_language",
    "diarize",
    "extract_audio",
    "extract_description_section",
    "extract_video_thumbnail",
    "format_diarized_transcript",
    "format_srt",
    "get_span",
    "gpu_available",
    "is_audio_silent",
    "normalize_language",
    "parse_srt",
    "probe",
    "probe_audio_codec",
    "read_mp4_info",
    "select_source",
    "short_code",
    "speaker_for",
    "summarize_trip",
    "transcribe",
    "translate",
    "unload_scene_model",
    "vision_gpu_available",
]
