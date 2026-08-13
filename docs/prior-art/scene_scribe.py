#!/usr/bin/env python3
r"""scene_scribe.py - standalone dashcam video describer/OCR prototype.

Deliberately kept OUTSIDE the beyond-video repo for now (Christer's own
call - "keep it outside of beyond video for now, if its good enough we
might merge it in later"). This is a prototype to answer one question:
is a locally-run open-source vision-language model good enough at
describing dashcam footage (and reading on-screen text) to be worth
building into bv-generate later, or does it need a cloud model like
Gemini to be usable?

Uses Qwen2.5-VL (currently the strongest practical open-source local
option for video understanding - see WebSearch research from this
conversation: it scores well below Gemini 2.5 Pro on general video
benchmarks like Video-MME, but this isn't a general benchmark task,
it's a narrow one: "what road/weather/traffic/notable event is this,
and what does any on-screen text say" - well within a 7B model's
reach, and it also has strong built-in OCR so one model does both jobs
in a single pass instead of needing a separate OCR tool).

Usage:
    python scene_scribe.py path\to\recording.mp4
    python scene_scribe.py path\to\recording.mp4 --task ocr
    python scene_scribe.py path\to\recording.mp4 --model Qwen/Qwen2.5-VL-3B-Instruct
    python scene_scribe.py path\to\recording.mp4 --output description.txt

    # Batch mode: give it a folder (or several files/folders) instead
    # of one video and the model loads exactly once, not once per
    # video - see --output-dir/--output-suffix/--overwrite. Each
    # video's result is written next to it (or into --output-dir) as
    # <video-stem>.scene-scribe.txt; already-processed videos are
    # skipped on a re-run unless --overwrite is given, so an
    # interrupted archive-scale run can just be re-launched.
    python scene_scribe.py C:\archive\kirby\2026

First run downloads the model from Hugging Face (~16GB for the default
7B model, one-time, cached under ~/.cache/huggingface). No HF_TOKEN
needed - Qwen2.5-VL-7B-Instruct is not a gated model.
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

DEFAULT_MODEL = "Qwen/Qwen2.5-VL-7B-Instruct"

# Qwen2.5-VL's vision encoder patchifies in 28px steps (14px patch size *
# 2x2 merge) - qwen_vl_utils' own smart_resize() always returns
# dimensions that are multiples of this, so any crop we do afterward
# has to preserve that or the vision tower's patch-merging step breaks.
# Qwen3-VL uses a 16px patch size instead (still 2x2 merge), so its
# factor is 32, not 28 - set_patch_factor_for_model() below updates this
# module-level value once at startup, based on --model, before any
# crop/resize function runs. Every function that rounds to a patch
# boundary reads this name as a global rather than taking it as a
# parameter, so updating it once here is enough to propagate correctly.
PATCH_FACTOR = 28


def set_patch_factor_for_model(model_name: str) -> None:
    """Pick the right patch factor for the model family in `model_name`
    and update the module-level PATCH_FACTOR global. Must be called
    once, before parse_args()'s caller does any crop/resize work, and
    after that point PATCH_FACTOR should be treated as read-only."""

    global PATCH_FACTOR
    PATCH_FACTOR = 32 if is_qwen3_vl(model_name) else 28


def is_qwen3_vl(model_name: str) -> bool:
    """Qwen3-VL needs a different model class (Qwen3VLForConditionalGeneration
    instead of Qwen2_5_VLForConditionalGeneration) and a different patch
    factor (32 instead of 28) - detected from the model name string
    rather than requiring a separate --model-family flag, since the
    name already has to be unambiguous (it's what gets passed to
    from_pretrained())."""

    return "qwen3-vl" in model_name.lower()

DESCRIBE_PROMPT = (
    "This is a clip from a car dashcam. Describe what's happening in "
    "plain language: what kind of road this is, the weather/lighting "
    "conditions, the traffic situation, and anything notable. If "
    "nothing notable happens, say so in a single plain sentence (for "
    "example: 'Routine driving, nothing notable happened.') - don't "
    "invent drama, and don't list off categories of incident that "
    "didn't occur."
    # Earlier version explicitly listed example categories (near miss,
    # stopped/erratic vehicle, pedestrian/animal, accident, road work,
    # unusual maneuver) - the model treated that as a checklist and
    # echoed each one back as absent ("no pedestrians or animals are
    # visible, and there doesn't appear to be any road work..."),
    # which is technically correct but reads like a report of what
    # *didn't* happen rather than a plain description. Dropping the
    # explicit list and adding "don't list off categories" directly
    # should stop that pattern.
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
    # previously-correct shop-sign read). The real fix for the
    # tunnel-name case is almost certainly --fps/--max-frames density
    # (see their help text) - the sign was probably never sampled at
    # all, so no amount of prompt wording can make the model read
    # pixels it never saw.
)

COMBINED_PROMPT = (
    f"{DESCRIBE_PROMPT}\n\nSeparately, then do this:\n\n{OCR_PROMPT}\n\n"
    "Structure your answer as two sections with the headings "
    "'## Description' and '## On-screen text'."
)

# Used by the optional --zoom-signs pipeline (zoom_into_signs()): a
# separate, targeted second pass that locates signs/plates in a
# full-resolution frame and re-reads just that crop, instead of
# relying on the main pass's whole-frame resolution to resolve small
# distant text. Qwen2.5-VL supports this bbox_2d/label JSON grounding
# format natively (per its own cookbook examples) - it's a real model
# capability, not a workaround.
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

# Separate prompt for crops the grounding pass already labeled as a
# "vehicle license plate" - narrower framing (parsing task, not
# description). Deliberately does NOT mandate a fixed-length format as
# a hard constraint: real-footage testing found a plate that read
# consistently as a 7-character string ("CWA 986D") under the generic
# prompt, and Christer explained why that's plausible rather than a
# hallucination - regular Swedish plates are 3 letters + space + 2
# digits + 1 trailing alphanumeric (the last slot became
# letter-or-digit once digit combinations started running low), and
# personalized/vanity plates can run longer still, with the space
# itself sometimes used as a meaningful character slot. An earlier,
# stricter version of this prompt stated a fixed format as a rule,
# which risked forcing exactly that kind of plate into the wrong
# shape. Described as the normal case, not a constraint, so it still
# nudges away from junk characters without overriding a genuine
# longer/irregular read.
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


def build_prompt(task: str) -> str:
    if task == "describe":
        return DESCRIBE_PROMPT
    if task == "ocr":
        return OCR_PROMPT
    return COMBINED_PROMPT


# Used by the optional --trip-summary pass: a text-only synthesis call
# over every recording's already-generated '## Description' section,
# explicitly asked to track change over the trip rather than just
# restate each segment - Christer's own words for the target shape:
# "moderate traffic became heavier after a while", not "segment 1: X.
# segment 2: Y. segment 3: Z."
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


def extract_description_section(output_text: str) -> str:
    """Pull just the '## Description' section out of a per-file
    scene-scribe result, dropping the On-Screen Text/Zoomed sign reads
    sections. The trip-summary pass only needs what happened, not the
    sign/plate transcriptions - keeping those out of the synthesis
    prompt avoids diluting it with text the model would otherwise try
    to weave into a narrative it doesn't belong in."""

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


