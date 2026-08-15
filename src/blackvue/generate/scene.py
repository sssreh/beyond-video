"""
Dashcam scene description and on-screen text (OCR) reading, using a
local Qwen2.5-VL/Qwen3-VL vision-language model.

Ported from the standalone scene-scribe prototype (kept outside this
repo while its accuracy was still being evaluated - see
WORKING_CONTEXT.md for the "if it's good enough we might merge it in
later" decision that gated this). Two real-footage findings from that
evaluation directly shaped this module and are worth keeping in mind
when reading it:

1. Confident wrong reads, not just illegible ones. A real plate read
   back as "ETR734" when the actual plate was "FTR78P" - the model
   didn't say "not legible", it committed to a plausible-looking wrong
   answer. See zoom_into_signs()'s plate confidence-check for the
   mitigation: every plate crop gets read twice (once greedy, once
   with sampling forced on) and only reported as-is if both reads
   agree; a disagreement is surfaced as unverified rather than picked
   between.

2. Confident invented content unrelated to the actual scene. "Palm
   Jumeirah" (a Dubai landmark) showed up as on-screen text on a
   Stockholm-area trip, on more than one run - a training-prior
   hallucination on "tunnel exit onto elevated highway" imagery,
   not a misread of anything actually on screen. No reliable model-
   side fix was found for this class of error (a stricter "don't
   infer from general knowledge" prompt was tried and reverted - see
   OCR_PROMPT's own comment - it fixed one case and caused a worse
   hallucination elsewhere). describe_scene()'s output carries an
   explicit disclaimer footer instead: treat every read here,
   especially specific/unusual place names, as unverified until
   checked against the source video.

Copyright (C) 2026 Christer R. (sssreh)

SPDX-License-Identifier: GPL-3.0-or-later
"""

from __future__ import annotations

import sys
import time
from dataclasses import dataclass
from dataclasses import field
from pathlib import Path

from .media import MediaToolError

# Was Qwen/Qwen2.5-VL-7B-Instruct (the standalone scene-scribe
# prototype's own original default, ported straight over when this
# module was built - see WORKING_CONTEXT.md, task #604) until
# Christer asked directly why Qwen3-VL-8B-Instruct - which he'd been
# passing explicitly via --scene-model all along - wasn't the
# default. There was no actual reason: Qwen3-VL support (is_qwen3_vl()/
# _patch_factor_for()/the model-class swap below) was added later as
# an *option*, but nobody had flipped this constant to match. Requires
# transformers>=4.57.0 for Qwen3VLForConditionalGeneration (see
# pyproject.toml's own scene extra) - _load_scene_model() below raises
# a clear MediaToolError naming that requirement if an older install
# hits this path.
DEFAULT_MODEL = "Qwen/Qwen3-VL-8B-Instruct"

# See set_patch_factor_for() below - Qwen2.5-VL patchifies in 28px
# steps (14px patch * 2x2 merge), Qwen3-VL in 32px steps (16px patch).
_PATCH_FACTOR_2_5_VL = 28
_PATCH_FACTOR_3_VL = 32

_SCENE_MODEL_CACHE: dict[str, "_LoadedSceneModel"] = {}
_VISION_GPU_AVAILABLE: bool | None = None

DISCLAIMER = (
    "\n\n---\n"
    "Note: the reads above (especially license plates, street/shop "
    "signs, and place names) are automated vision-model OCR output, "
    "not verified fact. This model has been observed to confidently "
    "report a wrong plate/sign read (not just say \"not legible\"), "
    "and to occasionally invent plausible-sounding but unrelated "
    "place names on scenes it finds ambiguous. Treat every read here "
    "as unverified until checked against the source video."
)

DESCRIBE_PROMPT = (
    "This is a clip from a car dashcam. Describe what's happening in "
    "plain language: what kind of road this is, the weather/lighting "
    "conditions, the traffic situation, and anything notable. If "
    "nothing notable happens, say so in a single plain sentence (for "
    "example: 'Routine driving, nothing notable happened.') - don't "
    "invent drama, and don't list off categories of incident that "
    "didn't occur."
)

OCR_PROMPT = (
    "Read every piece of text visible anywhere in this video - "
    "dashboard/overlay text (timestamp, speed, GPS coordinates), "
    "street signs, shop signs, license plates if legible, anything on "
    "other vehicles, and any text on the road itself. List each piece "
    "of text you find, one per line. If you can't make something out "
    "clearly, say so rather than guessing."
    # A stricter "don't infer a name from general knowledge of the
    # area" version of this prompt was tried and reverted - on real
    # footage it fixed one hallucinated tunnel name but caused a much
    # worse regression elsewhere (a long invented wall of fake Swedish
    # administrative text on a different clip, plus loss of a
    # previously-correct shop-sign read). See DISCLAIMER above for the
    # fix that was kept instead: flag the whole output as unverified
    # rather than trying to prompt the hallucination away.
)

