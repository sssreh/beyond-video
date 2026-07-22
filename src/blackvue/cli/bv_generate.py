"""
bv-generate.

Copyright (C) 2026 Christer R. (sssreh)

SPDX-License-Identifier: GPL-3.0-or-later
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

from ..archive import Archive
from ..archive import Asset
from ..archive.recording import Recording
from .errors import run_cli
from ..generate import MediaToolError
from ..generate import detect_language
from ..generate import diarize
from ..generate import extract_audio
from ..generate import format_diarized_transcript
from ..generate import format_lrc
from ..generate import format_srt
from ..generate import get_span
from ..generate import normalize_language
from ..generate import select_source
from ..generate import short_code
from ..generate import transcribe
from ..generate import translate
from ..lexicaltimeparser import LexicalTimeParser

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
        default="small",
        help="faster-whisper model size (default: %(default)s).",
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
        "--lrc",
        action="store_true",
        help=(
            "Also write an LRC timestamp file (<recording>.lrc), one "
            "[mm:ss.xx] line per transcript segment. Requires "
            "--transcribe or --translate."
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
        or args.transcribe
        or args.translate is not None
    ):
        parser.error(
            "specify at least one action: --extract-audio, "
            "--get-duration, --transcribe, or --translate"
        )

    if args.diarize and not (args.transcribe or args.translate is not None):
        parser.error("--diarize requires --transcribe or --translate")

    if (args.srt or args.lrc) and not (
        args.transcribe or args.translate is not None
    ):
        parser.error("--srt/--lrc require --transcribe or --translate")

    # --language/--translate accept either the 2-letter code Whisper
    # and argos-translate use, or the 3-letter code generated
    # filenames use - normalize to the 2-letter form once, here, so
    # every call site downstream can assume that form.
    if args.language is not None:
        args.language = normalize_language(args.language)

    if args.translate is not None:
        args.translate = normalize_language(args.translate)

    return args


def _interactive() -> bool:
    """Return True if running attached to a real terminal."""

    return sys.stdin.isatty() and sys.stdout.isatty()


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

    print(
        f"bv-generate: {path.name}: already exists, skipping "
        "(use --overwrite)",
        file=sys.stderr,
    )
    return False


def _should_write_for(path: Path, args: argparse.Namespace) -> bool:
    """_should_write(), reading overwrite/dry_run/the shared
    per-run overwrite decision straight from args - the common case
    for every call site below."""

    return _should_write(
        path,
        overwrite=args.overwrite,
        dry_run=args.dry_run,
        overwrite_decision=getattr(args, "overwrite_decision", None),
    )


def _report(verbose: bool, message: str) -> None:
    if verbose:
        print(message)


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
) -> bool:
    """Extract audio for one recording. Return True on error."""

    if recording.id.is_parking:
        print(
            f"bv-generate: {recording.id}: parking-mode (timelapse) "
            "recording has no audio, skipping",
            file=sys.stderr,
        )
        return False

    destination = archive_path / f"{recording.id}.aac"

    if not _should_write_for(destination, args):
        return False

    source_file = select_source(recording)
    if source_file is None:
        print(
            f"bv-generate: {recording.id}: no front or rear video, "
            "skipping audio extraction",
            file=sys.stderr,
        )
        return True

    if args.dry_run:
        print(
            f"{recording.id}: would extract audio from "
            f"{source_file.name} -> {destination.name}"
        )
        return False

    try:
        extract_audio(source_file.path, destination)
    except MediaToolError as exc:
        print(f"bv-generate: {recording.id}: {exc}", file=sys.stderr)
        return True

    _report(
        args.verbose,
        f"{recording.id}: extracted audio -> {destination.name}",
    )
    return False


def _do_get_duration(
    recording: Recording,
    archive_path: Path,
    args: argparse.Namespace,
) -> bool:
    """Compute and report the span for one recording. Return True on error."""

    source_file = select_source(recording)
    if source_file is None:
        print(
            f"bv-generate: {recording.id}: no front or rear video, "
            "skipping duration",
            file=sys.stderr,
        )
        return True

    try:
        span = get_span(recording.id, source_file.path)
    except MediaToolError as exc:
        print(f"bv-generate: {recording.id}: {exc}", file=sys.stderr)
        return True

    print(f"{recording.id}: {span}s")

    destination = archive_path / f"{recording.id}.duration.txt"

    if not _should_write_for(destination, args):
        return False

    if args.dry_run:
        print(f"{recording.id}: would write {destination.name}")
        return False

    destination.write_text(f"{span}\n", encoding="utf-8")
    _report(args.verbose, f"{recording.id}: wrote {destination.name}")
    return False


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


def _do_transcribe_and_translate(
    recording: Recording,
    archive_path: Path,
    args: argparse.Namespace,
) -> bool:
    """Transcribe and/or translate one recording. Return True on error."""

    if recording.id.is_parking:
        print(
            f"bv-generate: {recording.id}: parking-mode (timelapse) "
            "recording has no audio, skipping",
            file=sys.stderr,
        )
        return False

    if args.transcribe:
        return _do_transcribe_with_optional_translate(
            recording, archive_path, args
        )

    if args.translate is not None:
        return _do_translate_only(recording, archive_path, args)

    return False


def _do_translate_only(
    recording: Recording,
    archive_path: Path,
    args: argparse.Namespace,
) -> bool:
    """Handle --translate without --transcribe.

    Reuses whatever's already been generated for this recording,
    cheapest first: an existing transcript, then already-extracted
    audio, then the source video. If it ends up doing the full
    extract+transcribe pipeline from scratch, it leaves the .aac and
    .transcript.txt files behind too, so the next run (of this or
    --transcribe) doesn't redo that work.

    Exception: if --srt/--lrc are given and actually need (re)writing,
    the existing-transcript reuse is skipped and Whisper always runs
    fresh - a cached plain-text transcript has no per-segment timing,
    so reusing it would produce no subtitles at all.
    """

    translation_destination = archive_path / _language_suffixed_name(
        recording.id,
        args.translate,
        "translation.txt",
        diarized=args.diarize,
    )
    need_translation_write = _should_write_for(translation_destination, args)

    # Computed up front (like _do_transcribe_with_optional_translate)
    # so a missing/needs-refresh .srt or .lrc alone is enough to keep
    # this recording from being skipped, even when translation.txt
    # itself is already up to date - that's the bug Christer hit:
    # translation.txt already existed from an earlier run, so the old
    # single-destination gate below returned early before ever
    # reaching the srt/lrc-writing code.
    srt_destination = archive_path / f"{recording.id}.srt" if args.srt else None
    need_srt_write = (
        _should_write_for(srt_destination, args) if args.srt else False
    )

    lrc_destination = archive_path / f"{recording.id}.lrc" if args.lrc else None
    need_lrc_write = (
        _should_write_for(lrc_destination, args) if args.lrc else False
    )

    if args.dry_run:
        if need_translation_write:
            print(
                f"{recording.id}: would translate -> "
                f"{translation_destination.name}"
            )
        if need_srt_write:
            print(f"{recording.id}: would write {srt_destination.name}")
        if need_lrc_write:
            print(f"{recording.id}: would write {lrc_destination.name}")
        return False

    if not (need_translation_write or need_srt_write or need_lrc_write):
        return False

    transcript_text: str | None = None
    transcript_language: str | None = None
    segments: tuple = ()
    turns: tuple = ()

    # 1. An existing transcript already has everything translation
    #    needs, so reuse it instead of re-running Whisper. Diarized
    #    and plain transcripts are tracked as separate assets, so
    #    this looks at whichever one matches what this run wants.
    #    Skipped entirely when an .srt/.lrc actually needs writing: a
    #    cached plain-text transcript has no per-segment timing, so
    #    reusing it would silently produce no subtitles - forcing a
    #    fresh transcribe() is the only way to actually satisfy that.
    want_segment_timing = need_srt_write or need_lrc_write
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
        _report(
            args.verbose,
            f"{recording.id}: reusing {existing_transcript.name}",
        )

    if transcript_text is None:
        # 2. Reuse already-extracted audio, or extract it fresh and
        #    leave it behind.
        audio_file = recording.file(Asset.AUDIO)

        if audio_file is not None:
            audio_source = audio_file.path
        else:
            video_source = select_source(recording)

            if video_source is None:
                print(
                    f"bv-generate: {recording.id}: no audio or video "
                    "source, skipping translation",
                    file=sys.stderr,
                )
                return True

            audio_destination = archive_path / f"{recording.id}.aac"

            try:
                extract_audio(video_source.path, audio_destination)
            except MediaToolError as exc:
                print(f"bv-generate: {recording.id}: {exc}", file=sys.stderr)
                return True

            _report(
                args.verbose,
                f"{recording.id}: extracted audio -> "
                f"{audio_destination.name}",
            )
            audio_source = audio_destination

        # 3. Transcribe, and leave the transcript behind too.
        try:
            transcript = transcribe(
                audio_source,
                language=args.language,
                model_size=args.model_size,
            )
        except MediaToolError as exc:
            print(f"bv-generate: {recording.id}: {exc}", file=sys.stderr)
            return True

        transcript_text = transcript.text
        transcript_language = transcript.language
        segments = transcript.segments

        if args.diarize:
            try:
                turns = diarize(audio_source, hf_token=args.hf_token)
                transcript_text = format_diarized_transcript(
                    segments, turns
                )
            except MediaToolError as exc:
                print(f"bv-generate: {recording.id}: {exc}", file=sys.stderr)
                return True

        transcript_destination = archive_path / _language_suffixed_name(
            recording.id,
            transcript_language,
            "transcript.txt",
            diarized=args.diarize,
        )

        if _should_write_for(transcript_destination, args):
            transcript_destination.write_text(
                transcript_text + "\n", encoding="utf-8"
            )
            _report(
                args.verbose,
                f"{recording.id}: wrote {transcript_destination.name}",
            )

        # SRT/LRC need per-segment timing, which only exists right
        # after a fresh transcribe() call (this branch) - a reused
        # cached transcript (above) has no segments to draw from, so
        # this is deliberately skipped in that case. need_srt_write/
        # need_lrc_write were already computed up front, before it
        # was known whether this branch would even run - reused here
        # rather than re-checking _should_write a second time.
        if need_srt_write:
            srt_destination.write_text(
                format_srt(segments, turns) + "\n", encoding="utf-8"
            )
            _report(
                args.verbose, f"{recording.id}: wrote {srt_destination.name}"
            )

        if need_lrc_write:
            lrc_destination.write_text(
                format_lrc(segments, turns) + "\n", encoding="utf-8"
            )
            _report(
                args.verbose, f"{recording.id}: wrote {lrc_destination.name}"
            )

    # Gated on need_translation_write, not just "did we get this far":
    # this point is also reached when only --srt/--lrc needed
    # (re)writing and translation.txt was already up to date - without
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
            print(f"bv-generate: {recording.id}: {exc}", file=sys.stderr)
            return True

        translation_destination.write_text(translated + "\n", encoding="utf-8")
        _report(
            args.verbose,
            f"{recording.id}: wrote {translation_destination.name}",
        )

    return False


def _do_transcribe_with_optional_translate(
    recording: Recording,
    archive_path: Path,
    args: argparse.Namespace,
) -> bool:
    """Handle --transcribe, optionally with --translate alongside it.

    Always runs Whisper fresh (subject to the normal overwrite/skip
    policy on the transcript file itself) and reuses that one run's
    output for translation too, rather than the cache-first approach
    _do_translate_only uses.
    """

    want_transcript_file = args.transcribe
    want_translation_file = args.translate is not None

    source_file = recording.file(Asset.AUDIO) or select_source(recording)

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
        need_translation_write = _should_write_for(translation_destination, args)

    # SRT/LRC filenames don't depend on language, so - like the
    # translation destination above - they can be checked up front.
    srt_destination = None
    need_srt_write = False

    if args.srt:
        srt_destination = archive_path / f"{recording.id}.srt"
        need_srt_write = _should_write_for(srt_destination, args)

    lrc_destination = None
    need_lrc_write = False

    if args.lrc:
        lrc_destination = archive_path / f"{recording.id}.lrc"
        need_lrc_write = _should_write_for(lrc_destination, args)

    # The transcript filename depends on the *spoken* language. If
    # --language was given, that's already known. Otherwise it has
    # to be detected first - cheaply, so a recording that's already
    # been transcribed doesn't pay for a full re-transcription just
    # to find out its own output already exists.
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
                if _should_write_for(transcript_destination, args):
                    print(
                        f"{recording.id}: would transcribe -> "
                        f"{transcript_destination.name}"
                    )
            else:
                print(
                    f"{recording.id}: would transcribe -> "
                    f"{recording.id}[_<lang>].transcript.txt "
                    "(language auto-detected)"
                )
        else:
            if transcript_language is None:
                if source_file is None:
                    print(
                        f"bv-generate: {recording.id}: no audio or video "
                        "source, skipping transcription",
                        file=sys.stderr,
                    )
                    return True

                try:
                    transcript_language = detect_language(
                        source_file.path, model_size=args.model_size
                    )
                except MediaToolError as exc:
                    print(
                        f"bv-generate: {recording.id}: {exc}",
                        file=sys.stderr,
                    )
                    return True

            transcript_destination = archive_path / _language_suffixed_name(
                recording.id,
                transcript_language,
                "transcript.txt",
                diarized=args.diarize,
            )
            need_transcript_write = _should_write_for(transcript_destination, args)

    if args.dry_run:
        if need_translation_write:
            print(
                f"{recording.id}: would translate -> "
                f"{translation_destination.name}"
            )
        if need_srt_write:
            print(f"{recording.id}: would write {srt_destination.name}")
        if need_lrc_write:
            print(f"{recording.id}: would write {lrc_destination.name}")
        return False

    if not (
        need_transcript_write
        or need_translation_write
        or need_srt_write
        or need_lrc_write
    ):
        return False

    if source_file is None:
        print(
            f"bv-generate: {recording.id}: no audio or video source, "
            "skipping transcription",
            file=sys.stderr,
        )
        return True

    # Reuse the .aac if one's already on disk (whether tracked from
    # the archive scan, or just written a moment ago by
    # --extract-audio earlier in this same run). Otherwise extract
    # it from the video once and leave it behind, same as
    # _do_translate_only does, instead of decoding the video
    # directly every time.
    audio_destination = archive_path / f"{recording.id}.aac"

    if audio_destination.exists():
        audio_source = audio_destination
    else:
        try:
            extract_audio(source_file.path, audio_destination)
        except MediaToolError as exc:
            print(f"bv-generate: {recording.id}: {exc}", file=sys.stderr)
            return True

        _report(
            args.verbose,
            f"{recording.id}: extracted audio -> {audio_destination.name}",
        )
        audio_source = audio_destination

    try:
        transcript = transcribe(
            audio_source,
            language=transcript_language,
            model_size=args.model_size,
        )
    except MediaToolError as exc:
        print(f"bv-generate: {recording.id}: {exc}", file=sys.stderr)
        return True

    had_error = False
    transcript_text = transcript.text
    turns: tuple = ()

    if args.diarize:
        try:
            turns = diarize(audio_source, hf_token=args.hf_token)
            transcript_text = format_diarized_transcript(
                transcript.segments, turns
            )
        except MediaToolError as exc:
            print(f"bv-generate: {recording.id}: {exc}", file=sys.stderr)
            return True

    if need_transcript_write:
        transcript_destination.write_text(
            transcript_text + "\n", encoding="utf-8"
        )
        _report(
            args.verbose,
            f"{recording.id}: wrote {transcript_destination.name}",
        )

    if need_srt_write:
        srt_destination.write_text(
            format_srt(transcript.segments, turns) + "\n", encoding="utf-8"
        )
        _report(args.verbose, f"{recording.id}: wrote {srt_destination.name}")

    if need_lrc_write:
        lrc_destination.write_text(
            format_lrc(transcript.segments, turns) + "\n", encoding="utf-8"
        )
        _report(args.verbose, f"{recording.id}: wrote {lrc_destination.name}")

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
            print(f"bv-generate: {recording.id}: {exc}", file=sys.stderr)
            had_error = True
        else:
            translation_destination.write_text(
                translated + "\n", encoding="utf-8"
            )
            _report(
                args.verbose,
                f"{recording.id}: wrote {translation_destination.name}",
            )

    return had_error


def run(args: argparse.Namespace) -> int:
    """Run bv-generate for already-parsed arguments."""

    archive_path = Path(args.path)
    archive = Archive(archive_path)

    try:
        interval = LexicalTimeParser(
            timestamp=args.timestamp,
            from_=args.from_,
            until=args.until,
        ).parse()
    except ValueError as exc:
        print(f"bv-generate: {exc}", file=sys.stderr)
        return EXIT_ARGS_ERROR

    recordings = [
        recording
        for recording in archive.recordings
        if recording.id.value in interval
    ]

    if not recordings:
        print(
            f"bv-generate: {archive_path} - no recordings found in "
            "range, nothing to do."
        )
        return EXIT_OK

    # Shared across every _should_write() call this run, so an
    # interactive "overwrite?" prompt is only ever asked once (on the
    # first existing file encountered), not once per file.
    args.overwrite_decision = _OverwriteDecision()

    had_error = False

    for recording in recordings:
        if args.extract_audio:
            had_error |= _do_extract_audio(recording, archive_path, args)

        if args.get_duration:
            had_error |= _do_get_duration(recording, archive_path, args)

        if args.transcribe or args.translate is not None:
            had_error |= _do_transcribe_and_translate(
                recording, archive_path, args
            )

    return EXIT_HAD_ERRORS if had_error else EXIT_OK


def main(argv: list[str] | None = None) -> int:
    """Run bv-generate."""

    args = parse_args(argv)
    return run_cli("bv-generate", lambda: run(args))


if __name__ == "__main__":
    raise SystemExit(main())
