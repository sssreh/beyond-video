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
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass
from dataclasses import field
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING
from typing import Sequence

from ..archive.photo import is_photo_path
from .media import MediaToolError, probe as probe_video

if TYPE_CHECKING:
    # Deferred (see _build_adaptive_content()'s own comment below) -
    # adaptive_sampling.py itself imports telemetry.gps_reader/
    # gsensor_reader, and telemetry.gps_reader imports back into
    # generate.media, which - if pulled in at this module's own
    # top level - forces Python to finish loading generate/__init__.py
    # before it's actually finished loading *this* file, a genuine
    # circular import (confirmed by direct testing: `import
    # blackvue.telemetry.gps_reader` before `import blackvue.generate`,
    # or `import blackvue.adapters.base` at all, both raised "cannot
    # import name 'GpsFix' from partially initialized module" here).
    # generate/stats.py hit this exact same trap for the same reason -
    # see its own module docstring/WORKING_CONTEXT.md entry - and this
    # follows its established fix: annotation-only imports (safe under
    # `from __future__ import annotations`, which makes every
    # annotation a lazy string) under TYPE_CHECKING, and the modules
    # that are actually *called* deferred into the function body that
    # needs them.
    from ..telemetry.gps_reader import GpsFix
    from ..telemetry.gsensor_reader import GSensorSample

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

# 2026-08-26 (task #1260 follow-up 4): appended (not baked into
# DESCRIBE_PROMPT itself) to the plain non-adaptive video path's prompt
# only - see the "else:" branch in describe_scene() below. Root cause
# of a real regression Christer reported right after task #1260's fix
# actually started working: DESCRIBE_PROMPT's own pacing guidance
# ("roughly one bullet every 5-15 seconds ... less often during a long
# uneventful stretch") was written before this project's video calls
# ever reliably conveyed real clip duration to the model at all (see
# task #1260's whole follow-up chain) - with --frames 16 sampled across
# a real ~180s clip (~11s apart), the model now has enough correct
# temporal grounding to actually follow that pacing literally, and
# spacing-out + merging uneventful stretches per that instruction
# produced only 7 bullets on Christer's real clip ("it only describes 7
# frames, it used to talk much more before, remember my bus" - a real,
# well-known bus event on this exact calibration clip, 20220927_132155_E,
# see _write_frames_as_temp_video()'s docstring history). Before
# task #1260's fix, the model had no coherent sense of real elapsed
# time to pace bullets against at all, so it likely ignored the 5-15s
# guidance and just described close to one bullet per frame instead -
# denser, but not because it was smarter, because it had no timeline to
# reconcile the instruction against. Christer's own historical
# benchmark for this exact clip at 16 frames, non-adaptive, predating
# any of this fix chain: 107s (see _build_adaptive_message_content()'s
# docstring, "732s vs 107s"). Fix: explicitly tell the model how many
# frames it was actually given and that per-frame density should win
# over the general 5-15s pacing guidance when sampling is this sparse -
# restores the old density without needing more --frames (which is
# what actually costs VRAM - see WORKING_CONTEXT.md follow-up 16).
# Scoped to the plain video branch only: adaptive sampling already has
# its own purpose-built frame-count-aware intro text
# (_adaptive_video_intro_text()), and a still photo has no frame count
# to reference at all.
_SPARSE_SAMPLING_HINT_TEMPLATE = (
    "\n\nThis video was sampled down to about {max_frames} frames "
    "spread evenly across its whole real duration, so consecutive "
    "frames may be many seconds apart rather than a smooth sequence. "
    "When sampling is this sparse, prioritize describing each visually "
    "distinct frame over the 5-15-seconds-per-bullet pacing above - if "
    "most of the {max_frames} frames show something worth mentioning, "
    "write close to {max_frames} bullets instead of merging them into "
    "a shorter, sparser summary."
)

# 2026-08-26 (task #1260 follow-up 10): Christer, after being told the
# plain (non-adaptive) path's "[t=Xs]" bullets are the model's own
# guess rather than a code-computed value (task #1260 follow-up 23,
# which explained why "the bus" stopped landing where he remembers it
# from - see that WORKING_CONTEXT.md entry): "split 301 s with 16,
# thats not to hard" - a fair challenge. It genuinely isn't hard: the
# plain path already hands qwen_vl_utils real `fps`/`max_frames`
# values (content_ele below) and lets it pick frames internally, but
# qwen_vl_utils' own sampling (its `smart_nframes()`, read directly
# from source back in task #1260 follow-up 13) is entirely
# deterministic given those two numbers plus the clip's real duration
# - `nframes = clamp(duration_seconds * fps, min_frames=4, max_frames)`,
# rounded to the nearest even number, then that many frames evenly
# spaced from the first frame to the last. Every one of those inputs
# is knowable *before* the model call: `fps`/`max_frames` are already
# `opts` fields, and duration is one cheap `ffprobe` call away via
# `probe_video()` (already imported, already used elsewhere in this
# module and project for exactly this - no full-file decord read
# needed, unlike the adaptive path's old ~60s cost). So instead of
# letting the model guess "elapsed time" from nothing (as it always
# has on this path), compute the real approximate timestamp of each
# frame qwen_vl_utils is about to sample and tell the model those
# exact values up front - the same grounding trick
# _adaptive_video_intro_text() already does for the adaptive path,
# just computed instead of extracted. Not frame-exact (qwen_vl_utils
# samples by real frame index against its own read of the container's
# frame count/fps, which can differ very slightly from ffprobe's
# duration-based estimate for imperfectly-CFR footage) - the same
# "nearest real frame, not a promise of exactness" tolerance already
# accepted for the adaptive path's own ffmpeg-seek timestamps (task
# #1260 follow-up 8's own docstring makes the identical trade-off
# explicitly). Good enough to replace a guess with a real, sourced
# number, which is the actual problem being fixed here.
_QWEN_VL_UTILS_FPS_MIN_FRAMES = 4
_QWEN_VL_UTILS_FRAME_FACTOR = 2


def _plain_video_frame_timestamps(
    duration_seconds: float, fps: float, max_frames: int
) -> list[float]:
    """Approximate the real elapsed-clip timestamps qwen_vl_utils'
    smart_nframes()/fetch_video() will sample for the plain (non-
    adaptive) video branch, given the same fps/max_frames values this
    module already passes it. See the long comment above this
    function for why this is knowable in advance and where the
    formula comes from. Returns an empty list for a degenerate
    duration/fps/max_frames (nothing meaningful to ground)."""

    if duration_seconds <= 0 or fps <= 0 or max_frames <= 0:
        return []

    raw_nframes = duration_seconds * fps
    min_frames = _QWEN_VL_UTILS_FPS_MIN_FRAMES
    nframes = max(min(raw_nframes, max_frames), min_frames)
    nframes = round(nframes / _QWEN_VL_UTILS_FRAME_FACTOR) * _QWEN_VL_UTILS_FRAME_FACTOR
    nframes = max(nframes, _QWEN_VL_UTILS_FRAME_FACTOR)
    if nframes <= 1:
        return [0.0]

    step = duration_seconds / (nframes - 1)
    return [round(i * step, 1) for i in range(nframes)]