COMBINED_PROMPT = (
    f"{DESCRIBE_PROMPT}\n\nSeparately, then do this:\n\n{OCR_PROMPT}\n\n"
    "Structure your answer as two sections with the headings "
    "'## Description' and '## On-screen text'."
)

GROUND_PROMPT = (
    "Locate every road sign, shop/storefront sign, or vehicle license "
    "plate visible in this image - ignore the dashboard/overlay text "
    "burned into the corner of the frame (camera name, timestamp, "
    "speed). Output a JSON list; each item must have \"bbox_2d\": "
    "[x1, y1, x2, y2] (pixel coordinates in this image) and \"label\": "
    "a short description of what it is. If nothing matches, output []."
)

ZOOM_OCR_PROMPT = (
    "This is a cropped, zoomed-in region of a dashcam frame, centered "
    "on a sign or plate. Read the text on it exactly as shown. If it's "
    "still not legible even at this zoom level, say \"not legible\" - "
    "don't guess a plausible-sounding replacement."
)

# See module docstring point 1 (the real ETR734-vs-FTR78P misread) for
# why this prompt describes the regular Swedish plate format as the
# normal case rather than a hard constraint - forcing every read into
# that shape would wrongly mangle personalized/vanity plates, which
# can be longer or use the space as a meaningful character slot.
ZOOM_OCR_PLATE_PROMPT = (
    "This is a cropped, zoomed-in region of a dashcam frame, centered "
    "on a vehicle's license plate. Regular Swedish plates are 3 "
    "letters, a space, then 2 digits and one more character that can "
    "be either a digit or a letter. Personalized/vanity plates can "
    "differ from this - they may run longer, and the space itself can "
    "sometimes be a meaningful part of the plate rather than just a "
    "separator. Read exactly what's on the plate as shown, don't "
    "force it into the regular pattern if it doesn't fit. Return only "
    "the plate characters, nothing else - no description, no "
    "commentary. If it's not legible even at this zoom level, say "
    "\"not legible\" - don't guess a plausible-sounding replacement."
)

TRIP_SUMMARY_PROMPT_TEMPLATE = (
    "Below are separate descriptions of consecutive dashcam recordings "
    "from a single trip, in chronological order. Each one covers a few "
    "minutes of driving. Write one flowing summary of the whole trip - "
    "not a list of per-segment descriptions restated back to back. "
    "Specifically call out how conditions changed over the course of "
    "the trip (for example: \"moderate traffic became heavier after a "
    "while\", or \"clear skies turned to rain partway through\") "
    "rather than describing each segment in isolation. Only mention a "
    "change if the descriptions actually support it - don't invent a "
    "progression that isn't there.\n\n{segments}"
)


@dataclass
class SceneOptions:
    """Tuning knobs for describe_scene()/summarize_trip(). Defaults
    match the values scene-scribe's real-footage tuning converged on -
    see the standalone prototype's own argparse help text (preserved
    in docs/man/bv-scribe.md) for the reasoning behind each one."""

    task: str = "both"  # "describe", "ocr", or "both"
    model: str = DEFAULT_MODEL
    fps: float = 1.0
    max_frames: int = 16
    max_pixels: int = 360 * 420
    resized_width: int = 1092
    resized_height: int = 588
    crop_top: float = 0.0378
    crop_bottom: float = 0.0344
    max_new_tokens: int = 768
    repetition_penalty: float = 1.15
    no_repeat_ngram_size: int = 3
    do_sample: bool = False
    temperature: float = 0.7
    top_p: float = 0.8
    top_k: int = 20
    zoom_signs: bool = True
    zoom_frames: int = 4
    zoom_detect_width: int = 1092
    zoom_padding: float = 0.15
    zoom_ocr_width: int = 640
    zoom_debug_dir: Path | None = None
    zoom_max_new_tokens: int = 200
    zoom_detect_max_new_tokens: int = 500
    zoom_repetition_penalty: float = 1.0
    zoom_no_repeat_ngram_size: int = 0
    zoom_plate_confidence_check: bool = True
    trip_summary_max_new_tokens: int = 768
    force_cpu: bool = False


