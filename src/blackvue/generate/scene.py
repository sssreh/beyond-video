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

3. Instruction-following drift on formatting, once real-world tested.
   After DESCRIBE_PROMPT was extended to ask for a bulleted,
   per-event-timestamped "## Description" (see DescriptionEvent
   below), the first real run against Christer's own footage - not a
   synthetic test - came back as a single plain paragraph with no
   bullets at all, despite the clip clearly containing multiple
   distinct moments (confirmed by its own "## Zoomed sign reads"
   section spanning t=0.0s to t=180.2s). The model wasn't confused
   about the content, just didn't hold onto the format instruction
   through the rest of COMBINED_PROMPT's competing OCR/structuring
   asks. Fixed two ways: DESCRIBE_PROMPT's format paragraph now opens
   with an explicit "REQUIRED FORMAT" / "never as a plain paragraph"
   directive instead of burying the requirement mid-paragraph, and
   COMBINED_PROMPT repeats a short reminder of it as the very last
   thing before generation starts (the position an instruction-tuned
   model tends to weight most heavily). The bulleted-timestamp
   extraction code already treated "no bullets found" as a clean,
   silent fallback to the old evenly-spaced-chunking description.srt
   behavior (see extract_description_events()) - so this failure mode
   never broke anything, it just meant the new real-timestamp feature
   silently wasn't engaging. There's no guarantee this fixes
   compliance every time; it's still a smaller (8B) instruction-tuned
   model being asked to hold a non-trivial formatting constraint
   across a long combined prompt.

4. Compliance without conformance - the model followed the bullet
   instruction but not the implied formatting around it. Once finding
   #3's fix landed, the very next real run against Christer's own
   footage DID produce bulleted "- [t=...]" markers - but crammed all
   ten of them onto a single line with no newlines between them, and
   varied the whitespace inside the brackets in every way a human
   never would: "[t=-0.3s]", "[ t=0s ]", "-[t= 0.6s]", even
   "[t = 0 .9 s]" with a space inside the number itself. The original
   parser assumed one bullet per line (splitlines() plus a
   line-anchored regex) and a fixed bracket format, so none of this
   matched - extract_description_events() silently found zero events
   and fell all the way back to raw bracket-laden prose (read aloud
   verbatim by the TTS "Read aloud" button) and the old evenly-spaced
   SRT chunking, exactly the "clean, silent fallback" behavior
   described in #3, just triggered by a new cause. Fixed by scanning
   the whole section text for "- [t=...]" markers wherever they occur
   (_BULLET_START_RE.finditer(), not splitlines()) and by stripping
   ALL whitespace out of the captured timestamp token before parsing
   it as a float (_parse_timestamp_token()), rather than assuming any
   particular spacing. A negative leading timestamp (t=-0.3s - the
   model's estimate for content right at the clip's start) is treated
   as legitimate input, not an error; see
   build_description_srt_from_events()'s docstring in
   web/archive_browser.py for how a cue that collapses to zero length
   after clamping now carries its text forward onto the next surviving
   cue instead of silently dropping it, which this negative-timestamp
   case triggers.

