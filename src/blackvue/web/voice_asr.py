"""Qwen3-ASR-1.7B transcription backend for the bv-search "Search by
voice" feature - replaces faster-whisper (generate/speech.py's
transcribe()) *only* for this one route. Christer, after diagnosing a
real failed search: bv-search's --place lookup failed because the
spoken place name "Vårby gård"/"Vårbygård" (accounts differ on the
exact spelling, but both agree it's one recognizable Swedish place)
came back mis-transcribed as two unrelated common words ("vår
bygård"). Investigation (chat, not code) found Qwen2-Audio doesn't
support Swedish at all, but Qwen3-ASR-1.7B does (one of 30 languages),
runs comfortably on Christer's hardware (2-7GB VRAM), and - the part
that actually matters here - beats Whisper-large-v3 on published
word-error-rate benchmarks while *also* supporting native vocabulary
biasing via a `context` string passed alongside the audio
(`qwen_asr.Qwen3ASRModel.transcribe(audio=..., context=..., ...)`,
confirmed against QwenLM/Qwen3-ASR's own example script - not a
homegrown workaround). Christer's explicit scope decision when asked:
"Replace Whisper for voice search only" - every other transcription
path in this project (bv-generate --transcribe/--translate, subtitle
generation, bv-scribe's own audio handling) keeps using
generate/speech.py's Whisper-based transcribe() completely unchanged.

Two independent building blocks, both usable on their own:

1. known_places_from_params() (pure): given the params dicts from past
   bv-web bv-search runs (web/app.py's own _recent_web_runs("bv-search"),
   already newest-first), pull out distinct, non-empty "place" values -
   these are Christer's own real place names from real prior searches,
   the exact kind of proper noun Whisper (and presumably Qwen3-ASR
   too, without help) tends to mangle. Capped at `limit` entries so an
   old, long-lived history file doesn't grow the bias prompt without
   bound.

2. transcribe_voice_query() (impure): loads/reuses a cached
   Qwen3ASRModel and runs one transcription, with `known_places` (if
   any) folded into the `context` bias string. Deliberately mirrors
   generate/scene.py's _get_scene_model() cache-by-key pattern and
   voice_llm.py's _get_small_text_model() precedent - same project-
   wide "load once, reuse across requests" shape every model-backed
   module here already follows.

Split into pure/impure the same way voice_llm.py is (see that module's
own docstring for the reasoning): known_places_from_params() and
_build_context() are offline-testable; _get_asr_model()/
transcribe_voice_query() actually load and run a real model and are
untested in this sandbox (no GPU/qwen_asr/network here), matching
generate/test_scene.py's own established precedent of never exercising
the real model-loading path in CI.

Real-hardware follow-up: Christer's first live run hit "Qwen3-ASR
transcription failed: Error opening '...tmpXXXX.webm': Format not
recognised." Whisper (faster-whisper) decodes whatever container ffmpeg
understands internally, so handing it the browser's raw
MediaRecorder .webm blob (see job_new_bv_search.html's "Search by
voice" JS) always just worked. Qwen3-ASR's own transcribe() apparently
loads audio via a libsndfile-backed reader (soundfile/librosa), which
has no WebM/Opus container support at all - that assumption
(untestable in this no-qwen_asr sandbox, so never caught before real
hardware surfaced it) was wrong. Fixed by explicitly transcoding to WAV
via ffmpeg first (_convert_to_wav(), mirrors generate/media.py's own
extract_audio() ffmpeg-subprocess pattern) - WAV/PCM is unambiguously
readable by any audio loader, unlike relying on Qwen3-ASR to guess a
container format the way Whisper's own ffmpeg-based decoder does.
"""

from __future__ import annotations

import os
import subprocess
import tempfile
from collections.abc import Mapping
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from ..generate.media import MediaToolError
from ..generate.speech import Transcript

DEFAULT_ASR_MODEL = "Qwen/Qwen3-ASR-1.7B"

_ASR_MODEL_CACHE: dict[str, "_LoadedAsrModel"] = {}


@dataclass(frozen=True)
class _LoadedAsrModel:
    model: object


def known_places_from_params(
    params_list: Sequence[Mapping[str, object]], *, limit: int = 20
) -> list[str]:
    """Pure - distinct, non-empty "place" values pulled from past
    bv-search web-form param snapshots, most-recent-first order
    preserved from `params_list` (callers pass an already newest-first
    sequence - see web/app.py's _recent_web_runs()), deduplicated
    case-insensitively (so "Vårby gård" and "vårby gård" from two
    different runs don't both appear) and capped at `limit`. Every
    other bv-search form field is ignored - only "place" is a proper
    noun Qwen3-ASR's own transcription can plausibly be biased toward
    recognizing; radius/dates/text aren't."""

    seen: set[str] = set()
    places: list[str] = []
    for params in params_list:
        place = params.get("place")
        if not isinstance(place, str):
            continue
        place = place.strip()
        if not place:
            continue
        key = place.casefold()
        if key in seen:
            continue
        seen.add(key)
        places.append(place)
        if len(places) >= limit:
            break
    return places