def summarize_trip(segments: list[tuple[str, str]], model, processor, process_vision_info, args) -> str:
    """Text-only pass: takes each recording's already-generated
    '## Description' text (via extract_description_section()) and
    asks the model to synthesize one trip-level narrative that tracks
    change over time, rather than the trip output just being those
    per-file descriptions concatenated back to back. No video/image
    input at all, so - unlike every other call in this script - its
    cost doesn't scale with --max-frames/--fps; it's just prose in,
    prose out."""

    segment_text = "\n\n".join(f"[{label}]\n{text}" for label, text in segments)
    prompt = TRIP_SUMMARY_PROMPT_TEMPLATE.format(segments=segment_text)

    messages = [{"role": "user", "content": [{"type": "text", "text": prompt}]}]
    text = processor.apply_chat_template(
        messages, tokenize=False, add_generation_prompt=True
    )
    image_inputs, video_inputs, video_kwargs = fetch_vision_inputs(process_vision_info, messages)
    inputs = processor(
        text=[text],
        images=image_inputs,
        videos=video_inputs,
        padding=True,
        return_tensors="pt",
        **video_kwargs,
    )
    inputs = inputs.to(model.device)
    generated_ids = model.generate(
        **inputs,
        max_new_tokens=args.trip_summary_max_new_tokens,
        repetition_penalty=args.repetition_penalty,
        no_repeat_ngram_size=args.no_repeat_ngram_size,
        **sampling_kwargs(args),
    )
    trimmed = [
        out_ids[len(in_ids):]
        for in_ids, out_ids in zip(inputs.input_ids, generated_ids)
    ]
    return processor.batch_decode(
        trimmed, skip_special_tokens=True, clean_up_tokenization_spaces=False
    )[0]


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Scene-Scribe: describe a dashcam video's contents and/or "
            "read its on-screen text using a local Qwen2.5-VL model."
        ),
        allow_abbrev=False,
    )
    parser.add_argument(
        "paths",
        type=Path,
        nargs="+",
        metavar="video",
        help=(
            "One or more video files, or a directory of .mp4 files "
            "(non-recursive) - a directory triggers batch mode, "
            "loading the model once and reusing it for every video "
            "instead of reloading per invocation."
        ),
    )
    parser.add_argument(
        "--task",
        choices=["describe", "ocr", "both"],
        default="both",
        help=(
            "'describe' for what's happening, 'ocr' for on-screen "
            "text only, 'both' for a single combined pass (default)."
        ),
    )
    parser.add_argument(
        "--model",
        default=DEFAULT_MODEL,
        help=(
            f"Hugging Face model id (default: {DEFAULT_MODEL}). Try "
            "Qwen/Qwen2.5-VL-3B-Instruct if you want faster iteration "
            "or are tight on VRAM, or Qwen/Qwen2.5-VL-7B-Instruct-AWQ "
            "for a quantized version of the default model. Qwen3-VL "
            "(e.g. Qwen/Qwen3-VL-8B-Instruct, sizes 2B/4B/8B/32B) is "
            "also supported - any model id containing 'qwen3-vl' "
            "(case-insensitive) auto-switches to the right model class "
            "and patch factor. Untested on real footage as of this "
            "flag's addition - see the code comment at the model-class "
            "selection in main() for the specific risk (its vision "
            "input pipeline may not be fully compatible with this "
            "script's qwen_vl_utils-based crop/resize approach). "
            "Requires transformers>=4.57.0."
        ),
    )
    parser.add_argument(
        "--fps",
        type=float,
        default=1.0,
        help=(
            "How many frames per second of video to sample and feed "
            "the model (default: 1.0). --max-frames is a safety cap on "
            "top of this, so on longer clips the effective spacing can "
            "be wider than 1/fps. A real-footage test tried 0.333 "
            "(every 3s) with --max-frames 60 to catch a briefly-visible "
            "highway sign the old denser-in-theory-but-capped setting "
            "missed - it cost ~20x more time (330s+ vs 15s per video) "
            "and did NOT fix the sign-reading problem: the model still "
            "invented plausible-but-wrong Swedish place names for text "
            "it landed a frame near but still couldn't actually resolve "
            "at this resolution. That points at --resized-width/-height "
            "as the real lever for legibility, not frame count - see "
            "those flags' defaults."
        ),
    )
    parser.add_argument(
        "--max-frames",
        type=int,
        default=16,
        help=(
            "Hard cap on sampled frames regardless of --fps/video "
            "length (default: 16). Lower this first if generation is "
            "taking too long - frame count and --resized-width/-height "
            "are the two biggest levers on speed and VRAM."
        ),
    )
    parser.add_argument(
        "--max-pixels",
        type=int,
        default=360 * 420,
        help=(
            "Resolution cap per sampled frame, in total pixels "
            "(default: 151200, i.e. roughly 420x360 - the same value "
            "Qwen2.5-VL's own README example uses for video). Each "
            "frame gets downscaled to at most this many pixels before "
            "the model sees it. This is the single biggest lever on "
            "both VRAM use and generation time, since attention cost "
            "scales with total vision tokens across all frames "
            "combined - full dashcam resolution (1080p+) per frame "
            "is serious overkill for this task and was almost "
            "certainly the main reason the first real run took ~8 "
            "minutes just to generate the answer."
        ),
    )
    parser.add_argument(
        "--resized-width",
        type=int,
        default=1092,
        help=(
            "Force an exact frame width (rounded to the nearest valid "
            "size internally), bypassing --max-pixels entirely - this "
            "is the actual default resolution knob now, not --max-pixels. "
            "Added because --max-pixels was empirically confirmed to be "
            "a no-op against the pinned qwen-vl-utils==0.0.8. Raised "
            "from 728 to 1092 (~2.25x more pixels/frame) after a "
            "--max-frames experiment showed more frames didn't fix "
            "small/distant road-sign legibility - the model was landing "
            "a frame near the sign but still couldn't resolve the "
            "characters at 728x392, so it fell back to inventing a "
            "plausible-sounding wrong name instead. Reverting "
            "--max-frames back down to 16 freed enough VRAM headroom "
            "(the 60-frame run was already at ~23.2/24GB) to spend on "
            "resolution instead. Pass --resized-width 0 (with "
            "--resized-height 0) to fall back to --max-pixels instead."
        ),
    )
    parser.add_argument(
        "--resized-height",
        type=int,
        default=588,
        help="Force an exact frame height - see --resized-width.",
    )
    parser.add_argument(
        "--crop-top",
        type=float,
        default=0.0378,
        help=(
            "Fraction of frame height to crop off the top before the "
            "model sees it (default: 0.0378, i.e. 145px of a measured "
            "3840px-tall source frame - Christer's own visual crop on "
            "real Kirby footage), to cut out BlackVue's burned-in "
            "overlay (camera nickname/timestamp/speed/model string) - "
            "that text is redundant with data bv-generate already has "
            "precisely from GPS/config, and the model was reading it "
            "inconsistently (wrong dates, garbled camera model), so "
            "it's better removed than guessed at. Check the 'Cropped "
            "to' line each run and adjust if the overlay is still "
            "visible or too much real scene got cut. Pass 0 to disable."
        ),
    )
    parser.add_argument(
        "--crop-bottom",
        type=float,
        default=0.0344,
        help=(
            "Fraction of frame height to crop off the bottom (default: "
            "0.0344, i.e. 132px of a measured 3840px-tall source frame) "
            "- see --crop-top."
        ),
    )
    parser.add_argument(
        "--max-new-tokens",
        type=int,
        default=768,
        help="Cap on how long the generated answer can be (default: 768).",
    )
    parser.add_argument(
        "--repetition-penalty",
        type=float,
        default=1.15,
        help=(
            "Penalizes tokens the model has already generated (default: "
            "1.15, 1.0 = off). Added after a real run at 728x392 got "
            "stuck repeating 'Kirby' (the camera's overlay name) "
            "hundreds of times until it hit --max-new-tokens - the same "
            "greedy-decoding repetition-loop failure mode bv-generate's "
            "own faster-whisper transcription hit and was fixed for "
            "(see WORKING_CONTEXT.md), just showing up here in text "
            "generation instead of audio transcription."
        ),
    )
    parser.add_argument(
        "--no-repeat-ngram-size",
        type=int,
        default=3,
        help=(
            "Blocks the model from repeating any 3-token sequence it's "
            "already generated (default: 3, 0 = off) - a harder "
            "guarantee against loops than --repetition-penalty alone, "
            "which only discourages repeats rather than forbidding them."
        ),
    )
    parser.add_argument(
        "--do-sample",
        action=argparse.BooleanOptionalAction,
        default=False,
        help=(
            "Enable probabilistic sampling instead of greedy decoding "
            "(default: off - greedy). Nothing in this script set "
            "do_sample before, so generation ran on whatever the "
            "model's shipped generation_config.json defaults to - "
            "likely sampling. Real footage showed the same illegible "
            "crop producing a different hallucinated word on separate "
            "runs (the word after a highway sign's 'Årsta' came back "
            "as 'Häson', 'Hässon', 'Måsson', and 'Hässan' across four "
            "runs) - a textbook symptom of sampling rather than "
            "committing to one best guess. Greedy decoding won't make "
            "illegible text legible, but it should stop the model "
            "from re-rolling a different guess every run - which is "
            "the exact instability the 'trust text that reads the "
            "same across runs' heuristic exists to route around."
        ),
    )
    parser.add_argument(
        "--temperature",
        type=float,
        default=0.7,
        help="Sampling temperature, only applied when --do-sample is set (default: 0.7).",
    )
    parser.add_argument(
        "--top-p",
        type=float,
        default=0.8,
        help="Nucleus sampling cutoff, only applied when --do-sample is set (default: 0.8).",
    )
    parser.add_argument(
        "--top-k",
        type=int,
        default=20,
        help="Top-k sampling cutoff, only applied when --do-sample is set (default: 20).",
    )
    parser.add_argument(
        "--zoom-signs",
        action=argparse.BooleanOptionalAction,
        default=True,
        help=(
            "After the main pass, separately extract a few full-"
            "resolution frames from the source video (not the "
            "downscaled tensor the main pass sees), ask the model to "
            "locate any road/shop signs or plates in each, then crop "
            "and re-run OCR on just that region at native resolution. "
            "Built because more frames (--max-frames) and more "
            "per-frame resolution (--resized-width/-height) both hit a "
            "ceiling on small/distant signage on real footage - a "
            "targeted crop gives the model far more effective pixels "
            "on the one thing that matters, without paying for that "
            "resolution across the whole frame. Adds real per-video "
            "time (one grounding call per sampled frame, plus one OCR "
            "call per detected item) on top of the main pass - pass "
            "--no-zoom-signs to skip it."
        ),
    )
    parser.add_argument(
        "--zoom-frames",
        type=int,
        default=4,
        help=(
            "How many full-resolution frames to sample for sign "
            "detection (default: 4, evenly spaced across the clip). "
            "Each one costs a separate grounding inference call plus "
            "one more per detected sign/plate in it, so this is a real "
            "time multiplier, not just a quality knob."
        ),
    )
    parser.add_argument(
        "--zoom-detect-width",
        type=int,
        default=1092,
        help=(
            "Resolution the model sees the whole full-res frame at "
            "during the detection (grounding) step, before any "
            "cropping (default: 1092, matching --resized-width) - this "
            "only needs to be good enough to locate a sign, not read "
            "it. The actual read happens on the crop at native "
            "resolution afterward."
        ),
    )
    parser.add_argument(
        "--zoom-padding",
        type=float,
        default=0.15,
        help=(
            "Fractional padding added around each detected box before "
            "cropping (default: 0.15, i.e. 15%% of the box's own size "
            "on each side) - grounding boxes are often drawn a little "
            "tight, and this avoids slicing off a character right at "
            "the edge."
        ),
    )
    parser.add_argument(
        "--zoom-ocr-width",
        type=int,
        default=640,
        help=(
            "Minimum width (patch-factor-rounded, aspect-preserved) the "
            "cropped sign/plate region is upscaled to before the OCR read "
            "call (default: 640). Previously this crop had no explicit "
            "resize at all and fell back to the vision library's default "
            "image sizing - given resolution was the lever that fixed "
            "main-pass sign legibility earlier, an explicit floor here "
            "should help small crops too. This only ever pushes resolution "
            "up: a crop whose native size already exceeds this width is "
            "left at its own (larger) native size, not shrunk down to it."
        ),
    )
    parser.add_argument(
        "--zoom-debug-dir",
        type=Path,
        default=None,
        help=(
            "If set, save every native-resolution sign/plate crop the "
            "zoom pipeline attempts to OCR into this directory, plus a "
            "manifest.tsv mapping each saved file to its timestamp, "
            "label, native crop size, and what the model actually read. "
            "Lets you look at the raw source pixels yourself instead of "
            "just trusting a 'not legible' read - useful for telling "
            "apart a genuine resolution floor (crop is real detail-"
            "starved pixels) from a fixable bug (crop is empty/"
            "misaligned/wrong region)."
        ),
    )
    parser.add_argument(
        "--zoom-max-new-tokens",
        type=int,
        default=200,
        help=(
            "Cap on generated tokens for the per-crop OCR read call "
            "(default: 200) - these answers are short (one line of "
            "text, or 'not legible'), so this is deliberately much "
            "lower than --max-new-tokens to keep the extra calls cheap."
        ),
    )
    parser.add_argument(
        "--zoom-detect-max-new-tokens",
        type=int,
        default=500,
        help=(
            "Cap on generated tokens for the grounding (detection) call "
            "(default: 500). Used to share --zoom-max-new-tokens's 200 "
            "with the OCR call, but real-footage testing found busy "
            "frames with several signs/plates at once need more room: "
            "each detection is ~40-50 tokens of JSON, so 5+ detections "
            "in one frame blew through 200 tokens and got cut off "
            "mid-list, silently losing every detection in that frame "
            "even though most of them were already complete. Raised "
            "as its own flag rather than just bumping --zoom-max-new-"
            "tokens, since the OCR call doesn't need this much."
        ),
    )
    parser.add_argument(
        "--zoom-repetition-penalty",
        type=float,
        default=1.0,
        help=(
            "Separate --repetition-penalty just for the grounding/OCR "
            "calls (default: 1.0, i.e. off). The main pass's "
            "--repetition-penalty/--no-repeat-ngram-size exist to stop "
            "open-ended prose from looping (the 'Kirby' bug), but a "
            "real-footage test found they actively corrupt structured "
            "JSON output here: asked to list multiple detections, the "
            "model needs to repeat the literal key \"bbox_2d\" several "
            "times in one response, and --no-repeat-ngram-size forbids "
            "that exact repeat - so it substituted lookalike Unicode "
            "characters to dodge the constraint (\"bbox_2д\" with a "
            "Cyrillic d, \"bbox_ডd\", \"box_2D\", etc), silently "
            "breaking every detection. Structured/short outputs like "
            "this don't have the same open-ended-loop risk prose does, "
            "so there's no real downside to leaving this off here."
        ),
    )
    parser.add_argument(
        "--zoom-no-repeat-ngram-size",
        type=int,
        default=0,
        help="Separate --no-repeat-ngram-size for the grounding/OCR calls - see --zoom-repetition-penalty.",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=None,
        help=(
            "Also write the result to this text file (in addition to "
            "printing it). Only valid with exactly one input video - "
            "use --output-dir/--output-suffix for batch mode."
        ),
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=None,
        help=(
            "Batch mode: write each video's result here instead of "
            "next to its source video. Created if it doesn't exist."
        ),
    )
    parser.add_argument(
        "--output-suffix",
        default=".scene-scribe.txt",
        help=(
            "Batch mode: filename suffix for each video's result "
            "(default: .scene-scribe.txt), written as "
            "<video-stem><suffix> - mirrors bv-generate's own "
            "generated-file naming (.transcript.txt, etc)."
        ),
    )
    parser.add_argument(
        "--overwrite",
        action="store_true",
        help=(
            "Batch mode: reprocess a video even if its output file "
            "already exists. Without this, already-processed videos "
            "are skipped - so a multi-hour archive run interrupted "
            "partway through can just be re-launched with the same "
            "command instead of starting over."
        ),
    )
    parser.add_argument(
        "--cpu",
        action="store_true",
        help=(
            "Force CPU inference. Extremely slow for a 7B video model - "
            "only useful to confirm the script runs at all without a "
            "working CUDA setup."
        ),
    )
    parser.add_argument(
        "--trip-summary",
        action=argparse.BooleanOptionalAction,
        default=False,
        help=(
            "Batch mode only (2+ videos): after processing every video "
            "individually, run one extra text-only pass that reads all "
            "of their '## Description' sections in chronological order "
            "(by filename) and writes a single synthesized trip-level "
            "narrative to trip_summary.txt in --output-dir. This is a "
            "real summary, not the per-file descriptions concatenated "
            "back to back - it's explicitly asked to track how things "
            "changed over the trip (e.g. 'moderate traffic became "
            "heavier after a while') rather than describe each segment "
            "in isolation. Off by default since it's an extra model "
            "call on top of an already-long batch run."
        ),
    )
    parser.add_argument(
        "--trip-summary-max-new-tokens",
        type=int,
        default=768,
        help="Cap on generated tokens for the --trip-summary pass (default: 768).",
    )
    return parser.parse_args(argv)


