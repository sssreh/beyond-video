"""
bv-generate.

Copyright (C) 2026 Christer R. (sssreh)

SPDX-License-Identifier: GPL-3.0-or-later
"""

from __future__ import annotations

import argparse
import re
import sys
import threading
import time
from datetime import datetime
from pathlib import Path

from ..adapters import registry
from ..archive import Asset
from ..archive.photo import recording_is_photo
from ..archive.recording import Recording
from .errors import run_cli
from ..core.camera_config import DEFAULT_ADAPTER_ID
from ..core.camera_config import default_config_dir
from ..core.camera_config import resolve_archive_path
from ..core.joblog import wrap_say
from ..core.joblog import wrap_warn
from ..core.lock import assets_fully_locked
from ..core.lock import load_lock_manifest
from ..core.resume import advance_resume_point
from ..core.resume import load_resume_state
from ..core.resume import resume_point
from ..core.resume import save_resume_state
from ..generate import MediaToolError
from ..generate import SCENE_DEFAULT_MODEL
from ..generate import SpeechSegment
from ..generate import describe_scene
from ..generate import detect_language
from ..generate import diarize
from ..generate import extract_audio
from ..generate import extract_video_thumbnail
from ..generate import format_diarized_transcript
from ..generate import format_srt
from ..generate import get_span
from ..generate import gpu_available
from ..generate import is_audio_silent
from ..generate import normalize_language
from ..generate import probe_audio_codec
from ..generate import select_source
from ..generate import short_code
from ..generate import transcribe
from ..generate import translate
from ..generate.mp4_repair import load_or_repair_parking_video
from ..lexicaltimeparser import LexicalTimeParser
from ..lexicaltimeparser import TimeInterval

_SPEAKER_LINE = re.compile(r"^\[(?P<speaker>[^\]]+)\]\s*(?P<text>.*)$")

# Transcripts/translations in this language keep the plain
# <id>.transcript.txt / <id>.translation.txt filename. Any other
# language gets "_<3-letter-code>" before the extension, e.g.
# <id>_swe.translation.txt, so multiple languages can coexist.
DEFAULT_LANGUAGE = "en"

