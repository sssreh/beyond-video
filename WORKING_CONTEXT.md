# WORKING_CONTEXT.md

## Current Goal

`bv-generate` is feature-complete and has been exercised against real dashcam
data on Christer's machine (real ffmpeg, real faster-whisper output seen,
including a real corrupted-header Parking-mode file that led to the MP4
box-reader fallback). `--translate` and `--diarize` are built and unit-tested
but not yet confirmed run end-to-end for real (need an argos-translate
language pack installed, and a HuggingFace token + accepted model license,
respectively).

---

## Working Agreement

Verkstad.

"Much coding, little talking."

When modifying code:

- Provide complete files, never snippets.
- Give exactly one step at a time.
- Include the exact command to run.
- Wait for the result before continuing.
- Do not end responses with "one more thing."
- Do not redesign the architecture unless asked.

---

## Current Files

- src/blackvue/cli/bv_generate.py
- src/blackvue/cli/bv_ls.py
- src/blackvue/cli/bv_lang.py
- src/blackvue/generate/media.py
- src/blackvue/generate/mp4_box_reader.py
- src/blackvue/generate/speech.py
- src/blackvue/generate/language_codes.py
- src/blackvue/generate/__init__.py
- src/blackvue/archive/asset.py
- src/blackvue/archive/archive_reader.py

---

## What bv-generate does

Selection options match bv-ls: `PATH --from --until --timestamp`.

- `--extract-audio`: audio from the front video, falls back to rear if
  there's no front video. Saved as `<id>.aac`, stream-copied via ffmpeg (no
  re-encode). Skipped for Parking-mode recordings (no audio track worth
  extracting - it's a silent timelapse).

- `--get-duration`: real-world span in seconds, from front video (rear
  fallback). Parking mode (P) is a 1fps timelapse, so span is derived from
  the raw video frame count, not playback duration. Printed to stdout and
  cached to `<id>.duration.txt`. If ffprobe can't open the file (some real
  dashcam Parking-mode files carry a vestigial, malformed audio track that
  trips ffmpeg's strict container validation even though the video track is
  intact), falls back to `mp4_box_reader.py`, a dependency-free MP4 box
  walker that reads `mvhd`/`stsz` directly and never touches the broken
  audio track. This fallback isn't gated on recording kind - it applies to
  any kind (N/E/M/P) whenever ffprobe fails, not just Parking.

- `--transcribe`: faster-whisper. Also extracts and persists audio first if
  it isn't already on disk (so a bare `--transcribe` run leaves you with
  both the audio and the transcript, not just the transcript). Saved as
  `<id>.transcript.txt` for English, `<id>_<lang>.transcript.txt` (3-letter
  code, e.g. `_swe`, `_tha`) for anything else. Skipped for Parking-mode
  recordings.

- `--translate LANG`: argos-translate, arbitrary target language (accepts
  either 2-letter or 3-letter codes). Requires the language pack already
  installed locally - nothing auto-downloads, by design. Cascades through
  whatever's already on disk: reuses a cached transcript if one exists,
  otherwise reuses cached audio, otherwise extracts audio from video first -
  persisting each intermediate file it has to generate along the way. Saved
  as `<id>.translation.txt` / `<id>_<lang>.translation.txt`. Works without
  `--transcribe`.

- `--diarize`: pyannote.audio speaker labels (`[SPEAKER_00] ...`). Requires
  `--transcribe` or `--translate`, plus a HuggingFace token (`--hf-token` or
  `HF_TOKEN`/`HUGGINGFACE_TOKEN` env var) and accepting the
  `pyannote/speaker-diarization-3.1` license *and* the
  `pyannote/segmentation-3.0` license it depends on, once each on
  huggingface.co. A missing token raises a step-by-step MediaToolError
  (create a token at huggingface.co/settings/tokens, accept both model
  licenses, pass --hf-token/HF_TOKEN); a pipeline-load failure after a
  token is supplied points at both license URLs too, since "token present
  but license not accepted" is a common real-world gotcha with this model.
  Diarized transcripts/translations get their own filename marker
  (`<id>.diarized.transcript.txt`) and their own Asset type
  (`TRANSCRIPT_DIARIZED` / `TRANSLATION_DIARIZED`), tracked separately from
  the plain versions rather than overwriting them.