def vision_gpu_available() -> bool:
    """Cheaply check whether this machine can run the vision model on
    a CUDA GPU. Mirrors generate/speech.py's own gpu_available(), kept
    as a separate cached flag since it checks plain torch.cuda rather
    than speech.py's ctranslate2-specific probe - the two libraries
    can in principle disagree about CUDA availability, and either one
    only ever picks a friendlier default, never blocks startup."""

    global _VISION_GPU_AVAILABLE

    if _VISION_GPU_AVAILABLE is None:
        try:
            import torch

            _VISION_GPU_AVAILABLE = bool(torch.cuda.is_available())
        except Exception:
            _VISION_GPU_AVAILABLE = False

    return _VISION_GPU_AVAILABLE


def is_qwen3_vl(model_name: str) -> bool:
    """Qwen3-VL needs a different model class
    (Qwen3VLForConditionalGeneration instead of
    Qwen2_5_VLForConditionalGeneration) and a different patch factor
    (32 instead of 28) - detected from the model name string rather
    than a separate flag, since the name is already what gets passed
    to from_pretrained()."""

    return "qwen3-vl" in model_name.lower()


def _patch_factor_for(model_name: str) -> int:
    return _PATCH_FACTOR_3_VL if is_qwen3_vl(model_name) else _PATCH_FACTOR_2_5_VL


@dataclass(frozen=True)
class _LoadedSceneModel:
    model: object
    processor: object
    process_vision_info: object
    patch_factor: int
    is_qwen3: bool


def _load_scene_model(model_name: str, *, force_cpu: bool) -> _LoadedSceneModel:
    try:
        import torch
        import torchvision  # noqa: F401 - qwen_vl_utils needs this importable
        from qwen_vl_utils import process_vision_info
        from transformers import AutoProcessor
    except ImportError as exc:
        raise MediaToolError(
            "scene description needs torch, torchvision, transformers, "
            f"and qwen-vl-utils installed ({exc})"
        ) from exc

    qwen3 = is_qwen3_vl(model_name)

    if qwen3:
        try:
            from transformers import Qwen3VLForConditionalGeneration as ModelClass
        except ImportError as exc:
            raise MediaToolError(
                "this transformers install doesn't have "
                f"Qwen3VLForConditionalGeneration ({exc}) - Qwen3-VL "
                "needs transformers>=4.57.0"
            ) from exc
    else:
        from transformers import Qwen2_5_VLForConditionalGeneration as ModelClass

    device_map = "cpu" if force_cpu else "auto"
    if not force_cpu and not torch.cuda.is_available():
        device_map = "cpu"

    try:
        model = ModelClass.from_pretrained(
            model_name, torch_dtype="auto", device_map=device_map
        )
        processor = AutoProcessor.from_pretrained(model_name)
    except Exception as exc:
        raise MediaToolError(f"could not load scene model {model_name!r}: {exc}") from exc

    return _LoadedSceneModel(
        model=model,
        processor=processor,
        process_vision_info=process_vision_info,
        patch_factor=_patch_factor_for(model_name),
        is_qwen3=qwen3,
    )


def _get_scene_model(model_name: str, *, force_cpu: bool) -> _LoadedSceneModel:
    """Return a cached loaded scene model, loading it if needed. Cache
    key includes the cpu flag, same reasoning as speech.py's
    _get_whisper_model() - a --cpu-forced call and an auto-detected
    call for the same model name may both legitimately be wanted
    within one process."""

    cache_key = f"{model_name}:{'cpu' if force_cpu else 'auto'}"

    if cache_key not in _SCENE_MODEL_CACHE:
        _SCENE_MODEL_CACHE[cache_key] = _load_scene_model(model_name, force_cpu=force_cpu)

    return _SCENE_MODEL_CACHE[cache_key]


def unload_scene_model(model_name: str | None = None, *, force_cpu: bool | None = None) -> None:
    """Evict loaded scene model(s) from `_SCENE_MODEL_CACHE` and release
    their GPU memory.

    A one-shot CLI process (`bv-scribe`, `bv-generate --describe-scene`,
    `bv-export --trip-summary`) never needs this - the cache just lives
    for the process's lifetime and the OS reclaims everything on exit,
    same as `speech.py`'s own Whisper model cache. `bv-web`'s in-process
    job runner is different: it's a long-running server process that
    may run any of those three job types back to back, and nothing was
    ever releasing the ~16GB Qwen3-VL-8B-Instruct model each one loads -
    "Scene model never unloads from GPU" (Christer). This is the fix:
    called from `JobRunner._spawn()`'s shared `finally` block after
    every job, so GPU memory is freed as soon as a job that may have
    touched the scene model finishes, regardless of outcome.

    With no arguments, clears every cached entry - the common case,
    since a job runner doesn't know in advance which `(model_name,
    force_cpu)` combination (if any) the job that just finished
    actually used. Pass `model_name` (and optionally `force_cpu`) to
    evict a single specific entry instead - `model_name` alone evicts
    both the `:cpu` and `:auto` variants of that model, matching
    `_get_scene_model()`'s own cache-key scheme.

    Safe to call when the cache is already empty (nothing loaded this
    process, or a previous call already cleared it) - a harmless no-op,
    not an error. Safe to call even when scene description was never
    used at all in this process (torch/transformers not installed) -
    the cache is empty in that case too, so the torch import below is
    never reached.
    """

    if not _SCENE_MODEL_CACHE:
        return

    if model_name is None:
        keys_to_drop = list(_SCENE_MODEL_CACHE)
    elif force_cpu is None:
        keys_to_drop = [
            key
            for key in _SCENE_MODEL_CACHE
            if key == f"{model_name}:cpu" or key == f"{model_name}:auto"
        ]
    else:
        keys_to_drop = [f"{model_name}:{'cpu' if force_cpu else 'auto'}"]

    if not keys_to_drop:
        return

    for key in keys_to_drop:
        del _SCENE_MODEL_CACHE[key]

    import gc
    gc.collect()

    try:
        import torch
    except ImportError:
        return

    if torch.cuda.is_available():
        torch.cuda.empty_cache()