def _plain_video_intro_text(timestamps: list[float], duration_seconds: float) -> str:
    """Build the grounding text prepended to the plain (non-adaptive)
    video branch's prompt, telling the model the real approximate
    elapsed-clip time of each frame it's about to see - see
    _plain_video_frame_timestamps()'s own comment for why this is
    computable in advance rather than left as a guess. Deliberately
    styled like _adaptive_video_intro_text() (one flowing sentence
    with a plain comma-separated list, not dashed bullet-shaped lines)
    for the same reason that function gives: avoiding structural
    resemblance to the model's own "- [t=Xs]" output bullets, which
    that function's docstring traces to a real no_repeat_ngram_size
    token-mangling bug on the adaptive path. Untested against a real
    model from this sandbox - no GPU/qwen_vl_utils here."""

    times = ", ".join(f"{timestamp:.1f}s" for timestamp in timestamps)
    return (
        f"This clip's {len(timestamps)} frames are sampled evenly "
        f"across the real {duration_seconds:.1f}s recording, from the "
        f"very start to the very end - not an assumption, a computed "
        f"fact about how this clip was built. In order, their real "
        f"elapsed times from the start of the recording are "
        f"approximately: {times}. Use these exact given values, not "
        "your own estimate of pacing or spacing, when writing the "
        "'[t=Xs]' timestamps requested below - pick whichever listed "
        "value is closest to what a bullet is actually describing."
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

# Batched counterparts of the two prompts above - read several crops
# in one model call instead of one call per crop (task #1244: Christer
# counted ~34 separate sequential model calls processing one real
# recording, almost all of them individual sign/plate reads from the
# loop this used to be - each call carries its own fixed overhead
# regardless of how small the crop is, which added up to most of the
# recording's total processing time). {count} is filled in with the
# number of images actually attached to the call - see
# _run_batch_image_prompt(). JSON-list output rather than a numbered-
# lines format to match this module's existing tolerant-JSON-parsing
# style (see _parse_grounding_boxes()/_parse_batch_reads()).
ZOOM_OCR_BATCH_PROMPT = (
    "Each image above is a separate cropped, zoomed-in region of a "
    "dashcam frame, centered on a sign - {count} images in total, in "
    "the order shown. For each one, read the text on it exactly as "
    "shown. If a given crop still isn't legible even at this zoom "
    "level, use \"not legible\" for that one - don't guess a "
    "plausible-sounding replacement. Output a JSON list of exactly "
    "{count} strings, one per image, in the same order as the images "
    "- nothing else, no commentary outside the list."
)

ZOOM_OCR_PLATE_BATCH_PROMPT = (
    "Each image above is a separate cropped, zoomed-in region of a "
    "dashcam frame, centered on a vehicle's license plate - {count} "
    "images in total, in the order shown. Regular Swedish plates are 3 "
    "letters, a space, then 2 digits and one more character that can "
    "be either a digit or a letter. Personalized/vanity plates can "
    "differ from this - they may run longer, and the space itself can "
    "sometimes be a meaningful part of the plate rather than just a "
    "separator. Read exactly what's on each plate as shown, don't "
    "force it into the regular pattern if it doesn't fit. Return only "
    "the plate characters for each one, nothing else - no description, "
    "no commentary. If a given plate isn't legible even at this zoom "
    "level, use \"not legible\" for that one - don't guess a "
    "plausible-sounding replacement. Output a JSON list of exactly "
    "{count} strings, one per image, in the same order as the images "
    "- nothing else, no commentary outside the list."
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
    # Task #1245 follow-up 5: real-hardware confirmation that
    # no_repeat_ngram_size=3 (tuned for the normal, non-adaptive describe
    # call, which the project's real-footage tuning shows tends to write
    # a handful of longer bullets) actively corrupts output once
    # adaptive sampling asks the model to write one "[t=Xs]" bullet per
    # sampled frame - Christer's real --adaptive-context-frames 2 output
    # (~80 sampled frames after context expansion, vs. 16 without it)
    # showed the "t" in "[t=" mutating letter-by-letter as generation
    # went on - t, T, F, f, r, R, E, e, -, L, l, I, i, o, O, Q, q, w, W,
    # X, x - the no-repeat-ngram ban forcing ever-more-desperate token
    # substitutions to dodge an exact-3-gram-repeat rule once ~80 near-
    # identical bullet openings have already appeared in the sequence.
    # Same mechanism diagnosed (at a much milder, digit-spacing-only
    # severity) in _adaptive_video_intro_text()'s own docstring, before
    # context frames existed to make it this much worse.
    #
    # These two fields replace repetition_penalty/no_repeat_ngram_size
    # for the main describe call (see the generate() call in
    # describe_scene()) - mirroring zoom_repetition_penalty=1.0/
    # zoom_no_repeat_ngram_size=0 just below, which already disables
    # both for exactly this shape of problem (many short structured
    # per-item outputs in one completion, there one line per sign/plate
    # crop instead of one bullet per frame) and has been running safely
    # in production.
    #
    # 2026-08-26 (task #1258 follow-up): originally scoped to the
    # adaptive_sampling path only, on the theory that the normal non-
    # adaptive describe call's own handful-of-longer-bullets style
    # wasn't at risk. Real hardware at `--frames 32` on the plain
    # non-adaptive path (Christer: "No, i tried to get 32 frames from
    # non adaptive") produced the same bracket-formatting drift on just
    # 5 bullets - "- [ t=0s ]", "-[t=2.8s]", "-[-t=6.9s]", "- [-t=9.2s]"
    # - as the original ~80-bullet adaptive-path corruption report, just
    # milder since fewer repeats means less pressure against the
    # no-repeat-3-gram ban. So repetition_penalty=1.15/
    # no_repeat_ngram_size=3 were never actually a safe default for
    # this output shape - raising max_frames on the plain path (this
    # same follow-up chain) made it common enough to see. Applied
    # unconditionally to the main describe call now, regardless of
    # opts.adaptive_sampling.
    #
    # repetition_penalty/no_repeat_ngram_size themselves (1.15/3) are
    # untouched and still used by summarize_trip()'s own separate
    # generate() call, which wasn't observed to have this problem.
    #
    # 2026-08-26 (task #1260 follow-up 5): adaptive_repetition_penalty
    # raised from 1.0 (no penalty at all) to 1.1 after real hardware
    # showed a real cost to zeroing BOTH knobs out together. Once
    # follow-up 17's sparse-sampling hint got the plain describe call
    # writing close to max_frames bullets again (14, for --frames 16),
    # several came back as near-verbatim repeats of earlier ones -
    # "The car approaches a traffic light, which is now green, and a
    # white van is visible ahead." appeared 3 times at evenly-spaced
    # timestamps, cycling through what looked like ~4 template
    # sentences - and the same completion's "## On-screen text"
    # section degenerated into the single word "LÄN" repeated 40+
    # times. Both are the classic unconstrained-repetition failure
    # mode `no_repeat_ngram_size` exists to catch - but that's the
    # knob directly implicated in the "- [t=" bracket-corruption bug
    # above, so it stays at 0 rather than risking that regression
    # coming back. repetition_penalty is a separate, independent, soft
    # per-token-logit mechanism - never actually implicated in the
    # bracket-corruption report, just zeroed out alongside
    # no_repeat_ngram_size as part of the same blunt "disable
    # everything" fix. Restoring a modest amount of it (1.1, well
    # under the original 1.15 that was tuned for a totally different -
    # few-long-bullets - output shape) should discourage exact-phrase
    # reuse across bullets/OCR lines without the hard n-gram ban that
    # caused the original corruption. Untested against real hardware -
    # next real run on this same clip should confirm both the cycling
    # sentences and the LÄN loop are gone, and that "- [t=" formatting
    # is still clean.
    # 2026-08-26 (task #1260 follow-up 6): adaptive_no_repeat_ngram_size
    # raised from 0 to 5 after real hardware showed follow-up 5's
    # repetition_penalty=1.1 bump wasn't enough on its own. Same
    # --frames 16 clip, --scene-quantize int8 this time: the "## Description"
    # section came back clean - 7 distinct bullets, no verbatim
    # repeats, "- [t=" formatting intact - but "## On-screen text"
    # degenerated into an alternating two-phrase loop, "Förbjudet att
    # köra på gatan" / "Körselväg", repeated 25+ times each until
    # max_new_tokens cut it off mid-word ("Förbjudet att köra på g").
    # Same failure mode as follow-up 5's "LÄN" spam, just a longer
    # repeating unit this time - a soft per-token penalty alone
    # couldn't stop a determined exact-phrase loop once it started.
    # `no_repeat_ngram_size` is the knob actually built for this (a
    # hard "you may not repeat this exact N-token sequence again" rule)
    # but it's also the knob that caused the original "- [t=" bracket
    # corruption at its old value of 3 - a 3-token window is short
    # enough to span just "- [t=" itself, which legitimately recurs at
    # the start of every bullet. "Förbjudet att köra på gatan" is
    # 6 real words (more once tokenized), so a large-enough N should
    # still ban that repeat while a 3-token "- [t=" match, followed by
    # a different digit each time, mostly won't line up as an identical
    # N-token run once N is bigger than the shared prefix itself. 5 is
    # a middle ground: bigger than the 3 that caused the original
    # corruption, smaller than the ~8-10 tokens in the new repeating
    # phrase pair, chosen by reasoning through the token-count math
    # here rather than verified against a real tokenizer - real
    # hardware is what will actually confirm it. Next real run on this
    # same clip should check all three at once: no cycling Description
    # bullets, no On-screen-text word/phrase loop, and clean "- [t="
    # formatting (the thing this whole repetition-settings chain
    # started from, task #1245 follow-up 5).
    adaptive_repetition_penalty: float = 1.1
    adaptive_no_repeat_ngram_size: int = 5
    # Task #1245 follow-up 6: max_new_tokens=768 was never a real budget
    # decision - it just happens to equal 16 (the fixed highlight/frame
    # count this whole file was tuned against) times ~48 tokens/bullet.
    # Once adaptive_context_frames could multiply the sampled-frame count
    # well past 16, that coincidence stopped holding: Christer's real
    # --adaptive-context-frames 2 output (~80 sampled frames) got the
    # repetition-corruption fix from follow-up 5, but still cut off
    # mid-sentence after ~20 bullets, silently dropping the other ~75%
    # of the sampled frames it never got to (only [t=8.5s]..[t=62.5s]
    # covered out of an 80-frame span reaching to [t=178.5s]) - the
    # fixed 768-token budget ran out before the model finished, and
    # nothing detected or reported the truncation.
    #
    # Fix: when adaptive_sampling actually produced sampled frames, the
    # describe call's max_new_tokens is the larger of the fixed
    # max_new_tokens above and
    # len(sampled_frame_timestamps) * adaptive_max_new_tokens_per_frame
    # (see the generate() call in describe_scene()) - so the budget
    # scales with how much the model is actually being asked to write
    # instead of staying pinned to the 16-frame case. 64 tokens/bullet
    # is a deliberately generous estimate (real output ran closer to
    # ~38/bullet before truncating) - headroom costs a bit of generation
    # time on a GPU that already has slack, but coming in under budget
    # loses content outright with no error raised, which is worse. Not
    # exposed as a CLI flag (same reasoning as adaptive_repetition_penalty/
    # adaptive_no_repeat_ngram_size above) - untested against a real model,
    # Christer needs to reinstall and re-run to confirm this actually
    # stops the truncation.
    adaptive_max_new_tokens_per_frame: int = 64
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
    # See adaptive_sampling.py's module docstring and describe_scene()'s
    # own gps_fixes/gsensor_samples/recording_start params below. False
    # (default) keeps today's behavior completely unchanged - a single
    # "video" content element handed to qwen_vl_utils, which does its
    # own internal evenly-spaced fps/max_frames sampling, exactly as
    # this project's real-footage tuning (see this class's own 2026-08-19
    # docstring note) was calibrated against. True switches to an
    # explicit multi-image message instead, built from timestamps
    # compute_adaptive_timestamps() picks - biased toward this
    # recording's own most eventful spans (speed changes, turns,
    # g-force) rather than evenly spaced - degrading gracefully back to
    # ~evenly-spaced whenever the caller has no GPS/g-sensor telemetry
    # to offer (see that function's own docstring).
    adaptive_sampling: bool = False
    # Task #1245 follow-up: Christer's own diagnosis for why adaptive-
    # sampling descriptions got choppier once frames were stitched into
    # a real video clip - "it reads frames just before and just after
    # to get a more fully description, but now the frames are totaly
    # differen from the neighbours" - real video's temporal-merging
    # assumes visually continuous neighbors, which adaptive sampling's
    # whole point (picking the moments that matter, not evenly-spaced
    # ones) deliberately violates. For each chosen highlight timestamp,
    # also pull this many extra real frames on either side (spaced
    # adaptive_context_offset_seconds apart) so every highlight sits in
    # a short burst of genuinely continuous motion the model can anchor
    # a fuller description to, instead of one isolated snapshot -
    # see _expand_with_context_frames(). 0 disables this and reproduces
    # exactly the original one-frame-per-highlight behavior. Off by
    # default: it's untested against a real model from this sandbox,
    # and it multiplies frame/decode count - opt-in until Christer
    # confirms the quality/speed trade-off is worth it.
    #
    # DANGER, confirmed on real hardware: Christer tried 10 (up to 21
    # frames per highlight, ~336 frames total for a 16-highlight clip)
    # and hit real VRAM exhaustion - GPU shared memory ballooned, the
    # sustained "big blocks" GPU-utilization pattern seen at low values
    # broke down into spikes again, and the run ended in a driver/
    # hypervisor crash that took down the whole machine and required a
    # reboot. There is no in-code cap on this value - no clamp, no
    # warning printed before the risky call, nothing. Keep it small
    # (2-3) and time it before going higher; see bv-generate's own
    # --adaptive-context-frames help text for the same warning at the
    # CLI level.
    adaptive_context_frames: int = 0
    adaptive_context_offset_seconds: float = 0.5


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
# card. Qwen3-VL-8B-Instruct's native bf16 weights are a ~19.3GB
# footprint unquantized (matches the ~20GB NONE floor below).
# device_map="auto" *can* shard a too-large model across multiple GPUs
# when it doesn't fit on one, but that's slower PCIe-pipelined
# inference, not what quantization is for here - the real win on
# Christer's dual-RTX-3080-Ti box (12GB each, discussed alongside this
# feature) is quantizing the model down onto *one* card so it never
# needs to shard at all, and so each card could eventually host an
# independent job rather than jointly hosting one slow one.
#
# INT8_MIN_GB was originally 10.0, on the assumption that bitsandbytes
# int8 roughly halves the unquantized footprint. Christer's real
# measurement running --scene-quantize int8 for real: it used almost
# 14GB, not ~9-10GB - int8 (LLM.int8()) doesn't quantize everything
# (the vision tower/embeddings stay fp16/bf16) and keeps fp16
# mixed-precision outliers, plus real inference adds frame-tensor and
# KV-cache overhead on top of the static weight footprint, so the
# naive "halves it" estimate was too optimistic. Raised to 16.0 (14GB
# measured + a safety margin) so a 12GB card - Christer's own
# dual-RTX-3080-Ti box, the case this auto-selection exists for -
# falls through to int4 instead of picking an int8 that wouldn't
# actually fit on one card. int4's own real-world footprint hasn't
# been measured yet; revisit this floor too if it turns out optimistic
# the same way.
_SCENE_QUANTIZE_NONE_MIN_GB = 20.0
_SCENE_QUANTIZE_INT8_MIN_GB = 16.0

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
        import os
        import torch
        import torchvision  # noqa: F401 - qwen_vl_utils needs this importable

        # 2026-08-26 (task #1260 follow-up): qwen-vl-utils>=0.0.14
        # (bumped from 0.0.8 to fix the --frames-ignored bug) picks its
        # video reader backend by priority - torchcodec first if the
        # package is merely importable, then decord, then torchvision -
        # and its own fetch_video() falls back to torchvision
        # unconditionally on any backend error, never to decord,
        # regardless of what's installed (see qwen_vl_utils/
        # vision_process.py's get_video_reader_backend()/fetch_video()).
        # Real hardware hit exactly that: torchcodec is present but its
        # native libtorchcodec DLL fails to load on Christer's Windows/
        # conda setup (FFmpeg build mismatch), so it silently fell back
        # to torchvision - whose installed version (paired with a very
        # new torch/cu128 build) no longer has torchvision.io.read_video
        # at all, a hard crash either way. This project's own `scene`
        # extra pins qwen-vl-utils[decord] specifically so a working
        # decord install is always present and was the sole, working
        # backend for every real-hardware run before the 0.0.14 bump -
        # forcing it via this env var (read once, at qwen_vl_utils' own
        # import time, so it must be set before the import below)
        # restores that known-good path and skips torchcodec/
        # torchvision entirely. setdefault(), not direct assignment, so
        # an operator who explicitly sets FORCE_QWENVL_VIDEO_READER
        # themselves (e.g. to test torchcodec once its Windows DLL issue
        # is sorted out) isn't overridden.
        os.environ.setdefault("FORCE_QWENVL_VIDEO_READER", "decord")

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


def _extract_raw_section(output_text: str, header_keyword: str) -> str:
    """Pull just the section under a '##' heading containing
    `header_keyword` (case-insensitive) out of a per-recording result,
    verbatim - dropping every other section, but not otherwise touching
    the content. Shared by _extract_raw_description_section() (keyword
    "description") and extract_sampled_frame_timestamps() (keyword
    "sampled frames") below - both need the same kind of section-slice-
    by-heading, just for different headings."""

    lines = output_text.splitlines()
    section = []
    in_section = False
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("#") and header_keyword in stripped.lower():
            in_section = True
            continue
        if stripped.startswith("#") and in_section:
            break
        if in_section:
            section.append(line)
    return "\n".join(section).strip()


def _extract_raw_description_section(output_text: str) -> str:
    """Pull just the '## Description' section out of a per-recording
    result, verbatim - dropping the on-screen-text/zoomed-sign-reads
    sections, but not otherwise touching the content. Shared by
    extract_description_section() (which cleans this up for display/
    summarization) and extract_description_events() (which parses the
    real per-event timestamps back out of it) below - both need the
    exact same raw slice, just processed differently."""

    return _extract_raw_section(output_text, "description")


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
    still isn't a number even after the fallback below, so one
    genuinely malformed bullet is skipped rather than crashing the
    whole parse or being silently treated as zero."""

    token = re.sub(r"\s+", "", raw_seconds)
    if token.lower().endswith("s"):
        token = token[:-1]
    try:
        return float(token)
    except ValueError:
        pass
    # 2026-08-26 (task #1260 follow-up 8): real hardware produced
    # bullets like '- [t="35s"]', "- [t='55s']", even "- [t=\"#155s\"]"
    # - the model progressively wrapped each successive bullet's
    # timestamp in extra stray quote characters as generation went on
    # (a classic autoregressive-drift pattern: a repeated template
    # mutates a little more each time it's copied), with a `#` showing
    # up by the last bullet. The strict strip-then-float attempt above
    # can't handle that (the token no longer ends in a plain digit or
    # "s"), so every one of those bullets was silently dropped from
    # the timeline - not corrupted content, just an invisible gap in
    # description.srt/TTS sync. Since the actual number is still in
    # there untouched, pulling out the first number-shaped substring
    # and using that is safe and general - same tolerance philosophy
    # as the whitespace-stripping above, just one layer further out.
    match = re.search(r"-?\d+(?:\.\d+)?", token)
    if match is None:
        return None
    try:
        return float(match.group())
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


def extract_sampled_frame_timestamps(output_text: str) -> list[float]:
    """Real per-frame timestamps describe_scene() actually sampled,
    parsed back out of the '## Sampled frames' section its
    adaptive_sampling=True path appends to its own output (see that
    section's own comment right before it's appended, further below in
    this module). [] if this scene.txt has no such section - either
    adaptive_sampling wasn't used for this recording, or it's an older
    scene.txt written before this feature existed - which callers
    should treat exactly like extract_description_events() returning []:
    "nothing real to sync against, fall back to your own even-spacing
    approximation" (see web/app.py's archive_recording_frames /
    archive_recording_frame_image routes, which prefer these real
    timestamps over _nominal_frame_timestamps()'s guess whenever
    they're available).

    Reuses _BULLET_START_RE/_parse_timed_events() - the "## Sampled
    frames" section is deliberately written in the exact same
    '- [t=X.Ys] text' bullet shape the '## Description' section uses,
    specifically so no new parsing logic was needed here. Each bullet's
    own text ("sampled frame") is discarded; only the timestamps
    matter to a caller of this function."""

    raw = _extract_raw_section(output_text, "sampled frames")
    events = _parse_timed_events(raw)
    return [event.timestamp_seconds for event in events]


def _fetch_vision_inputs(process_vision_info, messages, *, is_qwen3: bool = False):
    """process_vision_info() wrapper that requests
    return_video_kwargs=True when supported (needed for Qwen3-VL to
    know the sampling rate of an already-extracted video tensor), and
    degrades gracefully on older qwen_vl_utils that don't accept it.

    2026-08-26 (task #1260 follow-up 3): confirmed via QwenLM/Qwen3-VL's
    own README that Qwen3-VL needs a *different* call shape than
    Qwen2.5-VL, not just the same return_video_kwargs=True this
    function already requested. Real hardware crashed with:
    `Field 'fps' with value [0.355...] doesn't match any type in
    (int, float, NoneType)` - process_vision_info()'s default video_
    kwargs shape (`{'do_sample_frames': False, 'fps': [sample_fps]}`,
    explicitly commented "BC for qwen2.5vl" in qwen_vl_utils' own
    source) flattens fps into a list for Qwen2.5-VL's older video
    processor, which Qwen3-VL's newer, stricter one rejects outright.
    The README's own Qwen3-VL example instead passes
    return_video_metadata=True (returning each video as a
    (tensor, metadata) tuple instead) and image_patch_size=16 (vs
    Qwen2.5-VL's 14 - matches _PATCH_FACTOR_3_VL/_PATCH_FACTOR_2_5_VL
    above), then unpacks the tuples and feeds metadata to the
    processor via its own video_metadata= kwarg rather than folding it
    into video_kwargs. video_inputs is unpacked back down to a plain
    list of tensors here (not left as (tensor, metadata) pairs) so
    every existing call site - and _crop_top_bottom(), which indexes
    video_inputs[0] expecting a plain (T, C, H, W) tensor - keeps
    working unchanged; video_metadata travels back separately.

    2026-08-26 (task #1258 follow-up): real hardware showed
    video_kwargs coming back as `{}` even on a real 180s clip at
    --frames 32/64, matching the "Asked to sample fps frames per
    second but no video metadata was provided ... Defaulting to
    fps=24" warning - i.e. the except branch below IS being hit. But
    this originally caught TypeError blindly with no logging, so there
    was no way to tell *why* - "return_video_kwargs isn't a supported
    kwarg on this qwen-vl-utils version" (the assumed reason) is one
    possible cause, but any other TypeError raised anywhere inside
    process_vision_info() (a torchvision/decord API mismatch, some
    other argument issue) would silently produce the exact same
    symptom. Logging the real exception here so the next real run
    shows which one it actually is, instead of continuing to guess."""

    kwargs = {"return_video_kwargs": True}
    if is_qwen3:
        kwargs["return_video_metadata"] = True
        kwargs["image_patch_size"] = 16

    try:
        image_inputs, video_inputs, video_kwargs = process_vision_info(
            messages, **kwargs
        )
    except TypeError as exc:
        print(
            f"bv-generate: process_vision_info({', '.join(kwargs)}) "
            f"unsupported/failed ({exc}), falling back to no video "
            "sampling metadata",
            file=sys.stderr,
        )
        image_inputs, video_inputs = process_vision_info(messages)
        return image_inputs, video_inputs, {}, None

    video_metadata = None
    if is_qwen3 and video_inputs is not None:
        videos, metadatas = zip(*video_inputs)
        video_inputs = list(videos)
        video_metadata = list(metadatas)

    return image_inputs, video_inputs, video_kwargs, video_metadata


def _processor_call_kwargs(video_kwargs: dict, video_metadata) -> dict:
    """Merges _fetch_vision_inputs()'s video_kwargs with the two extra
    kwargs every processor() call site below now needs, per QwenLM/
    Qwen3-VL's own README example (see _fetch_vision_inputs()'s
    docstring):

    - do_resize=False unconditionally - qwen_vl_utils' process_vision_
      info() already resizes images/videos itself (smart_resize()), so
      leaving the processor's own default do_resize=True on would
      silently resize a second time.
    - video_metadata=... only when not None (Qwen3-VL video calls) -
      Qwen2.5-VL's processor was never confirmed to accept a
      video_metadata kwarg at all (the README's Qwen2.5-VL example
      never passes one), and this project's real Qwen3-VL fps crash
      just showed HF processors can validate kwargs strictly enough to
      reject an unexpected shape outright - safer to omit it entirely
      for the non-Qwen3/no-video cases than risk the same failure mode
      for a kwarg with no evidence it's even accepted there."""

    kwargs = dict(video_kwargs)
    kwargs["do_resize"] = False
    if video_metadata is not None:
        kwargs["video_metadata"] = video_metadata
    return kwargs


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
    image_inputs, video_inputs, video_kwargs, video_metadata = _fetch_vision_inputs(
        loaded.process_vision_info, messages, is_qwen3=loaded.is_qwen3
    )
    inputs = loaded.processor(
        text=[text], images=image_inputs, videos=video_inputs,
        padding=True, return_tensors="pt",
        **_processor_call_kwargs(video_kwargs, video_metadata),
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


def _run_batch_image_prompt(
    images: list[tuple[int | None, int | None, "PILImage.Image"]],
    prompt_template: str,
    loaded: _LoadedSceneModel,
    opts: SceneOptions,
    *,
    max_new_tokens: int | None = None,
    repetition_penalty: float | None = None,
    no_repeat_ngram_size: int | None = None,
    force_sample: bool = False,
) -> list[str]:
    """Read several crops in ONE model call instead of one call per
    crop - each element of `images` is (resized_width, resized_height,
    image), same shape _run_single_image_prompt() takes for a single
    image. `prompt_template` must accept a {count} placeholder (see
    ZOOM_OCR_BATCH_PROMPT/ZOOM_OCR_PLATE_BATCH_PROMPT above). Returns
    one string per image, same order and same length as `images`
    regardless of how the model actually answered - see
    _parse_batch_reads() for the tolerant-parsing contract that makes
    that guarantee possible.

    Exists to fix a real measured cost (task #1244): _zoom_into_signs()
    used to call _run_single_image_prompt() once per detected sign/
    plate crop, plus a second call per plate for the confidence check
    - Christer counted ~34 separate sequential model calls processing
    one real recording, watching GPU-usage spikes in Task Manager with
    long idle gaps between them (each call pays its own fixed
    overhead - rebuilding the chat template, re-running the vision
    tower, an autoregressive decode from scratch - no matter how small
    the crop is). Multiple images in one message is already proven
    safe in this module: _build_adaptive_message_content() already
    sends several independently-sized image elements in a single
    call. Grouping every same-type crop from one frame into one call
    here cuts _zoom_into_signs()'s OCR cost from one call per crop
    down to at most one call per frame per crop-type (signs, plates,
    plus one more for the plate confidence re-read) - the per-frame
    sign/plate *detection* call above this in _zoom_into_signs() is
    unchanged, since each frame is a genuinely different image and
    can't be merged with the others."""

    if not images:
        return []

    content: list[dict] = []
    for resized_width, resized_height, image in images:
        image_ele = {"type": "image", "image": image}
        if resized_width and resized_height:
            image_ele["resized_width"] = resized_width
            image_ele["resized_height"] = resized_height
        content.append(image_ele)
    content.append({"type": "text", "text": prompt_template.format(count=len(images))})

    messages = [{"role": "user", "content": content}]
    text = loaded.processor.apply_chat_template(
        messages, tokenize=False, add_generation_prompt=True
    )
    image_inputs, video_inputs, video_kwargs, video_metadata = _fetch_vision_inputs(
        loaded.process_vision_info, messages, is_qwen3=loaded.is_qwen3
    )
    inputs = loaded.processor(
        text=[text], images=image_inputs, videos=video_inputs,
        padding=True, return_tensors="pt",
        **_processor_call_kwargs(video_kwargs, video_metadata),
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
    raw = loaded.processor.batch_decode(
        trimmed, skip_special_tokens=True, clean_up_tokenization_spaces=False
    )[0]
    return _parse_batch_reads(raw, len(images))


def _parse_batch_reads(raw_text: str, expected_count: int) -> list[str]:
    """Parse a batched OCR response (a JSON list of `expected_count`
    strings, one per image, see ZOOM_OCR_BATCH_PROMPT/
    ZOOM_OCR_PLATE_BATCH_PROMPT) - tolerant of markdown fences and
    stray text around the list, same style as _parse_grounding_boxes()
    just below. Always returns exactly `expected_count` strings:
    padded with an '[unread - ...]' placeholder if the model returned
    too few, truncated if it returned too many, and every entry comes
    back as that same placeholder if the response couldn't be parsed
    as a list at all. Batching trades a little robustness for many
    fewer model calls (see _run_batch_image_prompt()'s own docstring),
    so this stays conservative about silently mismatching an OCR read
    to the wrong crop - a placeholder that's visibly a failure is
    better than a plausible-looking read attached to the wrong sign."""

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
        return ["[unread - batch response wasn't parseable]"] * expected_count

    reads = [str(item).strip() if item is not None else "" for item in items]
    if len(reads) < expected_count:
        reads = reads + ["[unread - batch response was short]"] * (expected_count - len(reads))
    elif len(reads) > expected_count:
        reads = reads[:expected_count]
    return reads


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


def _extract_frame_at_timestamp(video_path: Path, timestamp: float):
    """Grab the single frame nearest `timestamp` (seconds from the
    start of the clip) from `video_path` via a direct ffmpeg seek-and-
    decode - `-ss` given BEFORE `-i` asks ffmpeg to seek at the
    container level to (approximately) the target timestamp before
    decoding starts, rather than decoding from the very beginning of
    the file, so the cost of pulling out one frame is proportional to
    how far into the file the timestamp is, not the whole file's
    length. Returns a PIL.Image, or None if ffmpeg couldn't produce
    one (a timestamp past the real end of a file whose probed
    duration ran slightly long, most likely) - callers should skip
    those, not treat one bad timestamp as a hard failure of the whole
    batch.

    Task #1245 follow-up 8: replaces a decord.VideoReader() +
    get_batch() approach (see _extract_frames_at_timestamps()'s
    history in this same function, and _video_duration_seconds() right
    below - both now removed) that opened and indexed the WHOLE source
    file up front. That already went through one real speed fix (task
    #1241: a single shared decord open instead of two, and one batched
    get_batch() call instead of 16 individual vr[idx] reads) - but
    real-hardware timing from Christer after all of that still showed
    ~60s just to open the file and build decord's index, before any
    actual frame reading happened, seemingly independent of how many
    timestamps were being sampled or how the --adaptive-context-frames
    count changed the run. decord needs a full random-access frame
    index to support get_batch(indices) at all, which for a multi-
    minute dashcam file (BlackVue MP4s in particular have already
    needed special-cased handling elsewhere in this project for
    duration probing - see media.py's mp4_box_reader.py) read over a
    network share means demuxing/reading the whole file just to answer
    a handful of "give me the frame nearest t=X" requests. A direct
    ffmpeg seek per timestamp instead only reads the portion of the
    file near each target, so total cost scales with how many
    timestamps are requested and how far apart they are, not with the
    source file's full length - which should matter a lot for a
    typically-short highlight/context-frame list against a multi-
    minute recording.

    Trades a small amount of frame-exactness for that: `-ss` before
    `-i` is a fast keyframe-adjacent seek, not a guaranteed frame-
    perfect one, landing on the nearest keyframe plus a short forward
    decode rather than necessarily the single closest frame. Acceptable
    here - adaptive sampling only ever wanted "the frame nearest this
    timestamp" in the first place, never an exact one; decord's own
    `int(round(timestamp * fps))` frame-index math was already only an
    approximation for anything but perfectly constant frame rate.

    Untested against a real network-mounted BlackVue file - Christer
    needs to reinstall and re-run to confirm this actually cuts the
    ~60s down, and that seek accuracy holds up on his real footage."""

    from io import BytesIO

    from PIL import Image as PILImage

    try:
        result = subprocess.run(
            [
                "ffmpeg", "-y", "-v", "error",
                "-ss", f"{max(0.0, timestamp):.3f}",
                "-i", str(video_path.resolve()),
                "-frames:v", "1",
                "-f", "image2pipe",
                "-vcodec", "png",
                "-",
            ],
            capture_output=True,
        )
    except FileNotFoundError as exc:
        raise MediaToolError("ffmpeg not found on PATH") from exc

    if result.returncode != 0 or not result.stdout:
        return None

    return PILImage.open(BytesIO(result.stdout)).convert("RGB")


def _extract_frames_at_timestamps(video_path: Path, timestamps: list[float]):
    """Grab the frame nearest each of `timestamps` (seconds from the
    start of the clip) from `video_path` - the adaptive-sampling
    counterpart to _extract_full_res_frames()'s evenly-spaced
    extraction above. Returns a list of (timestamp_seconds, PIL.Image)
    tuples, in the same order as `timestamps` (already ascending -
    compute_adaptive_timestamps() guarantees that) but possibly
    shorter, if ffmpeg couldn't produce a frame for one of them (see
    _extract_frame_at_timestamp() - skipped, not raised). Not used for
    a still photo - describe_scene() never reaches this branch for one
    (see is_photo_path() gate at its own call site).

    Task #1245 follow-up 8: one ffmpeg subprocess per timestamp now,
    replacing a single shared decord.VideoReader() + one batched
    get_batch() call - see _extract_frame_at_timestamp()'s own
    docstring for why (decord's mandatory whole-file index build, not
    the per-frame extraction itself, was the real cost)."""

    frames = []
    for timestamp in timestamps:
        image = _extract_frame_at_timestamp(video_path, timestamp)
        if image is not None:
            frames.append((timestamp, image))
    return frames


# Historical/superseded: this used to be prefixed as a literal text
# element ahead of a `{"type": "image", ...}` list, one per adaptively-
# chosen frame, each preceded by its own "[Frame at t=Xs]" text label.
# That worked, but task #1245 found it was the actual cause of a real,
# Christer-measured ~7x slowdown (732s adaptive vs 107s non-adaptive on
# the identical 180s clip, both post the #1241/#1244 speed fixes):
# feeding the model N independent `{"type": "image", ...}` elements
# instead of one genuine `{"type": "video", ...}` element loses
# whatever temporal-merging efficiency Qwen2.5-VL/Qwen3-VL-style models
# apply to real video input (confirmed not a resolution difference -
# both branches already used the same resized_width/resized_height).
# _build_adaptive_message_content() below now stitches the chosen
# frames into a small throwaway clip and feeds that in as a single
# video element instead - see _write_frames_as_temp_video() and
# _adaptive_video_intro_text(), which replaces this per-frame-label
# approach with one text block listing frame-order -> real-timestamp
# mapping up front (a video element can't carry an inline label per
# frame the way a list of image elements could). Left defined (but no
# longer used by _build_adaptive_message_content()) only because two
# existing tests in test_scene.py reference it as a monkeypatch
# sentinel value.
ADAPTIVE_FRAME_INTRO_PROMPT = (
    "The frames below are NOT evenly spaced in time - they were chosen "
    "from the moments most likely to matter in this specific clip, so "
    "some spans of the video got more frames and some got fewer. Each "
    "frame is preceded by a text label giving its own real elapsed "
    "time from the start of the clip in seconds - use those exact "
    "given values, not an assumed even spacing, when writing the "
    "'[t=Xs]' timestamps requested below."
)


def _adaptive_video_intro_text(highlight_timestamps: list[float], total_frame_count: int) -> str:
    """Build the text element that precedes the adaptive-sampling
    synthetic clip's `{"type": "video", ...}` element (see
    _write_frames_as_temp_video()), telling the model each of that
    clip's own frames' real elapsed-time in the ORIGINAL recording -
    the video-native replacement for the old per-frame "[Frame at
    t=Xs]" image labels (see ADAPTIVE_FRAME_INTRO_PROMPT's comment for
    why that approach got dropped). There's nowhere left to interleave
    a per-frame label once the frames are one video element instead of
    a list of separate image elements, so this lists the whole
    frame-order -> real-timestamp mapping up front instead, in the
    same order the frames appear in the synthetic clip (ascending -
    compute_adaptive_timestamps() guarantees that).

    Task #1245 follow-up, Christer's real-output report: description
    quality dropped two ways once this synthetic-clip approach shipped
    - (1) far shorter, choppier sentences, closer to one bullet per
    sampled frame than the fuller multi-frame prose the old image-list
    approach tended to produce, and (2) the model's own "[t=Xs]"
    timestamps came out visibly corrupted in the back half of longer
    descriptions ("t = 1 4 4 . 1 s" instead of "t=144.1s"), getting
    worse as the description went on. Christer's own diagnosis for (1):
    "My guess is that it reads frames just before and just after to
    get a more fully description, but now the frames are totaly
    differen from the neighbours" - i.e. a real video's temporal-
    merging machinery assumes neighboring frames are visually
    continuous (adjacent moments in time), which is exactly what
    adaptive sampling deliberately violates (that's the whole point -
    picking the moments that matter, not evenly-spaced ones), so the
    model has nothing to visually bridge between frames and falls back
    to describing each one as its own isolated moment. (2) traces to a
    second, independent mechanism: this function's first version built
    that frame -> timestamp mapping as 16 near-identical dashed lines
    ("- frame N: t=X.Xs"), structurally similar to the very "- [t=Xs]"
    bullet format DESCRIBE_PROMPT asks the model to write - `generate()`
    is called with `no_repeat_ngram_size=3` (SceneOptions default,
    tuned against real footage and deliberately left untouched here),
    which bans any 3-token sequence that's already appeared anywhere in
    the sequence so far, prompt included; once the model has written
    enough near-identical "[t=" bracket-openings (its own, and/or ones
    that already appeared in the old dashed intro list), it has to
    contort the exact token sequence to dodge the ban, which is
    genuinely what token-level whitespace-mangling like this looks
    like - and it compounds every time, matching the "gets worse deeper
    into the description" pattern Christer reported.

    This version addresses both without touching any tuned generation
    parameter: the frame -> timestamp mapping is now one flowing
    sentence (a plain comma list) instead of 16 dashed bullet-shaped
    lines, removing the structural resemblance to the model's own
    output format that was very likely feeding the ngram-block
    mangling; and the text now explicitly tells the model these frames
    are non-adjacent highlights, not continuous motion, and asks it to
    synthesize them into a connected narrative rather than caption each
    one in isolation - a direct, cheap (prompt-only) attempt at
    Christer's hypothesis for the choppiness. Neither is guaranteed to
    fully restore the old image-list version's prose quality - if it
    doesn't, the next lever worth trying is loosening
    no_repeat_ngram_size/repetition_penalty specifically for this call,
    which this change deliberately avoids gambling on without being
    able to test it against a real model from this sandbox.

    Update, task #1245 follow-up 5: that next lever got pulled.
    Adding real context frames (see _expand_with_context_frames()) made
    this same corruption dramatically worse - roughly 5x more sampled
    frames means roughly 5x more "[t=" bullet-openings competing for the
    same limited 3-gram space, and real hardware output showed the
    corruption escalate from mangled digit-spacing all the way to the
    "t" itself mutating through a couple dozen unrelated characters as
    generation went on. See SceneOptions.adaptive_repetition_penalty/
    adaptive_no_repeat_ngram_size for the fix that finally went in.

    Update, task #1245 follow-up 7: follow-up 5 fixed the corruption and
    follow-up 6 fixed the resulting truncation, but the real output
    those two follow-ups fixed still wasn't what Christer originally
    asked for. His own words, once he saw the (corruption-free,
    complete) 80-bullet result: "The idea was to add some frames
    around, but only look for 16 of them, maybe the model would have
    peeked around a little to get a fuller description, never to look
    att more frames from our side." I.e. context frames were only ever
    meant as extra visual input to help the model write a FULLER
    description of each of the 16 original highlights - not as 64 more
    things to separately describe. Every earlier version of this
    function (all the way back to the version above this update) handed
    the model the full expanded frame list (highlights + context) as
    "these are the moments to describe", which is exactly what made it
    write one bullet per frame instead. This version is called with
    `highlight_timestamps` - the original, un-expanded list
    compute_adaptive_timestamps() picked - even though
    _build_adaptive_message_content() still extracts and stitches every
    context frame into the actual clip the model sees. The extra frames
    are still real visual input (so the model's video-native temporal-
    merging can still use them - see this module's own architecture
    notes on why real video input over independent images matters here
    at all), they're just no longer listed as separate things to
    caption. Untested against a real model - it's possible the model
    still doesn't follow the "one bullet per highlight, not per frame"
    instruction any better than it followed the earlier "synthesize
    instead of captioning each frame" instruction for the widely-spaced
    highlights case (see this function's own note on that, above) -
    Christer needs to reinstall and re-test to find out."""

    times = ", ".join(f"{timestamp:.1f}s" for timestamp in highlight_timestamps)
    text = (
        "The clip below was assembled by sampling "
        f"{len(highlight_timestamps)} separate highlighted moments out "
        "of a longer recording, shown in order - it is NOT continuous "
        "motion the way an ordinary video clip is. Consecutive "
        "highlighted moments are often several seconds or more apart in "
        "the original recording, so don't assume smooth visual "
        "continuity between them; treat each as a distinct moment, but "
        "still synthesize them into one connected description of what "
        "happened over the whole recording, rather than describing each "
        "one in isolation. In playback order, these highlighted "
        "moments' real elapsed times from the start of the original "
        f"recording are: {times}. Use these exact given values, not an "
        "assumed even spacing across this short clip's own length, when "
        "writing the '[t=Xs]' timestamps requested below."
    )
    if total_frame_count > len(highlight_timestamps):
        text += (
            " This clip actually contains more individual frames than "
            f"that ({total_frame_count} in total) - a few extra real "
            "frames from just before and after each highlighted moment "
            "are included too, so the moments right around each "
            "highlight play as brief, genuinely continuous motion "
            "instead of one isolated snapshot, letting you get a fuller "
            "read on what's happening there (direction of travel, "
            "what's approaching or receding, and so on). These extra "
            "frames are only there to inform your description - do NOT "
            f"write a separate bullet for each one. Write exactly "
            f"{len(highlight_timestamps)} bullets total, one per "
            "highlighted moment listed above, each timestamped at that "
            "moment's own value."
        )
    return text


def _expand_with_context_frames(
    timestamps: list[float],
    duration_seconds: float,
    context_frames: int,
    offset_seconds: float,
) -> list[float]:
    """Task #1245 follow-up: Christer's direct request after seeing the
    choppy-sentences fix ("You could also add frames before and after
    our specified friend, just to get more") - a more substantive
    version of the same fix as _adaptive_video_intro_text()'s rewrite.
    That earlier fix only changed prompt wording; this one gives the
    model real extra visual data to back it up. For each of the
    adaptively-chosen highlight `timestamps`, add `context_frames`
    extra real timestamps on either side, spaced `offset_seconds` apart
    (e.g. context_frames=2, offset_seconds=0.5 turns one highlight at
    t=30.0s into five frames: 29.0s, 29.5s, 30.0s, 30.5s, 31.0s) - so
    every highlight sits inside a short burst of genuinely continuous
    real motion the video model's temporal-merging can bridge, instead
    of one isolated snapshot next to other snapshots seconds or minutes
    away. See that function's own docstring for the full root-cause
    story this builds on.

    No-op (returns `timestamps` unchanged) when `context_frames <= 0`
    (the default) or `offset_seconds <= 0` - callers gate on
    `opts.adaptive_context_frames` before calling this, but the guard
    is repeated here too so this function is safe to call unconditionally.

    Context timestamps that would fall outside [0, duration_seconds]
    are clamped into range rather than dropped, so a highlight near the
    very start/end of the recording still gets as many context frames
    as fit - they just pile up at the boundary instead of past it.
    Highlights close enough together that their context windows
    overlap, and any clamped duplicates, collapse naturally since the
    result is built as a set before sorting - _extract_frames_at_timestamps()
    would otherwise decode the same frame twice for no benefit."""

    if context_frames <= 0 or offset_seconds <= 0:
        return timestamps

    expanded: set[float] = set()
    for timestamp in timestamps:
        expanded.add(round(timestamp, 3))
        for step in range(1, context_frames + 1):
            offset = step * offset_seconds
            before = max(0.0, min(duration_seconds, timestamp - offset))
            after = max(0.0, min(duration_seconds, timestamp + offset))
            expanded.add(round(before, 3))
            expanded.add(round(after, 3))

    return sorted(expanded)


def _write_frames_as_temp_video(frames: list[tuple[float, "PILImage.Image"]], fps: float) -> Path:
    """Encode `frames` (already-ordered PIL Images, one per adaptively-
    chosen timestamp) into a small throwaway .mp4 in a fresh temp
    directory, one image per frame at `fps` frames/sec, via an ffmpeg
    subprocess - the same tool _photo_as_pil_image() already leans on
    elsewhere in this module for photo decode (see that function's own
    docstring for why ffmpeg over a Python-native encoder: it's already
    a hard dependency of this whole pipeline, no new one to add).

    Returns the temp video's path. The caller (
    _build_adaptive_message_content()) is responsible for handing the
    parent temp directory back up to describe_scene() so it can be
    deleted once the model call that reads this file has finished -
    qwen_vl_utils' "video" content-element handling reads its input
    from a real file on disk, not from memory, so the file has to
    still exist well after this function returns (the model call
    itself happens later, back in describe_scene())."""

    import tempfile

    tmp_dir = Path(tempfile.mkdtemp(prefix="bv-adaptive-clip-"))
    for i, (_, frame) in enumerate(frames):
        frame.convert("RGB").save(tmp_dir / f"frame_{i:04d}.png")

    video_path = tmp_dir / "adaptive_clip.mp4"
    try:
        result = subprocess.run(
            [
                "ffmpeg", "-y", "-v", "error",
                "-framerate", str(fps),
                "-i", str(tmp_dir / "frame_%04d.png"),
                "-c:v", "libx264", "-pix_fmt", "yuv420p",
                str(video_path),
            ],
            capture_output=True,
        )
    except FileNotFoundError as exc:
        shutil.rmtree(tmp_dir, ignore_errors=True)
        raise MediaToolError("ffmpeg not found on PATH") from exc

    if result.returncode != 0:
        shutil.rmtree(tmp_dir, ignore_errors=True)
        raise MediaToolError(
            "could not encode adaptive-sampling frames into a temp "
            f"clip: {result.stderr.decode('utf-8', errors='replace')}"
        )

    return video_path


def _build_adaptive_message_content(
    video_path: Path,
    opts: SceneOptions,
    gps_fixes: Sequence[GpsFix],
    gsensor_samples: Sequence[GSensorSample],
    recording_start: datetime | None,
    *,
    warn,
) -> tuple[list[dict], list[float], list[Path]]:
    """Build the adaptive-sampling counterpart to describe_scene()'s
    normal single `{"type": "video", ...}` content element: the chosen
    frames stitched into their own small throwaway clip (see
    _write_frames_as_temp_video()), preceded by one text element (see
    _adaptive_video_intro_text()) spelling out each highlighted moment's
    real elapsed-clip time, at timestamps compute_adaptive_timestamps()
    picks from `gps_fixes`/`gsensor_samples`. Returns
    (content_elements, highlight_timestamps, cleanup_paths) -
    describe_scene() appends highlight_timestamps to the output as a
    "## Sampled frames" section (also uses its length to scale
    max_new_tokens - see SceneOptions.adaptive_max_new_tokens_per_frame),
    and deletes each of cleanup_paths (the temp clip's parent directory)
    once its model call has finished reading it.

    Task #1245 follow-up 7: `highlight_timestamps` is the original,
    un-expanded list from compute_adaptive_timestamps() - NOT every real
    frame this function extracts and stitches into the clip. When
    `opts.adaptive_context_frames > 0`, more frames than that get pulled
    in and shown to the model (see _expand_with_context_frames()), but
    only the original highlight count is what the model is told to
    write one bullet per (see _adaptive_video_intro_text()) and what
    gets reported/budgeted for here. Before this follow-up,
    highlight_timestamps and the expanded frame list were the same
    variable, which is exactly what made the model write one bullet per
    real frame (up to 80 for a 16-highlight clip) instead of one fuller
    bullet per highlight informed by its neighboring context frames -
    see _adaptive_video_intro_text()'s own docstring for the real-output
    evidence and Christer's own description of what this was supposed
    to do instead.

    Task #1245: this used to build content_elements as a list of one
    `{"type": "image", ...}` element per chosen frame instead (see
    ADAPTIVE_FRAME_INTRO_PROMPT's comment for the full story) - real,
    Christer-measured hardware timing showed that was ~7x slower than
    the plain non-adaptive path on the identical clip (732s vs 107s),
    traced to feeding the model N independent images instead of one
    genuine video input. Stitching the same frames into a real clip
    and feeding that in as a single video element instead should let
    the model apply whatever temporal-merging efficiency it gets for
    real video input, matching the non-adaptive path's speed.

    Falls back to describe_scene()'s normal single "video" element
    (returning `([], [], [])` to signal this) if ffprobe can't even
    determine a usable duration for `video_path`, or if ffmpeg fails
    to extract any frames or to encode the temp clip - the same
    "graceful degradation, never a hard failure" contract
    compute_adaptive_timestamps() itself already has for missing
    telemetry, extended here to a missing/unreadable video or a
    failed encode too.

    Task #1245 follow-up 8: this used to open a decord.VideoReader()
    on `video_path` for both the duration probe and the frame
    extraction below - see _extract_frame_at_timestamp()'s own
    docstring for why that got replaced with ffprobe (duration) and
    direct per-timestamp ffmpeg seeks (frames) instead. No decord
    import remains anywhere in this function."""

    # Deferred for the same reason GpsFix/GSensorSample are TYPE_CHECKING-
    # only above: adaptive_sampling.py's own top-level imports reach
    # back into telemetry.gps_reader, which reaches back into
    # generate.media - a genuine circular import if pulled in while
    # generate/__init__.py is still mid-load (confirmed by direct
    # testing - see this module's own import-block comment). Safe here:
    # this function only ever runs at real describe_scene() call time,
    # long after every package involved has finished loading.
    from .adaptive_sampling import compute_adaptive_timestamps

    try:
        # Task #1245 follow-up 8: ffprobe-based duration read (fast -
        # just the container's own metadata header, no full-file scan)
        # instead of opening a decord.VideoReader() just to compute
        # len(vr)/fps - see _extract_frame_at_timestamp()'s docstring
        # for why decord's mandatory whole-file index build was worth
        # removing from this path entirely, not just de-duplicating.
        duration_seconds = probe_video(video_path).duration_seconds
    except MediaToolError as exc:
        warn(f"  adaptive-sampling: couldn't probe duration ({exc}), falling back to uniform sampling.")
        return [], [], []

    if duration_seconds <= 0:
        warn("  adaptive-sampling: couldn't determine a usable duration, falling back to uniform sampling.")
        return [], [], []

    highlight_timestamps = compute_adaptive_timestamps(
        duration_seconds, gps_fixes, gsensor_samples, recording_start, opts.max_frames
    )
    if not highlight_timestamps:
        return [], [], []

    # Task #1245 follow-up 7: frame_timestamps (what actually gets
    # extracted and stitched into the clip the model sees) and
    # highlight_timestamps (what the model is told to write one bullet
    # per) deliberately diverge once context frames are on -
    # highlight_timestamps must NOT be reassigned to the expanded list
    # here, or _adaptive_video_intro_text() below goes back to listing
    # every context frame as its own thing to describe, which is
    # exactly the bug this follow-up fixes. See that function's own
    # docstring for the full story (Christer: "The idea was to add some
    # frames around, but only look for 16 of them... never to look att
    # more frames from our side").
    frame_timestamps = highlight_timestamps
    if opts.adaptive_context_frames > 0:
        frame_timestamps = _expand_with_context_frames(
            highlight_timestamps, duration_seconds, opts.adaptive_context_frames, opts.adaptive_context_offset_seconds
        )

    frames = _extract_frames_at_timestamps(video_path, frame_timestamps)
    if not frames:
        return [], [], []

    fps = opts.fps if opts.fps and opts.fps > 0 else 1.0
    try:
        clip_path = _write_frames_as_temp_video(frames, fps)
    except MediaToolError as exc:
        warn(f"  adaptive-sampling: couldn't build temp clip ({exc}), falling back to uniform sampling.")
        return [], [], []

    video_ele = {
        "type": "video",
        "video": str(clip_path),
        "fps": fps,
        "max_frames": len(frames),
    }
    if opts.resized_width and opts.resized_height:
        video_ele["resized_width"] = opts.resized_width
        video_ele["resized_height"] = opts.resized_height
    else:
        video_ele["max_pixels"] = opts.max_pixels

    content: list[dict] = [
        {"type": "text", "text": _adaptive_video_intro_text(highlight_timestamps, len(frames))},
        video_ele,
    ]

    return content, highlight_timestamps, [clip_path.parent]


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

        # Build every crop for this frame first (geometry only, no
        # model calls yet), split into a sign bucket and a plate
        # bucket - each bucket gets read in ONE batched model call
        # below instead of one call per crop (task #1244: this used to
        # call _run_single_image_prompt() once per crop here, plus a
        # second call per plate for the confidence check - Christer
        # counted ~34 sequential model calls total on one real
        # recording, almost all of them from this loop). Geometry/
        # cropping/debug-saving is unchanged, only how the OCR read
        # itself gets requested.
        sign_crops = []
        plate_crops = []
        for det_index, det in enumerate(boxes):
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

            entry = {
                "det_index": det_index,
                "label": det["label"],
                "width": ocr_width,
                "height": ocr_height,
                "crop": crop,
                "crop_native_width": crop_native_width,
                "crop_native_height": crop_native_height,
                "debug_path": debug_path,
            }
            (plate_crops if is_plate else sign_crops).append(entry)

        # frame_results collects every crop's finished output line/
        # manifest entry keyed by its original detection order, so the
        # final append below can walk boxes in the same order the
        # model detected them in - batching signs and plates
        # separately (and reassembling here) shouldn't change what the
        # output looks like, only how many model calls it took to get
        # there.
        frame_results: dict[int, tuple[str, str | None]] = {}

        if sign_crops:
            try:
                reads = _run_batch_image_prompt(
                    [(item["width"], item["height"], item["crop"]) for item in sign_crops],
                    ZOOM_OCR_BATCH_PROMPT, loaded, opts,
                    max_new_tokens=opts.zoom_max_new_tokens,
                    repetition_penalty=opts.zoom_repetition_penalty,
                    no_repeat_ngram_size=opts.zoom_no_repeat_ngram_size,
                )
            except Exception as exc:  # noqa: BLE001
                warn(f"  zoom: batch sign read failed at t={timestamp:.1f}s ({exc}), skipping {len(sign_crops)} sign(s).")
            else:
                for item, read_text in zip(sign_crops, reads):
                    line = f"- [t={timestamp:.1f}s] {item['label']}: {read_text.strip()}"
                    manifest_entry = None
                    if item["debug_path"] is not None:
                        manifest_entry = (
                            f"{item['debug_path'].name}\t{timestamp:.1f}\t{item['label']}\t"
                            f"{item['crop_native_width']}x{item['crop_native_height']}\t{read_text.strip()}"
                        )
                    frame_results[item["det_index"]] = (line, manifest_entry)

        if plate_crops:
            try:
                reads = _run_batch_image_prompt(
                    [(item["width"], item["height"], item["crop"]) for item in plate_crops],
                    ZOOM_OCR_PLATE_BATCH_PROMPT, loaded, opts,
                    max_new_tokens=opts.zoom_max_new_tokens,
                    repetition_penalty=opts.zoom_repetition_penalty,
                    no_repeat_ngram_size=opts.zoom_no_repeat_ngram_size,
                )
            except Exception as exc:  # noqa: BLE001
                warn(f"  zoom: batch plate read failed at t={timestamp:.1f}s ({exc}), skipping {len(plate_crops)} plate(s).")
            else:
                second_reads = None
                if opts.zoom_plate_confidence_check:
                    try:
                        second_reads = _run_batch_image_prompt(
                            [(item["width"], item["height"], item["crop"]) for item in plate_crops],
                            ZOOM_OCR_PLATE_BATCH_PROMPT, loaded, opts,
                            max_new_tokens=opts.zoom_max_new_tokens,
                            repetition_penalty=opts.zoom_repetition_penalty,
                            no_repeat_ngram_size=opts.zoom_no_repeat_ngram_size,
                            force_sample=True,
                        )
                    except Exception as exc:  # noqa: BLE001 - confidence check is a bonus, not core
                        warn(f"  zoom: plate confidence re-read failed ({exc}), reporting reads unverified.")

                for i, item in enumerate(plate_crops):
                    read_text = reads[i]
                    confidence_note = ""
                    if opts.zoom_plate_confidence_check:
                        if second_reads is None:
                            confidence_note = " [unverified - confidence re-read failed]"
                        elif _normalize_plate_text(read_text) != _normalize_plate_text(second_reads[i]):
                            confidence_note = (
                                " [unverified - two independent reads disagreed: "
                                f"{read_text.strip()!r} vs {second_reads[i].strip()!r}]"
                            )
                    line = f"- [t={timestamp:.1f}s] {item['label']}: {read_text.strip()}{confidence_note}"
                    manifest_entry = None
                    if item["debug_path"] is not None:
                        manifest_entry = (
                            f"{item['debug_path'].name}\t{timestamp:.1f}\t{item['label']}\t"
                            f"{item['crop_native_width']}x{item['crop_native_height']}\t"
                            f"{read_text.strip()}{confidence_note}"
                        )
                    frame_results[item["det_index"]] = (line, manifest_entry)

        for det_index in sorted(frame_results):
            line, manifest_entry = frame_results[det_index]
            lines.append(line)
            if manifest_entry is not None:
                debug_manifest.append(manifest_entry)

    if debug_dir is not None and debug_manifest:
        manifest_path = debug_dir / "manifest.tsv"
        header = "filename\ttimestamp_s\tlabel\tnative_crop_size\tocr_read"
        manifest_path.write_text(header + "\n" + "\n".join(debug_manifest) + "\n", encoding="utf-8")

    if not lines:
        return ""
    return "\n\n## Zoomed sign reads\n" + "\n".join(lines)


# 2026-08-26 (task #1260 follow-up 7): a structural safety net, added
# after generate()-parameter tuning alone failed to stop a degenerate
# repeat loop across THREE separate real-hardware runs in a row. Each
# time it was adaptive_repetition_penalty/adaptive_no_repeat_ngram_size
# (see their own comments, follow-ups 5 and 6) that got adjusted first,
# and each time the "## On-screen text" section still degenerated into
# an exact-repeat loop cut off mid-word by max_new_tokens - just with
# different specific content each run ("LAN" x40+; "Forbjudet att kora
# pa gatan"/"Korselvag" alternating x25+; "LANGA"/"FORSTA" alternating
# x45+). Three consecutive real-world failures of the same fix strategy
# is strong evidence that decoding-parameter tuning can't reliably
# prevent this failure mode - it can only make the model *less likely*
# to fall into a loop, not guarantee it won't. So instead of trying a
# fourth parameter, this catches the failure mode itself: any short
# chunk of decoded text that repeats several times back to back is
# unambiguously degenerate (real prose, and even the legitimate
# repeating "- [t=" bullet prefix in "## Description", never repeats
# the *same exact substring* 4+ times contiguously, since each bullet's
# content after the prefix differs) and gets truncated before it
# reaches Christer's .scene.txt file.
_DEGENERATE_REPEAT_RE = re.compile(r"(.{1,80}?)\1{3,}", re.DOTALL)


def _truncate_repeated_lines(text: str) -> str:
    """Cut off a degenerate exact-repeat loop, if the decoded text has one.

    See the comment directly above _DEGENERATE_REPEAT_RE for why this
    exists and why a >=4x contiguous exact repeat is a safe signal
    (real generated text, including the legitimate repeating "- [t="
    bullet prefix, doesn't trigger this - only true decoding
    degeneration does). Truncates at the start of the loop rather than
    trying to keep one copy of the repeated chunk, since by the time
    generation degenerates into a loop, nothing after that point in
    the response is trustworthy content anyway.
    """
    match = _DEGENERATE_REPEAT_RE.search(text)
    if match is None:
        return text
    return text[: match.start()].rstrip()


def describe_scene(
    video_path: Path,
    *,
    opts: SceneOptions | None = None,
    warn=None,
    gps_fixes: Sequence[GpsFix] = (),
    gsensor_samples: Sequence[GSensorSample] = (),
    recording_start: datetime | None = None,
    debug: bool = False,
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

    gps_fixes/gsensor_samples/recording_start are only consulted when
    opts.adaptive_sampling is True (default False - see that field's
    own docstring) - the caller (cli/bv_generate.py) is responsible for
    fetching them via adapters/telemetry_bridge.py first, since this
    module deliberately has no adapter/Recording knowledge of its own.
    Passing none of the three (the default) with adaptive_sampling=True
    still works - compute_adaptive_timestamps() degrades to ~evenly-
    spaced sampling with no telemetry to bias toward, rather than
    erroring - it just won't be adaptive to anything in that case.

    debug=True prints wall-clock timing to stderr for each phase (model
    load, adaptive frame extraction if opts.adaptive_sampling is on,
    vision-input decode, the main generate() call, and the zoom-signs
    sub-pipeline if enabled) - same convention as bv-export's own
    --debug. Added (task #1260 follow-up 9) after Christer described
    watching a "small block, then long wait for big blocks" pattern in
    Task Manager's GPU graph during a normal-length run and asked what
    each part corresponded to - previously the only available timing was
    bv-generate's own single start/finished total, so answering that
    kind of question meant guessing from the code structure instead of
    reading real numbers. The adaptive-extraction timer was added
    separately, later the same follow-up, once it became clear the
    original four timers didn't cover the ffmpeg-seek/temp-clip-stitch
    work _build_adaptive_message_content() does before the model ever
    sees a frame - exactly the phase Christer had separately measured at
    ~60s with the old decord-based implementation (since replaced, task
    #1260 follow-up 8 above) and asked to see confirmed against the new
    one.
    """

    if opts is None:
        opts = SceneOptions(**overrides)
    elif overrides:
        raise TypeError("pass either opts= or **overrides, not both")

    warn = warn or (lambda msg: print(msg, file=sys.stderr))

    load_start = time.monotonic() if debug else None
    loaded = _get_scene_model(
        opts.model,
        force_cpu=opts.force_cpu,
        quantize=opts.quantize,
        gpu_memory_fraction=opts.gpu_memory_fraction,
    )
    if debug:
        print(f"bv-generate: model load took {time.monotonic() - load_start:.1f}s", file=sys.stderr)

    prompt = build_prompt(opts.task)

    sampled_frame_timestamps: list[float] = []
    # Parent dir(s) of any temp clip _build_adaptive_message_content()
    # wrote (see _write_frames_as_temp_video()) - deleted in the
    # finally: block below, once the model call that reads it (if any)
    # has finished. Stays empty for the plain-video/photo paths, which
    # never write scratch files here.
    adaptive_cleanup_paths: list[Path] = []

    if is_photo_path(video_path):
        photo_image = _photo_as_pil_image(video_path)
        photo_image = _crop_overlay_from_image(photo_image, opts.crop_top, opts.crop_bottom)
        content_ele = {"type": "image", "image": photo_image}
        if opts.resized_width and opts.resized_height:
            content_ele["resized_width"] = opts.resized_width
            content_ele["resized_height"] = opts.resized_height
        message_content = [content_ele, {"type": "text", "text": prompt}]
    else:
        adaptive_content: list[dict] = []
        if opts.adaptive_sampling:
            # 2026-08-26 (task #1260 follow-up 9, debug timing): this is
            # the phase Christer specifically asked to see the cost of -
            # "about 60 s before qwen-vl-utils using decord to read
            # video" (follow-up 8 above, since replaced with direct
            # ffmpeg seeks, real hardware not yet timed against this).
            # Everything in here - the duration probe, picking
            # timestamps, the per-timestamp ffmpeg seek-and-decode, and
            # writing the small stitched temp clip via
            # _write_frames_as_temp_video() - happens before the model
            # ever sees a single frame, and is invisible inside the
            # "vision-input decode" timer below (that one only wraps
            # qwen_vl_utils' own read of the tiny already-stitched clip,
            # which is a completely different, much smaller cost).
            adaptive_extract_start = time.monotonic() if debug else None
            adaptive_content, sampled_frame_timestamps, adaptive_cleanup_paths = _build_adaptive_message_content(
                video_path, opts, gps_fixes, gsensor_samples, recording_start, warn=warn
            )
            if debug:
                print(
                    f"bv-generate: adaptive frame extraction (ffmpeg "
                    f"seeks + temp clip stitch) took "
                    f"{time.monotonic() - adaptive_extract_start:.1f}s",
                    file=sys.stderr,
                )

        if sampled_frame_timestamps:
            # Deliberately keyed on sampled_frame_timestamps, not
            # adaptive_content, even though they're built together by
            # _build_adaptive_message_content() above - adaptive_content
            # is a list that always starts with one intro-text element
            # (see that function), so it's truthy even when zero real
            # frames got extracted (e.g. a transient decord/network-read
            # failure on _extract_frames_at_timestamps() that returns []
            # without raising). A real bug: checking adaptive_content
            # here used to let that empty-but-truthy case slip through
            # as a text-only message with no images at all - the model
            # then has nothing to actually look at and fabricates a
            # plausible-sounding but ungrounded description instead of
            # erroring or falling back (task #1238, a real-archive
            # report: "20220927_132155_E"'s adaptive-sampling retry
            # produced a generic 4-bullet description bunched at t~0s
            # with none of that recording's known real content, while
            # the separate zoom-signs pass - its own independent frame
            # extraction - correctly read every sign across the whole
            # clip). sampled_frame_timestamps is exactly as long as the
            # frames actually extracted, so it's falsy in precisely the
            # cases that should fall back to the plain "video" element
            # below instead.
            message_content = adaptive_content + [{"type": "text", "text": prompt}]
        else:
            # opts.adaptive_sampling is False (today's default,
            # unchanged behavior), or it's True but
            # _build_adaptive_message_content() itself gracefully
            # degraded (an unreadable/zero-duration video, or - see the
            # comment above - zero frames actually extracted) - all
            # cases fall back to the same plain "video" element
            # qwen_vl_utils samples internally.
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
            # See _SPARSE_SAMPLING_HINT_TEMPLATE's own comment (task
            # #1260 follow-up 4) - only this plain-video branch appends
            # it; `prompt` itself stays untouched so the photo/adaptive
            # branches above are unaffected.
            plain_prompt = prompt + _SPARSE_SAMPLING_HINT_TEMPLATE.format(max_frames=opts.max_frames)
            # 2026-08-26 (task #1260 follow-up 10): ground the plain
            # path's own "[t=Xs]" bullets in a real, computed timestamp
            # list instead of leaving the model to guess - see
            # _plain_video_frame_timestamps()'s long comment for why
            # this is knowable in advance. probe_video() is one cheap
            # ffprobe metadata call (no full-file decode), same one
            # this project already uses for .duration.txt self-healing
            # elsewhere - failure here (corrupt/unreadable header) is
            # not fatal, it just means this run falls back to the old
            # ungrounded behavior exactly like before this follow-up.
            try:
                plain_duration = probe_video(video_path).duration_seconds
            except MediaToolError:
                plain_duration = None
            if plain_duration:
                plain_timestamps = _plain_video_frame_timestamps(
                    plain_duration, opts.fps, opts.max_frames
                )
                if plain_timestamps:
                    plain_prompt = (
                        _plain_video_intro_text(plain_timestamps, plain_duration)
                        + "\n\n"
                        + plain_prompt
                    )
            message_content = [content_ele, {"type": "text", "text": plain_prompt}]

    messages = [{"role": "user", "content": message_content}]
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
        decode_start = time.monotonic() if debug else None
        image_inputs, video_inputs, video_kwargs, video_metadata = _fetch_vision_inputs(
            loaded.process_vision_info, messages, is_qwen3=loaded.is_qwen3
        )
        if video_inputs:
            video_inputs = _crop_top_bottom(
                video_inputs, opts.crop_top, opts.crop_bottom, loaded.patch_factor
            )
        if debug:
            print(
                f"bv-generate: vision-input decode took {time.monotonic() - decode_start:.1f}s",
                file=sys.stderr,
            )

        # Task #1258 follow-up (Christer: "i run with frames 64, but it
        # looks like it ignores me" - --frames 32 and 64 both only ever
        # described the first few seconds of a 180s clip, unchanged by
        # the frame count): diagnostic for the "Asked to sample fps
        # frames per second but no video metadata was provided ...
        # Defaulting to fps=24" warning qwen_vl_utils/transformers
        # prints on this machine. Hypothesis: if the processor falls
        # back to assuming 24fps for an already-extracted N-frame
        # tensor, its own sense of "how much real time this covers" is
        # N/24 seconds - ~1.3s at N=32, ~2.7s at N=64 - regardless of
        # the real ~180s the frames were actually sampled across, which
        # would fully explain bullets clustering near t=0 no matter how
        # high --frames goes. This print surfaces exactly what
        # video_kwargs _fetch_vision_inputs() got back (do_sample_frames
        # present? what fps value?) so that hypothesis can be confirmed
        # or ruled out against Christer's real installed qwen-vl-utils/
        # transformers versions instead of guessed at further.
        print(f"bv-generate: video_kwargs={video_kwargs!r}", file=sys.stderr)

        inputs = loaded.processor(
            text=[text], images=image_inputs, videos=video_inputs,
            padding=True, return_tensors="pt",
            **_processor_call_kwargs(video_kwargs, video_metadata),
        )
        inputs = inputs.to(loaded.model.device)

        # Task #1245 follow-up 6: scale the token budget with how many
        # bullets the model is actually being asked to write, instead of
        # leaving it pinned at the 16-frame-tuned default - see
        # SceneOptions.adaptive_max_new_tokens_per_frame.
        describe_max_new_tokens = opts.max_new_tokens
        if opts.adaptive_sampling and sampled_frame_timestamps:
            describe_max_new_tokens = max(
                opts.max_new_tokens,
                len(sampled_frame_timestamps) * opts.adaptive_max_new_tokens_per_frame,
            )

        generate_start = time.monotonic() if debug else None
        generated_ids = loaded.model.generate(
            **inputs,
            max_new_tokens=describe_max_new_tokens,
            # Task #1245 follow-up 5 originally scoped this relaxation to
            # adaptive_sampling only. Real hardware at --frames 32 on the
            # plain non-adaptive path (task #1258's follow-up) showed the
            # same "[t=" bracket-formatting drift on just 5 bullets ("- [
            # t=0s ]", "-[t=2.8s]", "-[-t=6.9s]") that the original bug
            # report showed at ~80 bullets on the adaptive path - so the
            # no_repeat_ngram_size=3/repetition_penalty=1.15 tuned values
            # were never actually safe for this "many short near-
            # identical bullet openings" shape of output in general, just
            # less likely to visibly trigger with fewer bullets. Applied
            # unconditionally now - see SceneOptions.adaptive_repetition_
            # penalty/adaptive_no_repeat_ngram_size.
            repetition_penalty=opts.adaptive_repetition_penalty,
            no_repeat_ngram_size=opts.adaptive_no_repeat_ngram_size,
            **_sampling_kwargs(opts),
        )
        if debug:
            print(
                f"bv-generate: main describe+OCR generate() took "
                f"{time.monotonic() - generate_start:.1f}s",
                file=sys.stderr,
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
        # Delete the adaptive-sampling temp clip (see
        # _write_frames_as_temp_video()) now that the model call above
        # has read it - it's genuine scratch data, not anything a
        # caller could want to keep, and there's no other cleanup path
        # for it (unlike video_path/photo inputs, which the caller
        # owns). ignore_errors=True: a failed cleanup here shouldn't
        # turn a successful describe_scene() call into a failed one.
        for cleanup_path in adaptive_cleanup_paths:
            shutil.rmtree(cleanup_path, ignore_errors=True)

    trimmed = [out_ids[len(in_ids):] for in_ids, out_ids in zip(inputs.input_ids, generated_ids)]
    output_text = loaded.processor.batch_decode(
        trimmed, skip_special_tokens=True, clean_up_tokenization_spaces=False
    )[0]
    # See _truncate_repeated_lines()'s own comment (task #1260 follow-up
    # 7) - applied only to the raw model decode, before the separately-
    # generated zoom-signs/sampled-frames sections get appended below,
    # so this can't accidentally truncate content it didn't generate.
    output_text = _truncate_repeated_lines(output_text)

    if opts.zoom_signs:
        zoom_start = time.monotonic() if debug else None
        output_text += _zoom_into_signs(video_path, loaded, opts, warn=warn)
        if debug:
            print(
                f"bv-generate: zoom-signs pass took {time.monotonic() - zoom_start:.1f}s",
                file=sys.stderr,
            )

    if sampled_frame_timestamps:
        # Records the real, non-uniform timestamps adaptive sampling
        # actually used - reusing the same "- [t=X.Ys]" bullet shape
        # DESCRIBE_PROMPT already asks the model for (so
        # _BULLET_START_RE/_parse_timed_events() can parse this section
        # too, with no new format needed) so a downstream consumer like
        # web/archive_browser.py's frame viewer can recover exactly
        # which frames this recording's description was written from,
        # instead of assuming even fps/max_frames spacing (see
        # ADAPTIVE_FRAME_INTRO_PROMPT's own comment for why that
        # assumption would otherwise be wrong here).
        frame_lines = "\n".join(
            f"- [t={timestamp:.1f}s] sampled frame" for timestamp in sampled_frame_timestamps
        )
        output_text += "\n\n## Sampled frames\n" + frame_lines

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
    image_inputs, video_inputs, video_kwargs, video_metadata = _fetch_vision_inputs(
        loaded.process_vision_info, messages, is_qwen3=loaded.is_qwen3
    )
    try:
        inputs = loaded.processor(
            text=[text], images=image_inputs, videos=video_inputs,
            padding=True, return_tensors="pt",
            **_processor_call_kwargs(video_kwargs, video_metadata),
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
