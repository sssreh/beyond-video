# WORKING_CONTEXT.md

## Current Goal

`bv-generate` is feature-complete and confirmed working against real dashcam
data on Christer's machine: `--extract-audio`, `--transcribe`, and now
`--diarize` have all completed successfully for real (real ffmpeg, real
faster-whisper output, real diarized transcript produced end-to-end),
including a real corrupted-header Parking-mode file that led to the MP4
box-reader fallback. Only `--translate` remains unconfirmed for real (see
below). `--diarize` in particular took five real issues, all fixed:

1. pyannote.audio's installed version renamed `use_auth_token` to `token` in
   `Pipeline.from_pretrained()` - fixed with a try-the-new-name-then-fall-
   back-to-the-old-one helper (`_load_pipeline` in speech.py).
2. `Pipeline.from_pretrained(DIARIZATION_MODEL)` (then still
   `pyannote/speaker-diarization-3.1`) reached into a second, undocumented
   gated repo (`pyannote/speaker-diarization-community-1`) for a shared
   file. Root cause turned out to be a pyannote.audio major version jump:
   `pyproject.toml` only pinned `pyannote.audio>=3.1`, so pip installed the
   latest 4.0.x (released Sep 2025) - a version built around a *new*
   default pipeline, `pyannote/speaker-diarization-community-1`, which
   replaces the legacy 3.1 one. Running 3.1 under 4.0 pulled in community-1
   assets as a side effect.
3. Decision (confirmed with Christer): switch `DIARIZATION_MODEL` to
   `pyannote/speaker-diarization-community-1` itself rather than keep
   fighting the legacy pipeline's cross-repo dependency under 4.0. It's
   also pyannote's own recommended default now, with better accuracy per
   their published benchmarks. `pyproject.toml`'s pin bumped to
   `pyannote.audio>=4.0` to match. `DEPENDENT_MODELS` is now empty (as far
   as observed, community-1 packages its own underlying models in one
   repo), but the error messages still generalize to "accept whatever repo
   a 403 names" rather than hardcoding an assumed-complete list, since that
   assumption already proved wrong once.
4. community-1's pipeline call also returns a different shape than 3.1 -
   `pipeline(path)` returns a wrapper object exposing the Annotation as
   `.speaker_diarization`, instead of returning the Annotation directly.
   `diarize()` now unwraps `getattr(output, "speaker_diarization", output)`
   so it supports either shape without knowing in advance which pipeline is
   configured.
5. Even with the model switch, the license accepted, and the token working,
   the actual audio decode step failed: pyannote.audio 4.x reads audio via
   `torchcodec`, which needs a native DLL built against the exact installed
   ffmpeg/torch version pair - on Christer's Windows machine (ffmpeg 8,
   torch 2.13.0+cpu) that DLL wouldn't load, so passing a bare file path to
   the pipeline failed with "torchcodec is not available." Fixed by not
   depending on pyannote's audio loader at all: `diarize()` now decodes
   audio itself via a direct `ffmpeg` subprocess call
   (`_load_waveform_via_ffmpeg` in speech.py, reusing the same tool this
   project already shells out to elsewhere) straight to the
   `{'waveform': tensor, 'sample_rate': 16000}` in-memory form pyannote's
   own docs say is supported, sidestepping torchcodec's DLL matching
   entirely rather than trying to fix it on Christer's machine.

Confirmed: `bv-generate --transcribe --diarize` produced a real diarized
transcript against Christer's actual archive after all five fixes above.
`--translate` is still only unit-tested, not yet confirmed end-to-end for
real (needs an argos-translate language pack installed via
`bv-lang install`).

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
- src/blackvue/generate/subtitles.py
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

- `--diarize`: pyannote.audio speaker labels (`[SPEAKER_00] ...`), using the
  `pyannote/speaker-diarization-community-1` pipeline (pyannote's current
  recommended default, replacing the legacy speaker-diarization-3.1 - see
  "Current Goal" above for why). Requires `--transcribe` or `--translate`,
  plus a HuggingFace token (`--hf-token` or `HF_TOKEN`/`HUGGINGFACE_TOKEN`
  env var) and accepting that model's license on huggingface.co. A missing
  token raises a step-by-step MediaToolError (create a token at
  huggingface.co/settings/tokens, accept the model license, pass
  --hf-token/HF_TOKEN); a pipeline-load failure after a token is supplied
  tells you to accept whatever repo the underlying 403 names, since
  pyannote.audio's own exception text always names the exact one - don't
  assume the known-dependency list is complete, it's proven wrong before.
  Audio is decoded via a direct `ffmpeg` subprocess call
  (`_load_waveform_via_ffmpeg`) rather than handed to pyannote.audio as a
  file path, to avoid depending on `torchcodec` - see "Current Goal" #5.
  Diarized transcripts/translations get their own filename marker
  (`<id>.diarized.transcript.txt`) and their own Asset type
  (`TRANSCRIPT_DIARIZED` / `TRANSLATION_DIARIZED`), tracked separately from
  the plain versions rather than overwriting them.

- `--srt` / `--lrc`: also write `<id>.srt` / `<id>.lrc` sidecar files with
  per-segment timestamps from the transcript. Requires `--transcribe` or
  `--translate`. See "SRT/LRC subtitle export" below for details and the
  cache-reuse caveat with bare `--translate`.

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

## bv-download --trace (done, this session)

Christer asked for a progress indicator on long downloads: a `.` printed for
every 10MB (`TRACE_INTERVAL_BYTES`; started at 50MB, tightened to 10MB the
same session after Christer asked whether it should instead be percentage-
based - kept byte-based: a percentage needs the whole run's total size
upfront, meaning HEAD-requesting every candidate file before the first dot
can print, and the dot spacing would vary a lot with run size, e.g. 5% of
10GB is a 500MB wait between dots - byte-based avoids both problems)
downloaded, across the whole run (not reset per file) - a "still alive"
signal, not a percentage. New `DotProgress` class in
`blackvue.cli.bv_download`, used as the `on_bytes` callback (see below);
`finish()` closes the line with a trailing newline, but only if at least one
dot was ever printed, so a `--trace` run that downloads nothing doesn't emit
a stray blank line. Wired with a `try/finally` around the download loop so
`finish()` still runs (closing the dot line cleanly) even if Ctrl-C fires
mid-download - pairs naturally with this session's earlier clean-Ctrl-C fix.

To get byte counts up to the CLI layer, `BlackVueClient.download()` and
`BlackVueCamera.download()` both gained an optional `on_bytes: Callable[[int],
None] | None` parameter - called with the size of each chunk actually
written (video files download in 64KB chunks and report per chunk; metadata
files download in one shot and report once). Backward compatible:
`on_bytes=None` (the default) changes nothing for existing callers.

---

## SRT/LRC subtitle export (done, this session)

Christer asked "any way to get timestamps in transcribe" - `SpeechSegment`
(faster-whisper) and `SpeakerTurn` (pyannote) already carry start/end timing
internally, but it was being discarded when transcripts were written to disk
(only the flattened `.text` string was ever persisted). Rather than inventing
a bespoke timestamp notation, exports to two standard, widely-supported
formats instead:

- `--srt`: numbered cues with `HH:MM:SS,mmm --> HH:MM:SS,mmm` per line -
  the common video-subtitle format almost every player understands. Saved
  as `<id>.srt`.
