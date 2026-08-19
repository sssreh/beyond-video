"""
bv-scribe.

Standalone, fully-tunable scene description/OCR over a BlackVue
archive - the batch-oriented counterpart to bv-generate's
--describe-scene (which uses fixed sensible defaults for running
scene description alongside other generation actions in one pass).
Reuses the same blackvue.generate.scene module and the same
LexicalTimeParser-based recording selection every other bv-* command
uses, rather than scene-scribe's original raw file/folder arguments.

Copyright (C) 2026 Christer R. (sssreh)

SPDX-License-Identifier: GPL-3.0-or-later
"""

from __future__ import annotations

import argparse
import sys
import threading
import time
from datetime import datetime
from pathlib import Path

from ..archive import Archive
from ..archive import Asset
from .errors import run_cli
from ..core.camera_config import default_config_dir
from ..core.camera_config import resolve_archive_path
from ..core.joblog import wrap_say
from ..core.joblog import wrap_warn
from ..generate import MediaToolError
from ..generate import SCENE_DEFAULT_MODEL
from ..generate import describe_scene
from ..lexicaltimeparser import LexicalTimeParser

EXIT_OK = 0
EXIT_ARGS_ERROR = 1
EXIT_HAD_ERRORS = 2

# --raw mode's recognized video extensions - deliberately broader than
# BlackVue's own ".mp4", since --raw exists precisely for non-BlackVue
# footage.
RAW_VIDEO_EXTENSIONS = {".mp4", ".mov", ".avi", ".mkv", ".m4v", ".webm"}


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Parse command line arguments."""

    parser = argparse.ArgumentParser(
        prog="bv-scribe",
        description=(
            "Describe recordings' contents and read their on-screen "
            "text using a local vision-language model (Qwen2.5-VL/"
            "Qwen3-VL), with the full set of tuning flags scene-"
            "scribe's real-footage testing converged on. For simple "
            "use alongside other bv-generate actions, see "
            "'bv-generate --describe-scene' instead."
        ),
        allow_abbrev=False,
    )

    parser.add_argument(
        "path",
        nargs="?",
        default=".",
        help=(
            "Archive directory, or (with --raw) a raw video file or "
            "a directory of raw video files. Also accepts a camera "
            "system id (see bv-config), resolved to that camera's "
            "archive target - use an explicit ./name or .\\name to "
            "force a literal directory of the same name instead. Not "
            "resolved as a camera id with --raw."
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
        "--raw",
        action="store_true",
        help=(
            "Treat `path` as a raw video file or a directory of raw "
            "video files instead of a BlackVue archive - no "
            "--from/--until/--timestamp selection (raw footage has "
            "no BlackVue recording-id timestamp to select on) and no "
            "--camera (no front/rear distinction). Cropping "
            "(--crop-top/--crop-bottom) defaults to disabled, since "
            "those defaults are tuned for BlackVue's burned-in "
            "overlay text, which won't exist on non-BlackVue "
            "footage. Output is written next to each source video as "
            "<video-stem>.scene.txt."
        ),
    )

    parser.add_argument(
        "--from",
        dest="from_",
        metavar="TIMESTAMP",
        help="Only consider recordings from this timestamp. Not used with --raw.",
    )
    parser.add_argument(
        "--until",
        metavar="TIMESTAMP",
        help="Only consider recordings up to this timestamp. Not used with --raw.",
    )
    parser.add_argument(
        "--timestamp",
        metavar="TIMESTAMP",
        help="Only consider recordings matching this timestamp or prefix. Not used with --raw.",
    )
    parser.add_argument(
        "--camera",
        choices=["front", "rear", "both"],
        default="front",
        help=(
            "Which camera(s) to process (default: front - same as "
            "before this flag existed: front video, or rear if "
            "there's no front). 'rear' processes only the rear "
            "video, with the normal full --task treatment (saved as "
            "<recording>.rear.scene.txt) - a deliberate choice gets "
            "full treatment, not just plates. 'both' adds a cheap "
            "OCR-only bonus pass on the rear video alongside the "
            "normal front pass, skipped if the recording has no "
            "distinct rear video (i.e. front was already using rear "
            "as its own fallback) - a rear-camera description would "
            "mostly just restate the front one's, so only "
            "plates/signs are worth the extra inference call. Not "
            "used with --raw (raw video files have no front/rear "
            "distinction)."
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
        default=SCENE_DEFAULT_MODEL,
        help=(
            f"Hugging Face model id (default: {SCENE_DEFAULT_MODEL}). "
            "Try a smaller Qwen2.5-VL for faster iteration or a "
            "quantized (-AWQ) variant if tight on VRAM. Qwen3-VL "
            "(any model id containing 'qwen3-vl') is also supported "
            "but less tested against real footage - requires "
            "transformers>=4.57.0."
        ),
    )
    parser.add_argument(
        "--fps",
        type=float,
        default=1.0,
        help="Frames per second of video to sample (default: 1.0).",
    )
    parser.add_argument(
        "--max-frames",
        type=int,
        default=64,
        help="Hard cap on sampled frames regardless of --fps (default: 64).",
    )
    parser.add_argument(
        "--max-pixels",
        type=int,
        default=360 * 420,
        help=(
            "Photo-mode resolution cap per image, in total pixels "
            "(default: 151200) - only used when --resized-width/"
            "--resized-height are both 0. Video sampling ignores this "
            "entirely - see --video-total-pixels."
        ),
    )
    parser.add_argument(
        "--resized-width",
        type=int,
        default=1092,
        help=(
            "Photo mode only: force an exact frame width, bypassing "
            "--max-pixels (default: 1092 - the actual resolution knob "
            "for photos; --max-pixels was found to be a no-op against "
            "the pinned qwen-vl-utils whenever an exact resize was also "
            "set - the same reason video sampling stopped setting this "
            "at all, see --video-total-pixels). Pass 0 (with "
            "--resized-height 0) to fall back to --max-pixels instead. "
            "Has no effect on video."
        ),
    )
    parser.add_argument(
        "--resized-height",
        type=int,
        default=588,
        help="Photo mode only - see --resized-width. Has no effect on video.",
    )
    parser.add_argument(
        "--video-total-pixels",
        type=int,
        default=16 * 1092 * 588,
        help=(
            "Video mode's resolution/frame-count tradeoff knob, in "
            "total pixels summed across every sampled frame (default: "
            "%(default)s - 16 frames at 1092x588, this project's "
            "real-footage-tuned per-clip budget). qwen_vl_utils divides "
            "this by the number of sampled frames (--max-frames/--fps) "
            "to get each frame's own resolution cap, so raising "
            "--max-frames for closer-together samples costs resolution "
            "per frame rather than multiplying total compute the way a "
            "fixed --resized-width/--resized-height would. Video never "
            "reads --max-pixels/--resized-width/--resized-height (those "
            "are photo-only) - this is the one knob for video."
        ),
    )
    parser.add_argument(
        "--crop-top",
        type=float,
        default=None,
        help=(
            "Fraction of frame height to crop off the top before the "
            "model sees it, to cut out BlackVue's burned-in overlay "
            "text (default: 0.0378, or 0 - disabled - with --raw, "
            "since raw footage has no BlackVue overlay to crop out)."
        ),
    )
    parser.add_argument(
        "--crop-bottom",
        type=float,
        default=None,
        help="Fraction of frame height to crop off the bottom - see --crop-top.",
    )
    parser.add_argument(
        "--max-new-tokens",
        type=int,
        default=768,
        help="Cap on generated answer length (default: 768).",
    )
    parser.add_argument(
        "--repetition-penalty",
        type=float,
        default=1.15,
        help="Penalizes repeated tokens (default: 1.15, 1.0 = off).",
    )
    parser.add_argument(
        "--no-repeat-ngram-size",
        type=int,
        default=3,
        help="Forbids repeating any N-token sequence (default: 3, 0 = off).",
    )
    parser.add_argument(
        "--do-sample",
        action=argparse.BooleanOptionalAction,
        default=False,
        help=(
            "Enable probabilistic sampling instead of greedy decoding "
            "(default: off). Greedy avoids re-rolling a different "
            "hallucinated guess on the same illegible text every run."
        ),
    )
    parser.add_argument("--temperature", type=float, default=0.7,
                         help="Sampling temperature, only with --do-sample.")
    parser.add_argument("--top-p", type=float, default=0.8,
                         help="Nucleus sampling cutoff, only with --do-sample.")
    parser.add_argument("--top-k", type=int, default=20,
                         help="Top-k sampling cutoff, only with --do-sample.")

    parser.add_argument(
        "--zoom-signs",
        action=argparse.BooleanOptionalAction,
        default=True,
        help=(
            "After the main pass, detect signs/plates in a few full-"
            "resolution frames and re-OCR just those crops at native "
            "resolution (default: on) - fixes small/distant signage "
            "the main pass's downscaled frames can't resolve."
        ),
    )
    parser.add_argument("--zoom-frames", type=int, default=4,
                         help="How many full-res frames to sample for sign detection (default: 4).")
    parser.add_argument("--zoom-detect-width", type=int, default=1092,
                         help="Resolution for the detection step (default: 1092).")
    parser.add_argument("--zoom-padding", type=float, default=0.15,
                         help="Padding fraction around each detected box (default: 0.15).")
    parser.add_argument("--zoom-ocr-width", type=int, default=640,
                         help="Minimum width a cropped sign/plate is upscaled to before OCR (default: 640).")
    parser.add_argument(
        "--zoom-debug-dir",
        type=Path,
        default=None,
        help="Save every zoom-pipeline crop + a manifest.tsv here, for inspecting raw source pixels.",
    )
    parser.add_argument("--zoom-max-new-tokens", type=int, default=200,
                         help="Cap on generated tokens for each crop's OCR read (default: 200).")
    parser.add_argument("--zoom-detect-max-new-tokens", type=int, default=500,
                         help="Cap on generated tokens for the detection call (default: 500).")
    parser.add_argument("--zoom-repetition-penalty", type=float, default=1.0,
                         help="Separate --repetition-penalty for detection/OCR calls (default: 1.0, off).")
    parser.add_argument("--zoom-no-repeat-ngram-size", type=int, default=0,
                         help="Separate --no-repeat-ngram-size for detection/OCR calls (default: 0, off).")
    parser.add_argument(
        "--zoom-plate-confidence-check",
        action=argparse.BooleanOptionalAction,
        default=True,
        help=(
            "Read every detected plate crop twice (once greedy, once "
            "with sampling forced on) and flag the read as unverified "
            "if the two disagree, instead of reporting a single "
            "possibly-wrong read as fact (default: on) - a real plate "
            "was once misread with full apparent confidence, not "
            "flagged 'not legible'. Costs one extra inference call per "
            "detected plate."
        ),
    )

    parser.add_argument(
        "--cpu",
        action="store_true",
        help="Force CPU inference. Extremely slow for a 7B+ video model.",
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
        "-v", "--verbose",
        action="store_true",
        help="Print each file as it is generated.",
    )

    args = parser.parse_args(argv)

    # --raw's cropping defaults differ from archive mode's (see --raw
    # and --crop-top's help text) - resolved here, once, rather than
    # in _scene_kwargs(), so an explicit --crop-top/--crop-bottom
    # always wins regardless of --raw.
    if args.crop_top is None:
        args.crop_top = 0.0 if args.raw else 0.0378
    if args.crop_bottom is None:
        args.crop_bottom = 0.0 if args.raw else 0.0344

    return args


def _interactive() -> bool:
    # sys.stdin/sys.stdout are process-wide, not per-thread - if
    # bv-web's own server process happens to be launched attached to a
    # real terminal (Christer's native, non-Docker setup: `bv-web
    # serve ...` typed directly into a pwsh window), isatty() returns
    # True even inside a background job thread, where there is no one
    # actually watching that console for this specific prompt. Without
    # the main-thread check below, _should_write() then calls input()
    # on that thread, which blocks forever - the job's own output box
    # stays empty (the prompt text goes to the server's raw console,
    # not through say()/warn()), no error is ever raised, and the job
    # is stuck showing "Running" indefinitely. Confirmed as the real
    # cause of a bv-scribe web job that looked hung with zero output
    # and no GPU usage - see WORKING_CONTEXT.md. Requiring the main
    # thread too means only a genuine direct CLI invocation (always
    # main-thread) can hit the interactive prompt; every bv-web job
    # (always a background thread) now safely falls through to the
    # warn()+skip branch below instead.
    return (
        sys.stdin.isatty()
        and sys.stdout.isatty()
        and threading.current_thread() is threading.main_thread()
    )


def _default_warn(message: str) -> None:
    print(message, file=sys.stderr)


def _should_write(path: Path, *, overwrite: bool, dry_run: bool, warn=_default_warn) -> bool:
    """Decide whether to (re)generate an output file - same policy as
    bv-generate's own _should_write (see its docstring): missing file
    always writes, --overwrite always rewrites, dry-run never prompts,
    an interactive terminal asks, a non-interactive run skips with a
    warning."""

    if not path.exists():
        return True
    if overwrite:
        return True
    if dry_run:
        return False
    if _interactive():
        answer = input(f"{path.name} already exists. Overwrite? [y/N] ").strip().lower()
        return answer in ("y", "yes")
    warn(f"bv-scribe: {path.name}: already exists, skipping (use --overwrite)")
    return False


def _scene_kwargs(args: argparse.Namespace) -> dict:
    """Build describe_scene()'s SceneOptions kwargs from parsed CLI
    args - one place mapping flag names to option fields."""

    return dict(
        task=args.task,
        model=args.model,
        fps=args.fps,
        max_frames=args.max_frames,
        max_pixels=args.max_pixels,
        resized_width=args.resized_width,
        resized_height=args.resized_height,
        video_total_pixels=args.video_total_pixels,
        crop_top=args.crop_top,
        crop_bottom=args.crop_bottom,
        max_new_tokens=args.max_new_tokens,
        repetition_penalty=args.repetition_penalty,
        no_repeat_ngram_size=args.no_repeat_ngram_size,
        do_sample=args.do_sample,
        temperature=args.temperature,
        top_p=args.top_p,
        top_k=args.top_k,
        zoom_signs=args.zoom_signs,
        zoom_frames=args.zoom_frames,
        zoom_detect_width=args.zoom_detect_width,
        zoom_padding=args.zoom_padding,
        zoom_ocr_width=args.zoom_ocr_width,
        zoom_debug_dir=args.zoom_debug_dir,
        zoom_max_new_tokens=args.zoom_max_new_tokens,
        zoom_detect_max_new_tokens=args.zoom_detect_max_new_tokens,
        zoom_repetition_penalty=args.zoom_repetition_penalty,
        zoom_no_repeat_ngram_size=args.zoom_no_repeat_ngram_size,
        zoom_plate_confidence_check=args.zoom_plate_confidence_check,
        force_cpu=args.cpu,
    )


def _run_scene_pass(
    label: str,
    video_path: Path,
    destination: Path,
    scene_kwargs: dict,
    args: argparse.Namespace,
    *,
    task_override: str | None = None,
    say=print,
    warn=_default_warn,
) -> bool:
    """Run one describe_scene() call for `label` (used only in
    messages) and write its result to `destination`. Returns
    had_error.

    `task_override`, when given, replaces scene_kwargs['task'] for
    just this call - used for --camera both's OCR-only rear bonus
    pass, which should ignore whatever --task the user asked for on
    the main pass."""

    need_write = _should_write(
        destination, overwrite=args.overwrite, dry_run=args.dry_run, warn=warn,
    )

    if not need_write:
        return False

    if args.dry_run:
        say(f"{label}: would describe scene -> {destination.name}")
        return False

    kwargs = dict(scene_kwargs)
    if task_override is not None:
        kwargs["task"] = task_override

    say(label)
    try:
        output_text = describe_scene(video_path, **kwargs)
    except MediaToolError as exc:
        warn(f"bv-scribe: {label}: {exc}")
        return True

    destination.write_text(output_text + "\n", encoding="utf-8")
    say(f"wrote {destination.name}")
    return False


def _describe_recording(
    recording,
    archive_path: Path,
    scene_kwargs: dict,
    args: argparse.Namespace,
    *,
    prefix: str,
    say=print,
    warn=_default_warn,
) -> bool:
    """Describe one recording's scene/on-screen text for whichever
    camera(s) --camera selects. Returns had_error."""

    front_file = recording.file(Asset.FRONT)
    rear_file = recording.file(Asset.REAR)
    had_error = False

    if args.camera in ("front", "both"):
        # Same front-preferred-with-rear-fallback source selection
        # select_source() itself uses - kept inline here so
        # front_file/rear_file, already looked up above for the rear
        # pass below, aren't looked up twice.
        source_file = front_file or rear_file
        if source_file is None:
            warn(f"bv-scribe: {recording.id}: no front or rear video, skipping")
            had_error = True
        else:
            had_error |= _run_scene_pass(
                f"{prefix}{recording.id}", source_file.path,
                archive_path / f"{recording.id}.scene.txt",
                scene_kwargs, args, say=say, warn=warn,
            )

    if args.camera in ("rear", "both"):
        if rear_file is None:
            if args.camera == "rear":
                warn(f"bv-scribe: {recording.id}: no rear video, skipping")
                had_error = True
            elif args.verbose:
                say(f"{recording.id}: no rear video, skipping rear scene pass")
        elif args.camera == "both" and front_file is None:
            # The front pass above already used rear_file as its own
            # fallback source - a second pass on the exact same video
            # would just duplicate that work under a different
            # filename.
            if args.verbose:
                say(f"{recording.id}: no distinct rear video (front pass "
                    "already used it as its own fallback), skipping rear "
                    "scene pass")
        else:
            had_error |= _run_scene_pass(
                f"{prefix}{recording.id} (rear)", rear_file.path,
                archive_path / f"{recording.id}.rear.scene.txt",
                scene_kwargs, args,
                task_override="ocr" if args.camera == "both" else None,
                say=say, warn=warn,
            )

    return had_error


def _collect_raw_videos(path: Path) -> list[Path]:
    """Return the video file(s) --raw mode should process for `path`:
    just the file itself if it's a single video, or every recognized
    video file directly inside it (not recursive, sorted by name) if
    it's a directory."""

    if path.is_file():
        return [path]

    return sorted(
        p for p in path.iterdir()
        if p.is_file() and p.suffix.lower() in RAW_VIDEO_EXTENSIONS
    )