_LEARNED_PLACES_FILENAME = "known_places_learned.txt"


def known_places_from_learned(config_dir: Path) -> list[str]:
    """Impure - reads `config_dir / "known_places_learned.txt"` (one
    place name per line) if it exists, otherwise returns an empty list.

    This file is never hand-maintained - see remember_known_place()
    below, which is the only thing that ever writes to it. First
    attempt at this gap was a manually-maintained known_places.txt
    that Christer would have had to create and keep up to date
    himself; Christer's own reaction: "I dont like halfway fixes like
    known_places, that needs to be updated for every single user" -
    fair complaint, a bias list only as good as everyone's willingness
    to hand-edit a text file doesn't scale past Christer's own machine,
    let alone to other people running this project. This replaces that
    file entirely with one the program keeps up to date on its own."""

    path = config_dir / _LEARNED_PLACES_FILENAME
    if not path.exists():
        return []

    places: list[str] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        places.append(line)
    return places


def remember_known_place(place: str, config_dir: Path) -> None:
    """Impure - appends `place` to `config_dir /
    "known_places_learned.txt"` if it isn't already there
    (case-insensitive dedup, same rule known_places_from_params() uses),
    creating the file/directory on first use.

    Called from cli/bv_search.py's _run() right after a --place lookup
    actually resolves to real coordinates (web/jobs.py's
    start_bv_search() runs through that exact same _run() function, so
    this covers both the CLI and the web UI with one hook) - the moment
    a place name is proven to be real and correctly spelled, by the one
    source of truth that matters (Nominatim actually geocoded it), is
    also the best moment to remember it for future ASR bias. No manual
    file to create or maintain: the first time anyone using this
    project successfully searches near "Vårby gård" - whether they
    typed it, corrected a bad voice guess before hitting Start, or got
    lucky on the first transcription - it's remembered automatically
    from then on, same as known_places_from_params()'s existing
    history-derived bias, just without waiting for a full job to be
    submitted and recorded first.

    Silently does nothing if `place` is blank/whitespace-only. Existing
    entries are preserved verbatim (first-seen spelling wins) - not
    rewriting the file on every call keeps this a plain append, and a
    place already known well enough to resolve doesn't need its
    spelling second-guessed."""

    place = place.strip()
    if not place:
        return

    config_dir.mkdir(parents=True, exist_ok=True)
    path = config_dir / _LEARNED_PLACES_FILENAME
    existing = known_places_from_learned(config_dir)
    if place.casefold() in {p.casefold() for p in existing}:
        return

    with path.open("a", encoding="utf-8") as f:
        f.write(place + "\n")


def _build_context(known_places: Sequence[str]) -> str:
    """Pure - builds the `context` bias string Qwen3ASRModel.transcribe()
    accepts, in the same "May say: ..." phrasing the upstream project's
    own examples use (see this module's own docstring). Empty string
    (Qwen3-ASR's own "no bias" value, confirmed against the upstream
    example script's `context=["", ...]` entries for un-biased batch
    members) when there's nothing to bias toward yet - a fresh install
    with no bv-search history."""

    if not known_places:
        return ""
    return "May mention these place names: " + ", ".join(known_places) + "."


def _get_asr_model(model_name: str = DEFAULT_ASR_MODEL, *, force_cpu: bool = False) -> _LoadedAsrModel:
    """Impure - loads/reuses a Qwen3ASRModel, cached by (model, device)
    key exactly the way generate/scene.py's _get_scene_model() and
    voice_llm.py's _get_small_text_model() already cache theirs.
    Deferred import: this module must stay importable even when
    torch/qwen_asr aren't installed - only actually transcribing needs
    them."""

    cache_key = f"{model_name}:{'cpu' if force_cpu else 'auto'}"
    if cache_key in _ASR_MODEL_CACHE:
        return _ASR_MODEL_CACHE[cache_key]

    try:
        import torch
        from qwen_asr import Qwen3ASRModel
    except ImportError as exc:
        raise MediaToolError(
            "Qwen3-ASR voice-search transcription needs the qwen-asr "
            f"package installed ({exc}) - pip install qwen-asr (see "
            "pyproject.toml's voice-asr extra)"
        ) from exc

    device_map = "cpu" if force_cpu else "auto"
    if not force_cpu:
        try:
            if not torch.cuda.is_available():
                device_map = "cpu"
        except Exception:
            device_map = "cpu"

    try:
        model = Qwen3ASRModel.from_pretrained(
            model_name,
            dtype=torch.bfloat16,
            device_map=device_map,
            max_inference_batch_size=1,
            max_new_tokens=256,
        )
    except Exception as exc:
        raise MediaToolError(f"could not load Qwen3-ASR model {model_name!r}: {exc}") from exc

    loaded = _LoadedAsrModel(model=model)
    _ASR_MODEL_CACHE[cache_key] = loaded
    return loaded