- `--lrc`: one `[mm:ss.xx] text` line per segment (start time only, no
  explicit end) - the karaoke/lyrics-sync convention, a lighter-weight
  option when you just want to scrub through a conversation. Saved as
  `<id>.lrc`.

Both require `--transcribe` or `--translate` (same validation pattern as
`--diarize`), and both prefix each line with `[SPEAKER_XX]` when `--diarize`
is also given, matching `format_diarized_transcript`'s convention. New
`blackvue.generate.subtitles` module (`format_srt`, `format_lrc`); renamed
`speech._speaker_for` to the now-public `speech.speaker_for` so subtitles.py
can reuse the same segment-to-speaker matching. New `Asset.SUBTITLES` /
`Asset.LYRICS` (`.srt`/`.lrc` suffixes registered in `ArchiveReader.ASSETS`)
- picked up by `bv-ls` automatically, no bv-ls changes needed.

Filenames are always `<id>.srt`/`<id>.lrc` - no language-suffix variants,
and scoped to the transcript only (not translations) for this first
version. In `_do_transcribe_with_optional_translate` (the `--transcribe`
path), a missing `.srt`/`.lrc` now counts toward the "does this recording
need any work" check even if the transcript/translation files themselves
are already up to date, so `--srt`/`--lrc` alone against an already-
transcribed archive still triggers a fresh Whisper run to get segment
timing.

**Two bugs found and fixed against Christer's real archive**, both in
`_do_translate_only` (bare `--translate`, no `--transcribe`):

1. It cache-first reuses an existing plain-text transcript when one
   exists - a cached `.transcript.txt` has no segment timing, so the
   original version silently produced no `.srt`/`.lrc` at all whenever
   that cache-hit path was taken, with no warning. Fixed by skipping the
   transcript-reuse cache whenever an `.srt`/`.lrc` actually needs
   writing, forcing a fresh `transcribe()` call so there's real segment
   timing to draw from.
