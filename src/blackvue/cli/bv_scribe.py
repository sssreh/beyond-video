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
from pathlib import Path

from ..archive import Archive
from .errors import run_cli
from ..generate import MediaToolError
from ..generate import SCENE_DEFAULT_MODEL
from ..generate import describe_scene
from ..generate import extract_description_section
from ..generate import select_source
from ..generate import summarize_trip
from ..lexicaltimeparser import LexicalTimeParser

EXIT_OK = 0
EXIT_ARGS_ERROR = 1
EXIT_HAD_ERRORS = 2


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
        help="Archive directory.",
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
        default=16,
        help="Hard cap on sampled frames regardless of --fps (default: 16).",
    )
    parser.add_argument(
        "--max-pixels",
        type=int,
        default=360 * 420,
        help=(
            "Resolution cap per sampled frame, in total pixels "
            "(default: 151200) - only used when --resized-width/"
            "--resized-height are both 0."
        ),
    )
    parser.add_argument(
        "--resized-width",
        type=int,
        default=1092,
        help=(
            "Force an exact frame width, bypassing --max-pixels "
            "(default: 1092 - the actual resolution knob; --max-pixels "
            "was found to be a no-op against the pinned qwen-vl-utils). "
            "Pass 0 (with --resized-height 0) to fall back to "
            "--max-pixels instead."
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
            "model sees it (default: 0.0378), to cut out BlackVue's "
            "burned-in overlay text. Pass 0 to disable."
        ),
    )
    parser.add_argument(
        "--crop-bottom",
        type=float,
        default=0.0344,
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
        "--trip-summary",
        action=argparse.BooleanOptionalAction,
        default=False,
        help=(
            "After processing every selected recording, run one extra "
            "text-only pass synthesizing a single trip-level narrative "
            "from their '## Description' sections (tracking how "
            "conditions changed over the trip, not just concatenating "
            "them), written to trip_summary.txt. Needs 2+ recordings "
            "in the selection."
        ),
    )
    parser.add_argument("--trip-summary-max-new-tokens", type=int, default=768,
                         help="Cap on generated tokens for --trip-summary (default: 768).")

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

    return parser.parse_args(argv)


def _interactive() -> bool:
    return sys.stdin.isatty() and sys.stdout.isatty()


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
    """Build describe_scene()/summarize_trip()'s SceneOptions kwargs
    from parsed CLI args - one place mapping flag names to option
    fields, since both call sites in _run() need the same mapping."""

    return dict(
        task=args.task,
        model=args.model,
        fps=args.fps,
        max_frames=args.max_frames,
        max_pixels=args.max_pixels,
        resized_width=args.resized_width,
        resized_height=args.resized_height,
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
        trip_summary_max_new_tokens=args.trip_summary_max_new_tokens,
        force_cpu=args.cpu,
    )


def _run(args: argparse.Namespace, *, say=print, warn=_default_warn) -> int:
    """Run bv-scribe for already-parsed arguments. `say`/`warn` are
    injectable (default: real stdout/stderr) so bv-web's job runner
    can capture this command's output into a job's transcript, same
    pattern as every other bv-* CLI's own `_run()`."""

    archive_path = Path(args.path)
    archive = Archive(archive_path)

    try:
        interval = LexicalTimeParser(
            timestamp=args.timestamp, from_=args.from_, until=args.until,
        ).parse()
    except ValueError as exc:
        warn(f"bv-scribe: {exc}")
        return EXIT_ARGS_ERROR

    recordings = [
        recording for recording in archive.recordings
        if recording.id.value in interval
    ]

    if not recordings:
        say(f"bv-scribe: {archive_path} - no recordings found in "
            "range, nothing to do.")
        return EXIT_OK

    scene_kwargs = _scene_kwargs(args)
    had_error = False
    trip_segments: list[tuple[str, str]] = []

    for i, recording in enumerate(recordings, start=1):
        prefix = f"[{i}/{len(recordings)}] "
        destination = archive_path / f"{recording.id}.scene.txt"

        need_write = _should_write(
            destination, overwrite=args.overwrite, dry_run=args.dry_run, warn=warn,
        )

        if not need_write:
            if args.trip_summary and destination.exists():
                try:
                    trip_segments.append((
                        str(recording.id),
                        extract_description_section(destination.read_text(encoding="utf-8")),
                    ))
                except OSError as exc:
                    warn(f"bv-scribe: {prefix}couldn't read {destination} "
                        f"for the trip summary ({exc})")
            continue

        source_file = select_source(recording)
        if source_file is None:
            warn(f"bv-scribe: {recording.id}: no front or rear video, skipping")
            had_error = True
            continue

        if args.dry_run:
            say(f"{prefix}{recording.id}: would describe scene -> {destination.name}")
            continue

        say(f"{prefix}{recording.id}")
        try:
            output_text = describe_scene(source_file.path, **scene_kwargs)
        except MediaToolError as exc:
            warn(f"bv-scribe: {recording.id}: {exc}")
            had_error = True
            continue

        destination.write_text(output_text + "\n", encoding="utf-8")
        say(f"{prefix}wrote {destination.name}")

        if args.trip_summary:
            trip_segments.append((str(recording.id), extract_description_section(output_text)))

    if args.trip_summary and not args.dry_run:
        if len(trip_segments) < 2:
            say("bv-scribe: trip-summary needs 2+ described recordings, skipping.")
        else:
            say(f"Summarizing trip across {len(trip_segments)} recording(s)...")
            try:
                summary_text = summarize_trip(trip_segments, **scene_kwargs)
            except MediaToolError as exc:
                warn(f"bv-scribe: trip-summary: {exc}")
                had_error = True
            else:
                summary_path = archive_path / "trip_summary.txt"
                summary_path.write_text(summary_text + "\n", encoding="utf-8")
                say(f"wrote {summary_path.name}")

    return EXIT_HAD_ERRORS if had_error else EXIT_OK


def main(argv: list[str] | None = None) -> int:
    """Run bv-scribe."""

    args = parse_args(argv)
    return run_cli("bv-scribe", lambda: _run(args))


if __name__ == "__main__":
    raise SystemExit(main())