def _run_raw(args: argparse.Namespace, *, say=print, warn=_default_warn) -> int:
    """Run bv-scribe in --raw mode: describe scene for a single raw
    video file, or every video file directly inside a raw directory -
    no archive, no LexicalTimeParser recording selection, since raw
    footage has none of BlackVue's filename/sidecar structure to
    select on."""

    if args.timestamp or args.from_ or args.until:
        warn("bv-scribe: --from/--until/--timestamp don't apply with "
            "--raw (raw video files have no BlackVue recording-id "
            "timestamp to select on)")
        return EXIT_ARGS_ERROR
    if args.camera != "front":
        warn("bv-scribe: --camera doesn't apply with --raw (raw video "
            "files have no front/rear distinction)")
        return EXIT_ARGS_ERROR

    raw_path = Path(args.path)
    if not raw_path.exists():
        warn(f"bv-scribe: {raw_path}: no such file or directory")
        return EXIT_ARGS_ERROR

    videos = _collect_raw_videos(raw_path)
    if not videos:
        say(f"bv-scribe: {raw_path} - no video files found, nothing to do.")
        return EXIT_OK

    scene_kwargs = _scene_kwargs(args)
    had_error = False

    for i, video in enumerate(videos, start=1):
        prefix = f"[{i}/{len(videos)}] "
        destination = video.with_name(f"{video.stem}.scene.txt")

        had_error |= _run_scene_pass(
            f"{prefix}{video.name}", video, destination, scene_kwargs, args,
            say=say, warn=warn,
        )

    return EXIT_HAD_ERRORS if had_error else EXIT_OK


