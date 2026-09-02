"""Local-LLM structured extraction for voice search - an experimental,
optional alternative to voice_query.py + voice_time.py's hand-rolled
regex parsers, run *in parallel* with them rather than replacing them.
Christer's own framing when this was scoped: "About Voice search:
local-LLM structured extraction instead of the current parser. I would
like to try it out in parallel to what we already have." - see
WORKING_CONTEXT.md's earlier "Note: local-LLM structured extraction for
voice search (future improvement)" for the original proposal this
implements (with two changes from that note's sketch, both from
Christer's own answers below: no Ollama/vLLM - this project already has
a working local-model pattern in generate/scene.py, so that's reused/
mirrored instead of standing up a second inference stack; and the field
shape matches voice_query.py/voice_time.py's own combined output
exactly, not the note's illustrative location_keyword/scene_contains
sketch).

Three design decisions, all Christer's own answers when asked:

1. Model choice is runtime-selectable between two options (not a
   single hardcoded model) - MODEL_SCENE reuses the already-loaded
   generate/scene.py Qwen3-VL-8B-Instruct model in text-only mode (no
   images/video attached to the prompt, via that module's own
   generate_text_only()), MODEL_SMALL loads a separate, dedicated small
   text model (DEFAULT_SMALL_TEXT_MODEL, Qwen3-1.7B - bumped from the
   original Qwen2.5-1.5B-Instruct once Christer asked whether any of
   this project's local models had newer versions worth using on his
   24GB card: a same-size swap, basically free on VRAM, but a
   generation newer with meaningfully better reasoning/instruction-
   following per Qwen's own benchmarks. Qwen3 dropped the separate
   Base/Instruct naming - Qwen/Qwen3-1.7B is itself the chat-tuned
   checkpoint, unified with a thinking mode forced off via
   enable_thinking=False below, since this is a fast structured-
   extraction call, not a reasoning task) via
   plain transformers AutoModelForCausalLM/AutoTokenizer. Reusing the
   scene model avoids the VRAM-contention concern the original
   WORKING_CONTEXT.md note flagged (relevant on a shared/lower-VRAM
   box, e.g. Christer's dual-RTX-3080-Ti desktop); a dedicated small
   model avoids evicting/reloading the ~16GB scene model just to
   answer a few-token structured-extraction query, and should
   generally be faster - genuinely a real tradeoff either way, hence
   runtime-selectable rather than picking one. (Christer separately
   mentioned having a laptop RTX 5090 for this work - ample VRAM either
   way, but the runtime-selectable design already covers both cases
   regardless of which machine actually runs it.)

2. UI surfacing (see web/app.py's transcribe_voice_search route and
   templates/job_new_bv_search.html's JS): ORIGINALLY this module's
   result was never what auto-filled the bv-search form - the regex
   parsers (voice_query.py/voice_time.py) did that unchanged, with this
   module's output surfaced alongside as a second, comparable
   interpretation. That changed in a later session (Christer: "i
   thought audio llm would understand that, its a thinker not sound to
   text only") - this module is now the *primary* parser by default,
   with the regex parsers as the fallback (LLM off, LLM errors, or -
   added still later, see the route's own comment - the LLM ran fine
   but found no place/date at all while the regex parser did). Either
   way, both results are always computed and both are always shown -
   whichever one is authoritative fills the form, the other is offered
   as a one-click "use this instead" alternative. The field shape
   below (design decision 3) is unchanged by any of this.

3. Extraction scope matches the *combined* regex parsers' own field
   shape exactly - text/place/radius_meters/timestamp/from_/until -
   nothing more (no scene-keyword or asset-restriction extraction in
   this first pass, deliberately deferred to a later increment if this
   comparison shows the LLM approach is worth extending).

Split into pure, offline-testable functions (_build_prompt(),
_parse_llm_json_response(), _build_parsed_result()) and two impure
functions that actually load and run a model (_generate_via_scene_model(),
_generate_via_small_text_model(), called from extract_voice_query_llm())
- same shape this project's test suite already expects for anything
that touches a real model (see tests/blackvue/generate/test_scene.py:
the model-loading path itself is never exercised in CI, only the
prompt-building/response-parsing logic around it). This sandbox has no
GPU/transformers/network, so none of the actual generation code below
has been run against a real model - only the pure functions are
covered by tests/blackvue/web/test_voice_llm.py.

Today's date is always passed in explicitly (`today: date`), never
inferred inside this module - matches voice_time.py's own
parse_spoken_timerange(transcript, today) signature exactly, and is
required so the prompt can ground relative dates ("last week",
"yesterday") in a real calendar date rather than the model's own
(unreliable, training-cutoff-anchored) sense of "today". Any date the
model returns is sanity-checked (_validate_ymd()) before being trusted
- exactly the safeguard the original WORKING_CONTEXT.md proposal note
flagged as necessary ("date arithmetic ... needs ... a sanity check
afterward - not blind trust in model output"), mirroring voice_time.py's
own _safe_date() guard against the same class of problem.

Also mirrors voice_query.py's AND-conflict-avoidance rule (see that
module's own docstring): bv-search ANDs Text against Place/Radius/date
filters, so if the model decided place/radius or a date was present, we
force text back to "" regardless of what the model itself put there -
a model that's supposed to null out "text" when it extracts a place or
date (the prompt asks it to) can't be trusted to always actually do
that, same reasoning this project already applies to model output
everywhere else (see DISCLAIMER in generate/scene.py)."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import date
from datetime import datetime

from ..generate.media import MediaToolError

DEFAULT_SMALL_TEXT_MODEL = "Qwen/Qwen3-1.7B"

MODEL_SCENE = "scene"
MODEL_SMALL = "small"
VALID_MODEL_CHOICES = (MODEL_SCENE, MODEL_SMALL)

_TEXT_MODEL_CACHE: dict[str, "_LoadedTextModel"] = {}


@dataclass(frozen=True)
class ParsedVoiceLLM:
    """Mirrors voice_query.py's ParsedVoiceQuery + voice_time.py's
    ParsedTimeRange combined into the single field shape bv-search's
    web form actually needs - see this module's own docstring, design
    decision 3. `raw_response` is kept (not shown in the form, but
    useful for debugging a bad extraction) - the model's own raw text
    output before JSON parsing."""

    text: str
    place: str | None
    radius_meters: float | None
    timestamp: str | None
    from_: str | None
    until: str | None
    raw_response: str


@dataclass(frozen=True)
class _LoadedTextModel:
    model: object
    tokenizer: object


def _build_prompt(transcript: str, today: date) -> str:
    """Pure prompt-builder - no model/network access, fully unit-
    testable. today's real date is injected directly (see module
    docstring) so the model resolves relative dates against a fact,
    not a guess."""

    return (
        "You are extracting structured search filters from a spoken "
        "query about a dashcam video archive. Today's date is "
        f"{today.isoformat()} ({today.strftime('%A')}).\n\n"
        f'Transcript: "{transcript}"\n\n'
        "Extract these fields as a single JSON object, with exactly "
        "these six keys - use null for any field the transcript "
        "doesn't mention, never omit a key:\n"
        '- "text": a free-text search phrase (transcript/translation/'
        "scene-description content) - only if the transcript is "
        "clearly searching for a topic or keyword rather than a place "
        "or a date, otherwise null.\n"
        '- "place": a place name mentioned as a search center (e.g. '
        '"near Slussen", "close to the airport") - null if none.\n'
        '- "radius_meters": a search radius in meters as a number, '
        "converting from any unit mentioned (km, miles) - only "
        'meaningful together with "place", null otherwise.\n'
        '- "timestamp": a single day, formatted YYYYMMDD, if the '
        'transcript names one specific day (e.g. "July 15th 2026", '
        '"yesterday") - null if it names a range or no date at all.\n'
        '- "from_": the start of a date range, formatted YYYYMMDD, if '
        'the transcript names a range (e.g. "last week", "from July '
        '1st to July 10th") - null otherwise.\n'
        '- "until": the end of that same date range, formatted '
        "YYYYMMDD - null unless \"from_\" is also set.\n\n"
        'Only one of "timestamp" or the ("from_", "until") pair should '
        "ever be set, never both. Resolve relative dates (\"yesterday\", "
        "\"last week\", \"this month\") against today's real date given "
        "above, not any other assumption. Output ONLY the JSON object, "
        "nothing else - no explanation, no markdown code fence."
    )


_CODE_FENCE_RE = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL)
_JSON_OBJECT_RE = re.compile(r"\{.*\}", re.DOTALL)


def _extract_json_blob(raw_text: str) -> str | None:
    """Tolerant JSON-blob extraction - strips a ```json ... ``` code
    fence if present, then grabs the first {...} span, so stray prose
    around the JSON (a common instruction-following miss on small
    models: "Here is the JSON: {...}") doesn't break parsing. Mirrors
    this project's existing tolerant-parsing philosophy for model
    output (see generate/scene.py's _parse_grounding_boxes()/
    _parse_batch_reads()). Returns None if no {...} span is found at
    all."""

    fence_match = _CODE_FENCE_RE.search(raw_text)
    candidate = fence_match.group(1) if fence_match else raw_text
    obj_match = _JSON_OBJECT_RE.search(candidate)
    return obj_match.group(0) if obj_match else None


def _parse_llm_json_response(raw_text: str) -> dict:
    """Pure JSON-parsing - no model/network access. Raises ValueError
    (not a bare JSONDecodeError/TypeError) on anything unparsable or
    not shaped like an object, so callers have one exception type to
    catch regardless of failure mode."""

    blob = _extract_json_blob(raw_text)
    if blob is None:
        raise ValueError(f"no JSON object found in LLM output: {raw_text!r}")
    try:
        data = json.loads(blob)
    except json.JSONDecodeError as exc:
        raise ValueError(f"malformed JSON from LLM: {exc}") from exc
    if not isinstance(data, dict):
        raise ValueError(f"expected a JSON object, got {type(data).__name__}")
    return data


def _validate_ymd(value: object) -> str | None:
    """Sanity-check a date string the model claims is YYYYMMDD -
    exactly the guard the original WORKING_CONTEXT.md proposal note
    flagged as necessary, mirroring voice_time.py's own _safe_date().
    Anything not a real calendar date in that exact shape (wrong type,
    wrong length, non-digits, an invalid date like day 31 of a 30-day
    month) is dropped to None rather than trusted - a bad date silently
    reaching bv-search's From/Until fields would just as silently
    return zero results, no crash to notice."""

    if not isinstance(value, str) or not re.fullmatch(r"\d{8}", value):
        return None
    try:
        datetime.strptime(value, "%Y%m%d")
    except ValueError:
        return None
    return value


def _build_parsed_result(raw_response: str, transcript: str) -> ParsedVoiceLLM:
    """Pure post-processing of one model call's raw text output into
    ParsedVoiceLLM - no model/network access, fully unit-testable by
    handing it a crafted raw_response string. Split out from
    extract_voice_query_llm() so the JSON-parsing/validation/AND-rule
    logic can be tested without a real model in this sandbox."""

    data = _parse_llm_json_response(raw_response)

    place = data.get("place")
    if not isinstance(place, str) or not place.strip():
        place = None

    radius_meters = data.get("radius_meters")
    if not isinstance(radius_meters, (int, float)) or isinstance(radius_meters, bool):
        radius_meters = None
    elif place is None:
        # radius_meters is only meaningful paired with place - see the
        # prompt's own instruction. A model that ignores that and
        # emits a radius with no place shouldn't have it silently
        # reach the form as an orphaned value.
        radius_meters = None

    timestamp = _validate_ymd(data.get("timestamp"))
    from_ = _validate_ymd(data.get("from_"))
    until = _validate_ymd(data.get("until"))
    if not (from_ and until):
        # Partial range (only one end valid/present) isn't usable -
        # mirrors voice_time.py's own all-or-nothing range handling.
        from_ = None
        until = None
    elif timestamp is not None:
        # "Only one of timestamp or the range should ever be set" - the
        # prompt says so, but don't trust it blindly (module docstring).
        # A range beats a single timestamp if the model emitted both,
        # since a range is strictly more informative to fall back to.
        timestamp = None

    text = data.get("text")
    if not isinstance(text, str):
        text = None

    matched_something = place is not None or timestamp is not None or bool(from_ and until)
    if matched_something:
        # AND-conflict-avoidance rule - see module docstring and
        # voice_query.py's own docstring for why this can't be left to
        # the model's own judgment.
        final_text = ""
    elif text is not None and text.strip():
        final_text = text.strip()
    else:
        # Nothing recognized at all - fall back to the literal
        # transcript, matching parse_spoken_query()'s own no-match
        # behavior (return text=transcript rather than empty).
        final_text = transcript

    return ParsedVoiceLLM(
        text=final_text,
        place=place,
        radius_meters=float(radius_meters) if radius_meters is not None else None,
        timestamp=timestamp,
        from_=from_,
        until=until,
        raw_response=raw_response,
    )


def _generate_via_scene_model(prompt: str, *, force_cpu: bool) -> str:
    """Impure - loads/reuses the real generate/scene.py vision-language
    model in text-only mode. See MODEL_SCENE's own explanation in the
    module docstring for why this option exists. Deferred import: this
    module (and its pure functions) must stay importable/testable even
    when torch/transformers aren't installed at all - only actually
    picking this model_choice at runtime needs them."""

    try:
        from ..generate.scene import DEFAULT_MODEL
        from ..generate.scene import generate_text_only
    except ImportError as exc:
        raise MediaToolError(
            f"local-LLM voice-search extraction (scene model) needs the "
            f"scene extra installed ({exc})"
        ) from exc

    return generate_text_only(prompt, model=DEFAULT_MODEL, force_cpu=force_cpu)


def _load_small_text_model(model_name: str, *, force_cpu: bool) -> _LoadedTextModel:
    try:
        import torch
        from transformers import AutoModelForCausalLM
        from transformers import AutoTokenizer
    except ImportError as exc:
        raise MediaToolError(
            "local-LLM voice-search extraction needs torch and "
            f"transformers installed ({exc})"
        ) from exc

    device_map = "cpu" if force_cpu else "auto"
    if not force_cpu and not torch.cuda.is_available():
        device_map = "cpu"

    try:
        tokenizer = AutoTokenizer.from_pretrained(model_name)
        model = AutoModelForCausalLM.from_pretrained(
            model_name, torch_dtype="auto", device_map=device_map
        )
    except Exception as exc:
        raise MediaToolError(f"could not load text model {model_name!r}: {exc}") from exc

    return _LoadedTextModel(model=model, tokenizer=tokenizer)


def _get_small_text_model(model_name: str, *, force_cpu: bool) -> _LoadedTextModel:
    """Cached the same way generate/scene.py's _get_scene_model() is -
    a --cpu-forced call and an auto-detected call for the same model
    name may both legitimately be wanted within one process."""

    cache_key = f"{model_name}:{'cpu' if force_cpu else 'auto'}"
    if cache_key not in _TEXT_MODEL_CACHE:
        _TEXT_MODEL_CACHE[cache_key] = _load_small_text_model(model_name, force_cpu=force_cpu)
    return _TEXT_MODEL_CACHE[cache_key]


def unload_text_model() -> None:
    """Evict the cached small text model(s) and release their GPU
    memory - mirrors generate/scene.py's unload_scene_model(). Not a
    JobRunner Job (this quick synchronous voice-search route isn't
    one, see web/app.py's transcribe_voice_search() docstring), so it
    doesn't get JobRunner._spawn()'s finally-block unload for free the
    way that function does - instead called from a shared idle timer
    (web/voice_idle_unload.py's touch()/IDLE_SECONDS, task #1427) that
    fires this and voice_asr.py's unload_asr_model() together after a
    few minutes of voice-search inactivity."""

    if not _TEXT_MODEL_CACHE:
        return

    _TEXT_MODEL_CACHE.clear()

    import gc

    gc.collect()

    try:
        import torch
    except ImportError:
        return

    if torch.cuda.is_available():
        torch.cuda.empty_cache()


def text_model_loaded() -> bool:
    """Pure-ish (reads, doesn't mutate) - whether a small text model is
    currently cached in memory. Used by web/app.py's voice-model-status
    route so the bv-search form's JS can show "Loading model..."
    instead of "Transcribing..." when a request is actually about to
    pay the cold-start model-load cost (first use, or after the idle
    timer evicted it) rather than reusing an already-warm model."""

    return bool(_TEXT_MODEL_CACHE)


def _generate_via_small_text_model(prompt: str, *, force_cpu: bool) -> str:
    """Impure - loads/reuses a small dedicated text model
    (DEFAULT_SMALL_TEXT_MODEL) via plain transformers
    AutoModelForCausalLM/AutoTokenizer. See MODEL_SMALL's own
    explanation in the module docstring for why this option exists.

    enable_thinking=False: DEFAULT_SMALL_TEXT_MODEL is a Qwen3 model,
    which defaults to emitting a <think>...</think> reasoning block
    before its actual answer. Left on, that would burn most of
    max_new_tokens on chain-of-thought for what's meant to be a quick
    structured-extraction call, risking truncation before the JSON
    object even starts. Qwen3's own apply_chat_template() accepts this
    kwarg directly - no separate call needed to suppress it."""

    loaded = _get_small_text_model(DEFAULT_SMALL_TEXT_MODEL, force_cpu=force_cpu)
    messages = [{"role": "user", "content": prompt}]
    text = loaded.tokenizer.apply_chat_template(
        messages, tokenize=False, add_generation_prompt=True, enable_thinking=False
    )
    inputs = loaded.tokenizer([text], return_tensors="pt").to(loaded.model.device)
    generated_ids = loaded.model.generate(**inputs, max_new_tokens=512)
    trimmed = [
        out_ids[len(in_ids):] for in_ids, out_ids in zip(inputs.input_ids, generated_ids)
    ]
    return loaded.tokenizer.batch_decode(trimmed, skip_special_tokens=True)[0]


def extract_voice_query_llm(
    transcript: str,
    today: date,
    *,
    model_choice: str = MODEL_SCENE,
    force_cpu: bool = False,
) -> ParsedVoiceLLM:
    """Impure entry point: build the prompt, run it through whichever
    model was chosen, and parse+validate the result. Raises
    MediaToolError for anything that goes wrong loading or running the
    model (missing extras, ImportError, a broken model download, ...),
    and ValueError if the model's own output can't be parsed as the
    expected JSON shape - callers (web/app.py's transcribe route) are
    expected to catch both and degrade gracefully, since this whole
    feature is explicitly an experimental comparison running alongside
    the proven regex parsers, never a replacement for them (see module
    docstring, design decision 2)."""

    if model_choice not in VALID_MODEL_CHOICES:
        raise ValueError(
            f"invalid model_choice {model_choice!r} - expected one of {VALID_MODEL_CHOICES}"
        )

    prompt = _build_prompt(transcript, today)

    if model_choice == MODEL_SCENE:
        raw_response = _generate_via_scene_model(prompt, force_cpu=force_cpu)
    else:
        raw_response = _generate_via_small_text_model(prompt, force_cpu=force_cpu)

    return _build_parsed_result(raw_response, transcript)