def unload_asr_model() -> None:
    """Evict the cached Qwen3-ASR model(s) and release their GPU
    memory - mirrors generate/scene.py's unload_scene_model() and
    voice_llm.py's unload_text_model(). Not currently wired into any
    cleanup hook, same "provided for symmetry / manual+test cleanup"
    reasoning voice_llm.py's own unload_text_model() docstring
    already gives - this route isn't a JobRunner Job either."""

    if not _ASR_MODEL_CACHE:
        return

    _ASR_MODEL_CACHE.clear()

    import gc

    gc.collect()

    try:
        import torch
    except ImportError:
        return

    if torch.cuda.is_available():
        torch.cuda.empty_cache()


def _convert_to_wav(source: Path) -> Path:
    """Impure - transcodes `source` (any container ffmpeg can read -
    the browser's MediaRecorder output is .webm/Opus) to a 16kHz mono
    PCM WAV file in a fresh temp path, via ffmpeg. See this module's
    own docstring for why this exists: Qwen3-ASR's transcribe() can't
    read .webm directly the way Whisper always could, confirmed by a
    real "Format not recognised" failure on Christer's hardware. 16kHz
    mono is the standard ASR input rate (also what faster-whisper
    itself resamples everything to internally) - downsampling here
    rather than trusting Qwen3-ASR's own loader to do it avoids
    depending on that loader supporting anything but the plainest
    possible WAV.

    Raises MediaToolError on any ffmpeg failure, exactly like
    generate/media.py's own extract_audio(). Caller owns cleanup of
    the returned temp file (mirrors how web/app.py's
    transcribe_voice_search() route already owns cleanup of its own
    upload temp file)."""

    fd, wav_path_str = tempfile.mkstemp(suffix=".wav")
    os.close(fd)
    wav_path = Path(wav_path_str)

    try:
        subprocess.run(
            [
                "ffmpeg",
                "-v", "error",
                "-y",
                "-i", str(source),
                "-vn",
                "-ac", "1",
                "-ar", "16000",
                "-acodec", "pcm_s16le",
                str(wav_path),
            ],
            capture_output=True,
            text=True,
            check=True,
        )
    except FileNotFoundError as exc:
        wav_path.unlink(missing_ok=True)
        raise MediaToolError("ffmpeg not found on PATH") from exc
    except subprocess.CalledProcessError as exc:
        wav_path.unlink(missing_ok=True)
        raise MediaToolError(
            f"ffmpeg failed converting {source.name} to WAV: {exc.stderr.strip()}"
        ) from exc

    return wav_path


def transcribe_voice_query(
    source: Path,
    *,
    known_places: Sequence[str] | None = None,
    model_name: str = DEFAULT_ASR_MODEL,
    force_cpu: bool = False,
) -> Transcript:
    """Impure entry point - transcribes `source` (an audio/video file,
    same as generate/speech.py's transcribe()) via Qwen3-ASR, biasing
    toward `known_places` if given. Raises MediaToolError for anything
    that goes wrong loading the model or during transcription -
    web/app.py's transcribe_voice_search() route is expected to catch
    it exactly the way it already caught Whisper's own MediaToolError,
    no behavior change there.

    `source` is transcoded to WAV first (_convert_to_wav()) - see this
    module's own docstring for the real "Format not recognised" failure
    that made this necessary; Qwen3-ASR's own audio loader can't read
    the browser's raw .webm the way Whisper's ffmpeg-based decoder
    always could. The temp WAV is always cleaned up here, regardless of
    whether transcription succeeds.

    Reuses generate/speech.py's own Transcript dataclass rather than
    defining a near-duplicate here - Transcript is a generic
    (text, language, segments) shape, not Whisper-specific.
    `segments` is always left empty: Qwen3-ASR's own transcribe() call
    here never requests forced-aligner timestamps (bv-search's voice
    UI only ever needs the plain transcript text), and nothing
    downstream of this route reads Transcript.segments."""

    loaded = _get_asr_model(model_name, force_cpu=force_cpu)
    context = _build_context(known_places or ())

    wav_path = _convert_to_wav(source)
    try:
        try:
            results = loaded.model.transcribe(
                audio=str(wav_path), context=context, language=None
            )
        except Exception as exc:
            raise MediaToolError(f"Qwen3-ASR transcription failed: {exc}") from exc
    finally:
        wav_path.unlink(missing_ok=True)

    if not results:
        raise MediaToolError("Qwen3-ASR returned no transcription result")

    result = results[0]
    text = (getattr(result, "text", "") or "").strip()
    language = getattr(result, "language", "") or ""
    return Transcript(text=text, language=language, segments=())