5. Confident invented vehicle motion, not just static content. Christer,
   reviewing a real front-camera clip's description: "The white van
   that overtakes me on the right side dont exist, unless it in my
   rear camera." No such vehicle appears anywhere in that clip's actual
   footage - the same "confident invented content" failure as finding
   #2 above (Palm Jumeirah), just applied to a moving object's
   relationship to the camera instead of a place name. A separate cue
   from the same real clip, originally read as the model inventing a
   rear-camera place name ("BIELEŃ" - see WORKING_CONTEXT.md's
   2026-08-19 curve-correction entry, where it was flagged as
   rear-camera/mirror bleed alongside the white van), turned out on
   closer look to likely be a garbled OCR-style reading of "BILEN"
   (Swedish for "the car") rather than actual evidence of this failure
   mode - noted here as a correction to that earlier entry, not as a
   second data point for it. DESCRIBE_PROMPT now includes an explicit,
   narrowly-scoped instruction: don't describe a vehicle as passing,
   overtaking, or approaching unless it's actually visible doing so in
   the frame itself, and don't describe rear-facing footage as if it
   were the forward view (or vice versa). Deliberately narrower than
   the general "don't infer from outside knowledge" prompt finding #2
   already tried and reverted for OCR - this targets spatial/motion
   grounding specifically (is this object visible in this camera's own
   frame right now) rather than semantic inference (is this a place I
   recognize), a different enough failure class that the same
   regression isn't a given, but also not something this change has
   verified against real footage yet. DISCLAIMER now also flags claims
   about another vehicle's movement relative to the camera as
   unverified, alongside plate/sign/place-name reads.

   Follow-up, same day: Christer pushed back on the "probably BILEN"
   correction above - "I am pretty sure BIELEN came from the rear
   camera, but nothing in the description indicates behind the car or
   rear view." That observation doesn't actually settle which reading
   is right, but it sharpens the concern either way: if BIELEŃ really
   is rear-camera content bleeding into a front-camera description,
   the text gives no self-aware signal of that at all (no "behind
   me"/"rear view" phrasing) - which is exactly what unflagged
   cross-camera bleed looks like from the outside, a plain factual-
   sounding sentence with no marker that it belongs to a different
   camera. The "correction to that earlier entry" framing above
   overreached by treating the BILEN theory as settled when it isn't -
   left as an open question rather than corrected further, since the
   real fix doesn't depend on resolving it: the DESCRIBE_PROMPT guard
   above (don't describe rear-facing footage as the front view or vice
   versa) targets the failure mode itself - unflagged cross-camera
   content presented as plain fact - regardless of which reading of
   this one ambiguous word turns out to be correct.

   Second follow-up, same day: the guard above overcorrected. Christer,
   testing against his own reference clip (its red bus event is his
   calibration anchor - "the bus is my most correct point," see
   WORKING_CONTEXT.md): "I AM MISSING MY RED BUS" - gone from the
   description at both max_frames=16 and 32, ruling out frame-sampling
   density as the cause (confirmed via AskUserQuestion: both tests ran
   after this same prompt-hardening change landed). The wording "unless
   you can see it appear, move through, or exit the frame in the
   footage itself" demanded motion continuity across frames - but this
   pipeline's frames are individual snapshots sampled seconds apart,
   never continuous video, so a real vehicle sighting almost never
   satisfies "appear, move through, and exit" even when it's genuinely
   visible in one of the sampled frames. The model, following that
   instruction literally, appears to have started suppressing real
   sightings like the bus, not just inventing ones. Loosened to require
   only that the vehicle is visible in at least one shown frame - drops
   the impossible-given-this-architecture motion-continuity bar while
   keeping the actual hallucination guard (don't invent a vehicle you
   can't see in any frame at all) and the separate mirror/cross-camera
   guard from the first follow-up above, both untouched.

Copyright (C) 2026 Christer R. (sssreh)

SPDX-License-Identifier: GPL-3.0-or-later
"""

from __future__ import annotations

import re
import subprocess
import sys
import time
from dataclasses import dataclass
from dataclasses import field
from pathlib import Path

from ..archive.photo import is_photo_path
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
_SCENE_GPU_VRAM_GB: list[float] | None = None

DISCLAIMER = (
    "\n\n---\n"
    "Note: the reads above (especially license plates, street/shop "
    "signs, and place names) are automated vision-model OCR output, "
    "not verified fact. This model has been observed to confidently "
    "report a wrong plate/sign read (not just say \"not legible\"), "
    "to occasionally invent plausible-sounding but unrelated "
    "place names on scenes it finds ambiguous, and to occasionally "
    "describe another vehicle passing or overtaking that isn't "
    "actually visible anywhere in the footage. Treat every read here, "
    "and any claim about another vehicle's movement relative to the "
    "camera, as unverified until checked against the source video."
)

DESCRIBE_PROMPT = (
    "This is a clip or still frame from a car dashcam or action "
    "camera. It usually shows driving footage, but it may occasionally "
    "be something else entirely - a stock photo, a test image, a "
    "photo someone took with the same camera. Describe what is "
    "actually visible in plain language, whatever that turns out to "
    "be: for ordinary driving footage, cover the road type, the "
    "weather/lighting conditions, the traffic situation, and anything "
    "notable; for anything else, just describe the real subject and "
    "setting the same way you would for any photo. Always describe "
    "what IS in the frame - never answer by saying what it is not "
    "(for example 'this is not dashcam footage' or 'the request "
    "cannot be fulfilled'), and never refuse to describe an image just "
    "because it doesn't show a road. Only describe a vehicle, sign, or "
    "other object as being in the scene if it is actually visible in "
    "a frame you're shown - a real sighting in even a single frame is "
    "enough, you do not need to see it enter, move through, and exit "
    "(the frames you're given are individual snapshots sampled seconds "
    "apart, not continuous video, so you will rarely see that much of "
    "it). Never invent a vehicle passing, overtaking, or approaching "
    "that you cannot actually see in any frame, and don't describe "
    "something that would only be visible in a mirror reflection as "
    "if it were in the camera's own direct view. This "
    "footage is from one single fixed camera (front-facing or "
    "rear-facing, whichever this clip actually is) - describe only "
    "what that one camera's own view actually shows, not what a "
    "different camera on the same vehicle might show.\n\n"
    "REQUIRED FORMAT for any video clip (not a single still photo): "
    "write the description ONLY as a bulleted list of distinct "
    "moments - never as a plain paragraph. Each bullet must be "
    "prefixed with its approximate elapsed time from the start of the "
    "clip in seconds, in exactly this format: '- [t=12.4s] A red bus "
    "passes on the left, driving in the opposite direction.' Start a "
    "new bullet whenever something worth mentioning appears, changes, "
    "or ends (a vehicle passing, a turn, a change in traffic or "
    "weather, and so on) - roughly one bullet every 5-15 seconds of "
    "footage is typical, more often if things are changing quickly, "
    "less often during a long uneventful stretch. Each bullet should "
    "be a complete, self-contained sentence ending in a period, since "
    "these will be read back individually. If nothing notable happens "
    "for the whole clip, still output a single bullet in this same "
    "format instead of a plain sentence (for example: '- [t=0.0s] "
    "Routine driving, nothing notable happened.') - don't invent "
    "drama, and don't list off categories of incident that didn't "
    "occur. The only exception: if this is a single still photo "
    "instead of a video clip, skip the timestamp/bullet format "
    "entirely - there is no timeline to reference - and just describe "
    "it directly in a sentence or two."
)

OCR_PROMPT = (
    "Read every piece of text visible anywhere in this frame - "
    "dashboard/overlay text (timestamp, speed, GPS coordinates), "
    "street signs, shop signs, license plates if legible, anything on "
    "other vehicles, and any text on the road itself, or any other "
    "text if this isn't a driving scene. List each piece of text you "
    "find, one per line. If there's genuinely no text anywhere in the "
    "frame, say so directly (for example: 'No text visible.') rather "
    "than treating it as an unanswerable request. If you can see text "
    "but can't make it out clearly, say so rather than guessing."
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
    "'## Description' and '## On-screen text'. Reminder: if this is a "
    "video clip, the '## Description' section itself must be written "
    "as the bulleted '[t=Xs]' timestamp list described above - not a "
    "plain paragraph."
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
    in docs/man/bv-scribe.md) for the reasoning behind each one.

    2026-08-19: max_frames was briefly raised past this tuning, twice,
    in the same day - both attempts reverted. First to 64 straight
    (Christer: "The problem is that i would like 64 frames per video
    so there is about 3 sec between them... our problem was that there
    where a heavy performane penalty for use to increase the number of
    frames"), which turned out to be a pure ~4x cost multiplier (the
    video branch was setting a fixed resized_width/resized_height,
    which bypasses qwen_vl_utils' own total_pixels budgeting) and, worse,
    a real quality regression - Christer: "Text description, less
    informative and fewer cues. I expected the oppsite, is it the
    cheaper model?" (it wasn't the model; qwen_vl_utils' own
    VIDEO_MAX_PIXELS ceiling, 768*28*28=602,112px/frame, no longer
    covered every frame once a total_pixels budget was divided across
    64 of them, so each frame's real resolution roughly halved). A
    second attempt tried max_frames=32 with total_pixels budgeting
    active, which mathematically reproduced the original 16-frame
    per-frame resolution while doubling temporal density - but Christer
    then asked to just go back to 16 outright rather than keep the
    added budgeting complexity: "I want to go back to 16." Reverted in
    full: max_frames back to 16, and the video branch back to the
    original fixed resized_width/resized_height (no total_pixels
    knob) - i.e. sampling behavior is now identical to what this
    project was tuned against before any of this same-day experimentation
    started. The DESCRIBE_PROMPT/DISCLAIMER hardening against phantom-
    vehicle hallucination from the same day's work is unrelated and
    was kept."""

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
    # "auto" (default), or an explicit "none"/"int8"/"int4" - see
    # resolve_scene_quantize() below for how "auto" gets resolved.
    # Christer: "yes, can you make it auto by looking at present
    # graphics cards" - asked right after learning quantization would
    # need its own flag (a loading-precision choice for the *same*
    # model, not a different model to pick), for hardware smaller than
    # his RTX 5090 laptop (his old dual-RTX-3080-Ti box, 12GB VRAM per
    # card, came up as the concrete example in that conversation).
    quantize: str = "auto"
    # None (default) = no cap, the model claims whatever VRAM it wants
    # (today that's ~19.3GB of a 24GB card for unquantized
    # Qwen3-VL-8B-Instruct). An explicit 0 < value <= 1.0 caps this
    # process's CUDA allocations to that fraction of each visible
    # card's total VRAM, via torch.cuda.set_per_process_memory_fraction() -
    # so scene description guarantees some VRAM stays free for
    # something else running on the same GPU at the same time, instead
    # of hoping the driver is polite about it. Christer, after asking
    # whether describe_scene() could run "with lower priority in order
    # to spare the GPU, for other stuff": there's no real per-process
    # GPU *compute* priority knob on consumer NVIDIA/Windows drivers
    # (that's an MPS priority-queue feature, data-center/Linux only) -
    # this memory-fraction cap is the one real, implementable lever,
    # logged as a note at the time ("put it in as a note") and built
    # once the auto-quantization work above landed. CUDA-only, same as
    # quantize - see _load_scene_model()'s own contradiction check
    # against force_cpu.
    gpu_memory_fraction: float | None = None


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


def scene_gpu_vram_gb() -> list[float]:
    """Total VRAM (in GB) of every CUDA device visible to this
    process, largest first. Cached the same way vision_gpu_available()
    is - only ever informs a friendlier default (see
    resolve_scene_quantize() below), never blocks startup. Empty list
    if CUDA isn't available at all, or if probing it raises for any
    reason (e.g. a broken driver install) - same "swallow, return the
    unhelpful-but-safe answer" contract every other GPU probe in this
    codebase follows."""

    global _SCENE_GPU_VRAM_GB

    if _SCENE_GPU_VRAM_GB is None:
        try:
            import torch

            if torch.cuda.is_available():
                _SCENE_GPU_VRAM_GB = sorted(
                    (
                        torch.cuda.get_device_properties(i).total_memory / (1024**3)
                        for i in range(torch.cuda.device_count())
                    ),
                    reverse=True,
                )
            else:
                _SCENE_GPU_VRAM_GB = []
        except Exception:
            _SCENE_GPU_VRAM_GB = []

    return _SCENE_GPU_VRAM_GB


# Thresholds (GB) for resolve_scene_quantize()'s "auto" behavior below,
# keyed on the *largest single* detected GPU - not the sum across every
# card. Qwen3-VL-8B-Instruct's native bf16 weights are a ~16GB
# footprint (Christer's own report: "Scene model never unloads from
# GPU"); bitsandbytes int8 roughly halves that, int4 roughly quarters
# it. device_map="auto" *can* shard a too-large model across multiple
# GPUs when it doesn't fit on one, but that's slower PCIe-pipelined
# inference, not what quantization is for here - the real win on
# Christer's dual-RTX-3080-Ti box (12GB each, discussed alongside this
# feature) is quantizing the model down onto *one* card so it never
# needs to shard at all, and so each card could eventually host an
# independent job rather than jointly hosting one slow one.
_SCENE_QUANTIZE_NONE_MIN_GB = 20.0
_SCENE_QUANTIZE_INT8_MIN_GB = 10.0

VALID_SCENE_QUANTIZE_LEVELS = ("none", "int8", "int4")


def resolve_scene_quantize(requested: str, *, force_cpu: bool) -> str:
    """Turn SceneOptions.quantize ("auto", or an explicit "none"/
    "int8"/"int4") into one of the three concrete levels - the same
    "auto-detected default, but still overridable" shape bv-generate's
    own --model-size already uses for Whisper (`args.model_size =
    "large" if gpu_available() else "small"`).

    An explicit non-"auto" value passes straight through unchanged
    (raises ValueError for anything else - a typo'd flag value should
    fail loudly, not silently fall back to some other level).

    "auto" resolves against scene_gpu_vram_gb(): CPU-forced or no CUDA
    GPU at all always resolves to "none" - bitsandbytes' int8/int4
    loading paths are CUDA-only, so quantizing on the way to a CPU load
    would buy nothing. Otherwise it's keyed on the *largest single*
    detected GPU - see the threshold constants' own comment above for
    why largest-single rather than summed-total."""

    if requested != "auto":
        if requested not in VALID_SCENE_QUANTIZE_LEVELS:
            raise ValueError(
                f"invalid scene quantize level {requested!r} - expected "
                f"'auto' or one of {VALID_SCENE_QUANTIZE_LEVELS}"
            )
        return requested

    if force_cpu:
        return "none"

    vram_gb = scene_gpu_vram_gb()
    if not vram_gb:
        return "none"

    largest_gb = vram_gb[0]
    if largest_gb >= _SCENE_QUANTIZE_NONE_MIN_GB:
        return "none"
    if largest_gb >= _SCENE_QUANTIZE_INT8_MIN_GB:
        return "int8"
    return "int4"


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


def _load_scene_model(
    model_name: str,
    *,
    force_cpu: bool,
    quantize: str = "none",
    gpu_memory_fraction: float | None = None,
) -> _LoadedSceneModel:
    # quantize ("int8"/"int4") is a bitsandbytes-backed, CUDA-only
    # loading precision - resolve_scene_quantize() already resolves
    # "auto" to "none" whenever force_cpu is set (see its own
    # docstring), so a caller reaching this with quantize != "none"
    # and force_cpu=True can only mean an explicit, contradictory
    # SceneOptions(quantize=..., force_cpu=True) - worth a clear error
    # rather than silently ignoring one of the two. Checked before the
    # torch/transformers imports below so the caller gets this specific
    # message even on a machine where those packages aren't installed
    # at all (an unrelated problem they'd hit either way).
    if quantize != "none" and force_cpu:
        raise MediaToolError(
            f"scene quantize={quantize!r} needs a CUDA GPU - it "
            "can't be combined with force_cpu/--cpu"
        )

    # gpu_memory_fraction is a CUDA-only cap (see SceneOptions' own
    # comment) - same contradiction-with-force_cpu reasoning as
    # quantize above, checked here for the same "fail before the
    # torch/transformers imports" reason. Range-checked here too
    # (rather than leaving it to torch.cuda.set_per_process_memory_fraction()
    # itself) so a bad CLI value fails with a message naming this
    # project's own flag, not a raw CUDA driver error surfacing deep
    # inside model loading.
    if gpu_memory_fraction is not None:
        if force_cpu:
            raise MediaToolError(
                f"scene gpu_memory_fraction={gpu_memory_fraction!r} needs a "
                "CUDA GPU - it can't be combined with force_cpu/--cpu"
            )
        if not (0.0 < gpu_memory_fraction <= 1.0):
            raise MediaToolError(
                f"scene gpu_memory_fraction={gpu_memory_fraction!r} must be "
                "greater than 0 and at most 1.0"
            )

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

    # Applied before the model actually allocates anything below, on
    # every visible CUDA device rather than just device 0 - device_map
    # "auto" can in principle shard the model across more than one GPU
    # (see the quantize threshold comment above for why that's not the
    # normal case this project tunes for, but it's still possible), and
    # this cap needs to hold on whichever device(s) end up hosting it.
    # A no-op if gpu_memory_fraction is None (the default) or CUDA
    # isn't available - matches every other GPU probe in this module's
    # "only ever informs a friendlier choice, never blocks startup"
    # contract, though here there's nothing left to inform, just a
    # limit to set.
    if gpu_memory_fraction is not None and torch.cuda.is_available():
        for device_index in range(torch.cuda.device_count()):
            torch.cuda.set_per_process_memory_fraction(gpu_memory_fraction, device_index)

    # quantize ("int8"/"int4") is a bitsandbytes-backed, CUDA-only
    # loading precision - resolve_scene_quantize() already resolves
    # "auto" to "none" whenever force_cpu is set (see its own
    # docstring), so a caller reaching this with quantize != "none"
    # and force_cpu=True can only mean an explicit, contradictory
    # SceneOptions(quantize=..., force_cpu=True) - worth a clear error
    # rather than silently ignoring one of the two.
    quantization_config = None
    if quantize != "none":
        if force_cpu:
            raise MediaToolError(
                f"scene quantize={quantize!r} needs a CUDA GPU - it "
                "can't be combined with force_cpu/--cpu"
            )
        try:
            from transformers import BitsAndBytesConfig
        except ImportError as exc:
            raise MediaToolError(
                f"scene quantize={quantize!r} needs bitsandbytes "
                f"installed ({exc}) - see pyproject.toml's scene extra"
            ) from exc
        quantization_config = BitsAndBytesConfig(
            load_in_8bit=(quantize == "int8"),
            load_in_4bit=(quantize == "int4"),
        )

    # torch_dtype: "auto" lets transformers pick the model's own native
    # dtype (bfloat16 for Qwen3-VL-8B-Instruct). bitsandbytes' int8
    # path (LLM.int8(), MatMul8bitLt) only ever computes in float16
    # though - handed bfloat16 activations, it silently casts them on
    # every single matmul and warns about it every single time
    # ("MatMul8bitLt: inputs will be cast from torch.bfloat16 to
    # float16 during quantization"), which floods stderr for the
    # thousands of matmuls in one video's worth of frames (Christer
    # hit this running --scene-quantize int8 for real). Loading in
    # float16 up front for int8 sidesteps the cast (and its warning)
    # entirely - it's what the weights get cast to anyway. int4
    # (NF4) doesn't have this issue: its own bnb_4bit_compute_dtype
    # defaults independently and isn't tied to the load dtype the
    # same way, so "auto" is left alone there.
    torch_dtype = "float16" if quantize == "int8" else "auto"
    from_pretrained_kwargs: dict = {"torch_dtype": torch_dtype, "device_map": device_map}
    if quantization_config is not None:
        from_pretrained_kwargs["quantization_config"] = quantization_config

    try:
        model = ModelClass.from_pretrained(model_name, **from_pretrained_kwargs)
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


def _get_scene_model(
    model_name: str,
    *,
    force_cpu: bool,
    quantize: str = "auto",
    gpu_memory_fraction: float | None = None,
) -> _LoadedSceneModel:
    """Return a cached loaded scene model, loading it if needed.

    `quantize` ("auto" by default, or an explicit "none"/"int8"/
    "int4") is resolved once here via resolve_scene_quantize() before
    the cache is consulted, so "auto" caches under whichever concrete
    level this machine's GPU(s) actually resolved to - see that
    function's own docstring. Cache key includes both the cpu flag and
    the resolved quantize level, same reasoning as speech.py's
    _get_whisper_model() - a --cpu-forced call, an auto-detected call,
    and a specific quantize level for the same model name may all
    legitimately be wanted within one process. `gpu_memory_fraction` is
    included in the cache key too, for the same reason - it's a
    process-wide CUDA setting only actually applied inside
    _load_scene_model() at load time (see that function), so a second
    call asking for a different cap wouldn't take effect against an
    already-loaded, cache-hit model otherwise."""

    resolved_quantize = resolve_scene_quantize(quantize, force_cpu=force_cpu)
    cache_key = (
        f"{model_name}:{'cpu' if force_cpu else 'auto'}:{resolved_quantize}"
        f":{gpu_memory_fraction}"
    )

    if cache_key not in _SCENE_MODEL_CACHE:
        _SCENE_MODEL_CACHE[cache_key] = _load_scene_model(
            model_name,
            force_cpu=force_cpu,
            quantize=resolved_quantize,
            gpu_memory_fraction=gpu_memory_fraction,
        )

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
    force_cpu, quantize)` combination (if any) the job that just
    finished actually used. Pass `model_name` (and optionally
    `force_cpu`) to evict matching entries instead - `model_name`
    alone evicts every quantize variant of both the `:cpu` and `:auto`
    forms of that model (prefix-matched, since `_get_scene_model()`'s
    cache key has a third, resolved-quantize segment this function
    doesn't need to know the value of); `model_name` plus `force_cpu`
    narrows that to just the `:cpu` or `:auto` variants, still across
    every quantize level.

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
            if key.startswith(f"{model_name}:cpu:") or key.startswith(f"{model_name}:auto:")
        ]
    else:
        prefix = f"{model_name}:{'cpu' if force_cpu else 'auto'}:"
        keys_to_drop = [key for key in _SCENE_MODEL_CACHE if key.startswith(prefix)]

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


def _extract_raw_description_section(output_text: str) -> str:
    """Pull just the '## Description' section out of a per-recording
    result, verbatim - dropping the on-screen-text/zoomed-sign-reads
    sections, but not otherwise touching the content. Shared by
    extract_description_section() (which cleans this up for display/
    summarization) and extract_description_events() (which parses the
    real per-event timestamps back out of it) below - both need the
    exact same raw slice, just processed differently."""

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


# Matches a "- [t=12.4s]" bullet marker anywhere in the text - not
# anchored to line start, and tolerant of stray whitespace inside the
# brackets. First real-world run of this feature (Christer, pasting
# the actual TTS/srt output back): the model didn't reliably put one
# bullet per line, and didn't reliably write "t=12.4s" with no
# whitespace either - real output included everything crammed onto a
# single line ("- [t=-0.3s] ... - [ t=0s ] ... -[t= 0.6s] ... - [t = 0
# .9 s] ..."), with spaces appearing between "t"/"="/the digits/the
# decimal point/"s" in every possible combination, and even a leading
# negative timestamp before the clip's actual start. The original
# regex was anchored to the start of an already-newline-split "- "
# line and required the exact literal "[t=12.4s]" with zero
# whitespace tolerance - it matched none of that, so every bullet
# silently failed to parse and the whole thing fell back to being
# treated as one big plain-text blob (bracket notation and all, read
# aloud verbatim by TTS). Finding bullet markers by scanning the whole
# text with this looser pattern - rather than requiring line
# boundaries - handles both the one-bullet-per-line format the prompt
# asks for and the single-line-crammed-together format the model
# actually produced.
_BULLET_START_RE = re.compile(r"-\s*\[\s*t\s*=\s*(?P<raw_seconds>[^\]]*)\]", re.IGNORECASE)


def _parse_timestamp_token(raw_seconds: str) -> float | None:
    """Turn whatever text a real model put between 't=' and ']' into a
    float number of seconds - tolerating internal whitespace anywhere
    (including inside the number itself, e.g. '0 .9 s') and an
    optional trailing 's' unit, both observed in real output (see
    _BULLET_START_RE's own comment). Returns None for anything that
    still isn't a number once whitespace is stripped, so one genuinely
    malformed bullet is skipped rather than crashing the whole parse
    or being silently treated as zero."""

    token = re.sub(r"\s+", "", raw_seconds)
    if token.lower().endswith("s"):
        token = token[:-1]
    try:
        return float(token)
    except ValueError:
        return None


@dataclass(frozen=True)
class DescriptionEvent:
    """One parsed '## Description' bullet, once DESCRIBE_PROMPT started
    asking for timestamped events instead of one holistic paragraph -
    see that prompt's own text for the exact format requested, and
    _parse_timed_events() for how a raw section turns into these.
    Christer, right after getting the sign-reads' scene.srt and the
    (then evenly-spaced) description.srt working: "It would have been
    nice to both say and subtitle 'To the left, there's a red bus
    passing alongside the vehicle' at the same time you can see the
    red buss pass" - this is what makes that possible: real per-event
    sync points for the description, the same way zoom_into_signs()
    already provides them for sign/plate reads."""

    timestamp_seconds: float
    text: str


def _parse_timed_events(section_text: str) -> list[DescriptionEvent]:
    """Parse a raw '## Description' section (as returned by
    _extract_raw_description_section()) into DescriptionEvents, if it's
    in the new bulleted-and-timestamped format DESCRIBE_PROMPT now asks
    for. Returns [] for anything else - a still photo's plain sentence
    (no timeline to bullet), an older scene.txt written before this
    format existed, or any other free-text response that isn't
    bulleted - so every caller can treat "no events" as "fall back to
    treating this as plain prose" without a separate format check.

    Deliberately does not assume one bullet per line. Finds every
    "- [t=...]" marker anywhere in the text via _BULLET_START_RE and
    takes each bullet's text as the span between one marker and the
    next (or end of text for the last one) - this naturally folds
    multi-line-wrapped bullet text back together (whitespace, newlines
    included, gets collapsed when building the text below) the same
    way the old line-based version's continuation-folding did, but
    also survives the model cramming every bullet onto a single line
    with no newlines at all (see _BULLET_START_RE's own comment for
    the real-world example that broke the original line-anchored
    version)."""

    matches = list(_BULLET_START_RE.finditer(section_text))
    events: list[DescriptionEvent] = []
    for index, match in enumerate(matches):
        seconds = _parse_timestamp_token(match.group("raw_seconds"))
        if seconds is None:
            continue
        start = match.end()
        end = matches[index + 1].start() if index + 1 < len(matches) else len(section_text)
        text = " ".join(section_text[start:end].split())
        if text:
            events.append(DescriptionEvent(timestamp_seconds=seconds, text=text))

    return events


def extract_description_section(output_text: str) -> str:
    """Pull just the '## Description' section out of a per-recording
    result and return it as a single clean, readable paragraph - used
    to build summarize_trip()'s input (see that function's own
    docstring) and by web/archive_browser.py's on-page description/TTS
    narration.

    Since DESCRIBE_PROMPT started asking for a bulleted, per-event-
    timestamped description (see that prompt and DescriptionEvent
    above) rather than one holistic paragraph, this strips each
    bullet's "- [t=X.Ys] " prefix and joins the remaining sentences
    with spaces into one flowing paragraph - callers here never see
    the bracket notation, exactly as if the model had written a plain
    paragraph directly. Christer: after getting the real-timestamp
    request in ("It would have been nice to both say and subtitle
    ... at the same time you can see ...  pass"), also: "please keep
    the old output" - this is that: every existing caller of this
    function keeps getting the same kind of plain-prose text back it
    always has, whether the underlying scene.txt is old-format (no
    bullets at all - returned unchanged, exactly as before this
    change) or new-format (bullets stripped down to prose). Callers
    that want the real per-event timestamps instead of prose should
    use extract_description_events() below."""

    raw = _extract_raw_description_section(output_text)
    events = _parse_timed_events(raw)
    if not events:
        return raw
    return " ".join(event.text for event in events)


def extract_description_events(output_text: str) -> list[DescriptionEvent]:
    """The real per-event timestamps behind extract_description_section()'s
    clean prose, for a caller that actually wants to sync something to
    the video's own timeline instead of just reading the text (see
    web/archive_browser.py's description_srt(), which prefers these
    real timestamps over its own evenly-spaced-chunk fallback whenever
    they're available). [] for a still photo or an older, pre-
    timestamp scene.txt - see _parse_timed_events()'s own docstring for
    why an empty list is exactly the right "nothing to sync against
    here" signal for callers to fall back on."""

    raw = _extract_raw_description_section(output_text)
    return _parse_timed_events(raw)


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


def _photo_as_pil_image(path: Path):
    """Decode a still photo (any of archive/photo.py's PHOTO_EXTENSIONS
    - jpg/jpeg/png/heic/gpr) into a PIL Image via an ffmpeg subprocess,
    piped through memory rather than handed to PIL's own format
    support directly. PIL doesn't reliably cover every one of those
    extensions on its own - HEIC needs a plugin it doesn't ship with,
    GPR is GoPro's own RAW format - the exact same reason
    export/media.py's render_image_as_video() already decodes photos
    via ffmpeg rather than PIL (see that function's own docstring).
    ffmpeg picks its decoder from the source extension, so this covers
    every PHOTO_EXTENSIONS member unmodified, no per-format branching
    needed here.

    Real bug this exists to fix: describe_scene() unconditionally
    built a `{"type": "video", ...}` message for every input, so
    bv-generate --describe-scene / bv-scribe on a photo recording fed
    a still image straight into qwen_vl_utils' video-decoding path
    (decord), which can't open a JPEG/PNG/etc at all - "pictures dont
    get scene asset" (Christer). describe_scene() below now branches
    on is_photo_path() and uses this helper to build a real `"image"`
    content element instead."""

    from io import BytesIO

    from PIL import Image as PILImage

    try:
        result = subprocess.run(
            [
                "ffmpeg", "-v", "error",
                "-i", str(path),
                "-frames:v", "1",
                "-f", "image2pipe",
                "-vcodec", "png",
                "-",
            ],
            capture_output=True,
            check=True,
        )
    except FileNotFoundError as exc:
        raise MediaToolError("ffmpeg not found on PATH") from exc
    except subprocess.CalledProcessError as exc:
        raise MediaToolError(
            f"could not decode photo {path.name}: "
            f"{exc.stderr.decode('utf-8', errors='replace')}"
        ) from exc

    return PILImage.open(BytesIO(result.stdout)).convert("RGB")


def _extract_full_res_frames(video_path: Path, count: int):
    """Grab `count` evenly-spaced frames from the source video at full
    native resolution, for the zoom pipeline's grounding step. Returns
    a list of (timestamp_seconds, PIL.Image) tuples. For a still photo
    (is_photo_path()), there's only ever one "frame" - the photo
    itself, decoded via _photo_as_pil_image() rather than decord (which
    can't open a still image at all) - so this returns a single
    t=0.0 entry instead of sampling anything."""

    if is_photo_path(video_path):
        return [(0.0, _photo_as_pil_image(video_path))]

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

    video_path may be a still photo (is_photo_path() - jpg/jpeg/png/
    heic/gpr) as well as a real video: a `Recording`'s FRONT/REAR asset
    is exactly the same either way (see archive/photo.py's own module
    docstring, "a picture is also a video, but 1 frame only"), and
    neither bv-generate's --describe-scene pass nor bv-scribe knows or
    checks which one a given recording is before calling this function.
    A photo builds a single `{"type": "image", ...}` content element
    (via _photo_as_pil_image()) instead of a `{"type": "video", ...}`
    one - the fix for a real gap Christer hit: every call here used to
    build a "video" element unconditionally, so a photo got handed to
    qwen_vl_utils' video-decoding path (decord), which can't open a
    still image at all - "pictures dont get scene asset" (Christer).
    fps/max_frames don't apply to a single image and are omitted for
    that branch; everything else (prompt, resized_width/height, the
    zoom-signs sub-pipeline, the disclaimer footer) works identically
    either way.
    """

    if opts is None:
        opts = SceneOptions(**overrides)
    elif overrides:
        raise TypeError("pass either opts= or **overrides, not both")

    warn = warn or (lambda msg: print(msg, file=sys.stderr))

    loaded = _get_scene_model(
        opts.model,
        force_cpu=opts.force_cpu,
        quantize=opts.quantize,
        gpu_memory_fraction=opts.gpu_memory_fraction,
    )

    prompt = build_prompt(opts.task)

    if is_photo_path(video_path):
        photo_image = _photo_as_pil_image(video_path)
        photo_image = _crop_overlay_from_image(photo_image, opts.crop_top, opts.crop_bottom)
        content_ele = {"type": "image", "image": photo_image}
        if opts.resized_width and opts.resized_height:
            content_ele["resized_width"] = opts.resized_width
            content_ele["resized_height"] = opts.resized_height
    else:
        content_ele = {
            "type": "video",
            "video": str(video_path.resolve()),
            "fps": opts.fps,
            "max_frames": opts.max_frames,
        }
        if opts.resized_width and opts.resized_height:
            content_ele["resized_width"] = opts.resized_width
            content_ele["resized_height"] = opts.resized_height
        else:
            content_ele["max_pixels"] = opts.max_pixels

    messages = [{"role": "user", "content": [content_ele, {"type": "text", "text": prompt}]}]
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

    loaded = _get_scene_model(
        opts.model,
        force_cpu=opts.force_cpu,
        quantize=opts.quantize,
        gpu_memory_fraction=opts.gpu_memory_fraction,
    )

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