- `--overwrite` / interactive prompt / silent batch-skip policy for existing
  outputs (same style as bv-download's confirm()).

- `--dry-run`, `-v`.

Missing external dependencies (ffmpeg/ffprobe not on PATH, faster-whisper /
argostranslate / pyannote.audio not installed, no HF token) never crash the
run - every failure point is wrapped in `MediaToolError` and printed as one
clean `bv-generate: <id>: <message>` line to stderr, then the run continues
to the next recording. No tracebacks.

New dependencies in pyproject.toml: faster-whisper, argostranslate,
pyannote.audio. ffmpeg/ffprobe are external binaries (not pip-installable) -
must be on PATH separately.

---

## bv-lang (new)

A small companion CLI for managing argos-translate language packages, since
`--translate` deliberately never downloads anything itself (offline/private
by default):

- `bv-lang list` - list installed language packages.
- `bv-lang list --available` - list what's in the argos-translate package
  index (updates the local index first; needs network).
- `bv-lang install SOURCE TARGET` - download and install the package for
  that language pair (2- or 3-letter codes accepted either way). This is
  the only place in beyond-video that touches the network for translation,
  and only when you explicitly run it.

`speech.py`'s `translate()` error messages now point here directly, e.g.
"no argos-translate language installed for 'en' -> 'sv' - install it with
'bv-lang install en sv'".

---

## bv-ls changes

`bv-ls` picks up new Asset types automatically (`Asset.display_order()` is
fully generic - no bv-ls code changes needed per new asset). Column labels
were shortened (`Dur`, `GPX`, `Plain`/`Diar` etc.) and diarized
transcript/translation columns are grouped under a "Transcript"/"Translate"
label on a header row above the column labels, to keep the whole table
narrow enough to read on one screen.

---

## Tested vs not tested

Confirmed against real data on Christer's machine:

- `--extract-audio` and `--transcribe` (real ffmpeg, real faster-whisper
  output, including hitting and fixing the Parking-mode "no audio track"
  and "corrupted header" cases for real).

Verified in a network-less sandbox (real ffmpeg/ffprobe available there,
faster-whisper/argostranslate/pyannote.audio not installed - those three are
exercised with hand-written fakes standing in for the real libraries):

- Everything compiles and imports cleanly.
- Span calculation (including parking mode), front/rear source fallback,
  ArchiveReader detecting every new suffix (plain, language-suffixed, and
  diarized), argument parsing/validation, the overwrite policy (missing /
  exists / --overwrite / dry-run / interactive prompt / batch skip),
  diarization segment-to-turn merging, per-line diarized translation,
  language-code mapping, and the full --transcribe/--translate control flow
  including cross-run and same-run reuse of cached audio/transcripts.
- The MP4 box-reader fallback for `--get-duration`: built and validated
  end-to-end against a synthetic MP4 whose audio track reproduces the real
  dashcam's "STSC entry 0 is invalid" corruption pattern - real ffprobe in
  the sandbox genuinely refuses to open it, and get_span() correctly falls
  back and returns the right span for every recording kind (N/E/M/P).
- bv-ls's two-row header grouping logic.
- Every failure path (missing ffmpeg, missing faster-whisper/argostranslate/
  pyannote.audio, missing HF token) produces a clean one-line stderr message
  with no traceback, and lets the run continue to the next recording.
- bv-lang's list/install logic, with argostranslate.package/translate faked
  out (real argostranslate isn't installed in the sandbox either).

NOT yet confirmed for real:

- `--translate` against a real argos-translate language pack.
- `--diarize` against a real pyannote.audio pipeline with a real HF token.
- `bv-lang install` against the real argos-translate package index (only
  the missing-dependency error path was confirmed for real, via
  argostranslate genuinely not being installed in this sandbox).
- The MP4 box-reader fallback against Christer's actual corrupted file (only
  validated against a synthetic reproduction of the same corruption
  pattern).

---

## Next Task

- Run `bv-lang install SOURCE TARGET` for real to install a language pack,
  then run `--translate` for real using it.
- Run `--diarize` for real once a HuggingFace token is set and the
  pyannote license is accepted.
- Re-run `--get-duration` against the actual Parking-mode file that
  originally failed, to confirm the box-reader fallback fixes it for real
  (not just against the synthetic reproduction).

After that: decide whether docs/CLI.md and docs/GLOSSARY.md (which describe
an aspirational `--type`/`--latest`/`--match` interface that no command
actually implements) should be reconciled with what's really there.
