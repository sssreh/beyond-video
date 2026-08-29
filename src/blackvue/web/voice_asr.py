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
"""

from __future__ import annotations

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

    Reuses generate/speech.py's own Transcript dataclass rather than
    defining a near-duplicate here - Transcript is a generic
    (text, language, segments) shape, not Whisper-specific.
    `segments` is always left empty: Qwen3-ASR's own transcribe() call
    here never requests forced-aligner timestamps (bv-search's voice
    UI only ever needs the plain transcript text), and nothing
    downstream of this route reads Transcript.segments."""

    loaded = _get_asr_model(model_name, force_cpu=force_cpu)
    context = _build_context(known_places or ())

    try:
        results = loaded.model.transcribe(
            audio=str(source), context=context, language=None
        )
    except Exception as exc:
        raise MediaToolError(f"Qwen3-ASR transcription failed: {exc}") from exc

    if not results:
        raise MediaToolError("Qwen3-ASR returned no transcription result")

    result = results[0]
    text = (getattr(result, "text", "") or "").strip()
    language = getattr(result, "language", "") or ""
    return Transcript(text=text, language=language, segments=())