2. Christer re-ran with the fix above applied and *still* got nothing -
   root cause was one level up: the whole function was gated on a single
   `_should_write` check for `translation.txt` alone. He'd already run
   plain `--translate` once, so `translation.txt` already existed; without
   `--overwrite`, that gate returned early before the function ever
   reached the srt/lrc-writing code, regardless of fix #1. Fixed by
   computing `need_translation_write`/`need_srt_write`/`need_lrc_write`
   independently up front (mirroring the pattern
   `_do_transcribe_with_optional_translate` already used) and gating on
   *any* of the three needing work, not just translation. Also tightened
   the cache-bypass from fix #1 to trigger on `need_srt_write or
   need_lrc_write` (whether a subtitle file actually needs writing)
   rather than `args.srt or args.lrc` (whether the flags were merely
   given) - avoids an unnecessary re-transcribe when srt/lrc are already
   up to date and only translation.txt needed refreshing. The final
   translate-and-write step is now also gated on `need_translation_write`,
   so a run that only needed to (re)write `.srt`/`.lrc` doesn't
   needlessly re-translate and overwrite an already-current
   `translation.txt`.

Fixed and unit-tested here (including a regression test reproducing
Christer's exact scenario: pre-existing `translation.txt`, missing
`.srt`/`.lrc`, no `--overwrite`); not yet reconfirmed by Christer against
the real archive that surfaced both bugs.

While chasing the above, Christer also hit a third, unrelated issue: he ran
`bv-generate` without a `PATH` argument (defaults to `.`, unlike his
`bv-export` commands which always included it explicitly) - zero recordings
matched, and `bv-generate` printed nothing at all, no error, no hint. Two
small UX fixes for this, both in `run()`:

- **"No recordings found" message (done, this session).** `bv-generate` now
  prints `bv-generate: <path> - no recordings found in range, nothing to
  do.` and returns `EXIT_OK` when the `PATH --from --until --timestamp`
  selection matches zero recordings, instead of silently doing nothing -
  same convention `bv-export` already used. Catches a wrong/omitted path,
  a `--timestamp` that doesn't match anything, or a genuinely empty
  archive, not just the missing-argument case that triggered this.
- **Ask-once overwrite prompt (done, this session).** Christer separately
  asked for this: the interactive "`<file>` already exists. Overwrite?"
  prompt used to fire once per existing output file, which is painful
  against an archive with many recordings and several output types
  (transcript, translation, srt, lrc, ...). New `_OverwriteDecision`
  (a small callable that asks once, on the first existing file, then
  caches the answer for the rest of the run) - one instance created in
  `run()` per invocation, stashed on `args.overwrite_decision`, and
  threaded through every `_should_write()` call via a new
  `_should_write_for(path, args)` convenience wrapper that replaced all
  12 call sites' repeated `overwrite=args.overwrite, dry_run=args.dry_run`
  boilerplate. `_should_write()` itself stays backward compatible -
  `overwrite_decision=None` (the default) falls back to asking every
  time, so any caller that doesn't have a decision object still works.
  `--overwrite`/`--dry-run`/non-interactive batch-skip behavior are all
  unchanged; this only changes how many times the interactive prompt
  itself fires.

---

## SRT/LRC trip-level merge for bv-export (done, this session)

Gap Christer spotted: `bv-generate --srt/--lrc` writes one `.srt`/`.lrc` per
*recording*, but `bv-export` wasn't merging them into a trip-level file the
way it already merges `transcript.txt`/`translation.txt` - so a
multi-recording trip ended up with several separate per-recording subtitle
files sitting in the archive instead of one `trip.srt`/`trip.lrc` in the
export folder, timestamps rebased onto the trip's timeline.

Text concatenation alone (what `merge_text_assets` does for transcripts)
isn't enough here - subtitle timestamps are relative to each recording's
own start, so simply gluing files together would leave every recording
after the first with wrong (too-early) cue times. Needed the same
offset-rebasing pattern `_merge_gsensor` already uses for `.3gf`.

New `blackvue.generate.subtitles.parse_srt()` / `parse_lrc()` - symmetric
readers for the existing `format_srt()`/`format_lrc()` writers, parsing
cues back into `SpeechSegment`s (any baked-in `[SPEAKER_XX]` prefix from a
diarized export is left as opaque text, not parsed back out - matches how
the formatters treat it when `turns=None`). Cue index numbers are
discarded on read since `format_srt()` renumbers sequentially on write
anyway. LRC has no explicit end time, so `parse_lrc()` sets each segment's
`end` equal to its `start` - fine, since `format_lrc()` never reads `end`.

New `blackvue.export.subtitles` module: `merge_srt(trip)` / `merge_lrc(trip)`
read every recording's `.srt`/`.lrc` in the trip (skipping recordings that
don't have one), shift each recording's cues by `recording.id.timestamp -
trip.start_timestamp` (same rebase math as g-sensor), sort the combined
cues by start time (so recordings processed out of chronological order
still merge correctly), and re-format/renumber as one trip-relative string.
Returns `None` if no recording in the trip has the asset - same "nothing
to work with" convention as the rest of `export_trip`'s outputs.

`export_trip()` writes `trip.srt`/`trip.lrc` unconditionally whenever there's
something to merge (no opt-in flag needed, unlike `--map` - this is pure
local text processing, no network, negligible cost). New
`ExportResult.srt`/`ExportResult.lrc` fields, included in `bv-export`'s
written-file count.

Confirmed against Christer's real archive (this is what surfaced both
`--translate --srt --lrc` bugs above, and the padding gap below).

---

## Pad merged subtitles to the real video length (done, this session)

Christer noticed, using his real archive: the last ~2 minutes of a trip had
no speech, so Whisper (correctly) emitted no segments for that silence -
but that meant the merged `trip.srt`/`trip.lrc` ended ~2 minutes before the
video actually did. Not a bug, just how Whisper works, but he wanted the
subtitle file's timeline to match the video's real length.

`_pad_to_duration()` (new, in `blackvue.export.subtitles`) appends one
empty trailing cue when the merged cues end before a given
`total_duration_seconds`, no-op otherwise (nothing to pad to, no real
content, or content already reaches the end). The padding cue starts
within the final second before `total_duration_seconds` (not exactly at
the last real cue's end, and never earlier than it) - avoids a
zero-duration SRT cue some players might reject, and for LRC (which only
has a start time, no end) puts the empty marker near the actual end of the
video rather than redundantly at the same spot as the last real line.
`merge_srt()`/`merge_lrc()` both gained an optional
`total_duration_seconds` keyword-only param that's threaded through to
this.

`export_trip()` gets that duration by probing (`generate.media.probe()`)
the concatenated `front.mp4`/`rear.mp4` it just wrote - not by summing
recordings' own `.duration.txt` files, which may not all exist (needs
`bv-generate --get-duration` to have been run) and wouldn't necessarily
match the actual concatenated output anyway. A probe failure degrades to
a warning (`ExportResult.warnings`), same resilience pattern as the rest
of `export_trip`'s optional outputs - the trip's other files are still
worth having even if duration probing fails. If there's no front/rear
video in the trip at all, padding is silently skipped (nothing to match
subtitle length to).

---

## bv-export: ask before wiping an existing trip folder (done, this session)

Christer asked: if he exports with `--map` (builds the expensive `map.mp4`),
then later re-exports the same trip without `--map`, does that second run
delete the map video? Previously yes - `bv-export` unconditionally did
`shutil.rmtree(folder)` on any existing trip folder before every export.
Christer's answer, once asked: in interactive mode ask whether to wipe or
keep; otherwise (batch/cron, nobody to ask) always keep, since re-generating
the map is expensive.

New behavior in `bv_export.py`:

- Default (no `--overwrite`): an existing trip folder is left in place -
  the run only overwrites whatever files it actually regenerates this time,
  so an earlier `--map` run's `map.mp4` survives a later plain export.
- `--overwrite` (new flag): wipes and rebuilds every trip folder from
  scratch, without asking - the old unconditional behavior, now opt-in.
- Without `--overwrite`, in an interactive run (`_interactive()`, same
  `sys.stdin.isatty() and sys.stdout.isatty()` check `bv-generate` uses):
  asks once, on the first trip folder that already exists
  (`_ask_wipe_existing()`, `[w/K]`, defaults to keep on empty input), and
  reuses that same answer (`wipe_decision`) for every other trip folder
  touched in the run - same "ask once per run" pattern as `bv-generate`'s
  overwrite prompt.
- Without `--overwrite`, non-interactive (batch/cron): always keeps, never
  asks, never wipes.
- `--dry-run` output now reflects this: reports "create" for a new folder,
  "wipe and rebuild" only when `--overwrite` is given, "update in place"
  otherwise.

Tested (10 tests): default-keeps-existing-files, `--overwrite`-wipes,
interactive-prompt-wipes-on-"w", interactive-prompt-keeps-on-default-empty-
answer, interactive-prompt-only-asked-once-across-two-trip-folders, and the
literal scenario Christer asked about - export with `--map` builds
`map.mp4`, a later plain non-interactive export leaves `map.mp4`
untouched (mtime unchanged).

---

## G-sensor dot-gauge overlay video (done, this session)

Christer asked for "some nice graphical video with the g-sensor data."
Asked which style; he picked a racing-telemetry-style dot gauge (dial with
a dot at the current x/y reading, short fading trail) over a scrolling
line-graph alternative.

New `gsensor.mp4`, off by default, opt in via `bv-export --gsensor-video`:

- `blackvue.export.gsensor_render`: `render_frame(scale, trail_points,
  position, *, timestamp_text=None, ...)` draws reference rings/axes, the
  fading trail, the current dot, and an optional wall-clock caption -
  same Pillow-drawing shape as `map_render.render_frame`.
  `scale_for_samples(samples, *, padding=1.2, minimum=1.0)` sets the
  gauge's outer-ring value from the trip's own observed peak |x|/|y|
  (floored at `minimum` so a flat/parked trip doesn't divide by ~0) -
  since the g-sensor's raw units aren't calibrated (see
  `gsensor_reader.py`'s module docstring: could be milli-g, raw ADC
  counts, or something else), this scales to the trip's own range rather
  than claiming any absolute g-force value. Axes are labeled X/Y, not
  "lateral"/"braking" - which physical direction each axis corresponds to
  isn't confirmed either.
- `blackvue.export.gsensor_video`: `interpolate_sample(samples, elapsed)`
  (same linear-interpolate-between-two-bracketing-points shape as
  `map_video.interpolate_position`, clamped at the ends) and
  `render_gsensor_video(samples, destination, *, fps=10,
  start_timestamp=None)`. `fps=10` matches the g-sensor's native ~100ms
  sample spacing (see `gsensor_reader.py`) rather than inventing detail
  interpolation doesn't have. A `DEFAULT_TRAIL_LENGTH = 8`-sample fading
  trail shows the shape of a turn/braking event, not just an instantaneous
  reading.
- `export_trip(..., render_gsensor=True)` reuses the trip's already-merged
  g-sensor samples (the same ones written to `trip.3gf`) - no extra file
  I/O. Degrades to a warning (`ExportResult.warnings`), not a failed
  export, on any ffmpeg problem - same resilience pattern as `--map`.
- Small refactor along the way: extracted the "encode a directory of
  frame_%06d.png into a video via ffmpeg" block (previously only inside
  `map_video.render_map_video`) into a shared
  `blackvue.export.media.encode_frame_sequence()`, since gsensor_video.py
  needed the identical logic - `map_video.py` now calls it too, no
  behavior change there (existing map tests still pass unmodified).

Tested (23 new tests: 5 `test_gsensor_render`, 8 `test_gsensor_video`, 3
wiring tests in `test_trip_export`, 2 CLI flag tests in `test_bv_export`) -
real ffmpeg encoding exercised end-to-end, plus a rendered frame visually
sanity-checked (dot, trail, rings, timestamp all present as expected). Not
yet confirmed against Christer's real archive - only unit-tested with
synthetic g-sensor data so far.

**Follow-up: center the gauge on the trip's own baseline (done, this
session).** Christer tried it against his real archive - worked, but asked
why the dot wasn't in the center. Cause: the gauge always drew raw (0, 0)
at its center, but a real accelerometer (mounted at even a slight angle,
or with its own bias) rarely reads exactly zero at rest, so the dot sat
off to one side the whole trip. Asked how to pick a center; Christer chose
the trip's own median reading over an alternative (average the first few
seconds, assuming the trip starts stationary/level).

- `gsensor_render.baseline_for_samples(samples) -> (x, y)`: median x and
  median y across the trip (median, not mean, so a stretch of hard
  turns/braking doesn't pull the baseline off to one side).
- `scale_for_samples()` gained a `baseline` keyword-only param (default
  `(0.0, 0.0)`, so old callers/tests are unaffected): measures peak
  deviation from `baseline` instead of from raw (0, 0).
- `render_gsensor_video()` computes the baseline once per trip and
  subtracts it from every interpolated (x, y) before it reaches
  `render_frame()` - the gauge itself (`render_frame()`) didn't need to
  change at all, it was already just drawing whatever (x, y) it's handed
  relative to the image center.

Tested: 5 new tests (baseline median calculation - odd/even sample counts,
empty input; scale-with-baseline; and a `render_gsensor_video()` wiring
test that monkeypatches `render_frame` to capture the position it's
called with, confirming a sample exactly matching the baseline renders at
the gauge's center). Full suite green (269 passed). Re-rendered a
synthetic frame with a constant offset baked into every sample (like a
tilted mount) and visually confirmed the trail now wobbles around the
center instead of sitting off to one side.

**Follow-up: drop the timestamp, chroma-key green background (done, this
session).** Two more requests once Christer had it running: remove the
wall-clock caption, and make the video "transparent, like green screen" so
it can be composited over the front/rear footage later (--stitch, still
future). h264/mp4 has no alpha channel, so real transparency isn't an
option in this container - a flat chroma-key background is the standard
way to get the same effect via ffmpeg's `colorkey`/`chromakey` filters at
composite time.

- Removed the timestamp caption entirely: `render_frame()` lost its
  `timestamp_text` param and the font-loading machinery that only existed
  for it; `render_gsensor_video()` lost `start_timestamp`;
  `export_trip()`'s call site no longer passes `trip.start_timestamp`.
- `BACKGROUND_COLOR` changed from the cream tone shared with `map_render.py`
  to pure `(0, 255, 0)` - a single flat RGB value with no anti-aliasing
  blend to account for (Pillow's basic `ImageDraw` doesn't anti-alias),
  the simplest possible target for a chroma-key filter to match exactly.
  `RING_COLOR`/`AXIS_COLOR` changed from light grey to white for contrast
  against the now-saturated green background (previously calibrated
  against the cream background instead).

Tested: 1 new test confirming the background is exactly `(0, 255, 0)`, the
wall-clock-caption test removed (feature gone). Full suite green (269
passed). Re-rendered a synthetic frame and visually confirmed a flat green
background with no timestamp, dot/trail/rings still clearly legible.

---

## map.mp4: rotating direction arrow, optional custom icon (done, this session)

Christer asked whether a car icon instead of the plain position dot would be
a good idea (yes - rotated to match direction of travel, it conveys more
than a static dot) and whether it would cost much render time (no -
negligible next to the existing per-frame PNG write and final ffmpeg
encode, which dominate). Asked which to build first; chose the arrow now,
with the option to try a custom car image later.

- `map_video._interpolate_course(a, b, t)`: circular interpolation between
  two compass courses (0-360 degrees, from the GPS fix's existing `course`
  field, previously unused) - a plain linear interpolation breaks at the
  0/360 wraparound (350 -> 10 degrees is a 20-degree turn through north,
  not a 340-degree turn back through 180). Falls back to whichever course
  is present if one fix's is `None` (empty in the raw NMEA data). Folds a
  floating-point edge case (result landing on exactly `360.0` instead of
  `0.0`) back to `0.0`.
- `interpolate_position()` now returns `(lat, lon, speed_kmh, course)`
  instead of a 3-tuple - a breaking change to this function's signature,
  updated at its one caller (`render_map_video()`) and in tests.
- `map_render._arrow_points(center, heading_degrees, ...)`: returns the 3
  corners of a triangle pointing at `heading_degrees`, computed directly
  with trig (no image asset, consistent with everything else this module
  draws) - cheap enough per frame to be a non-issue.
- `render_frame()` gained `heading` and `marker_image` params: draws the
  arrow when `heading` is given, a custom image (rotated via
  `Image.rotate()`, RGBA alpha-pasted) when `marker_image` is given
  instead, or the original plain dot when neither is available (e.g. a
  single-fix/stationary trip with no course to point at) - so existing
  behavior is unchanged for callers that don't pass either.
- New `bv-export --map-icon PATH`: loads a custom image once per trip (not
  per frame) via `render_map_video(..., marker_image_path=...)`, threaded
  through `export_trip(..., map_icon=...)`. A bad path raises
  `MediaToolError`, degrading to a warning through the same path
  `--map`'s other failure modes already use - the rest of the trip's
  export still succeeds. Christer doesn't have a car image yet; he'll
  supply his own PNG (transparency recommended, drawn pointing "up"/north
  in its own file) when he wants to try it.

Tested (12 new tests across `test_map_render`/`test_map_video`, plus 2 map
wiring tests in `test_trip_export` and 1 CLI flag test in `test_bv_export`):
arrow geometry at known headings (0/90 degrees), course-interpolation
wraparound and None-fallback, custom-icon loading/rotation/paste (verified
pixel-exact at heading 0, where no rotation should occur), and the missing-
icon-file warning path. Full suite green (281 passed). Also visually
confirmed via two rendered frames (heading 45 and 180) that the arrow
points the correct real-world direction, not just a plausible-looking one.

---

## GPU auto-detect with CPU fallback (done, this session)

Christer asked whether video generation uses his NVIDIA GPU at all (no -
`libx264` for map/gsensor encoding, `device="cpu"` for Whisper, nothing
touches CUDA anywhere) and whether that'd be worth changing. Didn't know
his card's NVENC support or whether CUDA/cuDNN is set up, so rather than
asking him to go find out, both paths now auto-detect and fall back to
CPU on their own - no configuration needed either way, works the same
regardless of what's actually on his machine.

- `export.media._nvenc_available()`: runs `ffmpeg -encoders` once per
  process (cached in a module global) and checks for `h264_nvenc`.
  `encode_frame_sequence()` (shared by `map.mp4`/`gsensor.mp4`) tries
  `h264_nvenc` first when listed, but *also* falls back to `libx264` if
  the NVENC attempt itself fails (encoder listed but no compatible
  GPU/driver actually present, say) - only raises if the CPU encoder
  fails too. A wrong "available" guess costs one failed attempt, never a
  broken export.
- `generate.speech._load_whisper_model()`: tries
  `WhisperModel(..., device="cuda", compute_type="float16")` first,
  falls back to today's `device="cpu", compute_type="int8"` on any
  exception - faster-whisper/CTranslate2 raise different exception types
  for "no GPU," "driver too old," "cuDNN/cuBLAS missing," etc., so this
  catches broadly rather than trying to enumerate them all.
- Neither path prints which one it picked (in scope for a later request,
  not part of this one) - real GPU usage is checkable externally via
  `nvidia-smi` while a `--transcribe`/`--map`/`--gsensor-video` run is in
  progress. The CUDA Whisper path also needs matching `nvidia-cudnn-cu12`/
  `nvidia-cublas-cu12` (or a system CUDA install) alongside faster-whisper
  itself - if those aren't present it silently uses CPU, same as before
  this change, just without a hard requirement to have them.

Tested: 8 new tests (`_nvenc_available` detection + caching,
`encode_frame_sequence` preferring/falling back between encoders including
one real fallback exercised end-to-end with actual ffmpeg since this
sandbox has no real NVENC hardware, `_load_whisper_model` CUDA-success and
CUDA-failure-falls-back-to-CPU with a fake `faster_whisper` module, plus a
genuine missing-dependency test since faster-whisper truly isn't installed
in this sandbox). Full suite green (291 passed). Not yet run on Christer's
real machine/GPU - the NVENC and CUDA success paths are only exercised via
fakes here, since this sandbox has neither.

---

## map.mp4: zoomed-in "follow camera" mode (done, this session)

Christer asked for an option to make the map more zoomed in and scroll as
the vehicle moves - the existing map framed the whole trip at once (a
static overview, same every frame), which is fine for seeing the whole
route but too small-scale to read individual streets/turns.

New `bv-export --map-zoom [METERS]`:

- `osm_roads.bounding_box_around_point(lat, lon, radius_meters)`: a
  square-ish bounding box of real-world half-width `radius_meters`,
  centered on a point - widens the longitude delta by `1/cos(latitude)`
  so the box is the same real-world size in both directions regardless of
  latitude (the same correction `map_render._project()` already applies
  the other way, when converting lat/lon into pixels). Floors at
  `MIN_ZOOM_RADIUS_METERS` (5m) so a `--map-zoom 0` or negative value
  can't produce a degenerate box.
- `render_map_video()` gained `zoom_meters`: when given, every frame gets
  its own bounding box from `bounding_box_around_point()` centered on
  that frame's own interpolated position, instead of reusing the single
  whole-trip `bbox` passed in - this is what makes the rendered map
  scroll/pan as the vehicle moves, since the position marker always
  lands center-frame (the box is centered on it every time) while the
  roads/route around it shift frame to frame.
- Road data is still fetched/cached for the *whole trip's* bounding box
  as before (unrelated to per-frame camera framing) - the follow camera
  only needs a small area on screen at once, but which small area varies
  every frame, so all of the trip's road context has to already be
  available.
- `--map-zoom` takes an optional value in meters (`DEFAULT_ZOOM_RADIUS_METERS
  = 120.0` if given with no value - roughly a 240m-wide street-level
  view); omitted entirely, `--map` keeps its original static-overview
  behavior unchanged.

Tested: 3 new tests on `bounding_box_around_point` (equator vs. higher-
latitude widening, floor at the minimum radius), 2 on `render_map_video`
(static bbox unchanged by default; per-frame bbox actually differs
frame-to-frame and is smaller than the trip-wide box when zoomed), plus
wiring tests in `test_trip_export`/`test_bv_export` (including the
`--map-zoom`-with-no-value-uses-the-default and value-omitted-stays-None
argparse cases). Full suite green (301 passed). Also visually confirmed
with three rendered frames along a straight route: the position marker
stays centered while a cross-street scrolls into view as the vehicle
approaches it - along a perfectly straight stretch, consecutive frames
look nearly identical, which is the *correct* follow-camera behavior
(same as a real nav app), not a bug.

---

## gsensor.mp4: black target rings (done, this session)

Christer asked for "a well defined target in black" on the g-sensor gauge -
ambiguous enough (by his own admission) that guessing risked a redo.
Rendered three real mockups against the actual green background - (A)
today's rings/crosshair recolored black, red dot/trail unchanged, (B) a
full bullseye with alternating black/white rings, (C) rings left white but
the marker itself turned into a black crosshair reticle - and asked him to
pick. He chose (A).

`gsensor_render.py`: `RING_COLOR`/`AXIS_COLOR` changed from white
`(255, 255, 255)` to black `(0, 0, 0)`. `TRAIL_COLOR`/`DOT_COLOR` (red)
and `DOT_OUTLINE` (white) unchanged. No test changes needed - nothing
asserted on the specific ring/axis color values, only that rendering
without error still produces the expected non-background pixels;
confirmed by re-running the full suite (still 301 passed).

---

## Diagnose NVENC not being used on Christer's real machine (done, this session)

Christer reported bv-export "just uses Intel graphics", never the RTX 5090.
Root-caused step by step rather than guessing:

- `ffmpeg` wasn't on PATH at all - only private copies bundled inside
  BlueStacks and CapCut existed, neither reachable by `subprocess.run(["ffmpeg", ...])`.
  A winget-installed Gyan.FFmpeg build existed but also wasn't on PATH.
  Fixed by adding its `bin` folder to the User PATH.
- Once `ffmpeg -encoders` listed `h264_nvenc` and `nvidia-smi` saw the 5090,
  a direct `ffmpeg ... -c:v h264_nvenc` test still failed: "Driver does not
  support the required nvenc API version. Required: 13.1 Found: 13.0" - the
  installed NVIDIA driver (591.91) was too old for this ffmpeg build's NVENC
  API. Fixed by updating the driver via NVIDIA App.
- After the driver update, the same manual `h264_nvenc` test succeeded, and
  a real `bv-export --map` run's output file confirmed
  `TAG:encoder=Lavc62.28.102 h264_nvenc` via `ffprobe` - proof from the file
  itself, since `encode_frame_sequence()`'s NVENC-then-CPU-fallback silently
  swallows a failed NVENC attempt with no warning, so a successful export
  alone never proves which encoder actually ran.
- Windows Task Manager's per-GPU "Video Encode" graph showed 0% even during
  a real NVENC run - not a bug, just Task Manager's ~1s sampling interval
  missing an encode that finishes in a fraction of a second. The `ffprobe`
  encoder tag remains the reliable check, not the graph.

No code changes were needed for this one - it was entirely a machine setup
issue (PATH, driver version). `encode_frame_sequence()`'s silent CPU fallback
was flagged as a real (if separate) observability gap - not fixed yet, since
by the time it came up NVENC was already confirmed working and the more
pressing issue turned out to be render speed (see next section).

---

## Speed up map.mp4 frame rendering (done, this session)

With NVENC confirmed working, Christer noticed `bv-export --map --map-zoom 240`
still took 2m34s for a ~6-minute trip and asked why. Traced with
`Measure-Command`: at `DEFAULT_FPS = 5`, a 6-minute trip is ~1,800 frames, and
NVENC only accelerates the final encode step - turning already-drawn PNGs into
video - which is a small fraction of that time. The real cost was in
`map_render.render_frame()`, called once per frame, entirely on CPU:

- Every frame redrew *every* road in the trip's whole dataset, even in
  `--map-zoom` follow-camera mode where each frame's bounding box only
  covers a small street-level sliver - almost all of that per-frame drawing
  was for roads far off-canvas.
- `_load_font()` reopened and re-parsed the same TrueType font file from disk
  on every single frame instead of once for the whole export.

Fixes:

- `osm_roads.py`: new `index_roads(roads)` precomputes each road's own
  (min/max lat/lon) bounding box once; new `roads_within_bbox(indexed_roads,
  bbox)` does a cheap rectangle-overlap test per road (not a real geometric
  intersection - a road that only grazes a corner of the frame can pass too,
  which is harmless, just occasionally draws one extra off-canvas line).
- `map_video.py`: in `--map-zoom` mode, `indexed_roads` is computed once
  before the frame loop, and each frame now calls `render_frame()` with
  `roads_within_bbox(indexed_roads, frame_bbox)` instead of the full,
  unfiltered `roads` tuple. Static (non-zoomed) mode is unchanged - every
  road is already relevant to the one whole-trip bbox there, nothing to
  filter.
- `map_render.py`: `_load_font()` now caches the loaded font in a
  module-level `_CACHED_FONT`, loaded once on first use instead of every
  frame.

Tested: 3 new tests on `roads_within_bbox` (overlapping, partially-crossing,
and no-overlap cases), 2 on `render_map_video` (zoomed mode filters roads
per-frame and drops a far-away road; static mode still passes every road
through unfiltered), 1 on `_load_font` (calls `ImageFont.truetype` only once
across two calls). Full suite green (309 passed). Not yet re-timed against
Christer's real archive to confirm the actual wall-clock improvement.

---

## Fix: trip.srt running longer than the video/trip.lrc (done, this session)

Christer noticed on his real archive: the merged `trip.srt` ran a couple of
seconds longer than both the actual video and `trip.lrc`. Root cause was in
`transcribe()` (`blackvue.generate.speech`), not in the merge/padding code:
faster-whisper decodes in fixed-size internal chunks, so a segment's end
timestamp - the last one especially - can land slightly past the real audio
length. That inflated end time then survives untouched through per-recording
`.srt` -> trip-level rebase/merge -> `trip.srt`.

`trip.lrc` didn't show the same overrun because LRC has no end timestamp at
all - `parse_lrc` sets `end=start`, so the padding check in
`_pad_to_duration` (`last_end = max(segment.end for segment in segments)`)
was comparing against each line's *start*, not an inflated end, and mostly
happened not to exceed the video length by coincidence. That made `trip.lrc`
look "correct" for the wrong reason, not because the underlying timing was
actually more accurate - the real bug was upstream of both.

Fix: `transcribe()` now clamps every segment's `start`/`end` to
`info.duration` - faster-whisper's own measurement of the real audio length
from the same decode, so no extra ffprobe call needed. This fixes it at the
source, for `bv-generate`'s own per-recording `.srt`/`.transcript.txt`/etc.
too, not just the trip-level merge.

Tested: `test_transcribe_clamps_segment_timestamps_to_the_real_audio_duration`
(new, `test_speech.py`) - a fake whisper model returns a segment ending at
121.5s against a 120.0s `info.duration`, asserts the returned `SpeechSegment`
is clamped to 120.0s. Not yet confirmed against Christer's real archive (the
original report was against real data, but this specific fix hasn't been
re-run against it yet).

---

## Tested vs not tested

Confirmed against real data on Christer's machine:

- `--extract-audio` and `--transcribe` (real ffmpeg, real faster-whisper
  output, including hitting and fixing the Parking-mode "no audio track"
  and "corrupted header" cases for real).
- `--diarize` (real pyannote.audio, real HuggingFace token/license flow,
  real ffmpeg-decoded audio, a real diarized transcript produced) - see
  "Current Goal" for the five issues hit and fixed along the way.

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
- `bv-lang install` against the real argos-translate package index (only
  the missing-dependency error path was confirmed for real, via
  argostranslate genuinely not being installed in this sandbox).
- The MP4 box-reader fallback against Christer's actual corrupted file (only
  validated against a synthetic reproduction of the same corruption
  pattern).

Sandbox limitation discovered this session: this sandbox's `python3` is 3.10,
but the project targets `>=3.13` (pyproject.toml) and `camera_config.py` uses
stdlib `tomllib`, which doesn't exist before 3.11. `bv_download.py`,
`bv_config.py`, and `camera_config.py` (and their test files) simply can't
be imported here at all - that's why `run_harness.py` never registered
`test_bv_download`/`test_bv_config`/`test_camera_config`, going all the way
back to whenever those files were first written, not something broken this
session. New tests added to `test_bv_download.py` this session (see
`--trace` below) were verified by copying the exact same logic into a
throwaway standalone script and running the same assertions against it
outside the broken import chain - not a substitute for actually running
`test_bv_download.py`, which still needs a real run on Christer's machine
(Python 3.13) to be fully confirmed.

---

## Next Task

- Run `bv-lang install SOURCE TARGET` for real to install a language pack,
  then run `--translate` for real using it.
- Re-run `--get-duration` against the actual Parking-mode file that
  originally failed, to confirm the box-reader fallback fixes it for real
  (not just against the synthetic reproduction).

After that: decide whether docs/CLI.md and docs/GLOSSARY.md (which describe
an aspirational `--type`/`--latest`/`--match` interface that no command
actually implements) should be reconciled with what's really there.

---

## Clean CLI error handling (done, this session)

Christer noticed a bad archive path (missing, or a file instead of a
folder) and Ctrl-C mid-run both dumped a raw Python traceback instead of a
short message - across every bv-* command, since none of them caught
`OSError` (what `os.scandir()` raises from inside `Archive()`/
`ArchiveReader.read()`) or `KeyboardInterrupt` anywhere.

New `blackvue.cli.errors.run_cli(prog, main)` wraps a command's body,
catching just those two failure modes and printing one `<prog>: <message>`
line on stderr instead: `OSError` -> exit 1 (`str(exc)` built from
`exc.strerror`/`exc.filename` when available, which is already a clean
"No such file or directory: /path" - no `[Errno N]` noise), Ctrl-C -> exit
130 (`"interrupted"`). Anything else (including `SystemExit`, so argparse's
own `--bad-flag` handling is untouched) propagates as before - this is
deliberately narrow, not a catch-all.

Has to live inside `main()` itself, not behind `if __name__ == "__main__":`
- the installed console-script entry points (pyproject.toml) call
`blackvue.cli.bv_ls:main` etc. directly, so that guard never runs for a
real install.

Wired into all six commands (`bv-ls`, `bv-export`, `bv-generate`,
`bv-download`, `bv-config`, `bv-lang`) by wrapping each `main()`'s call into
its own body - `bv-download`/`bv-config`/`bv-lang`, whose `main()` did
argument parsing and the actual work in one function, got that work split
into a `_run(args)` so parsing (which should still raise `SystemExit`
normally) stays outside the wrapped call. `bv-lang`'s `_run()` also fixed a
latent bug surfaced by that split: its unreachable "unknown command"
fallback referenced `parser`, which no longer exists in that scope now
that parsing and dispatch are separate functions - replaced with a plain
`print()`.

---

## Trip support / future bv-export (roadmap)

Christer's vision, captured for continuity across sessions:

**Duration-aware gap calculation (done, this session, cuts across items 1
and 3 below).** Christer noticed `Asset.DURATION`/`.duration.txt` (the real
elapsed-time span `bv-generate --get-duration` computes and persists,
important because a Parking-mode timelapse's played-back file length can be
nothing like its real duration) was written but never actually read by
anything - `TripBuilder`'s gap check and `Trip.duration` were both pure
start-to-start timestamp math, so a recording that's itself longer than
`max_gap` could get wrongly split from the one after it. Fixed the gap
side: `TripBuilder` gained an optional `recording_duration: Callable[
[Recording], int | None] | None` constructor param - when it returns a
value for a recording, that recording's real end (`start + duration`) is
used instead of its bare start when computing the gap to the *next*
recording, before that gap is ever compared to `max_gap` (a recording with
no known duration falls back to its start timestamp, so this degrades one
recording at a time, not all-or-nothing). `recording_duration=None` (the
default) reproduces the old behaviour exactly, matching the same
backward-compatible pattern as the `bridge` param added earlier. New
`blackvue.generate.media.read_duration_seconds(recording)` reads a
recording's `.duration.txt` without touching ffprobe/ffmpeg (returns None
if the file's missing or unparseable). Wired into both `bv-ls --trips` and
`bv-export` as the default, with a `--no-duration` opt-out flag mirroring
`--no-movement`. Still open: `Trip.duration`/`Trip.label` (the identifier
used in bv-export folder names) still use the *last* recording's raw start
timestamp as the trip's "end", so the reported/displayed trip duration
still undercounts by that last recording's own real length - deliberately
left alone since changing it would also change already-shipped folder
naming, and hasn't been asked for yet.

**Fuzzy gap tolerance (done, this session).** Christer asked about a small
buffer (~10s) on top of `max_gap`, to absorb measurement noise that has
nothing to do with whether the vehicle actually stopped: `.duration.txt` is
rounded to the nearest second, recording timestamps only have 1-second
resolution (from the filename), and real dashcams take a moment to close
one file and open the next even mid-recording. `TripBuilder` gained
`gap_tolerance: timedelta = DEFAULT_GAP_TOLERANCE` (10s), added on top of
`max_gap` before a gap counts as a split (`gap > max_gap + gap_tolerance`).
Unlike `bridge`/`recording_duration`, this defaults *on* rather than being
opt-in, since it's noise-absorption rather than a detection feature -
`gap_tolerance=timedelta(0)` recovers the exact old boundary if ever
needed. Wired into both commands as `--gap-tolerance SECONDS`.

Also worth recording here since Christer asked directly: of the three
signals now feeding trip detection, they're not peers - duration is a
correction applied to the gap itself before anything is compared to
`max_gap` (folded in unconditionally, since it's a measured fact, not a
guess); `max_gap` (plus the tolerance above) is the actual decision
threshold; movement bridging is consulted last, only when the
duration-corrected gap still exceeds that threshold, since it's the most
speculative of the three (GPS speed at the recording edges is fairly
direct when a fix exists but only sees ~15s at each edge with nothing from
inside the gap itself; g-sensor variance is weaker still, self-calibrated
because the raw unit is unconfirmed). Within the movement check itself, GPS
and g-sensor aren't a strict fallback chain - both are checked and either
one returning true is enough - deliberately, since bridging only ever
prevents a split and never causes one, so a false-positive bridge is a
cheap mistake while a false-negative (an over-eager split) is the more
annoying failure for "assemble one holiday video."

1. **bv-ls --trips (done, this session).** Detect trips - runs of recordings
   with no gap longer than `--max-gap` (default 10 min) between them - and
   list one row per trip instead of one row per recording. Built on
   `blackvue.trip.{Trip,TripBuilder}`, which already existed but were
   completely unused and had a real bug (referenced `recording.recording_id`,
   which doesn't exist - the real field is `Recording.id` - hidden until now
   because both existing test files used a `FakeRecording` with the same
   wrong attribute name). Fixed, and a real-`Recording` test added to each
   so that class of drift can't hide again. Added `Trip.label` (
   `trip_<start>_<end>`, e.g. `trip_20260715_133458_20260715_141235`) -
   this is also the exact suffix bv-export's folder names will use.

2. **GPS-aware trip heuristic (done, this session).** Christer supplied two
   real files (`20260720_135524_E.gps`, `20260720_135824_N.3gf`), which let
   the raw formats be reverse-engineered against real data instead of
   guessed:
   - `.gps` is NMEA-0183 text, each sentence prefixed with a bracketed
     Unix-epoch-ms timestamp; only `$GPRMC` is parsed (has fix
     validity/position/speed/course in one line). The bracket timestamp
     matches `RecordingId.timestamp` to the second in the real sample, so no
     timezone conversion is needed. Built as `blackvue.telemetry.gps_reader`
     (`GpsFix`, `read_gps()`).
   - `.3gf` is a headerless binary stream of fixed 10-byte records
     (`>Ihhh` - 4-byte big-endian ms offset + signed X/Y/Z). Verified the ms
     field is a genuine 4-byte counter (doesn't wrap at the 16-bit/65536ms
     boundary) against the real sample, which ran ~2m49s. Built as
     `blackvue.telemetry.gsensor_reader` (`GSensorSample`, `read_gsensor()`).
     The physical unit of X/Y/Z isn't confirmed, so nothing here assumes a
     calibrated g-force threshold.

   Policy (confirmed with Christer via AskUserQuestion): time-gap stays the
   *primary* trip-split rule; movement only ever *bridges* a gap that would
   otherwise split a trip, never splits one the time-gap rule would have
   kept together. The g-sensor movement signal is self-calibrating relative
   variance against the recording's own quietest window, not a fixed
   threshold (since the unit is unconfirmed).

   Implemented in `blackvue.telemetry.movement`
   (`movement_bridges_gap(previous, current)`, plus the underlying
   `gps_shows_movement_at_{start,end}()` /
   `gsensor_shows_movement_at_{start,end}()` checks). `TripBuilder` gained an
   optional `bridge: Callable[[Recording, Recording], bool] | None`
   constructor param - only consulted when the time-gap rule would split,
   `bridge=None` (the default) reproduces the old pure-time-gap behaviour
   exactly, so this is fully backward compatible. `bv-ls --trips` passes
   `movement_bridges_gap` as the bridge by default; `--no-movement` falls
   back to the pure `--max-gap` rule. Missing/unreadable GPS/g-sensor files
   are treated as "no evidence", never as "stationary" - they can't force a
   split on their own.

3. **bv-export command, plus most of the "hard work" (done, this session).**
   Christer's answer when asked how much bv-export's first version should do
   was, in effect, "the hard work too" - so items 3 and 4 landed together
   rather than as separate passes.

   New `bv-export PATH --target DIR [--prefix PREFIX] [--from --until
   --timestamp] [--max-gap MINUTES] [--no-movement] [--dry-run]` command
   (`blackvue.cli.bv_export`). Scans the archive over the standard
   `PATH --from --until --timestamp` range, detects trips the same way
   `bv-ls --trips` does (same `TripBuilder` + GPS/g-sensor movement
   bridging, same `--max-gap`/`--no-movement` flags), and for each trip
   creates/refreshes a subfolder under `--target` named
   `<PREFIX_>trip_<start>_<end>` (e.g. `--prefix Holiday` ->
   `Holiday_trip_20260715_133458_20260715_141235`). If a trip's folder
   already exists from a previous run it's wiped and rebuilt from scratch
   (confirmed with Christer: overwrite/refresh, not skip-if-exists).

   The per-trip assembly itself lives in the new `blackvue.export` package
   (`export_trip(trip, destination) -> ExportResult`, kept separate from the
   CLI so it's independently testable):
   - `export/media.py`: `concatenate_media(sources, destination)` joins
     video or audio files via ffmpeg's concat demuxer (stream copy, no
     re-encode). Front and rear video are concatenated separately into
     `front.mp4`/`rear.mp4`; audio into `audio.aac` - each only written if
     at least one recording in the trip has that asset.
   - `export/text.py`: `merge_text_assets(trip, asset)` concatenates
     transcript/translation text across the trip's recordings (plain and
     diarized, translated or not - all four `Asset` variants), each block
     headed by `# <recording_id>`, into `transcript.txt` /
     `transcript.diarized.txt` / `translation.txt` /
     `translation.diarized.txt`.
   - `export/gpx_writer.py`: `write_gpx(fixes, path)` turns merged
     `GpsFix`es (from `blackvue.telemetry.gps_reader`, item 2) into a real
     GPX 1.1 track file (`trip.gpx`) - the first thing in this codebase
     that actually generates a `.gpx` rather than just detecting one that's
     already there. Invalid fixes are skipped; speed/course go in a
     `<extensions>` block (speed converted km/h -> m/s, the GPX
     convention).
   - G-sensor: `gsensor_reader.py` gained a symmetric `write_gsensor()`
     (round-trips through the same 10-byte binary format as `read_gsensor`)
     so `trip_export.py` can merge every recording's `.3gf` samples into one
     `trip.3gf`, rebasing each recording's offsets from
     per-recording-relative to per-trip-relative (recording N's samples get
     shifted forward by `recording_N.timestamp - trip.start_timestamp`).

   Not done yet: rendering an actual map-overlay video. That's tracked
   separately below (item 4) since it's a genuinely different kind of work
   (picking a mapping/tile approach, image/video generation) rather than
   more file merging.