def _run(args: argparse.Namespace, *, say=print, warn=_default_warn) -> int:
    """Run bv-scribe for already-parsed arguments. `say`/`warn` are
    injectable (default: real stdout/stderr) so bv-web's job runner
    can capture this command's output into a job's transcript, same
    pattern as every other bv-* CLI's own `_run()`.

    Prints "started HH:MM:SS" up front and, wrapped in try/finally so
    every exit path (including --raw mode and an unhandled per-
    recording exception) hits it, "finished HH:MM:SS (N.Ns)" - same
    pattern bv-search's own `_run()` uses. bv-scribe is the command
    Christer originally asked for this on: a 902-recording batch ran
    half a day+ with no timing output at all, and he ended up checking
    .scene.txt file timestamps by hand instead (see WORKING_CONTEXT.md).
    """

    started_at = datetime.now()
    started_monotonic = time.monotonic()
    say(f"bv-scribe: started {started_at:%H:%M:%S}")

    try:
        return _run_dispatch(args, say=say, warn=warn)
    finally:
        elapsed_seconds = time.monotonic() - started_monotonic
        finished_at = datetime.now()
        say(f"bv-scribe: finished {finished_at:%H:%M:%S} ({elapsed_seconds:.1f}s)")


def _run_dispatch(args: argparse.Namespace, *, say=print, warn=_default_warn) -> int:
    """The actual archive-mode/--raw-mode dispatch _run() used to do
    directly - split out so _run() itself can wrap it in a single
    started/finished timing block covering both modes uniformly,
    without duplicating that wrapping inside _run_raw() too."""

    if args.raw:
        return _run_raw(args, say=say, warn=warn)

    archive_path, _camera_config = resolve_archive_path(args.path, args.config_dir)
    archive = Archive(archive_path)

    try:
        interval = LexicalTimeParser(
            timestamp=args.timestamp, from_=args.from_, until=args.until,
        ).parse()
    except ValueError as exc:
        warn(f"bv-scribe: {exc}")
        return EXIT_ARGS_ERROR

    matching = [
        recording for recording in archive.recordings
        if recording.id.value in interval
    ]
    # Parking-mode recordings are never considered for bv-scribe, full
    # stop - unlike bv-generate's --describe-scene (which deliberately
    # does run on them, "they're still video, just no audio"), a
    # dedicated batch run over a whole archive shouldn't burn GPU time
    # and risk hitting one on a long, often-uneventful parking clip.
    # Also sidesteps a real failure mode: parking recordings tend to be
    # the largest files (hours of timelapse), which made one the first
    # to hit a flaky network read on Christer's \\NAS\ archive and take
    # down an entire 902-recording batch (see the per-recording
    # try/except below and WORKING_CONTEXT.md) - excluding them here is
    # a belt-and-suspenders fix, not the only one. Point --raw directly
    # at a parking .mp4 if you genuinely want one described - that mode
    # has no RecordingId-based filtering at all.
    recordings = [r for r in matching if not r.id.is_parking]
    skipped_parking = len(matching) - len(recordings)
    if skipped_parking:
        say(
            f"bv-scribe: skipping {skipped_parking} parking-mode "
            "recording(s) - not considered for scene description "
            "(use --raw against the file directly if you really want "
            "one described)."
        )

    if not recordings:
        say(f"bv-scribe: {archive_path} - no recordings found in "
            "range, nothing to do.")
        return EXIT_OK

    scene_kwargs = _scene_kwargs(args)
    had_error = False
    failures: list[tuple[str, str]] = []

    for i, recording in enumerate(recordings, start=1):
        prefix = f"[{i}/{len(recordings)}] "
        try:
            err = _describe_recording(
                recording, archive_path, scene_kwargs, args,
                prefix=prefix, say=say, warn=warn,
            )
        except Exception as exc:  # noqa: BLE001 - one bad recording shouldn't kill an hours-long batch (see WORKING_CONTEXT.md)
            had_error = True
            failures.append((str(recording.id), str(exc)))
            warn(f"bv-scribe: {prefix}{recording.id}: FAILED - {exc}")
            continue
        had_error |= err

    if failures:
        say(f"bv-scribe: {len(failures)} recording(s) failed:")
        for recording_id, message in failures:
            say(f"  {recording_id}: {message}")

    return EXIT_HAD_ERRORS if had_error else EXIT_OK


def main(argv: list[str] | None = None) -> int:
    """Run bv-scribe."""

    args = parse_args(argv)
    # wrap_say()/wrap_warn() (core/joblog.py) mirror every printed line
    # into the persistent, monthly-rotating output log alongside the
    # real terminal output - see WORKING_CONTEXT.md's "Scope - settled:
    # direct CLI calls too" note. bv-web's own job runner gets the same
    # coverage for free via Job.append_output() itself.
    say = wrap_say("bv-scribe")
    warn = wrap_warn("bv-scribe", _default_warn)
    return run_cli(
        "bv-scribe", lambda: _run(args, say=say, warn=warn), argv=argv
    )


if __name__ == "__main__":
    raise SystemExit(main())