EXIT_OK = 0
EXIT_ARGS_ERROR = 1
EXIT_HAD_ERRORS = 2


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Parse command line arguments."""

    parser = argparse.ArgumentParser(
        prog="bv-generate",
        description=(
            "Generate derived assets (audio, duration/span, transcript, "
            "translation, optionally speaker-labeled via --diarize) for "
            "recordings in a local BlackVue archive. Generated files are "
            "written next to their source recording and appear in bv-ls."
        ),
        # See bv_export.py's own ArgumentParser for why: argparse's
        # default prefix-abbreviation matching silently breaks the
        # moment a sibling flag sharing a prefix gets added later.
        allow_abbrev=False,
    )

    parser.add_argument(
        "path",
        nargs="?",
        default=".",
        help=(
            "Archive directory, or a configured camera system id (see "
            "bv-config) - resolved to that camera's own archive "
            "directory. A path containing a separator (e.g. ./Kirby) "
            "is always used literally, never as an id, so a real "
            "directory sharing a camera's name is never ambiguous."
        ),
    )

    parser.add_argument(
        "--config-dir",
        type=Path,
        default=default_config_dir(),
        help=(
            "Directory camera configs live in, for resolving `path` "
            "as a camera id (default: %(default)s)."
        ),
    )

    parser.add_argument(
        "--from",
        dest="from_",
        metavar="TIMESTAMP",
        help="Only consider recordings from this timestamp.",
    )

    parser.add_argument(
        "--until",
        metavar="TIMESTAMP",
        help="Only consider recordings up to this timestamp.",
    )

    parser.add_argument(
        "--timestamp",
        metavar="TIMESTAMP",
        help="Only consider recordings matching this timestamp or prefix.",
    )

    parser.add_argument(
        "--resume",
        action="store_true",
        help=(
            "Skip straight to new recordings instead of walking the "
            "whole archive every run - for a daily/cron invocation "
            "with a stable set of action flags. Remembers, per exact "
            "combination of action flags used, the newest recording "
            "reached by the last --resume run, saved as "
            "'.bv-generate-resume.json' next to the archive; each "
            "later --resume run narrows --from up to that point "
            "(combined with an explicit --from/--until, not replacing "
            "them - whichever bound is later wins) and, if it finds "
            "at least one recording, advances the cursor again "
            "afterwards. A recording is included once more on the "
            "very next run after being the newest one seen (cheap - "
            "already-generated files are still skipped the normal "
            "way), as a safety margin against an interrupted run. "
            "Changing the action flags starts that new combination's "
            "own cursor from the beginning rather than risking a "
            "silent gap. Doesn't skip --dry-run's own reporting, and "
            "a dry run never advances the cursor. First run for a "
            "given combination (no cursor yet) behaves exactly like "
            "not passing --resume at all."
        ),
    )

    parser.add_argument(
        "--extract-audio",
        action="store_true",
        help=(
            "Extract the audio track from the front camera video "
            "(or the rear camera video if there is no front video). "
            "Saved as <recording>.aac. Parking-mode (P) recordings are "
            "skipped - they are timelapses with no audio."
        ),
    )

    parser.add_argument(
        "--get-duration",
        action="store_true",
        help=(
            "Compute the real-world duration in seconds, from the front "
            "camera video (or rear if there is no front video). Parking "
            "mode (P) recordings are 1-frame-per-second timelapses, so "
            "the reported value is the real elapsed time span, not the "
            "video's own playback length. Saved as <recording>.duration.txt."
        ),
    )

    parser.add_argument(
        "--thumbnail",
        action="store_true",
        help=(
            "Generate a small JPEG frame-grab thumbnail from the front "
            "camera video (or rear if there is no front video). Saved "
            "as <recording>.thumb.jpg. Only useful for archives with "
            "no camera-native thumbnail sidecar (FolderAdapter/"
            "GoProAdapter - see docs/CAMERA_ADAPTERS.md); a recording "
            "that already has one, or that recording_is_photo() "
            "already treats as its own thumbnail, is skipped. bv-web's "
            "archive browser generates the same permanent file itself "
            "on first view if this hasn't been run yet, so running "
            "this ahead of time just avoids paying that cost on first "
            "view."
        ),
    )

    parser.add_argument(
        "--transcribe",
        action="store_true",
        help=(
            "Transcribe the recording's audio to text. Saved as "
            "<recording>.transcript.txt. Parking-mode (P) recordings "
            "are skipped - they are timelapses with no audio."
        ),
    )

    parser.add_argument(
        "--translate",
        metavar="LANG",
        default=None,
        help=(
            "Translate the transcript into LANG (e.g. 'es', 'fr') and "
            "save it as <recording>.translation.txt. Implies "
            "transcription internally; --transcribe is not required."
        ),
    )

    parser.add_argument(
        "--language",
        metavar="LANG",
        default=None,
        help=(
            "Spoken language hint for --transcribe/--translate "
            "(e.g. 'en'). Auto-detected if omitted."
        ),
    )

    parser.add_argument(
        "--model-size",
        default=None,
        help=(
            "faster-whisper model size. Defaults to 'large' if a GPU "
            "is detected on this machine, otherwise 'small' - see "
            "--cpu to force the small/CPU combination on a GPU "
            "machine anyway (e.g. to compare against the GPU default)."
        ),
    )

    parser.add_argument(
        "--cpu",
        action="store_true",
        help=(
            "Force faster-whisper onto CPU even if a GPU is available "
            "- e.g. to compare 'bv-generate --transcribe --model-size "
            "small --cpu' against the GPU-default 'bv-generate "
            "--transcribe' (large, on GPU). Has no effect with "
            "--npu-model-dir, which has no CPU/GPU choice of its own."
        ),
    )

    parser.add_argument(
        "--npu-model-dir",
        metavar="PATH",
        type=Path,
        default=None,
        help=(
            "Use an Intel NPU (OpenVINO GenAI) instead of faster-whisper "
            "for --transcribe/--translate, pointed at an OpenVINO IR "
            "Whisper model directory (see docs/man/bv-generate.md for "
            "the one-time 'optimum-cli export openvino' conversion "
            "step). Requires --language - this backend cannot "
            "auto-detect the spoken language. Not verified against "
            "real Intel NPU hardware; try it and report back if it "
            "doesn't work as documented."
        ),
    )

    parser.add_argument(
        "--diarize",
        action="store_true",
        help=(
            "Label who is speaking in the transcript/translation "
            "(e.g. '[SPEAKER_00] ...'), using pyannote.audio. Requires "
            "a HuggingFace access token - see --hf-token."
        ),
    )

    parser.add_argument(
        "--hf-token",
        metavar="TOKEN",
        default=None,
        help=(
            "HuggingFace access token for --diarize's speaker "
            "diarization model. Create one at "
            "https://huggingface.co/settings/tokens, then accept the "
            "model license at https://huggingface.co/pyannote/"
            "speaker-diarization-community-1 - if you still get a 403 "
            "for some other repo after that, accept its license too, "
            "pyannote names the exact repo each time. Falls back to "
            "the HF_TOKEN environment variable if omitted."
        ),
    )

    parser.add_argument(
        "--srt",
        action="store_true",
        help=(
            "Also write an SRT subtitle file (<recording>.srt) with "
            "per-segment start/end timestamps from the transcript. "
            "Requires --transcribe or --translate."
        ),
    )

    parser.add_argument(
        "--describe-scene",
        action="store_true",
        help=(
            "Describe the recording's contents and read its on-screen "
            "text using a local vision-language model. Saved as "
            "<recording>.scene.txt. Works on Parking-mode recordings "
            "too (they're still video, just no audio). Output includes "
            "a disclaimer: reads have been observed to be confidently "
            "wrong (a real plate came back misread, not flagged as "
            "illegible) or to invent plausible-looking but unrelated "
            "text on ambiguous scenes - treat every read as unverified "
            "until checked against the source video. See bv-scribe for "
            "the full set of tuning flags (frame sampling, resolution, "
            "the sign-zoom sub-pipeline, batch mode) - this flag uses "
            "sensible defaults for running scene "
            "description alongside other bv-generate actions in one "
            "pass."
        ),
    )

    parser.add_argument(
        "--scene-model",
        default=None,
        help=(
            "Vision-language model for --describe-scene (default: "
            f"{SCENE_DEFAULT_MODEL}). ~16GB download on first use, "
            "cached under ~/.cache/huggingface."
        ),
    )

    parser.add_argument(
        "--scene-quantize",
        choices=["auto", "none", "int8", "int4"],
        default="auto",
        help=(
            "Loading precision for --describe-scene's vision-language "
            "model - not a different model, just a smaller-footprint "
            "way to load the same one (default: auto). 'auto' picks a "
            "level from the largest single GPU detected on this "
            "machine: comfortably-sized GPUs (~20GB+) get 'none' (full "
            "precision, e.g. an RTX 5090 laptop), mid-size GPUs "
            "(~10-20GB) get 'int8' (e.g. a single RTX 3080 Ti's "
            "12GB), and smaller GPUs get 'int4' - see --cpu for "
            "forcing CPU instead, which 'auto' always resolves to "
            "'none' under (int8/int4 need a CUDA GPU, via "
            "bitsandbytes - see pyproject.toml's scene extra). Pass "
            "'none'/'int8'/'int4' explicitly to override the "
            "auto-detected choice."
        ),
    )

    parser.add_argument(
        "--scene-gpu-memory-fraction",
        type=float,
        default=None,
        help=(
            "Cap --describe-scene's vision-language model to this "
            "fraction (0 exclusive - 1.0 inclusive) of each visible "
            "GPU's total VRAM, via torch's own "
            "set_per_process_memory_fraction() - so it guarantees some "
            "VRAM stays free for something else running on the same "
            "card at the same time, instead of hoping the driver is "
            "polite about it. Not set by default (no cap - the model "
            "claims whatever it needs). CUDA-only, same as "
            "--scene-quantize - can't be combined with --cpu."
        ),
    )

    parser.add_argument(
        "--camera",
        choices=["front", "rear", "both"],
        default="front",
        help=(
            "Which camera(s) --describe-scene processes (default: "
            "front - same as before this flag existed: front video, "
            "or rear if there's no front). 'rear' processes only the "
            "rear video, with the normal full description+OCR pass "
            "(saved as <recording>.rear.scene.txt) - a deliberate "
            "choice gets full treatment, not just plates. 'both' adds "
            "a cheap OCR-only bonus pass on the rear video alongside "
            "the normal front pass, skipped if the recording has no "
            "distinct rear video (i.e. front was already using rear "
            "as its own fallback) - a rear-camera description would "
            "mostly just restate the front one's, so only plates/signs "
            "are worth the extra inference call."
        ),
    )

    parser.add_argument(
        "--ignore-lock",
        action="store_true",
        help=(
            "Run even if the selected range is fully locked (see "
            "bv-lock) for every action flag given here. For the rare "
            "case of needing to touch an otherwise-locked range again "
            "(e.g. a bug fix, or a genuinely new value for an "
            "already-locked asset like a fresh --translate) without "
            "editing the lock itself - narrow the selection with "
            "--timestamp/--from/--until first, this does not also "
            "narrow it for you."
        ),
    )

    parser.add_argument(
        "--overwrite",
        action="store_true",
        help="Regenerate files that already exist without asking.",
    )

    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show what would be generated without generating it.",
    )

    parser.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="Print each file as it is generated.",
    )

    args = parser.parse_args(argv)

    if not (
        args.extract_audio
        or args.get_duration
        or args.thumbnail
        or args.transcribe
        or args.translate is not None
        or args.describe_scene
    ):
        parser.error(
            "specify at least one action: --extract-audio, "
            "--get-duration, --thumbnail, --transcribe, --translate, "
            "or --describe-scene"
        )

    if args.scene_model is None:
        args.scene_model = SCENE_DEFAULT_MODEL

    if args.diarize and not (args.transcribe or args.translate is not None):
        parser.error("--diarize requires --transcribe or --translate")

    if args.npu_model_dir is not None and args.language is None:
        parser.error(
            "--npu-model-dir requires --language - the Intel NPU "
            "backend cannot auto-detect the spoken language"
        )

    if args.srt and not (args.transcribe or args.translate is not None):
        parser.error("--srt requires --transcribe or --translate")

    # --language/--translate accept either the 2-letter code Whisper
    # and argos-translate use, or the 3-letter code generated
    # filenames use - normalize to the 2-letter form once, here, so
    # every call site downstream can assume that form.
    if args.language is not None:
        args.language = normalize_language(args.language)

    if args.translate is not None:
        args.translate = normalize_language(args.translate)

    # --model-size defaults to None (rather than a fixed string) so it
    # can be resolved here, once, against whether this machine actually
    # has a GPU - Christer: "I would like medium or large model default
    # if you have a gpu", after confirming on his own hardware that a
    # `large` model transcribes about as fast as `small` used to (see
    # WORKING_CONTEXT.md's cuBLAS DLL entries). Only calls
    # gpu_available() (which imports ctranslate2) when a Whisper action
    # is actually going to run - --extract-audio/--get-duration-only
    # invocations never pay for that import at all. The NPU path
    # doesn't use model_size, but there's no harm leaving it resolved
    # for that case too.
    if args.model_size is None:
        if args.npu_model_dir is None and (
            args.transcribe or args.translate is not None
        ):
            args.model_size = "large" if gpu_available() else "small"
        else:
            args.model_size = "small"

    return args


def _interactive() -> bool:
    """Return True if running attached to a real terminal, on the
    main thread.

    sys.stdin/sys.stdout are process-wide, not per-thread - if bv-web's
    own server process happens to be launched attached to a real
    terminal (a native, non-Docker setup: `bv-web serve ...` typed
    directly into a console), isatty() returns True even inside a
    background job thread, where there is no one actually watching
    that console for this specific prompt. Without the main-thread
    check below, _should_write() then calls input() on that thread,
    which blocks forever - the job's own output box stays empty (the
    prompt text goes to the server's raw console, not through
    say()/warn()), no error is ever raised, and the job is stuck
    showing "Running" indefinitely. Confirmed as the real cause of a
    bv-scribe web job that looked hung with zero output and no GPU
    usage - see WORKING_CONTEXT.md (same fix applied to bv_scribe.py's
    own _interactive()). Requiring the main thread too means only a
    genuine direct CLI invocation (always main-thread) can hit the
    interactive prompt; every bv-web job (always a background thread)
    now safely falls through to the warn()+skip branch instead."""

    return (
        sys.stdin.isatty()
        and sys.stdout.isatty()
        and threading.current_thread() is threading.main_thread()
    )


def _default_warn(message: str) -> None:
    """Default `warn` for every function below that takes one - real
    stderr, the CLI's normal error-output contract. Every `say`/`warn`
    -taking function in this module defaults to real stdout/stderr
    (print/`_default_warn`), all the way down the call tree - the same
    "every function gets the same real-io defaults" pattern
    bv_config.py's own `prompt`/`edit_endpoints`/`run_wizard`/`_run`
    already use - so bv-web's job runner (see web/jobs.py) only has to
    override `say`/`warn` at the single top-level `_run()` call for
    every helper underneath to pick it up too, while direct callers
    (including this project's own tests) that don't pass them still
    get real print/stderr unchanged. See bv_gps.py's own
    `_default_warn` for why this is a named function rather than a
    lambda."""

    print(message, file=sys.stderr)


def _requested_lock_assets(args: argparse.Namespace) -> set[str]:
    """The core/lock.py asset names this run's action flags correspond
    to - the same vocabulary bv-lock's --lock-assets/--unlock-assets
    accept (core.lock.LOCKABLE_ASSETS). --translate maps to the single
    "translate" name regardless of target language - see
    core/lock.py's own module docstring for why. --diarize is its own
    name (not folded into "transcribe"/"translate") so a range locked
    without it still lets a later --diarize-only re-run through."""

    requested = set()
    if args.extract_audio:
        requested.add("extract-audio")
    if args.get_duration:
        requested.add("get-duration")
    if args.thumbnail:
        requested.add("thumbnail")
    if args.transcribe:
        requested.add("transcribe")
    if args.translate is not None:
        requested.add("translate")
    if args.srt:
        requested.add("srt")
    if args.describe_scene:
        requested.add("describe-scene")
    if args.diarize:
        requested.add("diarize")
    return requested


class _OverwriteDecision:
    """Caches the interactive "overwrite existing files?" answer for
    a whole bv-generate run, so it's asked once - on the first
    existing file encountered - instead of once per file. One
    instance is created per run() call and threaded through every
    _should_write() call via args.overwrite_decision.
    """

    def __init__(self) -> None:
        self._answered = False
        self._overwrite = False

    def __call__(self, path: Path) -> bool:
        if not self._answered:
            answer = input(
                f"{path.name} already exists. Overwrite this and any "
                "other existing files for the rest of this run? [y/N] "
            ).strip().lower()
            self._overwrite = answer in ("y", "yes")
            self._answered = True

        return self._overwrite


def _should_write(
    path: Path,
    *,
    overwrite: bool,
    dry_run: bool,
    warn=_default_warn,
    overwrite_decision: "_OverwriteDecision | None" = None,
) -> bool:
    """Decide whether to (re)generate an output file.

    - Missing file: always write.
    - Existing file with --overwrite: always rewrite.
    - Existing file, interactive terminal, no --overwrite: ask (once
      per run if overwrite_decision is given, otherwise once per call).
    - Existing file, non-interactive (batch/cron), no --overwrite: skip.
    - Dry-run never prompts; it only reports what it would do.
    """

    if not path.exists():
        return True

    if overwrite:
        return True

    if dry_run:
        return False

    if _interactive():
        if overwrite_decision is not None:
            return overwrite_decision(path)

        answer = input(
            f"{path.name} already exists. Overwrite? [y/N] "
        ).strip().lower()
        return answer in ("y", "yes")

    warn(
        f"bv-generate: {path.name}: already exists, skipping "
        "(use --overwrite)"
    )
    return False


def _should_write_for(path: Path, args: argparse.Namespace, *, warn=_default_warn) -> bool:
    """_should_write(), reading overwrite/dry_run/the shared
    per-run overwrite decision straight from args - the common case
    for every call site below."""

    return _should_write(
        path,
        overwrite=args.overwrite,
        dry_run=args.dry_run,
        warn=warn,
        overwrite_decision=getattr(args, "overwrite_decision", None),
    )


def _has_usable_audio(path: Path) -> bool:
    """Return True only if an already-extracted `.aac` at `path` is
    actually worth reusing, not just present.

    extract_audio() (generate/media.py) now cleans up after itself on
    failure, but archives written before that fix - or a file deleted
    or truncated by something outside bv-generate entirely - can still
    have a 0-byte or otherwise empty `.aac` sitting on disk. Treating
    that the same as "not extracted yet" lets a stuck recording
    self-heal on the very next run instead of failing forever on a
    corrupt cached file - the same self-healing discipline
    load_or_compute_duration() already applies to `.duration.txt`
    (generate/media.py).
    """

    try:
        return path.stat().st_size > 0
    except OSError:
        return False


def _is_audio_silent_safe(path: Path, recording_id, warn) -> bool:
    """is_audio_silent(), but never lets a probe failure (e.g. ffmpeg
    missing) propagate out of a bv-generate call site.

    extract_audio() just succeeded when this is called, which normally
    means ffmpeg is available - but tests and unusual environments can
    still hit this, and a probe failure is no reason to throw away
    audio that might be real. Same "err toward keeping it" default
    is_audio_silent() itself uses for an unparseable result.
    """

    try:
        return is_audio_silent(path)
    except MediaToolError as exc:
        warn(f"bv-generate: {recording_id}: couldn't check audio "
            f"loudness, keeping it: {exc}")
        return False


def _report(say, verbose: bool, message: str) -> None:
    if verbose:
        say(message)


def _language_suffixed_name(
    recording_id,
    language: str,
    suffix: str,
    *,
    diarized: bool = False,
) -> str:
    """Build a generated filename.

    '<id>.<suffix>' for the default language, '<id>_<lang>.<suffix>'
    for any other (<lang> is a 3-letter code, e.g. 'swe', 'tha').
    When diarized is True, '.diarized' is inserted before <suffix>
    (e.g. '<id>.diarized.transcript.txt'), so a diarized and a plain
    version of the same recording can coexist.
    """

    name = str(recording_id)

    if language.strip().lower() != DEFAULT_LANGUAGE:
        name += f"_{short_code(language)}"

    if diarized:
        name += ".diarized"

    return f"{name}.{suffix}"


def _language_from_generated_filename(
    recording_id, filename: str, suffix: str
) -> str:
    """Recover the language _language_suffixed_name encoded.

    '<id>.<suffix>' -> DEFAULT_LANGUAGE
    '<id>_<code>.<suffix>' -> the 2-letter form of <code>
    '<id>[_<code>].diarized.<suffix>' -> same, ignoring the marker
    """

    stem = filename[len(str(recording_id)):]
    stem = stem[: -(len(suffix) + 1)]  # drop the trailing '.<suffix>'
    stem = stem.removesuffix(".diarized")

    if not stem:
        return DEFAULT_LANGUAGE

    return normalize_language(stem.lstrip("_"))


def _do_extract_audio(
    recording: Recording,
    archive_path: Path,
    args: argparse.Namespace,
    *,
    say=print,
    warn=_default_warn,
) -> bool:
    """Extract audio for one recording. Return True on error."""

    if recording.id.is_parking:
        warn(f"bv-generate: {recording.id}: parking-mode (timelapse) "
            "recording has no audio, skipping")
        return False

    if recording_is_photo(recording):
        warn(f"bv-generate: {recording.id}: photo has no audio, skipping")
        return False

    destination = archive_path / f"{recording.id}.aac"

    if not _should_write_for(destination, args, warn=warn):
        return False

    source_file = select_source(recording)
    if source_file is None:
        warn(f"bv-generate: {recording.id}: no front or rear video, "
            "skipping audio extraction")
        return True

    # Checked up front, same as the parking-mode/photo checks above:
    # a clip with zero embedded audio streams (Christer's real case -
    # stock/downloaded clips mixed into a GoPro archive, confirmed via
    # `v-ls --all`'s blank Aud column) is exactly as unextractable as
    # a photo or a parking-mode timelapse, just discovered later.
    # Without this, extract_audio() still runs ffmpeg anyway (it only
    # uses probe_audio_codec() internally to pick copy-vs-transcode,
    # not to skip), which fails with a real but noisy multi-line dump
    # ("Output file does not contain any stream" / "Error opening
    # output file ... Invalid argument") - and bv_generate.py counted
    # it as a real error (had_error=True) for a condition that isn't
    # one. speech.py's detect_language()/transcribe() already skip
    # this cleanly via the same probe_audio_codec() check (task #928);
    # this brings extract-audio in line with that precedent instead of
    # leaving it as the one action that still surfaces raw ffmpeg
    # noise and a spurious non-zero exit code for a perfectly ordinary
    # audio-less video.
    try:
        has_audio_stream = probe_audio_codec(source_file.path) is not None
    except MediaToolError:
        # A corrupted/truncated source (e.g. ffprobe's "moov atom not
        # found" on a partially-downloaded file - a real report from
        # Christer's archive) used to propagate straight out of here
        # uncaught, crashing the whole bv-generate run instead of just
        # this one recording. trip_export.py's own audio-extraction
        # pass already treats a probe failure this same way: assume
        # there's audio and let extract_audio() below (already
        # try/except MediaToolError-guarded) attempt the real thing and
        # report on whatever it actually runs into, rather than
        # silently giving up - or crashing - on a probe-only failure.
        has_audio_stream = True

    if not has_audio_stream:
        warn(f"bv-generate: {recording.id}: no audio stream, skipping")
        return False

    if args.dry_run:
        say(f"{recording.id}: would extract audio from "
            f"{source_file.name} -> {destination.name}")
        return False

    try:
        extract_audio(source_file.path, destination)
    except MediaToolError as exc:
        warn(f"bv-generate: {recording.id}: {exc}")
        return True

    if _is_audio_silent_safe(destination, recording.id, warn):
        destination.unlink(missing_ok=True)
        _report(
            say,
            args.verbose,
            f"{recording.id}: audio track is silent, skipping -> "
            f"{destination.name}",
        )
        return False

    _report(say, args.verbose, f"{recording.id}: extracted audio -> {destination.name}")
    return False


def _do_get_duration(
    recording: Recording,
    archive_path: Path,
    args: argparse.Namespace,
    *,
    say=print,
    warn=_default_warn,
) -> bool:
    """Compute and report the span for one recording. Return True on error."""

    source_file = select_source(recording)
    if source_file is None:
        warn(f"bv-generate: {recording.id}: no front or rear video, "
            "skipping duration")
        return True

    try:
        span = get_span(recording.id, source_file.path)
    except MediaToolError as exc:
        warn(f"bv-generate: {recording.id}: {exc}")
        return True

    say(f"{recording.id}: {span}s")

    destination = archive_path / f"{recording.id}.duration.txt"

    if not _should_write_for(destination, args, warn=warn):
        return False

    if args.dry_run:
        say(f"{recording.id}: would write {destination.name}")
        return False

    destination.write_text(f"{span}\n", encoding="utf-8")
    _report(say, args.verbose, f"{recording.id}: wrote {destination.name}")
    return False


def _do_thumbnail(
    recording: Recording,
    archive_path: Path,
    args: argparse.Namespace,
    *,
    say=print,
    warn=_default_warn,
) -> bool:
    """Generate and write one recording's permanent thumbnail sidecar
    (<recording>.thumb.jpg, the Asset.THUMBNAIL generated asset - see
    archive/asset.py). Return True on error.

    Skipped (not an error) for a photo recording - recording_is_photo()
    already treats the photo itself as its own thumbnail, the same
    check web/archive_browser.py's thumbnail_path() makes - or one
    that already has a real camera-native `*_THUMBNAIL` sidecar
    (FRONT_THUMBNAIL/REAR_THUMBNAIL/INTERIOR_THUMBNAIL): that
    function's own fallback chain always prefers a native sidecar over
    a generated frame-grab, so writing one here would just be wasted
    work that's never actually served. In practice this makes
    --thumbnail a no-op for ordinary BlackVue archives and a real
    generator only for FolderAdapter/GoProAdapter archives, which is
    the whole point - see CAMERA_ADAPTERS.md.

    Once written, this is a normal, permanent archive asset like
    .aac or .duration.txt - not a separate app-level cache. bv-web's
    archive browser writes the exact same file itself, at the same
    path, if a recording is viewed before this action has ever run for
    it (see web/archive_browser.py's ArchiveRecording.thumbnail_path()),
    so running this ahead of time only saves paying that cost on first
    view - it isn't required for thumbnails to work at all."""

    if recording_is_photo(recording):
        return False

    if (
        recording.has(Asset.FRONT_THUMBNAIL)
        or recording.has(Asset.REAR_THUMBNAIL)
        or recording.has(Asset.INTERIOR_THUMBNAIL)
    ):
        return False

    source_file = select_source(recording)
    if source_file is None:
        warn(f"bv-generate: {recording.id}: no front or rear video, "
            "skipping thumbnail")
        return True

    destination = archive_path / f"{recording.id}.thumb.jpg"

    if not _should_write_for(destination, args, warn=warn):
        return False

    if args.dry_run:
        say(f"{recording.id}: would extract thumbnail from "
            f"{source_file.name} -> {destination.name}")
        return False

    try:
        extract_video_thumbnail(source_file.path, destination)
    except MediaToolError as exc:
        warn(f"bv-generate: {recording.id}: {exc}")
        return True

    _report(say, args.verbose, f"{recording.id}: wrote {destination.name}")
    return False


def _run_describe_scene_pass(
    recording: Recording,
    video_path: Path,
    destination: Path,
    args: argparse.Namespace,
    *,
    task: str | None = None,
    say=print,
    warn=_default_warn,
) -> bool:
    """Run one describe_scene() call and write its result. Return True
    on error. Shared by both the front and rear passes of
    _do_describe_scene() - task=None uses SceneOptions' own default
    ("both": description + OCR); the --camera both rear bonus pass
    forces task="ocr" instead, since a rear-camera description would
    mostly just restate the front one's (see --camera's help text)."""

    if not _should_write_for(destination, args, warn=warn):
        return False

    if args.dry_run:
        say(f"{recording.id}: would describe scene -> {destination.name}")
        return False

    kwargs = {
        "model": args.scene_model,
        "force_cpu": args.cpu,
        "quantize": args.scene_quantize,
        "gpu_memory_fraction": args.scene_gpu_memory_fraction,
    }
    if task is not None:
        kwargs["task"] = task

    # Parking-mode video has an empty, broken audio track that trips
    # strict container validation - ffmpeg/libavformat then log
    # "contradictionary STSC and STCO" / "error reading header"
    # straight to stderr (describe_scene()'s own video decoding, via
    # qwen_vl_utils -> decord/ffmpeg, isn't wrapped in this project's
    # own subprocess capture the way media.py's probe()/extract_audio()
    # are, so those lines leak to the real terminal instead of being
    # caught cleanly). The video track itself is fine - only the
    # container's bookkeeping for the unused audio track is broken -
    # so swap in a repaired, cached copy first. Same fix already used
    # by web/app.py's video-serving route and export/trip_export.py's
    # own pipeline; load_or_repair_parking_video() falls back to
    # `video_path` unchanged for anything outside the one narrow,
    # confirmed pattern it knows how to fix, so this is always safe to
    # try.
    if recording.id.is_parking:
        cache_dir = default_config_dir() / ".parking_repair_cache"
        video_path = load_or_repair_parking_video(video_path, cache_dir)

    try:
        output_text = describe_scene(video_path, **kwargs)
    except MediaToolError as exc:
        warn(f"bv-generate: {recording.id}: {exc}")
        return True

    destination.write_text(output_text + "\n", encoding="utf-8")
    _report(say, args.verbose, f"{recording.id}: wrote {destination.name}")
    return False


def _do_describe_scene(
    recording: Recording,
    archive_path: Path,
    args: argparse.Namespace,
    *,
    say=print,
    warn=_default_warn,
) -> bool:
    """Describe one recording's scene/on-screen text, for whichever
    camera(s) --camera selects. Return True on error. Unlike audio
    actions, this runs on Parking-mode recordings too - they're
    timelapse video, not audio, so there's real content for a vision
    model to look at."""

    front_file = recording.file(Asset.FRONT)
    rear_file = recording.file(Asset.REAR)
    had_error = False

    if args.camera in ("front", "both"):
        # Same front-preferred-with-rear-fallback source selection
        # select_source() itself uses - kept inline here (rather than
        # calling select_source()) so front_file/rear_file, already
        # looked up above for the rear pass below, aren't looked up
        # twice.
        source_file = front_file or rear_file
        if source_file is None:
            warn(f"bv-generate: {recording.id}: no front or rear video, "
                "skipping scene description")
            had_error = True
        else:
            had_error |= _run_describe_scene_pass(
                recording, source_file.path,
                archive_path / f"{recording.id}.scene.txt",
                args, say=say, warn=warn,
            )

    if args.camera in ("rear", "both"):
        if rear_file is None:
            if args.camera == "rear":
                warn(f"bv-generate: {recording.id}: no rear video, "
                    "skipping scene description")
                had_error = True
            else:
                _report(say, args.verbose,
                    f"{recording.id}: no rear video, skipping rear "
                    "scene pass")
        elif args.camera == "both" and front_file is None:
            # The front pass above already used rear_file as its own
            # fallback source (select_source()'s own behavior) - a
            # second pass on the exact same video would just
            # duplicate that work under a different filename.
            _report(say, args.verbose,
                f"{recording.id}: no distinct rear video (front pass "
                "already used it as its own fallback), skipping rear "
                "scene pass")
        else:
            had_error |= _run_describe_scene_pass(
                recording, rear_file.path,
                archive_path / f"{recording.id}.rear.scene.txt",
                args,
                task="ocr" if args.camera == "both" else None,
                say=say, warn=warn,
            )

    return had_error


def _translate_diarized(
    text: str,
    *,
    source_language: str,
    target_language: str,
) -> str:
    """Translate a '[SPEAKER_XX] text' transcript line by line.

    Only the spoken text of each line is sent to the translator; the
    speaker label is preserved as-is.
    """

    lines = []

    for line in text.splitlines():
        match = _SPEAKER_LINE.match(line)

        if match is None:
            lines.append(
                translate(
                    line,
                    source_language=source_language,
                    target_language=target_language,
                )
            )
            continue

        translated = translate(
            match.group("text"),
            source_language=source_language,
            target_language=target_language,
        )
        lines.append(f"[{match.group('speaker')}] {translated}")

    return "\n".join(lines)


def _translate_segments(
    segments: tuple[SpeechSegment, ...],
    *,
    source_language: str,
    target_language: str,
) -> tuple[SpeechSegment, ...]:
    """Translate each segment's text individually, keeping its own
    start/end timing intact - the per-segment analogue of translate()
    (whole text) and _translate_diarized() (line-by-line "[SPEAKER_XX]
    text") above.

    Used so --srt reflects --translate's target language instead of
    always being in the transcript's original spoken language - the
    whole point of asking for subtitles alongside a translation is to
    get them in the target language. Diarization speaker labels
    aren't part of segment.text (format_srt() adds them separately via
    speaker_for(), matched by each segment's own start/end, which this
    leaves untouched), so this works the same whether or not --diarize
    was also given.
    """

    return tuple(
        SpeechSegment(
            start=segment.start,
            end=segment.end,
            text=translate(
                segment.text,
                source_language=source_language,
                target_language=target_language,
            ),
        )
        for segment in segments
    )


def _do_transcribe_and_translate(
    recording: Recording,
    archive_path: Path,
    args: argparse.Namespace,
    *,
    say=print,
    warn=_default_warn,
) -> bool:
    """Transcribe and/or translate one recording. Return True on error."""

    if recording.id.is_parking:
        warn(f"bv-generate: {recording.id}: parking-mode (timelapse) "
            "recording has no audio, skipping")
        return False

    if recording_is_photo(recording):
        warn(f"bv-generate: {recording.id}: photo has no audio, skipping")
        return False

    if args.transcribe:
        return _do_transcribe_with_optional_translate(
            recording, archive_path, args, say=say, warn=warn
        )

    if args.translate is not None:
        return _do_translate_only(recording, archive_path, args, say=say, warn=warn)

    return False


def _do_translate_only(
    recording: Recording,
    archive_path: Path,
    args: argparse.Namespace,
    *,
    say=print,
    warn=_default_warn,
) -> bool:
    """Handle --translate without --transcribe.

    Reuses whatever's already been generated for this recording,
    cheapest first: an existing transcript, then already-extracted
    audio, then the source video. If it ends up doing the full
    extract+transcribe pipeline from scratch, it leaves the .aac and
    .transcript.txt files behind too, so the next run (of this or
    --transcribe) doesn't redo that work.

    Exception: if --srt is given and actually needs (re)writing, the
    existing-transcript reuse is skipped and Whisper always runs
    fresh - a cached plain-text transcript has no per-segment timing,
    so reusing it would produce no subtitles at all.
    """

    translation_destination = archive_path / _language_suffixed_name(
        recording.id,
        args.translate,
        "translation.txt",
        diarized=args.diarize,
    )
    need_translation_write = _should_write_for(translation_destination, args, warn=warn)

    # Computed up front (like _do_transcribe_with_optional_translate)
    # so a missing/needs-refresh .srt alone is enough to keep this
    # recording from being skipped, even when translation.txt itself
    # is already up to date - that's the bug Christer hit:
    # translation.txt already existed from an earlier run, so the old
    # single-destination gate below returned early before ever
    # reaching the srt-writing code.
    srt_destination = archive_path / f"{recording.id}.srt" if args.srt else None
    need_srt_write = (
        _should_write_for(srt_destination, args, warn=warn) if args.srt else False
    )

    if args.dry_run:
        if need_translation_write:
            say(f"{recording.id}: would translate -> "
                f"{translation_destination.name}")
        if need_srt_write:
            say(f"{recording.id}: would write {srt_destination.name}")
        return False

    if not (need_translation_write or need_srt_write):
        return False

    transcript_text: str | None = None
    transcript_language: str | None = None
    segments: tuple = ()
    turns: tuple = ()

    # 1. An existing transcript already has everything translation
    #    needs, so reuse it instead of re-running Whisper. Diarized
    #    and plain transcripts are tracked as separate assets, so
    #    this looks at whichever one matches what this run wants.
    #    Skipped entirely when an .srt actually needs writing: a
    #    cached plain-text transcript has no per-segment timing, so
    #    reusing it would silently produce no subtitles - forcing a
    #    fresh transcribe() is the only way to actually satisfy that.
    want_segment_timing = need_srt_write
    existing_transcript = (
        None
        if want_segment_timing
        else recording.file(
            Asset.TRANSCRIPT_DIARIZED if args.diarize else Asset.TRANSCRIPT
        )
    )

    if existing_transcript is not None:
        transcript_language = _language_from_generated_filename(
            recording.id, existing_transcript.name, "transcript.txt"
        )
        transcript_text = existing_transcript.path.read_text(
            encoding="utf-8"
        ).strip()
        _report(say, args.verbose, f"{recording.id}: reusing {existing_transcript.name}")

    if transcript_text is None:
        # 2. Reuse already-extracted audio, or extract it fresh and
        #    leave it behind.
        audio_file = recording.file(Asset.AUDIO)

        if audio_file is not None and _has_usable_audio(audio_file.path):
            audio_source = audio_file.path
        else:
            video_source = select_source(recording)

            if video_source is None:
                warn(f"bv-generate: {recording.id}: no audio or video "
                    "source, skipping translation")
                return True

            # Same upfront check as _do_extract_audio()/
            # _do_transcribe_with_optional_translate(): a real video
            # with zero audio streams at all is a normal, non-error
            # skip, not a failure - matches the parking-mode/photo
            # skips already at the top of _do_transcribe_and_translate().
            #
            # Wrapped in try/except MediaToolError for the same reason
            # as _do_extract_audio()'s own probe (see its comment): a
            # corrupted/truncated source makes ffprobe itself fail
            # (e.g. "moov atom not found"), which used to propagate
            # straight out of here uncaught and crash the whole
            # bv-generate run on this one bad recording. Assume audio
            # is present and let extract_audio() just below (already
            # try/except-guarded) attempt the real thing and report on
            # whatever it actually runs into.
            try:
                has_audio_stream = probe_audio_codec(video_source.path) is not None
            except MediaToolError:
                has_audio_stream = True

            if not has_audio_stream:
                warn(f"bv-generate: {recording.id}: no audio stream, "
                    "skipping translation")
                return False

            audio_destination = archive_path / f"{recording.id}.aac"

            try:
                extract_audio(video_source.path, audio_destination)
            except MediaToolError as exc:
                warn(f"bv-generate: {recording.id}: {exc}")
                return True

            if _is_audio_silent_safe(audio_destination, recording.id, warn):
                audio_destination.unlink(missing_ok=True)
                warn(f"bv-generate: {recording.id}: audio track is "
                    "silent, skipping translation")
                return False

            _report(say, args.verbose, f"{recording.id}: extracted audio -> "
                f"{audio_destination.name}")
            audio_source = audio_destination

        # 3. Transcribe, and leave the transcript behind too.
        try:
            transcript = transcribe(
                audio_source,
                language=args.language,
                model_size=args.model_size,
                npu_model_dir=args.npu_model_dir,
                force_cpu=args.cpu,
            )
        except MediaToolError as exc:
            warn(f"bv-generate: {recording.id}: {exc}")
            return True

        transcript_text = transcript.text
        transcript_language = transcript.language
        segments = transcript.segments

        if not transcript_text.strip():
            # Whisper found no actual speech - a non-silent (by
            # mean-volume) track can still have nothing to transcribe,
            # e.g. road/wind/engine noise or the camera's own short
            # voice prompts ("Parking mode off") that get picked up
            # but leave nothing for a *different* clip to say. Forcing
            # a language guess onto near-nothing also tends to produce
            # a wrong one (Christer hit this: a real, sizable .aac
            # with no speech in it got tagged "_nno" and an empty
            # transcript.txt/.srt written anyway). Bail out before any
            # of that gets written rather than leave junk files with a
            # bogus language suffix behind.
            warn(f"bv-generate: {recording.id}: no speech detected, "
                "skipping translation")
            return False

        if args.diarize:
            try:
                turns = diarize(audio_source, hf_token=args.hf_token)
                transcript_text = format_diarized_transcript(
                    segments, turns
                )
            except MediaToolError as exc:
                warn(f"bv-generate: {recording.id}: {exc}")
                return True

        transcript_destination = archive_path / _language_suffixed_name(
            recording.id,
            transcript_language,
            "transcript.txt",
            diarized=args.diarize,
        )

        if _should_write_for(transcript_destination, args, warn=warn):
            transcript_destination.write_text(
                transcript_text + "\n", encoding="utf-8"
            )
            _report(say, args.verbose, f"{recording.id}: wrote {transcript_destination.name}")

        # SRT needs per-segment timing, which only exists right after
        # a fresh transcribe() call (this branch) - a reused cached
        # transcript (above) has no segments to draw from, so this is
        # deliberately skipped in that case. need_srt_write was
        # already computed up front, before it was known whether this
        # branch would even run - reused here rather than
        # re-checking _should_write a second time.
        #
        # This whole function only ever runs with --translate given
        # (see _do_transcribe_and_translate's dispatch above), so
        # unlike _do_transcribe_with_optional_translate's own SRT
        # block, there's no "no translation requested" case to weigh
        # against - subtitles here always reflect args.translate's
        # target language, never the original spoken one.
        if need_srt_write:
            try:
                subtitle_segments = _translate_segments(
                    segments,
                    source_language=transcript_language,
                    target_language=args.translate,
                )
            except MediaToolError as exc:
                warn(f"bv-generate: {recording.id}: {exc}")
                return True

            srt_destination.write_text(
                format_srt(subtitle_segments, turns) + "\n",
                encoding="utf-8",
            )
            _report(say, args.verbose, f"{recording.id}: wrote {srt_destination.name}")

    # Gated on need_translation_write, not just "did we get this far":
    # this point is also reached when only --srt needed (re)writing
    # and translation.txt was already up to date - without
    # this check, that case would re-translate and silently overwrite
    # an already-good translation.txt, bypassing the overwrite policy
    # for a file that didn't need touching.
    if need_translation_write:
        translate_fn = _translate_diarized if args.diarize else translate

        try:
            translated = translate_fn(
                transcript_text,
                source_language=transcript_language,
                target_language=args.translate,
            )
        except MediaToolError as exc:
            warn(f"bv-generate: {recording.id}: {exc}")
            return True

        translation_destination.write_text(translated + "\n", encoding="utf-8")
        _report(say, args.verbose, f"{recording.id}: wrote {translation_destination.name}")

    return False


def _do_transcribe_with_optional_translate(
    recording: Recording,
    archive_path: Path,
    args: argparse.Namespace,
    *,
    say=print,
    warn=_default_warn,
) -> bool:
    """Handle --transcribe, optionally with --translate alongside it.

    Always runs Whisper fresh (subject to the normal overwrite/skip
    policy on the transcript file itself) and reuses that one run's
    output for translation too, rather than the cache-first approach
    _do_translate_only uses.
    """

    want_transcript_file = args.transcribe
    want_translation_file = args.translate is not None

    _existing_audio = recording.file(Asset.AUDIO)
    source_file = (
        _existing_audio
        if _existing_audio is not None and _has_usable_audio(_existing_audio.path)
        else select_source(recording)
    )

    # Checked up front against whatever source_file actually resolved
    # to (an already-extracted .aac, or the raw video) - a real video
    # with zero audio streams at all (not just a silent one, which
    # is_audio_silent() below already handles gracefully) can't
    # produce a transcript no matter what. Without this,
    # detect_language() below already fails cleanly on its own (task
    # #928's probe_audio_codec() pre-check inside speech.py), but this
    # function still treated that clean failure as a real error
    # (had_error=True) - the same normal, non-error condition photos/
    # parking-mode recordings get a free pass on above in the sibling
    # _do_transcribe_and_translate(). Bailing out here, before any of
    # the write-target/dry-run bookkeeping below, keeps this function
    # consistent with that precedent instead of only being "clean" in
    # its error message and not in its exit status.
    if source_file is not None:
        # Wrapped in try/except MediaToolError for the same reason as
        # _do_extract_audio()'s own probe: a corrupted/truncated
        # source makes ffprobe itself fail (e.g. "moov atom not
        # found"), which used to propagate straight out of here
        # uncaught and crash the whole bv-generate run on this one bad
        # recording. Assume audio is present and let transcribe()
        # below (which does its own probe_audio_codec() check inside
        # speech.py and raises a clean MediaToolError that's already
        # caught further down) attempt the real thing instead.
        try:
            has_audio_stream = probe_audio_codec(source_file.path) is not None
        except MediaToolError:
            has_audio_stream = True

        if not has_audio_stream:
            warn(f"bv-generate: {recording.id}: no audio stream, skipping transcription")
            return False

    # The translation filename only depends on the (already known)
    # --translate target, so it can be checked without touching
    # Whisper at all.
    translation_destination = None
    need_translation_write = False

    if want_translation_file:
        translation_destination = archive_path / _language_suffixed_name(
            recording.id,
            args.translate,
            "translation.txt",
            diarized=args.diarize,
        )
        need_translation_write = _should_write_for(translation_destination, args, warn=warn)

    # SRT's filename doesn't depend on language, so - like the
    # translation destination above - it can be checked up front.
    srt_destination = None
    need_srt_write = False

    if args.srt:
        srt_destination = archive_path / f"{recording.id}.srt"
        need_srt_write = _should_write_for(srt_destination, args, warn=warn)

    # The transcript filename depends on the *spoken* language. If
    # --language was given, that's already known. Otherwise it has
    # to be detected first - cheaply, so a recording that's already
    # been transcribed doesn't pay for a full re-transcription just
    # to find out its own output already exists. This detect_language()
    # call always uses faster-whisper regardless of --npu-model-dir -
    # but parse_args() already requires --language whenever
    # --npu-model-dir is given (the NPU backend can't auto-detect), so
    # transcript_language is never None here in that case and this
    # branch is naturally never reached.
    transcript_destination = None
    need_transcript_write = False
    transcript_language = args.language

    if want_transcript_file:
        if args.dry_run:
            if transcript_language is not None:
                transcript_destination = (
                    archive_path
                    / _language_suffixed_name(
                        recording.id,
                        transcript_language,
                        "transcript.txt",
                        diarized=args.diarize,
                    )
                )
                if _should_write_for(transcript_destination, args, warn=warn):
                    say(f"{recording.id}: would transcribe -> "
                        f"{transcript_destination.name}")
            else:
                say(f"{recording.id}: would transcribe -> "
                    f"{recording.id}[_<lang>].transcript.txt "
                    "(language auto-detected)")
        else:
            if transcript_language is None:
                if source_file is None:
                    warn(f"bv-generate: {recording.id}: no audio or video "
                        "source, skipping transcription")
                    return True

                try:
                    transcript_language = detect_language(
                        source_file.path,
                        model_size=args.model_size,
                        force_cpu=args.cpu,
                    )
                except MediaToolError as exc:
                    warn(f"bv-generate: {recording.id}: {exc}")
                    return True

            transcript_destination = archive_path / _language_suffixed_name(
                recording.id,
                transcript_language,
                "transcript.txt",
                diarized=args.diarize,
            )
            need_transcript_write = _should_write_for(transcript_destination, args, warn=warn)

    if args.dry_run:
        if need_translation_write:
            say(f"{recording.id}: would translate -> "
                f"{translation_destination.name}")
        if need_srt_write:
            say(f"{recording.id}: would write {srt_destination.name}")
        return False

    if not (
        need_transcript_write
        or need_translation_write
        or need_srt_write
    ):
        return False

    if source_file is None:
        warn(f"bv-generate: {recording.id}: no audio or video source, "
            "skipping transcription")
        return True

    # Reuse the .aac if one's already on disk (whether tracked from
    # the archive scan, or just written a moment ago by
    # --extract-audio earlier in this same run). Otherwise extract
    # it from the video once and leave it behind, same as
    # _do_translate_only does, instead of decoding the video
    # directly every time.
    audio_destination = archive_path / f"{recording.id}.aac"

    if _has_usable_audio(audio_destination):
        audio_source = audio_destination
    else:
        try:
            extract_audio(source_file.path, audio_destination)
        except MediaToolError as exc:
            warn(f"bv-generate: {recording.id}: {exc}")
            return True

        if _is_audio_silent_safe(audio_destination, recording.id, warn):
            audio_destination.unlink(missing_ok=True)
            warn(f"bv-generate: {recording.id}: audio track is silent, "
                "skipping transcription")
            return False

        _report(say, args.verbose, f"{recording.id}: extracted audio -> {audio_destination.name}")
        audio_source = audio_destination

    try:
        transcript = transcribe(
            audio_source,
            language=transcript_language,
            model_size=args.model_size,
            npu_model_dir=args.npu_model_dir,
            force_cpu=args.cpu,
        )
    except MediaToolError as exc:
        warn(f"bv-generate: {recording.id}: {exc}")
        return True

    had_error = False
    transcript_text = transcript.text
    turns: tuple = ()

    if not transcript_text.strip():
        # See the matching check in _do_translate_only for the full
        # explanation - a track that isn't silent by mean-volume can
        # still have no actual speech in it, and forcing a language
        # guess onto near-nothing tends to produce a wrong one. Bail
        # out before writing a transcript/srt/translation file with a
        # bogus language suffix and empty content.
        warn(f"bv-generate: {recording.id}: no speech detected, "
            "skipping")
        return False

    if args.diarize:
        try:
            turns = diarize(audio_source, hf_token=args.hf_token)
            transcript_text = format_diarized_transcript(
                transcript.segments, turns
            )
        except MediaToolError as exc:
            warn(f"bv-generate: {recording.id}: {exc}")
            return True

    if need_transcript_write:
        transcript_destination.write_text(
            transcript_text + "\n", encoding="utf-8"
        )
        _report(say, args.verbose, f"{recording.id}: wrote {transcript_destination.name}")

    # SRT reflects --translate's target language when it was given,
    # not the original spoken one - matching bv-generate.md's own
    # "transcribe and translate, with subtitles" example. The whole
    # point of asking for subtitles alongside a translation is to get
    # them in the target language; translating each segment
    # individually (rather than reusing transcript_text below) keeps
    # each cue's own start/end timing intact. Computed once here so a
    # failure is reported once, not duplicated by the whole-text
    # translation further down.
    subtitle_segments = transcript.segments
    subtitle_translation_failed = False

    if want_translation_file and need_srt_write:
        try:
            subtitle_segments = _translate_segments(
                transcript.segments,
                source_language=transcript.language,
                target_language=args.translate,
            )
        except MediaToolError as exc:
            warn(f"bv-generate: {recording.id}: {exc}")
            had_error = True
            subtitle_translation_failed = True

    if need_srt_write and not subtitle_translation_failed:
        srt_destination.write_text(
            format_srt(subtitle_segments, turns) + "\n", encoding="utf-8"
        )
        _report(say, args.verbose, f"{recording.id}: wrote {srt_destination.name}")

    if need_translation_write:
        translate_fn = (
            _translate_diarized if args.diarize else translate
        )

        try:
            translated = translate_fn(
                transcript_text,
                source_language=transcript.language,
                target_language=args.translate,
            )
        except MediaToolError as exc:
            warn(f"bv-generate: {recording.id}: {exc}")
            had_error = True
        else:
            translation_destination.write_text(
                translated + "\n", encoding="utf-8"
            )
            _report(say, args.verbose, f"{recording.id}: wrote {translation_destination.name}")

    return had_error


def _run(
    args: argparse.Namespace, *, say=print, warn=_default_warn
) -> int:
    """Run bv-generate for already-parsed arguments.

    `say`/`warn` are injectable (default: real stdout/stderr via
    print) so bv-web's job runner (see web/jobs.py) can capture this
    command's output into a job's transcript instead of the real
    terminal - bv-generate has no interactive prompts once --overwrite
    is decided one way or the other (see _OverwriteDecision/
    _should_write's own docstrings: a non-interactive run, which is
    exactly what the job runner is, always skips existing files rather
    than blocking on input()), so unlike bv_config.py's `_run()`
    there's no `ask` to thread through here - same reasoning as
    bv_gps.py's own `_run()`.

    Prints a `bv-generate: started HH:MM:SS` line up front and, wrapped
    in try/finally so every exit path from there on hits it (argument
    errors, an empty selection, an unhandled exception), a
    `bv-generate: finished HH:MM:SS (N.Ns)` line - same
    started/finished pattern and placement bv-search's own `_run()`
    uses (see its docstring). A batch run over hundreds of recordings
    with --describe-scene/--transcribe can run for hours, and Christer
    has already had to check file timestamps by hand to answer "how
    long did that take" (see WORKING_CONTEXT.md's bv-scribe timing
    note - the same gap existed here).
    """

    archive_path, camera_config = resolve_archive_path(args.path, args.config_dir)
    # Which CameraAdapter (see adapters/registry.py) scans archive_path -
    # defaults to "blackvue" for a literal path with no camera config
    # behind it, same as an un-migrated CameraConfig with no `adapter`
    # key. Christer: running bv-generate against his GoPro archive
    # ("bv-generate gp --describe-scene ...") reported "no recordings
    # found in range" - this used to construct the raw archive.Archive
    # directly, whose ArchiveReader.read() requires BlackVue's literal
    # YYYYMMDD_HHMMSS_K filename convention (RecordingId.parse() returns
    # None, and the file is skipped, for anything else - a GoPro's own
    # on-camera names like GH010123.MP4 never matched), so every
    # recording in a non-BlackVue archive was silently invisible before
    # the "no recordings" range check even ran. bv-ls was already wired
    # through the adapter registry for exactly this reason (see its own
    # _run() docstring); bv-generate just never got the same fix. Going
    # through the adapter here instead gives GoProAdapter.open_archive()
    # a chance to assign each file a synthetic BlackVue-shaped id from
    # its own timestamp (adapters/_recursive_scan.py's
    # assign_recording_ids()) - sortable by the interval filter below
    # and safe for recording.id.is_parking, with no further changes
    # needed anywhere in this file.
    adapter_id = (
        camera_config.adapter if camera_config is not None else DEFAULT_ADAPTER_ID
    )
    archive = registry.get_adapter(adapter_id).open_archive(archive_path)

    started_at = datetime.now()
    started_monotonic = time.monotonic()
    say(f"bv-generate: started {started_at:%H:%M:%S}")

    try:
        try:
            interval = LexicalTimeParser(
                timestamp=args.timestamp,
                from_=args.from_,
                until=args.until,
            ).parse()
        except ValueError as exc:
            warn(f"bv-generate: {exc}")
            return EXIT_ARGS_ERROR

        # --resume: narrow the lower bound up to wherever the last
        # --resume run for this exact combination of action flags got
        # to, so a daily/cron invocation only ever walks new
        # recordings instead of re-scanning the whole archive's
        # history every time (Christer: "i am planning to run it
        # daily and dont want to scan through all previous assets").
        # max(), not replace - an explicit --from/--until the caller
        # gave is never widened, only ever narrowed further by the
        # cursor. requested_assets is needed again below (to advance
        # the cursor after a real run), so it's computed once here
        # regardless of --ignore-lock/--resume.
        requested_assets = _requested_lock_assets(args)
        resume_cursor = (
            resume_point(load_resume_state(archive_path), requested_assets)
            if args.resume
            else None
        )
        if resume_cursor is not None and resume_cursor > interval.first:
            interval = TimeInterval(first=resume_cursor, last=interval.last)

        if not args.ignore_lock:
            manifest = load_lock_manifest(archive_path)
            locked_entry = assets_fully_locked(
                manifest, interval, requested_assets
            )
            if locked_entry is not None:
                say(
                    f"bv-generate: {archive_path} - "
                    f"{interval.first}..{interval.last} already locked "
                    f"for [{', '.join(sorted(requested_assets))}], "
                    "skipping (see bv-lock --list, or --ignore-lock "
                    "to run anyway)"
                )
                return EXIT_OK

        recordings = [
            recording
            for recording in archive.recordings
            if recording.id.value in interval
        ]

        if not recordings:
            if args.resume and resume_cursor is not None:
                say(f"bv-generate: {archive_path} - up to date, nothing "
                    f"new since the last --resume run (cursor "
                    f"{resume_cursor}).")
            else:
                say(f"bv-generate: {archive_path} - no recordings found "
                    "in range, nothing to do.")
            return EXIT_OK

        # Shared across every _should_write() call this run, so an
        # interactive "overwrite?" prompt is only ever asked once (on
        # the first existing file encountered), not once per file.
        args.overwrite_decision = _OverwriteDecision()

        had_error = False
        resume_high_water: str | None = None

        for recording in recordings:
            if args.extract_audio:
                had_error |= _do_extract_audio(
                    recording, archive_path, args, say=say, warn=warn
                )

            if args.get_duration:
                had_error |= _do_get_duration(
                    recording, archive_path, args, say=say, warn=warn
                )

            if args.thumbnail:
                had_error |= _do_thumbnail(
                    recording, archive_path, args, say=say, warn=warn
                )

            if args.transcribe or args.translate is not None:
                had_error |= _do_transcribe_and_translate(
                    recording, archive_path, args, say=say, warn=warn
                )

            if args.describe_scene:
                had_error |= _do_describe_scene(
                    recording, archive_path, args, say=say, warn=warn
                )

            # Advance and persist the cursor after every single
            # recording, not just once at the very end - a crash or
            # Ctrl-C partway through an hours-long batch (Christer hit
            # this for real: a describe-scene run that took 37017s
            # ended with an unrelated "[Errno 22] Invalid argument"
            # crash - see WORKING_CONTEXT.md) used to mean the whole
            # run's --resume progress was thrown away, because the old
            # single save sat after the loop and a `finally` only
            # covers _run() printing its own "finished" line, not
            # reaching this far. Written regardless of had_error - a
            # recording that failed for a real reason (a corrupted
            # source, say) still got a real attempt, and every
            # _do_*() failure already surfaced its own warn() above and
            # rolls up into this run's own non-zero exit code. Retrying
            # it automatically forever would mean --resume can never
            # advance past a single permanently-broken recording; a
            # plain re-run (with or without --resume) still retries it
            # directly. Not advanced for --dry-run, which never writes
            # anything else either. max() against the running
            # high-water mark (not just this recording's own id) keeps
            # the same "advance to the newest walked, never backslide"
            # guarantee the old end-of-run save had, in case recordings
            # aren't perfectly sorted by id.
            if args.resume and not args.dry_run:
                resume_high_water = max(
                    resume_high_water or recording.id.value, recording.id.value
                )
                save_resume_state(
                    archive_path,
                    advance_resume_point(
                        load_resume_state(archive_path),
                        requested_assets,
                        resume_high_water.rsplit("_", 1)[0],
                    ),
                )

        return EXIT_HAD_ERRORS if had_error else EXIT_OK
    finally:
        elapsed_seconds = time.monotonic() - started_monotonic
        finished_at = datetime.now()
        say(f"bv-generate: finished {finished_at:%H:%M:%S} ({elapsed_seconds:.1f}s)")


def main(argv: list[str] | None = None) -> int:
    """Run bv-generate."""

    args = parse_args(argv)
    # See bv_scribe.py's own main() for why - wrap_say()/wrap_warn()
    # (core/joblog.py) mirror every printed line into the persistent
    # output log alongside the real terminal output.
    say = wrap_say("bv-generate")
    warn = wrap_warn("bv-generate", _default_warn)
    return run_cli(
        "bv-generate", lambda: _run(args, say=say, warn=warn), argv=argv
    )


if __name__ == "__main__":
    raise SystemExit(main())