def resolve_video_paths(paths: list[Path]) -> list[Path]:
    """Expand any directories in `paths` to their *.mp4 files
    (non-recursive) and flatten everything into one sorted, deduped
    list of real video files."""

    videos: list[Path] = []
    for path in paths:
        if path.is_dir():
            videos.extend(sorted(path.glob("*.mp4")))
        else:
            videos.append(path)
    # dict.fromkeys() dedupes while preserving order - simpler than a
    # set() here since Path already hashes/compares sanely.
    return list(dict.fromkeys(videos))


def crop_top_bottom(video_inputs, crop_top: float, crop_bottom: float):
    """Crop `crop_top`/`crop_bottom` fractions of height off each
    sampled frame, in place, rounding the kept region down to a
    multiple of PATCH_FACTOR so the vision encoder's patch-merging
    step doesn't choke on an odd tensor shape."""

    if not video_inputs or (crop_top <= 0 and crop_bottom <= 0):
        return video_inputs

    frames = video_inputs[0]
    _, _, height, width = frames.shape
    top_px = int(round(height * crop_top))
    bottom_px = int(round(height * crop_bottom))
    kept = height - top_px - bottom_px
    kept = (kept // PATCH_FACTOR) * PATCH_FACTOR
    if kept <= 0:
        raise ValueError(
            f"--crop-top/--crop-bottom leave nothing to feed the model "
            f"(frame height {height}px, requested top={crop_top}, "
            f"bottom={crop_bottom})"
        )
    # Re-derive top_px so the kept band is centered in what's left
    # after rounding, rather than always shaving the rounding error
    # off the bottom.
    top_px = min(top_px, height - kept)
    video_inputs[0] = frames[:, :, top_px : top_px + kept, :]
    return video_inputs


def fetch_vision_inputs(process_vision_info, messages):
    """Wrapper around qwen_vl_utils.process_vision_info() that requests
    return_video_kwargs=True when the installed qwen_vl_utils supports
    it, and falls back to the older two-value call otherwise.

    return_video_kwargs=True matters for Qwen3-VL: without it, its
    processor can't tell what sampling rate an already-extracted video
    tensor came from and warns + guesses ("no video metadata was
    provided... Defaulting to fps=24"), which on real footage
    correlated with a large, unexplained drop in the final input
    token count. But the parameter itself was added to qwen_vl_utils
    at some point and isn't guaranteed to be in whatever version is
    actually installed (confirmed on real hardware: TypeError -
    process_vision_info() got an unexpected keyword argument
    'return_video_kwargs') - so this degrades gracefully instead of
    requiring an upgrade."""

    try:
        image_inputs, video_inputs, video_kwargs = process_vision_info(
            messages, return_video_kwargs=True
        )
    except TypeError:
        image_inputs, video_inputs = process_vision_info(messages)
        video_kwargs = {}
    return image_inputs, video_inputs, video_kwargs


def sampling_kwargs(args) -> dict:
    """Shared do_sample/temperature/top_p/top_k kwargs for every
    model.generate() call. Explicit do_sample=False (the default)
    makes generation greedy/deterministic - see --do-sample's help
    text for why that matters here. temperature/top_p/top_k are only
    included when sampling is actually enabled, since transformers
    warns on every call if they're passed alongside do_sample=False."""
    kwargs = {"do_sample": args.do_sample}
    if args.do_sample:
        kwargs["temperature"] = args.temperature
        kwargs["top_p"] = args.top_p
        kwargs["top_k"] = args.top_k
    return kwargs


def run_single_image_prompt(
    image,
    prompt: str,
    model,
    processor,
    process_vision_info,
    args,
    resized_width: int | None = None,
    resized_height: int | None = None,
    max_new_tokens: int | None = None,
    repetition_penalty: float | None = None,
    no_repeat_ngram_size: int | None = None,
) -> str:
    """Feed one PIL image + a text prompt through the model and return
    the generated text. Shared by the sign-grounding call and each
    per-crop OCR call in the zoom pipeline. Kept separate from
    process_one_video()'s video path since qwen_vl_utils routes
    images (fetch_image) and video (fetch_video) through different
    code, and fetch_image happily accepts a PIL.Image object directly
    (confirmed by reading qwen_vl_utils' own source) - no need to
    round-trip through a temp file on disk."""

    image_ele = {"type": "image", "image": image}
    if resized_width and resized_height:
        image_ele["resized_width"] = resized_width
        image_ele["resized_height"] = resized_height

    messages = [
        {
            "role": "user",
            "content": [image_ele, {"type": "text", "text": prompt}],
        }
    ]
    text = processor.apply_chat_template(
        messages, tokenize=False, add_generation_prompt=True
    )
    image_inputs, video_inputs, video_kwargs = fetch_vision_inputs(
        process_vision_info, messages
    )
    inputs = processor(
        text=[text],
        images=image_inputs,
        videos=video_inputs,
        padding=True,
        return_tensors="pt",
        **video_kwargs,
    )
    inputs = inputs.to(model.device)
    generated_ids = model.generate(
        **inputs,
        max_new_tokens=max_new_tokens or args.max_new_tokens,
        repetition_penalty=(
            args.repetition_penalty if repetition_penalty is None else repetition_penalty
        ),
        no_repeat_ngram_size=(
            args.no_repeat_ngram_size if no_repeat_ngram_size is None else no_repeat_ngram_size
        ),
        **sampling_kwargs(args),
    )
    trimmed = [
        out_ids[len(in_ids):]
        for in_ids, out_ids in zip(inputs.input_ids, generated_ids)
    ]
    return processor.batch_decode(
        trimmed, skip_special_tokens=True, clean_up_tokenization_spaces=False
    )[0]


def extract_full_res_frames(video_path: Path, count: int):
    """Grab `count` evenly-spaced frames from the source video at full
    native resolution (not the downscaled tensor the main pass uses),
    for the zoom pipeline's grounding step. Returns a list of
    (timestamp_seconds, PIL.Image) tuples. Uses decord directly rather
    than qwen_vl_utils' fetch_video(), since that always downscales -
    the zoom pipeline specifically needs the untouched source pixels."""

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


def crop_overlay_from_image(image, crop_top: float, crop_bottom: float):
    """PIL-level equivalent of crop_top_bottom() for a single full-res
    frame - keeps the zoom pipeline from wasting a detection call on
    BlackVue's own burned-in overlay text, same reasoning as the main
    pass's --crop-top/--crop-bottom."""

    if crop_top <= 0 and crop_bottom <= 0:
        return image
    width, height = image.size
    top_px = int(round(height * crop_top))
    bottom_px = int(round(height * crop_bottom))
    if top_px + bottom_px >= height:
        return image
    return image.crop((0, top_px, width, height - bottom_px))


def parse_grounding_boxes(raw_text: str):
    """Parse the model's JSON bbox_2d/label response into a list of
    {"label": str, "box": (x1, y1, x2, y2)} dicts. Tolerates markdown
    code fences and stray text around the JSON (both common in real
    model output) rather than requiring an exact match, and falls back
    to an empty list - not an error - on anything unparseable, since
    most frames genuinely have nothing to detect and one bad grounding
    response shouldn't kill the whole video."""

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
        # Real-footage testing found busy frames (several signs/plates at
        # once) can blow through --zoom-detect-max-new-tokens before the
        # model reaches the closing "]", leaving a truncated, unparseable
        # list even though most of the individual objects in it are
        # complete and well-formed. Recover those instead of throwing the
        # whole frame's detections away: pull out every standalone
        # {...} chunk (non-nested, so this naturally skips a trailing
        # object that got cut off mid-way, since its brace never closes)
        # and parse each independently.
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

        # Prefer the exact key, but fall back to a fuzzy match on any key
        # containing "box" - the --zoom-repetition-penalty/--zoom-no-repeat-
        # ngram-size fix should stop the model from corrupting "bbox_2d" into
        # homoglyph variants, but this is cheap defense-in-depth in case a
        # misspelling still slips through.
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


def zoom_into_signs(video_path: Path, model, processor, process_vision_info, args) -> str:
    """The detect-then-zoom pipeline: sample a few full-res frames,
    ask the model to locate signs/plates in each, crop each detection
    (with padding) out of the native frame, and OCR just that crop.
    Returns a formatted '## Zoomed sign reads' section to append to
    the main pass's output, or '' if nothing was found/attempted.

    Built after --max-frames and --resized-width/-height alone both
    failed to fix small/distant sign legibility on real footage: the
    model kept landing a frame near a sign but still couldn't resolve
    its characters at any whole-frame resolution that fit in VRAM.
    This spends pixels on the one region that matters instead of the
    whole frame."""

    try:
        frames = extract_full_res_frames(video_path, args.zoom_frames)
    except Exception as exc:  # noqa: BLE001 - zoom is a bonus pass, not core
        print(f"  zoom: couldn't extract full-res frames ({exc}), skipping.", file=sys.stderr)
        return ""

    debug_dir = args.zoom_debug_dir
    debug_manifest = []
    crop_counter = 0
    if debug_dir is not None:
        debug_dir.mkdir(parents=True, exist_ok=True)

    lines = []
    for timestamp, frame in frames:
        frame = crop_overlay_from_image(frame, args.crop_top, args.crop_bottom)
        native_width, native_height = frame.size
        if native_width <= 0 or native_height <= 0:
            continue

        detect_width = PATCH_FACTOR * round(args.zoom_detect_width / PATCH_FACTOR)
        detect_height = PATCH_FACTOR * round(
            (args.zoom_detect_width * native_height / native_width) / PATCH_FACTOR
        )
        if detect_width <= 0 or detect_height <= 0:
            continue

        try:
            raw = run_single_image_prompt(
                frame, GROUND_PROMPT, model, processor, process_vision_info, args,
                resized_width=detect_width, resized_height=detect_height,
                max_new_tokens=args.zoom_detect_max_new_tokens,
                repetition_penalty=args.zoom_repetition_penalty,
                no_repeat_ngram_size=args.zoom_no_repeat_ngram_size,
            )
        except Exception as exc:  # noqa: BLE001
            print(f"  zoom: detection failed at t={timestamp:.1f}s ({exc}), skipping frame.", file=sys.stderr)
            continue

        boxes = parse_grounding_boxes(raw)
        print(
            f"  zoom: detect @ t={timestamp:.1f}s raw response: {raw[:600]!r}"
            f" -> parsed {len(boxes)} box(es).",
            file=sys.stderr,
        )
        if not boxes:
            continue

        # Scale from the detection resolution back to the native frame.
        scale_x = native_width / detect_width
        scale_y = native_height / detect_height

        # Qwen3-VL's bbox_2d values are normalized to a 0-1000 scale
        # (confirmed against real footage: raw y-values as high as 730
        # showed up on detect frames only ~579px tall - only possible
        # if the values aren't already in the detect frame's pixel
        # space). Qwen2.5-VL uses absolute pixel coordinates in the
        # resized detect frame directly, so this step is a no-op there.
        qwen3 = is_qwen3_vl(args.model)

        for det in boxes:
            x1, y1, x2, y2 = det["box"]
            if qwen3:
                x1, x2 = x1 / 1000 * detect_width, x2 / 1000 * detect_width
                y1, y2 = y1 / 1000 * detect_height, y2 / 1000 * detect_height
            x1, x2 = x1 * scale_x, x2 * scale_x
            y1, y2 = y1 * scale_y, y2 * scale_y
            box_w, box_h = x2 - x1, y2 - y1
            if box_w <= 0 or box_h <= 0:
                # Seen with Qwen3-VL on real footage: a box gets detected
                # and parsed fine, then silently vanishes here with no
                # explanation. Possible cause - a different grounding
                # coordinate convention (e.g. already-native-scale or
                # x/y-swapped) than Qwen2.5-VL's, which the scale_x/
                # scale_y math above assumes. Print instead of silently
                # dropping so a real run tells us which case it is.
                print(
                    f"  zoom: dropped '{det['label']}' at t={timestamp:.1f}s - "
                    f"degenerate box after scaling (raw={det['box']}, "
                    f"scale=({scale_x:.3f},{scale_y:.3f}), "
                    f"scaled=({x1:.1f},{y1:.1f},{x2:.1f},{y2:.1f})).",
                    file=sys.stderr,
                )
                continue
            pad_x, pad_y = box_w * args.zoom_padding, box_h * args.zoom_padding
            crop_box = (
                max(0, int(x1 - pad_x)),
                max(0, int(y1 - pad_y)),
                min(native_width, int(x2 + pad_x)),
                min(native_height, int(y2 + pad_y)),
            )
            if crop_box[2] <= crop_box[0] or crop_box[3] <= crop_box[1]:
                print(
                    f"  zoom: dropped '{det['label']}' at t={timestamp:.1f}s - "
                    f"degenerate crop_box={crop_box} (frame is "
                    f"{native_width}x{native_height}).",
                    file=sys.stderr,
                )
                continue
            crop = frame.crop(crop_box)
            crop_native_width, crop_native_height = crop.size
            if crop_native_width <= 0 or crop_native_height <= 0:
                print(
                    f"  zoom: dropped '{det['label']}' at t={timestamp:.1f}s - "
                    f"degenerate crop size {crop_native_width}x{crop_native_height} "
                    f"from crop_box={crop_box}.",
                    file=sys.stderr,
                )
                continue

            # Never shrink - only push small crops up to the configured
            # floor width, preserving aspect ratio. A crop that's already
            # bigger than the floor is left at its own native size.
            ocr_target_width = max(crop_native_width, args.zoom_ocr_width)
            ocr_width = PATCH_FACTOR * round(ocr_target_width / PATCH_FACTOR)
            ocr_height = PATCH_FACTOR * round(
                (ocr_target_width * crop_native_height / crop_native_width) / PATCH_FACTOR
            )
            if ocr_width <= 0 or ocr_height <= 0:
                print(
                    f"  zoom: dropped '{det['label']}' at t={timestamp:.1f}s - "
                    f"degenerate OCR resize target {ocr_width}x{ocr_height} "
                    f"from crop {crop_native_width}x{crop_native_height}.",
                    file=sys.stderr,
                )
                continue

            ocr_prompt = (
                ZOOM_OCR_PLATE_PROMPT if "plate" in det["label"].lower() else ZOOM_OCR_PROMPT
            )

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
                    print(f"  zoom: couldn't save debug crop to {debug_path} ({exc}).", file=sys.stderr)
                    debug_path = None

            try:
                read_text = run_single_image_prompt(
                    crop, ocr_prompt, model, processor, process_vision_info, args,
                    resized_width=ocr_width, resized_height=ocr_height,
                    max_new_tokens=args.zoom_max_new_tokens,
                    repetition_penalty=args.zoom_repetition_penalty,
                    no_repeat_ngram_size=args.zoom_no_repeat_ngram_size,
                )
            except Exception as exc:  # noqa: BLE001
                print(f"  zoom: read failed for '{det['label']}' at t={timestamp:.1f}s ({exc}).", file=sys.stderr)
                if debug_path is not None:
                    debug_manifest.append(
                        f"{debug_path.name}\t{timestamp:.1f}\t{det['label']}\t"
                        f"{crop_native_width}x{crop_native_height}\tread failed: {exc}"
                    )
                continue

            lines.append(f"- [t={timestamp:.1f}s] {det['label']}: {read_text.strip()}")
            if debug_path is not None:
                debug_manifest.append(
                    f"{debug_path.name}\t{timestamp:.1f}\t{det['label']}\t"
                    f"{crop_native_width}x{crop_native_height}\t{read_text.strip()}"
                )

    if debug_dir is not None and debug_manifest:
        manifest_path = debug_dir / "manifest.tsv"
        header = "filename\ttimestamp_s\tlabel\tnative_crop_size\tocr_read"
        manifest_path.write_text(header + "\n" + "\n".join(debug_manifest) + "\n", encoding="utf-8")
        print(
            f"  zoom: saved {len(debug_manifest)} debug crop(s) + manifest to {debug_dir}",
            file=sys.stderr,
        )

    if not lines:
        return ""
    return "\n\n## Zoomed sign reads\n" + "\n".join(lines)


def process_one_video(video_path: Path, model, processor, process_vision_info, args) -> str:
    """Run one video through the model and return the generated text.
    Shared by both single-video and batch mode so there's exactly one
    place that builds the prompt/video element and calls generate()."""

    prompt = build_prompt(args.task)
    video_ele = {
        "type": "video",
        "video": str(video_path.resolve()),
        "fps": args.fps,
        "max_frames": args.max_frames,
    }
    if args.resized_width and args.resized_height:
        # Bypasses the max_pixels computation in fetch_video() entirely -
        # see the --resized-width help text for why this exists. This is
        # the default path (--resized-width/--resized-height default to
        # 728x392, not 0) - pass --resized-width 0 --resized-height 0 to
        # fall back to --max-pixels instead.
        video_ele["resized_width"] = args.resized_width
        video_ele["resized_height"] = args.resized_height
    else:
        video_ele["max_pixels"] = args.max_pixels

    messages = [
        {
            "role": "user",
            "content": [
                video_ele,
                {"type": "text", "text": prompt},
            ],
        }
    ]

    text = processor.apply_chat_template(
        messages, tokenize=False, add_generation_prompt=True
    )
    image_inputs, video_inputs, video_kwargs = fetch_vision_inputs(
        process_vision_info, messages
    )
    if video_inputs:
        # Ground-truth on what actually got fed to the model, rather
        # than trusting --max-pixels/--max-frames took effect the way
        # the flags imply - qwen_vl_utils has its own internal budget
        # logic (a total-token-budget-per-video cap layered on top of
        # any per-frame max_pixels you pass) that can silently make a
        # requested value a no-op. Print the real tensor shape every
        # run so tuning is based on what happened, not what was asked.
        frames, channels, height, width = video_inputs[0].shape
        print(
            f"Actual video tensor fed to model: {frames} frames @ "
            f"{width}x{height} ({width * height} pixels/frame).",
            file=sys.stderr,
        )
        video_inputs = crop_top_bottom(video_inputs, args.crop_top, args.crop_bottom)
        if args.crop_top > 0 or args.crop_bottom > 0:
            _, _, cropped_height, cropped_width = video_inputs[0].shape
            print(
                f"Cropped to {cropped_width}x{cropped_height} "
                f"(top={args.crop_top}, bottom={args.crop_bottom}) - "
                f"check this still covers the real scene and fully "
                f"excludes the overlay text.",
                file=sys.stderr,
            )
    inputs = processor(
        text=[text],
        images=image_inputs,
        videos=video_inputs,
        padding=True,
        return_tensors="pt",
        **video_kwargs,
    )
    inputs = inputs.to(model.device)
    print(
        f"Input sequence: {inputs.input_ids.shape[1]} tokens "
        f"(frames/resolution mostly drive this - compare this number "
        f"across runs when tuning --max-frames/--max-pixels).",
        file=sys.stderr,
    )

    print("Generating...", file=sys.stderr)
    gen_start = time.monotonic()
    generated_ids = model.generate(
        **inputs,
        max_new_tokens=args.max_new_tokens,
        repetition_penalty=args.repetition_penalty,
        no_repeat_ngram_size=args.no_repeat_ngram_size,
        **sampling_kwargs(args),
    )
    generated_ids_trimmed = [
        out_ids[len(in_ids) :]
        for in_ids, out_ids in zip(inputs.input_ids, generated_ids)
    ]
    output_text = processor.batch_decode(
        generated_ids_trimmed,
        skip_special_tokens=True,
        clean_up_tokenization_spaces=False,
    )[0]
    print(f"Generated in {time.monotonic() - gen_start:.1f}s.", file=sys.stderr)

    if args.zoom_signs:
        print("Zooming into detected signs/plates...", file=sys.stderr)
        zoom_start = time.monotonic()
        zoom_section = zoom_into_signs(video_path, model, processor, process_vision_info, args)
        print(f"Zoom pass done in {time.monotonic() - zoom_start:.1f}s.", file=sys.stderr)
        output_text += zoom_section

    return output_text


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)

    for path in args.paths:
        if not path.exists():
            print(f"error: path not found: {path}", file=sys.stderr)
            return 1

    videos = resolve_video_paths(args.paths)
    if not videos:
        print("error: no .mp4 files found in the given path(s)", file=sys.stderr)
        return 1

    batch_mode = len(videos) > 1
    if args.output is not None and batch_mode:
        print(
            "error: --output only works with exactly one input video - "
            "use --output-dir/--output-suffix for multiple videos.",
            file=sys.stderr,
        )
        return 1

    # Imported here, not at module level, so `--help` works even before
    # torch/transformers/qwen_vl_utils are installed - same reasoning
    # bv-generate's own speech.py uses for its heavy, optional imports.
    try:
        import torch
        from qwen_vl_utils import process_vision_info
        from transformers import AutoProcessor
    except ImportError as exc:
        print(
            "error: missing dependency - see the setup instructions "
            f"in README.md ({exc})",
            file=sys.stderr,
        )
        return 1

    set_patch_factor_for_model(args.model)
    qwen3 = is_qwen3_vl(args.model)
    if qwen3:
        # Qwen3-VL is a separate model class from Qwen2.5-VL, only
        # available in transformers>=4.57.0 (contributed Sept 2025).
        # The rest of this script's vision loading still goes through
        # qwen_vl_utils.process_vision_info() the same way it does for
        # Qwen2.5-VL - that function just fetches/resizes images and
        # videos into tensors and doesn't know or care which model
        # consumes them, so the crop/resize machinery (--crop-top/
        # -bottom, --resized-width/-height, the zoom pipeline's
        # explicit per-crop resize) should carry over unchanged. This
        # is the untested part of the port though: Qwen3-VL's own HF
        # usage example uses a newer one-step
        # processor.apply_chat_template(tokenize=True, return_dict=True,
        # return_tensors="pt") pattern instead, which may mean the
        # processor expects different keys than qwen_vl_utils produces.
        # If generation fails or produces garbage on real footage, that
        # mismatch is the first thing to check.
        try:
            from transformers import Qwen3VLForConditionalGeneration as ModelClass
        except ImportError as exc:
            print(
                "error: this transformers install doesn't have "
                f"Qwen3VLForConditionalGeneration ({exc}). Qwen3-VL "
                "needs transformers>=4.57.0 - try `pip install -U "
                "transformers` and retry.",
                file=sys.stderr,
            )
            return 1
    else:
        from transformers import Qwen2_5_VLForConditionalGeneration as ModelClass

    device_map = "cpu" if args.cpu else "auto"
    if not args.cpu and not torch.cuda.is_available():
        print(
            "warning: torch.cuda.is_available() is False - falling back "
            "to CPU, which will be very slow for a video model. If you "
            "have an RTX 50-series (Blackwell) GPU, this usually means "
            "your PyTorch build predates sm_120 support - see README.md.",
            file=sys.stderr,
        )
        device_map = "cpu"

    print(
        f"Loading {args.model} ({device_map}, "
        f"{'Qwen3-VL' if qwen3 else 'Qwen2.5-VL'} class, "
        f"patch_factor={PATCH_FACTOR})...",
        file=sys.stderr,
    )
    load_start = time.monotonic()
    model = ModelClass.from_pretrained(
        args.model,
        torch_dtype="auto",
        device_map=device_map,
    )
    processor = AutoProcessor.from_pretrained(args.model)
    print(
        f"Model loaded in {time.monotonic() - load_start:.1f}s "
        f"({'reused for all ' + str(len(videos)) + ' videos below' if batch_mode else 'single video'}).",
        file=sys.stderr,
    )

    if args.output_dir is not None:
        args.output_dir.mkdir(parents=True, exist_ok=True)

    def output_path_for(video_path: Path) -> Path:
        directory = args.output_dir if args.output_dir is not None else video_path.parent
        return directory / (video_path.stem + args.output_suffix)

    batch_start = time.monotonic()
    processed = 0
    skipped = 0
    failed = 0
    trip_segments: list[tuple[str, str]] = []
    for i, video_path in enumerate(videos, start=1):
        prefix = f"[{i}/{len(videos)}] " if batch_mode else ""
        target = args.output if args.output is not None else output_path_for(video_path)

        if batch_mode and not args.overwrite and target.exists():
            print(f"{prefix}{video_path.name}: already done, skipping (--overwrite to redo)", file=sys.stderr)
            skipped += 1
            if args.trip_summary:
                # Still need this video's description for the trip
                # summary even though we're not regenerating it -
                # read it back off disk rather than skipping it, or a
                # --overwrite-free re-run would silently drop earlier
                # videos out of the trip narrative.
                try:
                    trip_segments.append(
                        (video_path.stem, extract_description_section(target.read_text(encoding="utf-8")))
                    )
                except OSError as exc:
                    print(
                        f"  trip-summary: couldn't read {target} for an already-done video ({exc}) - "
                        "it'll be missing from the trip summary.",
                        file=sys.stderr,
                    )
            continue

        print(f"{prefix}{video_path.name}", file=sys.stderr)
        try:
            output_text = process_one_video(
                video_path, model, processor, process_vision_info, args
            )
        except torch.cuda.OutOfMemoryError as exc:
            # Seen at --fps 0.333/--max-frames 60: a single video already
            # sits around 23.2/24GB on the 5090 laptop, so a longer-than-
            # usual clip in a big batch can tip over. Clear the cache so
            # the failure doesn't poison every video after it, then move
            # on rather than aborting an hours-long run over one clip.
            torch.cuda.empty_cache()
            print(
                f"{prefix}{video_path.name}: FAILED - out of VRAM "
                f"({exc}). Try a lower --max-frames/--fps or "
                f"--resized-width/--resized-height if this keeps "
                f"happening.",
                file=sys.stderr,
            )
            failed += 1
            continue
        except Exception as exc:  # noqa: BLE001 - one bad file shouldn't kill an hours-long batch
            print(f"{prefix}{video_path.name}: FAILED - {exc}", file=sys.stderr)
            failed += 1
            continue
        finally:
            if not args.cpu:
                # Frame tensors get large enough at this density that
                # letting them pile up across an hours-long batch is a
                # real risk, not just tidiness - see the OOM handling
                # above, measured on real footage at ~97% VRAM used for
                # a single video.
                torch.cuda.empty_cache()

        if batch_mode:
            target.write_text(output_text, encoding="utf-8")
            print(f"{prefix}wrote {target}\n", file=sys.stderr)
        else:
            print()
            print(output_text)
            if args.output is not None:
                args.output.write_text(output_text, encoding="utf-8")
                print(f"\nAlso wrote: {args.output}", file=sys.stderr)
        processed += 1
        if args.trip_summary:
            trip_segments.append((video_path.stem, extract_description_section(output_text)))

    if batch_mode:
        elapsed = time.monotonic() - batch_start
        print(
            f"\nDone: {processed} processed, {skipped} skipped, "
            f"{failed} failed, in {elapsed / 60:.1f} min "
            f"({elapsed / max(processed, 1):.1f}s/video average, "
            f"model loaded once).",
            file=sys.stderr,
        )

    if args.trip_summary:
        if not batch_mode:
            print("trip-summary: needs 2+ videos, skipping (only one was given).", file=sys.stderr)
        elif not trip_segments:
            print("trip-summary: no video descriptions available, skipping.", file=sys.stderr)
        else:
            print(f"\nSummarizing trip across {len(trip_segments)} recording(s)...", file=sys.stderr)
            summary_start = time.monotonic()
            try:
                trip_summary_text = summarize_trip(
                    trip_segments, model, processor, process_vision_info, args
                )
            except Exception as exc:  # noqa: BLE001 - a synthesis failure shouldn't erase the per-file work already done
                print(f"trip-summary: FAILED - {exc}", file=sys.stderr)
            else:
                summary_dir = args.output_dir if args.output_dir is not None else videos[0].parent
                summary_path = summary_dir / "trip_summary.txt"
                summary_path.write_text(trip_summary_text, encoding="utf-8")
                print(
                    f"trip-summary: done in {time.monotonic() - summary_start:.1f}s, "
                    f"wrote {summary_path}",
                    file=sys.stderr,
                )

    return 1 if failed and not processed else 0


if __name__ == "__main__":
    raise SystemExit(main())