def build_prompt(task: str) -> str:
    if task == "describe":
        return DESCRIBE_PROMPT
    if task == "ocr":
        return OCR_PROMPT
    return COMBINED_PROMPT


def extract_description_section(output_text: str) -> str:
    """Pull just the '## Description' section out of a per-recording
    result, dropping the on-screen-text/zoomed-sign-reads sections -
    used to build summarize_trip()'s input."""

    lines = output_text.splitlines()
    section = []
    in_description = False
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("#") and "description" in stripped.lower():
            in_description = True
            continue
        if stripped.startswith("#") and in_description:
            break
        if in_description:
            section.append(line)
    return "\n".join(section).strip()


def _fetch_vision_inputs(process_vision_info, messages):
    """process_vision_info() wrapper that requests
    return_video_kwargs=True when supported (needed for Qwen3-VL to
    know the sampling rate of an already-extracted video tensor), and
    degrades gracefully on older qwen_vl_utils that don't accept it."""

    try:
        image_inputs, video_inputs, video_kwargs = process_vision_info(
            messages, return_video_kwargs=True
        )
    except TypeError:
        image_inputs, video_inputs = process_vision_info(messages)
        video_kwargs = {}
    return image_inputs, video_inputs, video_kwargs


def _sampling_kwargs(opts: SceneOptions, *, force_sample: bool = False) -> dict:
    """Shared do_sample/temperature/top_p/top_k kwargs for a
    model.generate() call. force_sample=True is used by the plate
    confidence check (zoom_into_signs()) to get a second, independent
    read regardless of opts.do_sample's normal (greedy) default."""

    if force_sample:
        return {
            "do_sample": True,
            "temperature": max(opts.temperature, 0.5),
            "top_p": opts.top_p,
            "top_k": opts.top_k,
        }

    kwargs: dict = {"do_sample": opts.do_sample}
    if opts.do_sample:
        kwargs["temperature"] = opts.temperature
        kwargs["top_p"] = opts.top_p
        kwargs["top_k"] = opts.top_k
    return kwargs