4. **Map-overlay video rendering (done, this session).** Renders `map.mp4`
   for a trip: route driven so far, a moving position dot, and a
   speed/timestamp text overlay, on a real street-level basemap. Opt-in via
   `bv-export --map` (off by default - see below for why).

   **Why not tile.openstreetmap.org, or a commercial tile API.** Before
   writing any code, checked the actual usage terms rather than assuming -
   good thing, since the original plan ("cache tiles per region, render
   offline after") turns out to violate both options' terms, not just be
   inconvenient:
   - `tile.openstreetmap.org`'s usage policy explicitly prohibits
     "pre-seeding" a region's tiles in advance and "building tile archives
     ... for later distribution" - exactly what rendering a trip's route
     needs (a whole bounding box fetched once, not tiles a human pans/zooms
     through interactively). Confirmed via
     https://operations.osmfoundation.org/policies/tiles/.
   - Commercial tile APIs (MapTiler, Mapbox, Thunderforest, etc.) generally
     license *live map display in an app*, not baking tiles into a
     permanently-stored video file. MapTiler's terms, for example,
     explicitly prohibit storing map content server-side or "using a
     screenshot or other static image" instead of live API access - close
     to what encoding a video from tile frames does. Confirmed with
     Christer (AskUserQuestion) this was worth solving properly rather than
     building on a shaky ToS footing.

   **What it actually does instead:** uses the Overpass API - OSM's own
   read-only data API, explicitly recommended by the OSM Foundation for
   small-area, non-editing queries like this
   (https://operations.osmfoundation.org/policies/api/) - to fetch raw road
   *geometry* for a trip's (padded) GPS bounding box. That's ODbL-licensed
   OSM *data*, not a rendered tile image, so caching/storing/redistributing
   it offline with attribution is explicitly fine, unlike a pre-rendered
   tile. beyond-video then draws the basemap itself from that geometry - no
   live map service is involved once a region's road data is cached.

   New `blackvue.export.osm_roads`: `bounding_box_for_fixes(fixes)`,
   `fetch_roads(bbox)` (Overpass query + parse, proper identifying
   User-Agent per OSM's API policy, wraps network/parse failures as
   `MediaToolError`), `load_or_fetch_roads(bbox, cache_dir)` (caches the
   raw response to disk by rounded bbox, so a trip - or repeat trips
   through the same area - only ever hits Overpass once, then works fully
   offline after; same one-fetch-then-offline pattern as `bv-lang
   install`).

   New `blackvue.export.map_render`: `render_frame(bbox, roads,
   route_points, position, ...)` draws one frame with Pillow - roads as
   thin gray lines, the route so far as a red line, a dot at the current
   position, speed/timestamp text in the corner - using a simple
   equirectangular projection (longitude scaled by `cos(mean latitude)`;
   a full Mercator projection would be overkill at the scale a single
   driving trip covers).

   New `blackvue.export.map_video`: `interpolate_position(fixes,
   timestamp)` linearly interpolates lat/lon/speed between the two GPS
   fixes bracketing a timestamp (clamped at the ends, not extrapolated);
   `render_map_video(fixes, roads, bbox, destination, fps=5)` builds the
   full frame sequence (growing the drawn route with every real fix
   reached, not just interpolated points) and hands it to ffmpeg
   (`libx264`/`yuv420p`) to encode. Returns `None` (writes nothing) if
   there aren't at least two valid, positioned, non-simultaneous fixes to
   draw a route from - same "nothing to work with" convention as
   `export_trip`'s other outputs.

   `export_trip()` gained `render_map: bool = False` and `map_cache_dir:
   Path | None = None` params, reusing the same merged `fixes` it already
   computes for `trip.gpx` rather than reading GPS twice. A road-fetch or
   render failure degrades to a warning (`ExportResult.warnings`), not a
   failed export - the rest of the trip's files are still worth having
   even if the map couldn't be built (e.g. Overpass unreachable). New
   `ExportResult.map: Path | None` field.

   `bv-export --map`: off by default (first-time-per-region network fetch,
   real render time per trip). `map_cache_dir` defaults to
   `--target/.osm_cache` - a sibling of the trip folders, not inside any
   one trip's own folder, since that folder gets wiped and rebuilt from
   scratch on every refresh; this way the OSM cache survives refreshes and
   is shared across every trip exported to the same `--target`.

   New dependency: `Pillow>=10.0` (pyproject.toml). Frame timestamps line
   up with the trip's real GPS timeline, so the result can sit next to the
   front/rear footage in `--stitch` (item 5) later. Not yet confirmed
   against a real Overpass query or Christer's actual archive - only
   unit-tested (roads/render/video-encode/CLI wiring all have real
   ffmpeg/Pillow exercised in tests; Overpass itself is faked via
   monkeypatched `load_or_fetch_roads`/`urlopen`, never hit for real in
   tests).

   **Satellite imagery instead of the road-line basemap (considered,
   deferred).** Christer asked about this right after `--map` shipped.
   Unlike roads, satellite imagery doesn't split into "open data +
   self-rendered" the way OSM does - the imagery itself is the licensed
   asset, so the same clean pattern doesn't carry over:
   - *NASA GIBS* - free, public-domain-ish, explicitly built for tile
     access (no offline-use prohibition). But ~250-500m/pixel resolution
     - a blurry color patch at driving-trip zoom, not a recognizable
     street. Not useful for this.
   - *Esri World Imagery (for Export)* - sub-meter, actually usable
     resolution, but a separate product from Esri's normal tile layer
     specifically because the normal one prohibits offline/export use.
     Needs an ArcGIS account, likely consumes paid export credits - exact
     terms/pricing not researched yet.
   - *Google Maps Platform / Mapbox Satellite* - Christer initially
     assumed "Google Earth supports this" would sidestep the licensing
     question; clarified that Google Earth (the app) and Google's actual
     imagery API are different things - the API needs a billing-enabled
     Google Cloud account, and its terms have the same "live display, not
     permanent offline storage" restriction as MapTiler/Mapbox found for
     street tiles.
   - *Sentinel-2 (Copernicus)* - genuinely free/open, but only 10m
     resolution (better than GIBS, still coarse) and would need an actual
     image-processing pipeline (cloud-free mosaicking, color correction)
     built from scratch, not just fetching ready-made tiles - a much
     bigger lift than the road renderer was.

   Decision: skip for now, keep the road-line basemap. Revisit if
   Christer wants to pursue a specific paid provider (would need real
   terms/pricing research first) or if a cleaner open high-resolution
   source turns up later.

5. **--stitch option (future).** Compose the front and rear video
   side-by-side (`left_right`) or stacked (`top_down`), and optionally
   stitch the map-overlay video in too, at a chosen position. This is
   essentially an ffmpeg `filter_complex` composition step
   (`hstack`/`vstack`/`overlay`) now that item 4 produces the map video to
   combine.

Immediate next step: confirm `--map` against a real archive (real Overpass
query, real GPS data) - see item 4's caveat above - then item 5 (`--stitch`)
is unblocked.