def _crop_top_bottom(video_inputs, crop_top: float, crop_bottom: float, patch_factor: int):
    if not video_inputs or (crop_top <= 0 and crop_bottom <= 0):
        return video_inputs

    frames = video_inputs[0]
    _, _, height, width = frames.shape
    top_px = int(round(height * crop_top))
    bottom_px = int(round(height * crop_bottom))
    kept = height - top_px - bottom_px
    kept = (kept // patch_factor) * patch_factor
    if kept <= 0:
        raise MediaToolError(
            "crop_top/crop_bottom leave nothing to feed the model "
            f"(frame height {height}px, requested top={crop_top}, "
            f"bottom={crop_bottom})"
        )
    top_px = min(top_px, height - kept)
    video_inputs[0] = frames[:, :, top_px : top_px + kept, :]
    return video_inputs


def _run_single_image_prompt(
    image,
    prompt: str,
    loaded: _LoadedSceneModel,
    opts: SceneOptions,
    *,
    resized_width: int | None = None,
    resized_height: int | None = None,
    max_new_tokens: int | None = None,
    repetition_penalty: float | None = None,
    no_repeat_ngram_size: int | None = None,
    force_sample: bool = False,
) -> str:
    """Feed one PIL image + a text prompt through the model and return
    the generated text. Shared by the sign-grounding call and each
    per-crop OCR call in the zoom pipeline."""

    image_ele = {"type": "image", "image": image}
    if resized_width and resized_height:
        image_ele["resized_width"] = resized_width
        image_ele["resized_height"] = resized_height

    messages = [{"role": "user", "content": [image_ele, {"type": "text", "text": prompt}]}]
    text = loaded.processor.apply_chat_template(
        messages, tokenize=False, add_generation_prompt=True
    )
    image_inputs, video_inputs, video_kwargs = _fetch_vision_inputs(
        loaded.process_vision_info, messages
    )
    inputs = loaded.processor(
        text=[text], images=image_inputs, videos=video_inputs,
        padding=True, return_tensors="pt", **video_kwargs,
    )
    inputs = inputs.to(loaded.model.device)
    generated_ids = loaded.model.generate(
        **inputs,
        max_new_tokens=max_new_tokens or opts.max_new_tokens,
        repetition_penalty=(
            opts.repetition_penalty if repetition_penalty is None else repetition_penalty
        ),
        no_repeat_ngram_size=(
            opts.no_repeat_ngram_size if no_repeat_ngram_size is None else no_repeat_ngram_size
        ),
        **_sampling_kwargs(opts, force_sample=force_sample),
    )
    trimmed = [out_ids[len(in_ids):] for in_ids, out_ids in zip(inputs.input_ids, generated_ids)]
    return loaded.processor.batch_decode(
        trimmed, skip_special_tokens=True, clean_up_tokenization_spaces=False
    )[0]


def _extract_full_res_frames(video_path: Path, count: int):
    """Grab `count` evenly-spaced frames from the source video at full
    native resolution, for the zoom pipeline's grounding step. Returns
    a list of (timestamp_seconds, PIL.Image) tuples."""

    import decord
    from PIL import Image as PILImage

    vr = decord.VideoReader(str(video_path.resolve()))
    total = len(vr)
    if total == 0:
        return []
    fps = vr.get_avg_fps() or 30.0

    if count <= 1 or total == 1:
        indices = [total // 2]
    else:
        indices = sorted({
            min(total - 1, int(round(i * (total - 1) / (count - 1))))
            for i in range(count)
        })

    frames = []
    for idx in indices:
        frame_np = vr[idx].asnumpy()
        frames.append((idx / fps, PILImage.fromarray(frame_np)))
    return frames


def _crop_overlay_from_image(image, crop_top: float, crop_bottom: float):
    if crop_top <= 0 and crop_bottom <= 0:
        return image
    width, height = image.size
    top_px = int(round(height * crop_top))
    bottom_px = int(round(height * crop_bottom))
    if top_px + bottom_px >= height:
        return image
    return image.crop((0, top_px, width, height - bottom_px))


def _parse_grounding_boxes(raw_text: str):
    """Parse the model's JSON bbox_2d/label response into a list of
    {"label": str, "box": (x1, y1, x2, y2)} dicts. Tolerates markdown
    fences, stray text, and truncated JSON (recovering whatever
    standalone {...} objects are still complete) rather than requiring
    an exact match - most frames genuinely have nothing to detect, and
    one bad grounding response shouldn't kill the whole video."""

    import json
    import re

    text = raw_text.strip()
    fence_match = re.search(r"```(?:json)?\s*(.*?)\s*```", text, re.DOTALL)
    if fence_match:
        text = fence_match.group(1)

    items = None
    try:
        data = json.loads(text)
        if isinstance(data, list):
            items = data
    except json.JSONDecodeError:
        pass

    if items is None:
        span_match = re.search(r"\[.*\]", text, re.DOTALL)
        if span_match:
            try:
                data = json.loads(span_match.group(0))
                if isinstance(data, list):
                    items = data
            except json.JSONDecodeError:
                pass

    if items is None:
        items = []
        for obj_match in re.finditer(r"\{[^{}]*\}", text, re.DOTALL):
            try:
                obj = json.loads(obj_match.group(0))
            except json.JSONDecodeError:
                continue
            items.append(obj)

    boxes = []
    for item in items:
        if not isinstance(item, dict):
            continue

        bbox = item.get("bbox_2d")
        if not (isinstance(bbox, (list, tuple)) and len(bbox) == 4):
            bbox = None
            for key, value in item.items():
                if "box" in str(key).lower() and isinstance(value, (list, tuple)) and len(value) == 4:
                    bbox = value
                    break
        if not (isinstance(bbox, (list, tuple)) and len(bbox) == 4):
            continue
        try:
            x1, y1, x2, y2 = (float(v) for v in bbox)
        except (TypeError, ValueError):
            continue

        label = item.get("label")
        if label is None:
            for key, value in item.items():
                if "lab" in str(key).lower():
                    label = value
                    break
        boxes.append({"label": str(label) if label is not None else "sign", "box": (x1, y1, x2, y2)})
    return boxes


def _normalize_plate_text(text: str) -> str:
    """Normalize a plate read for the confidence-check comparison -
    case/whitespace differences shouldn't count as a disagreement."""

    return " ".join(text.strip().upper().split())


def _zoom_into_signs(
    video_path: Path, loaded: _LoadedSceneModel, opts: SceneOptions, *, warn=None
) -> str:
    """The detect-then-zoom pipeline: sample a few full-res frames,
    ask the model to locate signs/plates in each, crop each detection
    (with padding) out of the native frame, and OCR just that crop.
    Returns a formatted '## Zoomed sign reads' section, or '' if
    nothing was found/attempted.

    Plate crops get an extra confidence check (module docstring point
    1): read once normally, then again with sampling forced on: if
    the two reads disagree, report both as unverified rather than
    picking one - the same "trust text that reads the same across
    runs" heuristic the original prototype's --do-sample help text
    already described, now actually applied instead of just noted.
    """

    warn = warn or (lambda msg: print(msg, file=sys.stderr))

    try:
        frames = _extract_full_res_frames(video_path, opts.zoom_frames)
    except Exception as exc:  # noqa: BLE001 - zoom is a bonus pass, not core
        warn(f"  zoom: couldn't extract full-res frames ({exc}), skipping.")
        return ""

    debug_dir = opts.zoom_debug_dir
    debug_manifest = []
    crop_counter = 0
    if debug_dir is not None:
        debug_dir.mkdir(parents=True, exist_ok=True)

    lines = []
    for timestamp, frame in frames:
        frame = _crop_overlay_from_image(frame, opts.crop_top, opts.crop_bottom)
        native_width, native_height = frame.size
        if native_width <= 0 or native_height <= 0:
            continue

        patch_factor = loaded.patch_factor
        detect_width = patch_factor * round(opts.zoom_detect_width / patch_factor)
        detect_height = patch_factor * round(
            (opts.zoom_detect_width * native_height / native_width) / patch_factor
        )
        if detect_width <= 0 or detect_height <= 0:
            continue

        try:
            raw = _run_single_image_prompt(
                frame, GROUND_PROMPT, loaded, opts,
                resized_width=detect_width, resized_height=detect_height,
                max_new_tokens=opts.zoom_detect_max_new_tokens,
                repetition_penalty=opts.zoom_repetition_penalty,
                no_repeat_ngram_size=opts.zoom_no_repeat_ngram_size,
            )
        except Exception as exc:  # noqa: BLE001
            warn(f"  zoom: detection failed at t={timestamp:.1f}s ({exc}), skipping frame.")
            continue

        boxes = _parse_grounding_boxes(raw)
        if not boxes:
            continue

        scale_x = native_width / detect_width
        scale_y = native_height / detect_height
        qwen3 = loaded.is_qwen3

        for det in boxes:
            x1, y1, x2, y2 = det["box"]
            if qwen3:
                x1, x2 = x1 / 1000 * detect_width, x2 / 1000 * detect_width
                y1, y2 = y1 / 1000 * detect_height, y2 / 1000 * detect_height
            x1, x2 = x1 * scale_x, x2 * scale_x
            y1, y2 = y1 * scale_y, y2 * scale_y
            box_w, box_h = x2 - x1, y2 - y1
            if box_w <= 0 or box_h <= 0:
                continue
            pad_x, pad_y = box_w * opts.zoom_padding, box_h * opts.zoom_padding
            crop_box = (
                max(0, int(x1 - pad_x)),
                max(0, int(y1 - pad_y)),
                min(native_width, int(x2 + pad_x)),
                min(native_height, int(y2 + pad_y)),
            )
            if crop_box[2] <= crop_box[0] or crop_box[3] <= crop_box[1]:
                continue
            crop = frame.crop(crop_box)
            crop_native_width, crop_native_height = crop.size
            if crop_native_width <= 0 or crop_native_height <= 0:
                continue

            ocr_target_width = max(crop_native_width, opts.zoom_ocr_width)
            ocr_width = patch_factor * round(ocr_target_width / patch_factor)
            ocr_height = patch_factor * round(
                (ocr_target_width * crop_native_height / crop_native_width) / patch_factor
            )
            if ocr_width <= 0 or ocr_height <= 0:
                continue

            is_plate = "plate" in det["label"].lower()
            ocr_prompt = ZOOM_OCR_PLATE_PROMPT if is_plate else ZOOM_OCR_PROMPT

            debug_path = None
            if debug_dir is not None:
                crop_counter += 1
                label_slug = "".join(
                    c if c.isalnum() else "_" for c in det["label"].lower()
                ).strip("_") or "region"
                debug_path = debug_dir / (
                    f"{crop_counter:03d}_t{timestamp:07.1f}_{label_slug}_"
                    f"{crop_native_width}x{crop_native_height}.png"
                )
                try:
                    crop.save(debug_path)
                except Exception as exc:  # noqa: BLE001 - debug dump is best-effort
                    warn(f"  zoom: couldn't save debug crop to {debug_path} ({exc}).")
                    debug_path = None

            try:
                read_text = _run_single_image_prompt(
                    crop, ocr_prompt, loaded, opts,
                    resized_width=ocr_width, resized_height=ocr_height,
                    max_new_tokens=opts.zoom_max_new_tokens,
                    repetition_penalty=opts.zoom_repetition_penalty,
                    no_repeat_ngram_size=opts.zoom_no_repeat_ngram_size,
                )
            except Exception as exc:  # noqa: BLE001
                warn(f"  zoom: read failed for '{det['label']}' at t={timestamp:.1f}s ({exc}).")
                continue

            confidence_note = ""
            if is_plate and opts.zoom_plate_confidence_check:
                try:
                    second_read = _run_single_image_prompt(
                        crop, ocr_prompt, loaded, opts,
                        resized_width=ocr_width, resized_height=ocr_height,
                        max_new_tokens=opts.zoom_max_new_tokens,
                        repetition_penalty=opts.zoom_repetition_penalty,
                        no_repeat_ngram_size=opts.zoom_no_repeat_ngram_size,
                        force_sample=True,
                    )
                except Exception as exc:  # noqa: BLE001 - confidence check is a bonus, not core
                    warn(f"  zoom: plate confidence re-read failed ({exc}), reporting single read unverified.")
                    confidence_note = " [unverified - confidence re-read failed]"
                else:
                    if _normalize_plate_text(read_text) != _normalize_plate_text(second_read):
                        confidence_note = (
                            " [unverified - two independent reads disagreed: "
                            f"{read_text.strip()!r} vs {second_read.strip()!r}]"
                        )

            lines.append(f"- [t={timestamp:.1f}s] {det['label']}: {read_text.strip()}{confidence_note}")
            if debug_path is not None:
                debug_manifest.append(
                    f"{debug_path.name}\t{timestamp:.1f}\t{det['label']}\t"
                    f"{crop_native_width}x{crop_native_height}\t{read_text.strip()}{confidence_note}"
                )

    if debug_dir is not None and debug_manifest:
        manifest_path = debug_dir / "manifest.tsv"
        header = "filename\ttimestamp_s\tlabel\tnative_crop_size\tocr_read"
        manifest_path.write_text(header + "\n" + "\n".join(debug_manifest) + "\n", encoding="utf-8")

    if not lines:
        return ""
    return "\n\n## Zoomed sign reads\n" + "\n".join(lines)


def describe_scene(
    video_path: Path,
    *,
    opts: SceneOptions | None = None,
    warn=None,
    **overrides,
) -> str:
    """Describe video_path's contents and/or read its on-screen text
    using a local Qwen2.5-VL/Qwen3-VL model, returning the formatted
    result (including a disclaimer footer - see module docstring).

    opts controls every tuning knob (model choice, frame sampling,
    resolution, the zoom-signs sub-pipeline, ...) - pass an explicit
    SceneOptions, or override individual fields via **overrides (e.g.
    describe_scene(path, task="ocr", zoom_signs=False)). The loaded
    model is cached per (model name, cpu) pair, so repeated calls
    within one process (e.g. bv-scribe's batch mode) only pay the load
    cost once.
    """

    if opts is None:
        opts = SceneOptions(**overrides)
    elif overrides:
        raise TypeError("pass either opts= or **overrides, not both")

    warn = warn or (lambda msg: print(msg, file=sys.stderr))

    loaded = _get_scene_model(opts.model, force_cpu=opts.force_cpu)

    prompt = build_prompt(opts.task)
    video_ele = {
        "type": "video",
        "video": str(video_path.resolve()),
        "fps": opts.fps,
        "max_frames": opts.max_frames,
    }
    if opts.resized_width and opts.resized_height:
        video_ele["resized_width"] = opts.resized_width
        video_ele["resized_height"] = opts.resized_height
    else:
        video_ele["max_pixels"] = opts.max_pixels

    messages = [{"role": "user", "content": [video_ele, {"type": "text", "text": prompt}]}]
    text = loaded.processor.apply_chat_template(
        messages, tokenize=False, add_generation_prompt=True
    )

    try:
        # Video reading/decoding (via qwen_vl_utils -> decord/ffmpeg)
        # lives inside this try block too now, not just the inference
        # calls below - it used to sit outside, so a read failure on a
        # network-mounted archive (a flaky/corrupt file over a \\NAS\
        # share, Christer's own setup) raised a raw, unwrapped
        # exception straight out of describe_scene(). Nothing upstream
        # (bv-scribe's per-recording loop, bv-web's job runner) was
        # prepared to catch that as anything other than fatal - it
        # escaped all the way to JobRunner._spawn()'s outer catch-all
        # and killed an entire 902-recording batch over one bad file.
        # Wrapping it here means it comes out as a normal MediaToolError
        # like every other describe_scene() failure, which _run_scene_
        # pass() already knows how to handle per-recording. See
        # WORKING_CONTEXT.md.
        image_inputs, video_inputs, video_kwargs = _fetch_vision_inputs(
            loaded.process_vision_info, messages
        )
        if video_inputs:
            video_inputs = _crop_top_bottom(
                video_inputs, opts.crop_top, opts.crop_bottom, loaded.patch_factor
            )

        inputs = loaded.processor(
            text=[text], images=image_inputs, videos=video_inputs,
            padding=True, return_tensors="pt", **video_kwargs,
        )
        inputs = inputs.to(loaded.model.device)

        generated_ids = loaded.model.generate(
            **inputs,
            max_new_tokens=opts.max_new_tokens,
            repetition_penalty=opts.repetition_penalty,
            no_repeat_ngram_size=opts.no_repeat_ngram_size,
            **_sampling_kwargs(opts),
        )
    except Exception as exc:
        # torch is already guaranteed importable here in real usage -
        # _get_scene_model() above imports it before this point ever
        # runs - but guarded anyway rather than letting an unrelated
        # ImportError mask the real exc being handled.
        try:
            import torch

            is_oom = isinstance(exc, torch.cuda.OutOfMemoryError)
        except ImportError:
            torch = None
            is_oom = False

        if is_oom:
            torch.cuda.empty_cache()
            raise MediaToolError(
                f"out of VRAM describing {video_path.name} ({exc}) - try "
                "a lower max_frames/fps or resized_width/resized_height"
            ) from exc
        raise MediaToolError(f"scene description failed for {video_path.name}: {exc}") from exc
    finally:
        if not opts.force_cpu:
            try:
                import torch

                torch.cuda.empty_cache()
            except Exception:
                pass

    trimmed = [out_ids[len(in_ids):] for in_ids, out_ids in zip(inputs.input_ids, generated_ids)]
    output_text = loaded.processor.batch_decode(
        trimmed, skip_special_tokens=True, clean_up_tokenization_spaces=False
    )[0]

    if opts.zoom_signs:
        output_text += _zoom_into_signs(video_path, loaded, opts, warn=warn)

    return output_text + DISCLAIMER


def summarize_trip(
    segments: list[tuple[str, str]],
    *,
    opts: SceneOptions | None = None,
    **overrides,
) -> str:
    """Text-only pass: synthesize one trip-level narrative from each
    recording's already-generated '## Description' text (see
    extract_description_section()), explicitly asked to track change
    over time rather than restate each segment back to back. No
    video/image input, so unlike describe_scene() its cost doesn't
    scale with fps/max_frames."""

    if opts is None:
        opts = SceneOptions(**overrides)
    elif overrides:
        raise TypeError("pass either opts= or **overrides, not both")

    loaded = _get_scene_model(opts.model, force_cpu=opts.force_cpu)

    segment_text = "\n\n".join(f"[{label}]\n{text}" for label, text in segments)
    prompt = TRIP_SUMMARY_PROMPT_TEMPLATE.format(segments=segment_text)

    messages = [{"role": "user", "content": [{"type": "text", "text": prompt}]}]
    text = loaded.processor.apply_chat_template(
        messages, tokenize=False, add_generation_prompt=True
    )
    image_inputs, video_inputs, video_kwargs = _fetch_vision_inputs(
        loaded.process_vision_info, messages
    )
    try:
        inputs = loaded.processor(
            text=[text], images=image_inputs, videos=video_inputs,
            padding=True, return_tensors="pt", **video_kwargs,
        )
        inputs = inputs.to(loaded.model.device)
        generated_ids = loaded.model.generate(
            **inputs,
            max_new_tokens=opts.trip_summary_max_new_tokens,
            repetition_penalty=opts.repetition_penalty,
            no_repeat_ngram_size=opts.no_repeat_ngram_size,
            **_sampling_kwargs(opts),
        )
    except Exception as exc:
        raise MediaToolError(f"trip summary synthesis failed: {exc}") from exc

    trimmed = [out_ids[len(in_ids):] for in_ids, out_ids in zip(inputs.input_ids, generated_ids)]
    return loaded.processor.batch_decode(
        trimmed, skip_special_tokens=True, clean_up_tokenization_spaces=False
    )[0]
