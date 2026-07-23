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

Confirmed on Christer's real archive afterward: the same `--map --map-zoom 240`
export dropped from 2m34s to 33s (~4.7x).

---

## --map-zoom made independent of --map (done, this session)

Christer noticed `--map-zoom` did nothing unless `--map` was also given, and
asked for it to work standalone, producing its own file named
`map_zoom_XXXm.mp4` (XXX = the zoom radius in meters, as a suffix). Asked how
`--map` + `--map-zoom` together should behave since that changes existing
combined behavior - he picked two separate files over keeping the old
single-video mode.

`--map` and `--map-zoom` are now fully independent toggles:

- `--map` always renders `map.mp4`, always the static whole-trip overview -
  it no longer changes mode when `--map-zoom` is also given.
- `--map-zoom METERS` (with or without `--map`) always renders its own
  `map_zoom_{METERS:g}m.mp4` - the `:g` format drops a trailing `.0` for
  whole numbers (`map_zoom_240m.mp4`) but keeps a fractional value as given
  (`map_zoom_75.5m.mp4`).
- Both together: two separate files, one static, one zoomed - not one video
  reused for both.
- `--map-icon` now applies to whichever of the two is rendered (previously
  documented as "only used together with --map").

`trip_export.py`: replaced the old single `_render_map()` with
`_load_trip_roads()` (fetches/caches OSM road data once - shared by both
possible outputs, so a network failure produces one "map data: ..." warning,
not one per output) and `_render_map_variant()` (renders one video at a given
destination/zoom_meters, called once for `map.mp4` when `render_map=True` and
again for `map_zoom_*m.mp4` when `map_zoom_meters` is given). `ExportResult`
gained a `map_zoom: Path | None` field alongside the existing `map` field.
`bv_export.py`'s written-file count and CLI help text updated to match.

Tested: `render_map`/`map_zoom_meters` combinations (zoom alone with no
`--map`, both together producing two distinct files with the right
`zoom_meters` passed to each, filename formatting for a fractional zoom
value) at the `export_trip()` level, plus CLI-level tests confirming
`map_zoom_75m.mp4` exists alongside `map.mp4` and that zoom-alone skips the
static file entirely. Full suite green (312 passed).

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

5. **--stitch option - design spec agreed with Christer; camera-layout
   composition (increment 1 of 5, done this session) implemented, the
   rest still future work.** Composes whatever's already in a trip folder into one
   video via ffmpeg `filter_complex` (`hstack`/`vstack`/`overlay`). Scope:
   `--stitch` only works with files that already exist - it never
   generates anything itself. A requested element with no source file
   (e.g. gsensor overlay asked for but `--gsensor-video` was never run, or
   subtitles asked for but no transcript exists) is silently skipped, same
   warning-and-continue pattern the rest of `bv-export` already uses. If
   something's missing, the fix is re-running `bv-generate`/`bv-export`
   with the right flags first, not `--stitch` doing it implicitly.

   **Camera layout** - one selected per run, auto-picked from the trip's
   own geometry when not given, always overridable:
   - `side_by_side` (`left_right`): front | rear, `hstack`.
   - `top_down`: front / rear, `vstack`.
   - `rearview_mirror`: front full-frame, rear flipped horizontally
     (mirror image, not raw footage - a real rearview mirror shows things
     reversed) and shrunk, overlaid top-center. Size 10-50% of front
     width, default 25% (a later `--rearview-size` flag, range enforced).

   **Auto-pick logic**: compare the trip's real-world north-south extent
   vs. east-west extent (same lat/lon-bbox math `--map-zoom` already has,
   via `bounding_box_for_fixes`/the `cos(latitude)` correction). A
   north-south trip (taller than wide) picks `top_down` cameras (front/rear
   stacked - itself a tall column) with the map panel placed on the left,
   itself tall - two tall pieces side by side stays roughly rectangular
   overall. An east-west trip (wider than tall) picks `side_by_side`
   cameras (a wide row) with the map panel on the bottom, itself wide -
   same logic, perpendicular. Camera arrangement and map placement are
   nested perpendicular to each other specifically so the final frame
   doesn't turn into a long thin ribbon in either direction.

   **Map panel** (optional, a third stacked panel alongside the camera
   composite - not an overlay on top of footage): shape (tall vs wide, how
   tall/wide) comes from the trip's own real-world lat/lon extent for the
   static `map.mp4` - this needs new work in `map_render.py`/`osm_roads.py`
   beyond what `--map-zoom` already does, since today's bbox math produces
   a *square* real-world area (`bounding_box_around_point`/
   `bounding_box_for_fixes`'s margin), not one shaped to an arbitrary
   target aspect ratio; rendering a non-square `render_frame()` today would
   just stretch a square-computed bbox unevenly. `map_zoom_XXXm.mp4`
   doesn't need this - it's a small local follow-camera view with no
   inherent "shape" the way the whole trip has, so it can render at
   whatever aspect ratio the panel needs directly. Selectable between the
   two (`map.mp4` vs `map_zoom_XXXm.mp4`) when both exist, defaults to the
   static one. In `rearview_mirror` mode specifically, the map panel (left
   or down) is capped at 30% of width/height, since most of that frame
   still needs to stay the primary front view.

   **G-sensor overlay** (optional, always a transparent chroma-keyed
   overlay on top of footage, never a panel - this is exactly why
   `gsensor.mp4` was built on a chroma-key green background rather than an
   opaque one): size 5-40%, default 15% (its own range, separate from the
   rearview mirror's 10-50%/25%). Placement is either a named position -
   any combination of left/right/top/down/center (e.g. "top-right", plain
   "center") - or an explicit x-y coordinate for full manual control.
   Named positions are defined relative to the visible *footage* region
   only (front/rear video, excluding whatever space the map panel or the
   rearview-mirror inset occupies) - so a named position can never
   accidentally land on top of the map or the mirror inset, both of which
   are their own distinct content worth keeping readable, not something to
   layer a gauge over. An explicit x-y coordinate is a deliberate
   override, though: it's allowed to land anywhere, including on the map
   or the mirror inset, even if that looks bad - the user asked for that
   exact pixel, so it goes there.

   **Subtitles** (optional): burned into the frame (not left as a sidecar
   `.srt`/`.lrc`, which already exist today with zero extra work) -
   standard placement, centered, near the bottom, with a dark
   semi-transparent background bar behind the text for readability
   (ffmpeg's `subtitles`/`drawtext` filter with a box, or burning
   `trip.srt` in directly via the `subtitles` filter - exact mechanism
   still to be decided at implementation time). Source is always
   `trip.srt`, never `trip.lrc` - Christer asked which one `--stitch-
   subtitles` would use if both exist; `trip.lrc` has no real per-line
   duration (`merge_lrc` always sets `end == start`), fine for a
   karaoke-style display but not a standard subtitle cue, so there's no
   actual choice to make here, not something that needs its own flag.

   **Argument list agreed with Christer:**
   - `--stitch` - master switch, produces the composed video.
   - `--stitch-layout {side_by_side,top_down,rearview_mirror}` -
     auto-picked from trip geometry if omitted.
   - `--stitch-mirror-size PERCENT` - 10-50, default 25. Only meaningful
     with `rearview_mirror`.
   - `--stitch-map [map|zoom]` - same optional-value `nargs='?'` pattern
     `--map-zoom` already uses: bare flag includes the panel using the
     static map, `--stitch-map zoom` picks `map_zoom_XXXm.mp4` instead,
     omitted entirely means no map panel.
   - `--stitch-map-side {left,right,top,down}` - override the auto-picked
     side (default: left for `top_down` cameras, bottom for
     `side_by_side`).
   - `--stitch-gsensor` - bool, include the gsensor overlay.
   - `--stitch-gsensor-size PERCENT` - 5-40, default 15.
   - `--stitch-gsensor-pos POSITION` - named position (combinations of
     left/right/top/down/center, e.g. `top-right`, `center`) - excluded
     from the map panel/mirror inset region, see above.
   - `--stitch-gsensor-xy X,Y` - explicit position as percent (not
     pixels - resolution-independent, consistent with the size flags
     being percentages too) of the footage region, e.g. `80,10`.
     Mutually exclusive with `--stitch-gsensor-pos`. Allowed to land on
     the map panel/mirror inset even though named positions can't - a
     deliberate override, the user asked for that exact spot.
   - `--stitch-subtitles` - bool, burn `trip.srt` into the frame.
   - `--no-subtitles-bg` - bool, disables the dark translucent background
     bar behind subtitle text (on by default when `--stitch-subtitles` is
     given) - named to match the existing `--no-movement`/`--no-duration`
     negative-flag convention elsewhere in this CLI, rather than a
     `--stitch-`-prefixed negative.

   **Increment 1 - camera-layout composition (done, this session).**
   `side_by_side`/`top_down` only (not `rearview_mirror` yet - that needs
   the flip+scale+overlay step, a later increment), no map panel/gsensor
   overlay/subtitles/auto-pick yet either.

   New `blackvue.export.stitch`: `stitch_cameras(front, rear, destination,
   *, layout)` - `hstack` (`side_by_side`) or `vstack` (`top_down`) via
   ffmpeg `filter_complex`. Front and rear can be different resolutions
   (some BlackVue setups pair a higher-res front with a lower-res rear) -
   rear is probed and scaled (stretched, not letterboxed) to front's exact
   width/height first, since `hstack`/`vstack` both require matching
   dimensions on the non-stacked axis. Tried ffmpeg's `scale2ref` filter
   for this first, but its "which input scales to match which" semantics
   turned out easy to get backwards in practice (empirically confirmed
   both orderings before giving up on it) - a plain probed `scale=W:H` is
   simpler to get right and easier to read. A trip with only one camera
   (the common single-front-camera case) falls back to a plain copy of
   whichever one exists, ignoring `layout` - same "do the sensible thing"
   convention as the rest of `bv-export`. No audio track yet - that's a
   later pass, trip-level audio already lives in its own `audio.aac`.

   `media.py`'s NVENC-fallback encode logic (previously private to
   `encode_frame_sequence()`, frame-sequence-input-specific) generalized
   into `encode_with_nvenc_fallback(input_args, destination)` so
   `stitch.py` gets the same NVENC-then-CPU-fallback behavior for free
   instead of duplicating the GPU-detect logic - `encode_frame_sequence()`
   is now a thin wrapper over it.

   `export_trip()` gained `stitch_layout: str | None = None` (None = no
   stitch); `ExportResult.stitch: Path | None`. A stitch failure degrades
   to a warning, same pattern as map/gsensor. `bv-export --stitch` (bool)
   + `--stitch-layout {side_by_side,top_down}` (default `side_by_side` -
   temporary, until auto-pick-from-geometry exists).

   Tested: 9 new tests on `stitch_cameras` (both layouts' output
   dimensions, mismatched-resolution scaling, front-only/rear-only
   fallback, neither-camera returns None, unknown layout raises,
   MediaToolError on a bad source), 4 on `export_trip` (skipped by
   default, produces a video, front-only fallback, warns instead of
   failing), 4 CLI-level (`--stitch` produces `stitch.mp4`, absent flag
   writes nothing, default/explicit `--stitch-layout` argparse wiring).
   Full suite green (342 passed, up from 312). Not yet confirmed against
   a real front+rear BlackVue archive - only unit-tested with synthetic
   ffmpeg testsrc clips.

   **Follow-up: --stitch-resolution/--stitch-bitrate (done, this
   session).** Christer's first real-archive `--stitch` run was slow
   (stitching is a genuine re-encode, not a stream copy) and asked for a
   way to force a small, fast test render - specifically wanted to try
   320x240 at 256kbps.

   `stitch_cameras()`/`_stack()` gained `resolution: tuple[int, int] |
   None` and `bitrate: str | None`. `resolution` chains a second `scale=`
   filter onto the finished hstack/vstack composite (independent of the
   existing front/rear-matching scale, which stays there purely so
   hstack/vstack don't refuse mismatched inputs) - `bitrate` (e.g.
   "256k") is passed straight through as `-b:v`/`-maxrate`/`-bufsize` (all
   three, since `-b:v` alone is only a target/average for most encoders,
   not a hard cap). A single-camera trip's cheap stream-copy fallback is
   skipped in favor of an actual re-encode whenever either is given,
   since a stream copy can't resize or re-bitrate.

   `media.py`'s `encode_with_nvenc_fallback()` gained `extra_codec_args:
   list[str] | None` - appended to *both* the NVENC and libx264 attempts,
   so bitrate capping applies regardless of which encoder actually runs.

   CLI: `--stitch-resolution WIDTHxHEIGHT` (e.g. `320x240`, parsed via a
   small `argparse.ArgumentTypeError`-raising type function) and
   `--stitch-bitrate RATE` (e.g. `256k`, `2M` - passed straight through,
   no parsing). Both only meaningful with `--stitch`.

   Tested: 3 new on `stitch_cameras` (resolution scales the stacked
   output, resolution forces a re-encode for a single camera, bitrate
   reaches the encoder call with the maxrate/bufsize trio), 3 CLI-level
   (`--stitch-resolution` produces an actually-scaled-down file, both
   flags parse through `main()` correctly, a malformed resolution string
   raises `SystemExit(2)` with a clean message rather than a traceback -
   argparse's own `type=` validation runs before `run_cli()` gets
   control, so this is a real `SystemExit` in the test, not a return
   value). Full suite green (351 passed, up from 342).

   **Follow-up: real bug found on Christer's archive - --stitch-resolution
   distorted the picture (fixed, this session).** Tried the 320x240
   fast-render on a real front(3840x2160)+rear(1920x1080) trip - both
   confirmed no rotation metadata (`ffprobe stream_side_data=rotation` and
   `stream_tags=rotate` both empty) - and the result looked visibly
   squished, worse in one axis than the other. Root cause: two 16:9
   cameras stacked `side_by_side` come out ultra-wide (~3.56:1,
   7680x2160 before any resolution flag), and the resolution scale was a
   plain `scale=W:H` - a full stretch into 320x240 (4:3, 1.33:1)
   regardless of the composite's own shape, squeezing width far more
   than height. A screenshot of the actual output (buildings leaning,
   sky dominating one panel) confirmed it visually before touching any
   code.

   Two fixes, both real correctness issues, not just the reported one:
   - `_stack()`'s front/rear-matching scale (needed so hstack/vstack
     don't refuse mismatched inputs) previously forced rear to front's
     *exact* width and height - happened to look fine only because the
     one real pair tested (both 16:9) shared an aspect ratio, not a safe
     assumption in general. Now only the one dimension that actually
     needs to match is scaled (height for `hstack`, width for `vstack`),
     the other left free via ffmpeg's `-2` (auto-computed, rounded even)
     so rear's own aspect ratio survives regardless of whether it
     matches front's.
   - The output-resolution scale (`--stitch-resolution`) changed from a
     plain stretch to `_fit_and_pad()`: scales to fit inside the
     requested box preserving the composite's own aspect ratio
     (`force_original_aspect_ratio=decrease`), then pads with black
     bars to exactly reach the requested dimensions - the output file is
     still exactly the size asked for, but the picture itself is no
     longer warped. Applied to both `_stack()`'s composite output and
     `_reencode_single()`'s single-camera path.

   Tested: 2 new tests - one with solid-color source clips (checked via
   extracted-frame pixel sampling: black letterbox bars top/bottom,
   red content band in the middle, not a uniformly-stretched frame), one
   confirming a rear camera with a genuinely different aspect ratio than
   front (640x480 vs 640x360) comes out at its own correctly-scaled
   width, not force-matched to front's. All prior dimension-only tests
   (asserting exact final WxH) still passed unchanged, since
   `_fit_and_pad()` still produces exactly the requested output size -
   they just didn't check picture content, which is why this shipped
   without being caught until real footage exposed it. Full suite green
   (353 passed, up from 351).

   Confirmed on Christer's real archive afterward at 1280x720 (a more
   watchable test size than 320x240): aspect ratio holds correctly now,
   both panels proportioned properly with clean letterboxing.

   **Follow-up: parallelize front/rear/audio concatenation (done, this
   session).** Christer noticed only ~50% CPU during a real export and
   asked whether any of `bv-export` could run in parallel. Root cause:
   `export_trip()` ran every step strictly sequentially even though
   several don't depend on each other at all - whichever phase was
   running used CPU differently (ffmpeg's own multithreading during
   concatenation/stitch vs. a single Python thread doing PIL frame
   drawing during map/gsensor rendering), so idle time in one phase
   never overlapped with a busy period in another.

   Scoped to the clear, safe win first (Christer's choice, offered a
   broader map/gsensor/stitch scheduling option too but flagged it as
   needing more care): `front.mp4`/`rear.mp4`/`audio.aac` concatenation
   are three independent ffmpeg subprocess calls - none reads another's
   output - now launched concurrently via
   `concurrent.futures.ThreadPoolExecutor` instead of one after another.
   Safe with plain threads despite the GIL: each worker mostly just
   blocks in `subprocess.run()` waiting on ffmpeg (which releases the
   GIL for the wait), and `list.append()` on the shared `warnings` list
   is itself atomic in CPython. Deliberately not extended to map/gsensor
   rendering in this pass - both do real CPU-bound Python work (PIL
   frame-by-frame drawing) that would genuinely contend for the GIL if
   threaded alongside each other, unlike the concatenation calls, which
   are pure subprocess waits.

   Tested: 1 new test confirming the actual correctness property that
   matters here - one of the three failing (front, simulated) doesn't
   block or lose the other two (rear/audio still produced, still
   correct) - real concurrency timing isn't practical to assert in a
   unit test, but independent failure handling is exactly what a naive
   sequential-to-concurrent refactor could get wrong. Full suite green
   (354 passed, up from 353).

   **Follow-up: NVDEC hardware decode (done, this session).** After the
   concatenation parallelization above, Christer measured a real
   `--map` + `--stitch --stitch-layout side_by_side --stitch-resolution
   1280x720 --stitch-bitrate 256k` run at 5m20s with only ~58% CPU and
   just one ffmpeg process visible at a time (past the brief 3-way
   concat-parallelization phase). Root cause: NVENC only ever
   accelerated the final *encode* step - *decoding* the source videos
   (a real front 4K + rear 1080p camera pair, several minutes each)
   always happened on the CPU, and that decode is unavoidable, real
   per-frame work for the source's full length regardless of how small
   the requested output is - the actual bottleneck on a real archive,
   not the encode side.

   Offered two options: scale front/rear down early (right after
   decode, before the expensive hstack/pad filtering) to cut filter
   cost while leaving decode itself CPU-bound, or add NVDEC hardware
   decode via `-hwaccel cuda`. Christer chose the bigger one: NVDEC.

   Added `_nvdec_available()` to `stitch.py` (mirrors `media.py`'s
   `_nvenc_available()` - checks `ffmpeg -hwaccels` for `"cuda"`,
   cached for the life of the run). When available, both `_stack()`
   (two-camera path) and `_reencode_single()` (single-camera +
   resolution/bitrate path) now try NVDEC decode first: each `-i` gets
   `-hwaccel cuda -hwaccel_output_format cuda`, keeping decoded frames
   in GPU memory, then `hwdownload,format=nv12` is inserted into the
   filter graph immediately after each such stream - `hstack`/
   `vstack`/`pad`/`scale` are CPU-only filters, not CUDA filters, so
   the frames have to come back to normal memory before those touch
   them. If the NVDEC attempt fails for any reason (raises
   `MediaToolError` - `encode_with_nvenc_fallback()` already tries both
   NVENC and libx264 encoders before giving up, so a decode-side
   failure surfaces as both those attempts failing the same way),
   `_stack()`/`_reencode_single()` catch it and transparently retry the
   whole thing with plain CPU decode - the same two-level fallback
   pattern as the encode side (outer: decode method GPU→CPU, wrapping
   the existing inner: encode method NVENC→libx264), so a --stitch run
   always produces a video either way, just faster with a real NVIDIA
   GPU behind it.

   Tested: 3 new tests, following the same "force `_NVDEC_AVAILABLE =
   True`, let the real `-hwaccel cuda` attempt genuinely fail in this
   sandbox (no GPU here), confirm graceful fallback to CPU decode still
   produces a correct video" pattern already used for NVENC in
   `test_export_media.py` - one for the two-camera stack path, one for
   the single-camera + resolution path, one confirming
   `_nvdec_available()` itself parses `ffmpeg -hwaccels` output and
   caches the result. Full suite green (365 passed, up from 354).

   **Follow-up: NVDEC turned out to be a real regression on Christer's
   hardware, not a win (done, this session).** First real-archive test
   of the NVDEC path (RTX 5090 laptop GPU, real front 4K + rear 1080p
   footage, same `--map --stitch --stitch-layout side_by_side
   --stitch-resolution 1280x720 --stitch-bitrate 256k` command as the
   original 5m20s CPU-decode baseline): 7m11s, then 7m29s on a repeat
   run - both slower than CPU decode, even though Task Manager's Video
   Decode graph climbed for real, confirming NVDEC genuinely engaged
   rather than silently falling back.

   To find out *why* rather than guess, added always-diagnostic-only
   timing (see the `--debug` follow-up right below - this was built
   before `--debug` existed, then moved behind it) breaking the run
   into concatenation/map/stitch phases in `trip_export.py`, plus
   per-decode-attempt timing in `stitch.py` reporting which method
   (nvdec/cpu) was used, success/failure, and wall time for that
   specific ffmpeg call. With `--debug` on, the isolated numbers came
   back: map phase 171.8s, stitch phase 276.2s (all NVDEC, no
   fallback). Comparing against the original 5m20s (320.5s) full-run
   baseline with roughly the same map cost, CPU-decode stitch was
   doing the same two-camera compose in roughly half the time NVDEC
   just took.

   Two credible causes, not yet distinguished further: `hwdownload`
   copying every decoded frame back from GPU to CPU memory before the
   CPU-only `hstack`/`pad` filters touch it is real per-frame PCIe
   traffic for a 4K stream; and/or front+rear each opening their own
   NVDEC decode session competes for what's typically a single
   hardware decoder engine, serializing work that used to run as two
   independent CPU threads. Left as-is for now rather than reverting
   outright or scoping to single-camera-only, since the diagnostic
   `--debug` output (see below) is now in place to keep investigating
   without needing another full round-trip - this is an open item, not
   closed.

   **Follow-up: --debug flag for phase/decode timing (done, this
   session).** The instrumentation above was originally always-on
   stderr output; Christer asked for it to live behind a `--debug`
   flag instead of printing unconditionally on every run. Added
   `--debug` to `bv_export.py`'s CLI, threaded a `debug: bool = False`
   parameter through `bv_export()` -> `export_trip()` -> the map/
   stitch print sites -> `stitch_cameras()` -> `_stack()`/
   `_reencode_single()` -> `_report_decode_timing()`, which now no-ops
   unless `debug=True`. Silent by default, matching every other
   diagnostic-only knob in this codebase.

   Tested: 2 new tests in `test_stitch.py` (silent by default, prints
   `decode=cpu ... succeeded in ...` to stderr when `debug=True`), 2
   new tests in `test_bv_export.py` (`--debug` defaults to False,
   `--debug` flag sets it True). Full suite green (369 passed, up from
   365).

   **Follow-up: real bug found via the --debug output itself - broken
   filter syntax on the single-camera + resolution path (done, this
   session).** The first single-camera `--debug` run (front-only trip,
   `--stitch-resolution 1280x720`) reported `decode=nvdec failed in
   0.6s` - far too fast to be a genuine decode attempt, and immediately
   suspicious. Root cause: `_run_reencode_single()`'s resolution branch
   built the filter_complex as `predecode + _fit_and_pad("0:v", "v",
   width, height)`, where `_fit_and_pad()` already returns a string
   starting with `[0:v]scale=...` - prepending `predecode`
   ("hwdownload,format=nv12,") in front of that produced
   `hwdownload,format=nv12,[0:v]scale=...`, putting the filter name
   *before* the bracketed input label instead of after it. ffmpeg
   requires the label first in a filter-chain link, so this was a
   syntax error, not a real hwaccel failure - NVDEC was never actually
   attempted on this path, silently masquerading as "NVDEC
   unavailable/failed" the whole time. The two-camera `_stack()` path
   didn't have this bug (it correctly builds `f"[1:v]{predecode}
   {rear_scale}[rear_scaled]"`, predecode inside the label reference) -
   only the single-camera resolution path was affected.

   Notable: the existing test for this exact scenario
   (`test_stitch_cameras_single_camera_falls_back_to_cpu_decode_when_nvdec_fails`)
   still passed the whole time, because it only checks that a correct
   video comes out the other end - true whether NVDEC failed for the
   right reason (no GPU in the sandbox) or the wrong one (bad syntax).
   Fixed `_fit_and_pad()` to take an optional `prefix` parameter
   inserted *inside* the bracketed label reference
   (`f"[{input_label}]{prefix}scale=..."`), and updated
   `_run_reencode_single()` to pass `prefix=predecode` instead of
   string-concatenating it on the outside.

   Tested: 2 new tests that inspect the actual filter_complex string
   rather than just the end-to-end outcome - one asserting
   `_fit_and_pad()`'s prefix lands right after the label, one
   exercising the real call site (`_run_reencode_single` with
   `hw_decode=True`, encoder faked so no real ffmpeg/GPU needed)
   confirming the string handed to the encoder is well-formed. Full
   suite green (371 passed, up from 369).

   **Follow-up: root cause of the two-camera NVDEC regression found and
   fixed - unshared CUDA device contexts, not real GPU contention (done,
   this session).** With the filter-syntax bug above fixed, the real
   single-camera NVDEC signal came back clean: front alone 38.5s, rear
   alone 17.0s (both real, both faster than CPU decode measured on the
   same trip - 61.9s and, by inference, proportionally more for rear).
   So NVDEC decode itself is a genuine win when only one camera is
   decoding - the question was why combining both into one `--stitch`
   run cost 276.2s, nearly 5x the 55.5s sum of the two solo numbers.

   Ruled out real GPU decoder-engine contention directly: ran the two
   single-camera commands in two separate PowerShell windows, started
   within a second of each other (confirmed genuinely overlapping, not
   sequential) - front came back at 44.8s, rear at 19.0s, both only
   modestly slower than their solo numbers. If the NVDEC hardware
   itself were the bottleneck, two decodes running concurrently in any
   process arrangement would cost close to that - not 5x worse. The
   difference had to be something specific to one ffmpeg process
   handling two `-hwaccel cuda` inputs in the same filter graph.

   That matches a known ffmpeg rough edge: without an explicit shared
   device, each `-hwaccel cuda` input opens its own separate CUDA
   context, and cross-context synchronization once both contexts feed
   into the same filter graph is expensive. Fix: `_shared_hw_device_args()`
   emits one `-init_hw_device cuda=cu:0` up front (before any `-i`,
   once per command, only when NVDEC is being attempted), and
   `_hwaccel_input_args()` now also passes `-hwaccel_device cu` on
   every hwaccel input, pinning it to that one shared device instead
   of implicitly creating its own. Applied to both `_run_stack()`
   (two-camera path, where this actually mattered) and
   `_run_reencode_single()` (single-camera path, harmless/no-op there
   since there's only one input anyway, but consistent).

   Tested: 2 new tests inspecting the actual args built - one
   confirming the single-input path pins to the shared device, one
   confirming the two-input path emits exactly one `-init_hw_device`
   (not one per input) and both `-i`'s reference the same
   `-hwaccel_device` value. Can't verify the actual speedup in this
   sandbox (no GPU) - real confirmation is Christer re-running the
   original two-camera `--stitch --stitch-resolution 1280x720
   --stitch-bitrate 256k --debug` command and comparing the new
   `decode=nvdec` stitch-phase time against the old 276.2s. Full suite
   green (373 passed, up from 371).

   The shared-device fix landed (276.2s -> 230.8s, ~16% faster) but fell
   far short of closing the gap to the ~55.5s sum of the two solo
   decodes - so unshared CUDA contexts were only part of the story, not
   the dominant cause. That result, plus Christer independently finding
   similar "context switching overhead" language in an online guide
   (whose specific proposed fixes - avoid all CPU-side copying, or run
   two separate processes - didn't fully match this module's
   compositing needs or what had already been tried), pointed at
   something more fundamental: ffmpeg's filter graph engine appears to
   serialize frame handling across simultaneous hardware-decoded inputs
   within a single process, no matter how the CUDA device is shared.
   Only genuine OS-level process parallelism (confirmed by Christer's
   own two-separate-PowerShell-windows test earlier: front 44.8s, rear
   19.0s, both close to solo) sidesteps that.

   **Follow-up: redesigned `_stack()` to decode front/rear as two
   separate ffmpeg processes (done, this session).** Given the above,
   rather than trying further single-process mitigations, `_stack()`
   was restructured around the one pattern already proven fast:

   - `_decode_camera()`/`_run_decode_camera()`: decode one camera
     (trying NVDEC first, falling back to CPU on a real failure - same
     per-camera fallback granularity as the single-camera path) into a
     plain intermediate video, applying the front/rear-matching scale
     (previously done inside the combined filter graph) along the way
     for rear.
   - `_stack()` now runs front's and rear's `_decode_camera()` calls
     concurrently via `concurrent.futures.ThreadPoolExecutor` (same
     safe-with-plain-threads reasoning as the front/rear/audio
     concatenation parallelization earlier), writing to a
     `tempfile.TemporaryDirectory()`, then does one final CPU-only pass
     (hstack/vstack the two now-already-decoded intermediates, plus the
     optional resolution fit-and-pad) - deliberately no hwaccel on this
     final pass, since there's nothing left to gain and it would just
     reintroduce the problem being avoided.
   - The old `_run_stack()` (single ffmpeg process, two hwaccel inputs,
     the `null`-filter front-labeling trick) is gone entirely - no
     longer needed once decode moved to separate processes.

   Tested: `_run_decode_camera()`'s single-input shared-device args
   (same assertion style as the single-camera path), a test confirming
   exactly 3 `encode_with_nvenc_fallback` calls happen (front decode,
   rear decode, final combine) with the two decode calls each having
   exactly one `-i` and the final combine legitimately having two (no
   hwaccel on either, so no contention), and a test confirming a
   requested `bitrate` lands only on the final combine call, not the
   two intermediate decodes. All existing `_stack()`-level tests
   (layout dimensions, mismatched-aspect-ratio scaling, letterboxing,
   NVDEC-fails-for-real fallback) needed no changes - same observable
   behavior, different internal architecture. Full suite green (375
   passed, up from 373).

   **Follow-up: two bugs found on the very first real-archive run of the
   two-process redesign (done, this session).** Christer's first
   `--debug` run with the redesign in place: `front.mp4 decode=nvdec
   failed in 0.5s` (again too fast to be real), `rear.mp4 decode=nvdec
   succeeded in 100.0s` (real, but far slower than expected), stitch
   phase 173.8s total - better than 230.8s, but not the clean win hoped
   for, and for the wrong reasons.

   Bug 1 (same class as the earlier `_fit_and_pad` prefix bug): in both
   `_run_reencode_single()`'s and the new `_run_decode_camera()`'s
   `elif hw_decode:` branch (taken when there's no scale filter to
   apply - front's case, since only rear needs the matching scale),
   the filter was built as `f"[0:v]{predecode}[v]"`. `predecode`
   ("hwdownload,format=nv12,") carries a trailing comma meant to
   separate it from a *following* filter (see `_fit_and_pad()`'s
   `prefix` param) - with nothing following it here, that trailing
   comma left a dangling `,[v]` right before the output label, another
   instant ffmpeg syntax error. This branch had simply never been
   exercised before - every prior real run always passed
   `--stitch-resolution`, which takes the other, correct branch. Fixed
   by stripping the trailing comma (`predecode.rstrip(',')`) in both
   `elif` branches.

   Bug 2, more consequential: `_stack()` was still matching rear to
   **front's full native height** before decoding, regardless of any
   requested `resolution` - e.g. front 4K (~2160p) + rear 1080p +
   `--stitch-resolution 1280x720` meant rear got upscaled to ~2160p as
   an intermediate, just to be shrunk straight back down to 720p two
   steps later. That upscale-then-downscale round trip is exactly
   rear's measured ~100s. Fixed: when `resolution` is given, *both*
   cameras' intermediates now scale directly toward it (e.g. `scale=-2:
   720` for both, for an hstack layout) instead of matching each
   other's native size first - this also means `_video_dimensions(front)`
   no longer needs to be probed at all in that case. When no
   `resolution` is given (full native-quality output), the original
   native-height-matching behavior is unchanged.

   Tested: 2 new regression tests asserting the exact filter string for
   the trailing-comma fix (one per call site), and 1 new test
   confirming both cameras' scale filters target the requested
   resolution's height/width directly (with front's real native
   height, 2160, explicitly asserted absent from either filter string)
   for a real front-4K/rear-1080p size mismatch. Full suite green (378
   passed, up from 375).

   **Investigation closed: both bugs fixed, NVDEC is now a clear win on
   the two-camera path too (confirmed on Christer's real archive).**
   Same command, both fixes in place: `rear.mp4 decode=nvdec succeeded
   in 18.7s`, `front.mp4 decode=nvdec succeeded in 41.5s`, stitch phase
   55.6s total - both cameras landing close to their earlier solo
   baselines (front 38.5s solo, rear 17.0s solo), confirming they're
   now genuinely overlapping with none of the earlier waste. 55.6s is
   ~5x faster than the original single-process NVDEC attempt (276.2s),
   ~4.2x faster than the shared-device-only attempt (230.8s), and
   ~2.7x faster than the original CPU-only baseline this whole
   investigation started from (~148s, implied). The two-process decode
   architecture plus both filter-graph bug fixes are the complete
   answer to why the first NVDEC attempt (task 57) made a real run
   slower instead of faster.

Summary of the full investigation, for anyone reading this later without
wanting to dig through every follow-up above: NVDEC decode is a genuine
win for `--stitch`, but only once (a) both hardware-decoded camera inputs
are decoded in separate ffmpeg processes rather than one process with two
`-hwaccel cuda` inputs (ffmpeg's filter graph engine serializes hardware-
decoded frame handling across simultaneous inputs within a single
process - confirmed by a controlled concurrent-vs-combined test on real
footage, not just theory), and (b) each camera's intermediate is scaled
toward the actual requested output resolution rather than matched to the
other camera's full native size first. Two filter-graph syntax bugs (a
prefix-ordering mistake and a trailing-comma mistake, both in branches
that real usage hadn't exercised until this investigation forced them
open) were found and fixed along the way, each initially misread as "NVDEC
unavailable" until the timing was too fast to be a real decode attempt.

**Follow-up: intermediate resolution was still bigger than it needed to be
(done, this session, found by Christer's own back-of-envelope math).**
Even after the fix above, both cameras' intermediates were scaled directly
to `out_height` (hstack) / `out_width` (vstack) - e.g. both scaled to
height 720 for a `--stitch-resolution 1280x720` request. But two same-
aspect-ratio (16:9) cameras placed side by side at height 720 combine to
width **2560**, not 1280 - exactly double the target, meaning the final
combine pass still had to shrink the whole composite by about half again.
Christer worked out by hand that each camera should instead target roughly
*half* of 1280 (i.e. ~640-wide, ~360-tall) and asked whether that was
right instead of him just being "stupid" - it was exactly right, and the
exact number (360, not a hardcoded half-split) falls out of a general
formula: for hstack, both cameras share height H, each contributes width
`H * its own aspect ratio`, and solving "combined width == out_width" for
H gives `out_width / (front_aspect + rear_aspect)` (capped at `out_height`
in case that would ask for an H taller than the target frame). vstack is
the mirror of this (shared width, solving on combined height instead).

Implemented as `_ideal_shared_dimension()`, replacing the naive
`out_height`/`out_width` scale target in `_stack()`'s resolution-given
branch. For Christer's real front-4K/rear-1080p pair at 1280x720/
side_by_side, this drops the shared intermediate height from 720 to 360 -
both intermediates roughly a quarter the pixel count of the previous fix,
on top of everything else already fixed in this investigation.

Tested: the existing real-archive-shaped test's expected scale filter
values updated (720 -> 360, with an added assertion that "720" doesn't
appear either, not just "2160"), plus 3 new tests directly on
`_ideal_shared_dimension()` - matching-aspect-ratio hstack case (360, the
real numbers), the vstack mirror case (640), and a narrow/tall-camera case
confirming the `out_height`/`out_width` cap kicks in rather than ever
producing an intermediate bigger than the final frame. Full suite green
(381 passed, up from 378).

Confirmed on Christer's real archive: rear 16.3s, front 40.5s, stitch phase
54.4s - essentially unchanged from the 55.6s before this fix (~2%
faster), not the further big drop expected from a ~4x pixel-count
reduction in the intermediate. Conclusion: decode time is dominated by
reading the *source* footage at its own native 4K/1080p resolution, not
by how large the intermediate encoded afterward is - NVENC's encode side
is fast enough on this hardware that shrinking the intermediate target
barely shows up in wall-clock time. The fix was still worth making (no
more wasted oversized intermediates, cleaner/more correct design), just
not where the remaining time actually goes. With two stable runs now
landing in the 54-56s range (down from the original 276.2s, ~5.1x), this
investigation is considered settled - no further NVDEC/--stitch
performance work planned unless a new real-world number suggests
otherwise.

Immediate next step: confirm `--map` against a real archive (real Overpass
query, real GPS data, see item 4's caveat above) - then continue `--stitch`
per the spec above, in order: the map-panel aspect-ratio work, then
g-sensor overlay placement, then subtitle burn-in, then `rearview_mirror`,
then the auto-pick-from-trip-geometry layer on top once the individual
pieces work with explicit flags.

## bv-export: --timestamp/--from/--until select trips, not recordings (done, this session)

Christer noticed, while thinking ahead to a future "refer to a trip by
name" feature, that `bv-export`'s `--timestamp`/`--from`/`--until` flags
worked by filtering *recordings* against the requested range before ever
building trips from them:

```python
recordings = [r for r in archive.recordings if r.id.value in interval]
trips = TripBuilder(...).build(recordings)
```

For a long continuous drive, this can silently truncate a trip: if the
real trip's recordings span outside the requested window (e.g. the drive
started a few minutes before a `--timestamp` window opens, or a
`--timestamp` prefix like `20260721_124` only covers a literal 10-minute
lexical range - see `lexicaltimeparser.py`), the recordings outside that
window are filtered out *before* `TripBuilder` ever sees them - so a real,
continuous trip gets exported as a truncated fragment, with the folder's
own label (`trip.label`, from the *surviving* recordings' timestamps) not
even reflecting the true, full drive.

Christer's actual want, stated directly: "all trips that were in the
range, and get recordings before and after range if the videos belong to
the trips found in that range." Implemented exactly that - trips are now
detected across the *whole* archive first, then a trip is kept if any one
of its own recordings falls inside the requested range; the whole trip is
then exported, including whatever recordings pushed it before or after
the range's own boundaries:

```python
all_trips = TripBuilder(...).build(archive.recordings)
trips = [
    trip for trip in all_trips
    if any(recording.id.value in interval for recording in trip)
]
```

This matches how `bv-ls --trips` already worked (detect trips over the
whole archive, filter for display afterward) - `bv-export` was the only
place still filtering the *input* to trip detection rather than filtering
the *output* of it.

Trade-off, called out explicitly in `bv_export()`'s docstring and the
`--timestamp`/`--from`/`--until` CLI help text: trip detection (and
whatever it reads per recording - `.duration.txt` unless `--no-duration`,
GPS/g-sensor data for movement bridging unless `--no-movement`) now runs
across the *entire* archive on every run, not just the requested range - a
real cost on a very large archive, accepted here in favor of never
silently truncating a trip. Revisit if this becomes a real problem on
Christer's actual archive size.

Tested: 2 new tests in `test_bv_export.py` - a 3-recording trip where
`--timestamp` matches *only* the middle recording exactly, confirming the
whole trip (all 3 recordings, full label) is still exported rather than
just the matching one; and a trip entirely outside the requested range
still being excluded (exporting the whole overlapping trip isn't the same
as exporting everything). Full suite green (383 passed, up from 381).

The "refer to a trip by name" feature itself (the reason this came up) is
still just an idea for later, not implemented - noted here for whenever
it's picked up.

## --stitch: cap --stitch-bitrate to the intermediates' own combined bitrate (done, this session)

Christer worked through this one himself, correctly, in two steps. First:
"if you have two input files with a bitrate of X each, would it be a waste
to allow the output bitrate to be greater than 2X?" - yes, roughly:
a side-by-side composite doesn't contain any information beyond what's in
the two source frames, so bits requested well beyond what the sources
actually carry mostly can't recover detail that isn't there. Then the
sharper refinement: "since we lower resolution and bitrate of each file
before merge, maybe output bitrate should be limited to the highest
bitrate of one of the two input files" - meaning the two *intermediates*
`_stack()` produces (front.mp4/rear.mp4 in the temp dir), not the
original cameras' native bitrates, since the final combine pass never
sees the originals again. Corrected one detail: **sum**, not the higher
of the two - both intermediates are now scaled to roughly the same size
(see `_ideal_shared_dimension()`), so the composite has roughly double
the pixel area of either alone; capping at just one intermediate's
bitrate would spread that same budget over twice the pixels, likely
looking *worse* than either intermediate on its own.

Implemented in `_stack()`, after both intermediates are decoded and
before the final combine encode:

- `_video_bitrate(path)`: ffprobe's `format.bit_rate` for a file, or
  `None` if it can't be determined (never worth failing the export
  over - just skips the cap check).
- `_parse_bitrate_bps(value)`: parses an ffmpeg-style bitrate string
  ("256k", "2M", "1500000") into plain bits/second, mirroring ffmpeg's
  own k/M suffix convention.
- If a `bitrate` was requested and both intermediates' own bitrates can
  be determined, the requested value is compared against their sum; if
  it exceeds that sum, the *effective* bitrate used for the final
  encode is clamped down to the sum, and a message is appended to a new
  `warnings: list[str] | None` parameter threaded through
  `stitch_cameras()` -> `_stack()` (and from `trip_export.py`, the same
  `warnings` list already used for map/gsensor/subtitle-padding
  warnings - so this surfaces through the exact same
  `bv-export: {trip.label}: warning: ...` mechanism, visible by
  default, not gated behind `--debug`).
- Skipped entirely (no probing, no warning) when `bitrate` is `None` -
  nothing to compare against.

Tested: unit tests for both new helpers (suffix parsing, real-file
bitrate reading, unreadable-file returns `None`), and four `_stack()`
-level tests with `_video_bitrate` monkeypatched to fixed values -
cap actually triggers and produces the right warning text, cap doesn't
trigger when the request is already under the ceiling, cap is skipped
gracefully when bitrate can't be determined, and probing never happens
at all when no `bitrate` was requested. Confirmed via a real (non-mocked)
`ffprobe`/`ffmpeg` check first that the two existing bitrate tests
(which fully mock `encode_with_nvenc_fallback`, writing empty
intermediate files) wouldn't be affected - `_video_bitrate()` on an
empty file returns `None` for real (ffprobe fails on it), so the cap
correctly no-ops there without needing to touch those tests. Full suite
green (391 passed, up from 383).

Separately, Christer asked why `stitch.mp4` comes out at 29.97fps
instead of 30fps like the source appears to be. Answer given (not a code
change): none of the ffmpeg calls in this pipeline set an explicit `-r`/
output-framerate anywhere, so whatever comes out is whatever the source
declares - and `generate/media.py`'s own `_parse_frame_rate()` docstring
already documents `'30000/1001'` (exactly 29.97) as an example format it
has had to handle, strongly suggesting the BlackVue source recordings
themselves report that NTSC-legacy fractional rate rather than a true
30.000, and the pipeline is just passing it through untouched. Not
independently confirmed against Christer's actual files - `ffprobe
-show_entries stream=r_frame_rate` on a raw front.mp4 would confirm it
for certain, if it's ever worth pinning down further.

Update: Christer ran `ffprobe -show_entries stream=r_frame_rate` against
a real front camera file and it came back `r_frame_rate=30/1` (nominal
30, not an NTSC 30000/1001), while ffmpeg's own human-readable summary
line for the same stream reported the true average as `29.99 fps, 30
tbr`. So the original hypothesis was wrong: the source doesn't declare
a fractional NTSC rate at all - it declares a clean 30 but its actual
frame delivery drifts slightly off that nominal rate (real-world
capture jitter, not a declared rate). Since nothing in this pipeline
forces CFR (no `-r`/`-vsync cfr` anywhere), that real drifting cadence
flows through decode -> scale -> concat -> re-encode untouched, picking
up a little more rounding noise along the way - landing at 29.97 in
stitch.mp4 rather than matching the source's measured 29.99 exactly.
Net effect on Christer's original question is the same either way (the
pipeline inherits it, doesn't introduce it), just the actual mechanism
is timestamp drift, not an NTSC-legacy rate label. No code change from
this - noted here in case exact-30fps output is ever wanted later (an
explicit `-r 30 -vsync cfr` on the final combine pass would force it,
at the cost of the encoder duplicating/dropping frames to hit that
rate).

## --stitch: map-panel aspect-ratio plumbing (done, this session)

First piece of the "map panel" item from the roadmap above - not the
full `--stitch-map` wiring yet (still needs the actual panel-placement
logic in stitch.py, `--stitch-map`/`--stitch-map-side` CLI flags, and
the auto-pick side/size logic), just the prerequisite math the spec
above already flagged as missing: `bounding_box_for_fixes()`'s bbox is
shaped by whatever the trip's real GPS extent happens to be, not by
any target canvas - rendering it directly onto a non-square panel would
come out visibly stretched, since `map_render.render_frame()` scales
longitude span to the canvas width and latitude span to the canvas
height *independently*.

`bounding_box_for_fixes()` gained `aspect_ratio: float | None = None`
(width/height). When given, whichever of the box's two real-world
dimensions is shorter gets grown symmetrically around the box's own
center until the ratio matches - real-world units compared via the
same `cos(latitude)` correction `render_frame()`/
`bounding_box_around_point()` already use, not raw degrees (a degree of
longitude is narrower than a degree of latitude away from the
equator). Only ever adds margin, never crops - the already-longer
dimension, and the box's center, are left untouched. `aspect_ratio=None`
(the default, and every existing caller today) is a no-op, unchanged
from before.

`bounding_box_around_point()` (the `--map-zoom` follow-camera box,
rebuilt fresh every frame) also gained `aspect_ratio`, but a simpler
version: since a follow-camera view has no pre-existing real-world
shape to preserve (it's freely chosen each frame, not derived from
actual GPS extent), it just builds the box already shaped to the ratio
directly - `radius_meters` keeps meaning the vertical half-height,
horizontal half-width becomes `radius_meters * aspect_ratio`. No
"growing" needed, unlike the whole-trip case.

`render_map_video()` gained `width`/`height` parameters (defaulting to
`map_render.py`'s existing 640x640), threaded straight to
`render_frame()`; in `zoom_meters` mode, `width / height` is computed
once and passed as `bounding_box_around_point()`'s new `aspect_ratio`
on every frame, so a non-square `--map-zoom` panel doesn't need its own
separate aspect-ratio argument - it falls out of the requested canvas
size automatically.

Not yet wired into `bv-export`'s CLI or into `_load_trip_roads()`/
`_render_map_variant()` in `trip_export.py` - both still call
`bounding_box_for_fixes(fixes)`/`render_map_video(...)` with no
aspect_ratio or width/height, so today's `--map`/`--map-zoom` output is
completely unchanged. This is groundwork only; the actual `--stitch`
map-panel increment (deciding panel width/height from the camera
composite's own geometry, the `--stitch-map`/`--stitch-map-side` flags,
the `rearview_mirror` 30% cap, choosing between `map.mp4` and
`map_zoom_XXXm.mp4`) is still ahead.

Tested: 5 new `osm_roads.py` tests (tall-trip widens longitude, wide-
trip grows latitude, both checked via the real-world-unit ratio rather
than hardcoded degrees since the trig doesn't reduce to round numbers,
`aspect_ratio=None` is a no-op, `bounding_box_around_point`'s width-
only scaling). 4 new `map_video.py` tests (`width`/`height` reach
`render_frame()`, defaults match `map_render.py`'s constants, zoom
mode's per-frame `aspect_ratio` is derived correctly, and a real
(non-mocked) end-to-end render at 320x180 confirmed via `ffprobe` that
the actual output video lands at exactly that size).

Not run through the project's own pytest suite this session - this
sandbox has neither `pytest` installed nor network access to fetch it
(a change from earlier in this project, when it evidently was
available; environment isn't persistent between sessions). Instead,
every assertion each new test makes was hand-verified with equivalent
plain-Python scripts run directly against the real modules (including
one real `ffmpeg`/`ffprobe` end-to-end render, not just monkeypatched
calls) - all passed. Still worth an actual `pytest` run on Christer's
machine before trusting this fully; the tests themselves are written
and committed either way.

Update: found a leftover custom test harness at `/tmp/run_harness.py`
in this sandbox (from an earlier session, apparently - `/tmp` persists
here even though it isn't one of the mounted project folders) that
fakes just enough of `pytest`/`monkeypatch`/`capsys`/`tmp_path` to
actually run real `test_*` functions with real `ffmpeg` calls, without
needing `pytest` itself installed. Used it to confirm the aspect-ratio
plumbing above for real: `test_osm_roads: 21 passed`, `test_map_video:
20 passed`, both 0 failed - the earlier "hand-verified with equivalent
plain-Python scripts, not the actual test suite" caveat no longer
applies to this pair of files. Worth remembering this harness exists
if a future sandbox session needs to actually run tests again.

## --stitch: wire the map panel into --stitch-map/--stitch-map-side (done, this session)

Full wiring on top of the aspect-ratio plumbing above - a map panel is
now a real, working part of `--stitch`, not just groundwork for one.

**The core design question, asked and confirmed with Christer first:**
should the panel be rendered fresh, sized exactly for the stitch
composite (no distortion, but `--stitch` generating one file itself -
a departure from its stated "only composes what already exists" rule),
or should it reuse whatever `map.mp4`/`map_zoom_*.mp4` already exists on
disk, scaled into the panel slot as-is (stays consistent with that
rule, but risks visible stretching)? Christer confirmed: render fresh.
The aspect-ratio plumbing built earlier this session exists specifically
to make that possible.

**Where the panel's target size comes from.** The camera composite's
own pixel dimensions are only knowable *inside* `_stack()`, after
front/rear are decoded (and, if `--stitch-resolution` was given, after
the final fit-and-pad) - so that's also the earliest point the panel
can be rendered; it can't happen any earlier; a candidate design where
`trip_export.py` renders the panel itself, ahead of calling
`stitch_cameras()`, was rejected for exactly this reason. `stitch.py`
gained direct dependencies on `map_video.render_map_video()` and
`osm_roads.bounding_box_for_fixes()`/`aspect_ratio_of()` as a result -
a real coupling this module didn't have before, but the map panel is a
first-class part of `--stitch`'s own spec, not a bolt-on.

**Panel sizing (`_map_panel_dimensions()`).** The axis shared with the
camera composite (height for a left/right panel, width for top/down) is
matched exactly - hstack/vstack both require that. The other, free axis
is sized from the trip's own real-world GPS aspect ratio (new
`osm_roads.aspect_ratio_of()`, the "just measure the ratio, don't grow
anything" cousin of `bounding_box_for_fixes()`'s `aspect_ratio`-growing
machinery) - a north-south trip wants a taller panel, east-west wants a
wider one. Clamped to `_MIN_MAP_PANEL_FRACTION`/`_MAX_MAP_PANEL_FRACTION`
(0.2-0.5) of the composite's own corresponding dimension, so a near-
straight-line trip can't ask for a degenerate sliver or an oversized
panel - picked to match the mirror inset's 10-50% and gsensor's 5-40%
clamp ranges stylistically, not independently negotiated with Christer;
worth revisiting if the real numbers look off on an actual archive. That
clamp is relative to the camera composite *alone*, not the eventual
composite+panel total (circular otherwise) - meaning when a map panel
is also requested, `--stitch-resolution` bounds the camera portion, not
necessarily the final file's own total dimensions, since the panel adds
to it. A documented simplification, not hidden.

**Default side (`_DEFAULT_MAP_SIDE_FOR_LAYOUT`).** `side_by_side` (a
wide camera row) defaults to `down`; `top_down` (a tall camera column)
defaults to `left` - per the already-agreed spec, nested perpendicular
to the camera arrangement so the final frame doesn't turn into a long
ribbon. This part didn't need to wait on the still-unbuilt "auto-pick
camera layout from trip geometry" feature - given whatever camera
layout is already in effect (explicit today), the map panel's own
default side is independently well-defined right now.

**`--stitch-map [map|zoom]`** (bare flag = static overview, `zoom` =
follow-camera, reusing `--map-zoom METERS` as the panel's radius -
`--map-zoom` must also be given for that variant, or the panel is
skipped with a warning naming the missing flag) and
**`--stitch-map-side {left,right,top,down}`** (override) are the new
CLI flags, both only meaningful with `--stitch`. `export_trip()` gained
matching `stitch_map`/`stitch_map_side` params, and now loads GPS
fixes/OSM roads (the same `_load_trip_roads()` already shared by `--map`
and `--map-zoom`) whenever `stitch_map` is requested too, not just
`render_map`/`map_zoom_meters` - one fetch/cache, shared by up to three
different renders in the same run.

**Failure handling** matches the rest of `--stitch`: no GPS data, no
default side for an unrecognized layout, a missing zoom radius, or any
render/ffmpeg problem all degrade to a `warnings` entry and no panel,
never a failed stitch. Scope gap, called out clearly rather than
silently: the map panel only combines with the two-camera composite
(`_stack()`) - the single-camera fallback path ignores `map_mode`
entirely, same as it already ignores `layout`. Christer's own archive is
often front-only, so this is a real, not theoretical, gap - worth
revisiting if single-camera trips are a common case for this feature.

**A real bug caught by actually running the test suite, not just manual
scripts.** The first version unconditionally computed the camera
composite's pixel dimensions (via an extra `ffprobe` call on the
decoded rear intermediate) any time `resolution` wasn't given -
including when no map panel was requested at all. Existing tests that
fully mock `encode_with_nvenc_fallback` to write empty (0-byte)
intermediate files broke immediately (`ffprobe failed for rear.mp4:
moov atom not found`) - caught the moment `test_stitch.py` was actually
run through the harness mentioned above, not by the manual verification
scripts used earlier this session (which always used real ffmpeg
output, never empty files, so this exact failure mode never showed up
in them). Fixed by moving that computation inside the
`if map_mode is not None and map_fixes:` block, so it's only ever
computed when a panel is actually being built. This is the clearest
evidence yet in this project that hand-verification scripts, however
careful, are not a substitute for running the real test suite - found
here entirely by luck of having a working harness available.

Tested (all confirmed via the real harness, genuinely executed, not
hand-verified): 16 new tests in `test_stitch.py` (`_map_panel_dimensions`
unit tests - matches shared axis both ways, clamps the free dimension,
returns None for no GPS data; `stitch_cameras()` end-to-end - default
side for each layout, side override, zoom without/with a radius, no-
GPS-data no-op, combines correctly with a requested `--stitch-resolution`
too, ignored for the single-camera fallback). 3 new `test_trip_export.py`
tests (panel actually grows the output vs. a plain stitch, `map_mode`/
`map_side`/`map_fixes`/`map_roads` correctly forwarded to
`stitch_cameras()`, roads never fetched when `stitch_map` isn't given).
3 new `test_bv_export.py` CLI tests (`--stitch-map` only means anything
together with `--stitch`, bare flag defaults to `map`, explicit
mode+side parse correctly). All green: `test_stitch: 51 passed`,
`test_trip_export: 26 passed`, `test_bv_export: 41 passed`, 0 failed
across all three. The rest of the suite (everything the harness covers)
re-run clean too, no regressions.

Not confirmed against a real front+rear BlackVue archive with real GPS
data - only against synthetic `testsrc` clips and hand-written GPS
fixtures. Worth a real `bv-export --stitch --stitch-map` run on
Christer's actual archive before calling this fully done.

## --stitch: wire the g-sensor overlay into --stitch-gsensor/-size/-pos/-xy (done, this session)

Third piece of the --stitch spec, after camera-layout composition and
the map panel: an existing gsensor.mp4 (see --gsensor-video) composited
as a transparent, chroma-keyed overlay on top of the camera footage.

Unlike the map panel, this one *does* follow --stitch's original "only
compose what already exists, never generate" rule - trip_export.py
checks whether `destination/gsensor.mp4` already exists on disk (this
run's own `render_gsensor=True`, or an earlier run's that wasn't wiped)
before ever calling stitch_cameras(); missing means a warning ("run
bv-export --gsensor-video first") and no overlay, not a fresh render.
No design fork to resolve here the way the map panel had one - the spec
was already unambiguous about gsensor.mp4 being a pure prerequisite.

**Filter chain** (all in stitch.py's `_stack()`): the gsensor input is
scaled to `gsensor_size` percent (5-40, default 15) of the camera
composite's own width, preserving its own aspect ratio, then ffmpeg's
`colorkey` filter removes the flat green background (gsensor_render.py's
exact RGB(0,255,0), similarity 0.15/blend 0.05 to absorb a bit of h264
compression bleed at the green/black-ring edges), then `overlay` composites
the result onto the camera footage. Applied *before* any map panel is
added alongside the composite - a named position is defined relative
to the footage region alone, per the spec's explicit note that named
positions must exclude the map panel/mirror-inset area.

**Positioning**: named position (`parse_gsensor_position()` - any
combination of left/right/top/down/center, e.g. "top-right", plain
"center", each axis independently defaulting to "center" if not named -
"top" alone means top-center, not an error) built into ffmpeg `overlay`
x/y expressions using its own `main_w`/`main_h`/`overlay_w`/`overlay_h`
runtime variables (only the small pixel margins are precomputed in
Python) - or an explicit `--stitch-gsensor-xy X,Y` (percent of the
footage region's top-left corner) with no margin at all, a deliberate
raw override per the spec, allowed to land anywhere including on the
map panel. Defaults to `DEFAULT_GSENSOR_POSITION = "top-right"` when
neither is given - **not specified in the agreed spec, my own pick**,
flagged here for Christer to override if a different corner reads
better in practice. The 2%-of-footage-dimension margin around named
positions is the same kind of unconfirmed default, purely visual
polish.

**Dynamic input indices**: with the map panel now also able to add a
third ffmpeg input, the gsensor overlay's input index couldn't stay
hardcoded - `_stack()` now tracks `next_input_index` (starting at 2,
front=0/rear=1 always present) and claims whichever index is next,
gsensor first if both are requested. Verified this actually works, not
just compiles, with a real render combining both in one call.

**CLI**: `--stitch-gsensor` (bool), `--stitch-gsensor-size PERCENT`
(argparse-level range validation, not silent clamping - matches the
spec's "range enforced" language), `--stitch-gsensor-pos POSITION`/
`--stitch-gsensor-xy X,Y` as an argparse mutually-exclusive group (a
typo or a contradictory position like "left-right" is a clear
command-line error via `parse_gsensor_position()`'s own validation,
reused for both CLI-time and _stack()'s own runtime parsing - not a
silent no-op or a --debug-only warning).

Tested: real end-to-end colorkey+overlay verification via a synthetic
green-background/red-box fake gsensor clip (not gsensor.mp4's actual
thin rings/dot - deliberately oversized so a scaled-down, re-encoded
sample pixel reliably lands inside it) - confirmed the green background
genuinely gets keyed out (footage shows through, not green) and the
overlay's own content survives, at the exact computed pixel coordinates
for both a named position and an explicit xy override. 21 new
test_stitch.py tests (parse_gsensor_position unit tests including the
match-style assertions rewritten as plain exception-message checks
after this sandbox's harness turned out not to support pytest.raises'
match= kwarg; default position; explicit xy; combined with a map panel,
proving the dynamic index bookkeeping; ignored for the single-camera
fallback; no-op when gsensor_video isn't given). 4 new
test_trip_export.py tests (uses a freshly-rendered file, reuses an
earlier run's file, warns when missing, options forwarded correctly -
using write_gsensor()'s real .3gf format after an initial fixture built
by hand with the wrong struct layout produced zero parsed samples and a
silently-empty gsensor.mp4, caught immediately by the real test run).
7 new test_bv_export.py CLI tests (flag gating, size/position parsing,
mutual exclusivity, range/position validation, one real end-to-end
render). All genuinely executed via the harness mentioned earlier this
session, all green: test_stitch 62 passed, test_trip_export 30 passed,
test_bv_export 48 passed, 0 failed.

Not confirmed against a real archive - only synthetic clips and a
fabricated gauge stand-in. The 5-40%/15% size range and 20-50% map
-panel clamp range both came from matching the mirror inset's own
range stylistically, not from anything measured on real footage -
worth a look together once there's a real side-by-side to compare
against.

## --stitch: wire subtitle burn-in into --stitch-subtitles/--no-subtitles-bg (done, this session)

Fourth piece of the --stitch spec, after camera-layout composition, the
map panel, and the g-sensor overlay: burning this trip's own trip.srt
into stitch.mp4's final frame - centered, near the bottom (libass's own
default SRT placement, so no explicit alignment override was needed),
with a dark translucent background bar behind the text on by default.
Never trip.lrc - already settled earlier in the --stitch spec
discussion, since `merge_lrc()` always sets `end == start` (no real
per-line duration, fine for karaoke-style display, not a proper
subtitle cue).

**Not gated behind its own "go render it first" step, unlike
gsensor.mp4.** trip.srt is written automatically by export_trip()
whenever the trip has *any* transcript data at all - not behind its own
render flag - so by the time the `stitch_subtitles` check runs, this
same call's own `srt_path` (already computed earlier in export_trip())
is always fresh for whatever this run's recordings currently have. If
the trip has no transcript data at all (`srt_path` stayed None), the
burn-in is skipped with a warning rather than failing the stitch -
consistent with the map panel/gsensor overlay's own "degrade, don't
fail" convention for a missing input.

**Filter chain** (stitch.py's `_stack()`, `_subtitles_filter()`): ffmpeg's
`subtitles` filter, applied *last* - after both the gsensor overlay and
the map panel, onto whatever the final composed frame is by that point
(camera-only, +gsensor, +map, or both together). This is a deliberate
choice, not an oversight: subtitles are dialogue captions for the whole
video being watched, not scoped to one visual region the way the
gsensor overlay deliberately is (see its own docstring note on why it's
confined to the footage region alone). "Centered, near the bottom" is
read here as the bottom of the *final* frame, map panel included -
**not checked against a layout where that visually lands the subtitle
text on top of the map panel itself** (e.g. side_by_side's default
down-side panel) - a known, undecided gap flagged here rather than
silently guessed at.

**Background bar**: `force_style='BorderStyle=4,Outline=0,Shadow=0,BackColour=&H80000000&'`
- BorderStyle=4 switches libass from its default outline-only text
rendering to a solid box using BackColour (ASS packs color as
`&HAABBGGRR` - blue/green/red order, not RGB, and the alpha byte gets
*more* transparent as it increases; `&H80` lands around 50% translucent
black). `--no-subtitles-bg` leaves the filter at libass's default style
entirely (no force_style at all) - plain outlined text, the same as a
bare .srt already renders as.

**Windows path escaping** (`_escape_subtitles_filename()`): ffmpeg's
`subtitles=` filter argument is parsed twice - once by ffmpeg's own
filtergraph parser (where `:` separates the filter name from its
options) and again by libass - before it's treated as a plain filename.
Backslashes are converted to forward slashes (Windows accepts `/`
everywhere ffmpeg/libass read a path, sidestepping `\`'s own meaning as
an escape character rather than trying to double-escape it), and a
drive-letter colon is escaped as `C\:` so the filtergraph parser doesn't
read it as its own option separator and truncate the path. Verified as
a pure string-escaping unit test (no real Windows path available in
this sandbox to exercise end-to-end) plus real ffmpeg renders on Linux
tmp paths (no colons there, so that specific escape never fires in this
sandbox's own end-to-end tests, but the *filter syntax itself* -
`subtitles='<path>':force_style='...'` - was confirmed working for real
via direct ffmpeg calls before writing any of this).

**No per-feature `warnings` entry on failure, unlike the map panel/
gsensor overlay** - a deliberate scope trade-off, not an inconsistency.
A subtitle-burn problem (a malformed .srt, a libass-less ffmpeg build)
surfaces as an ordinary MediaToolError failing the whole stitch, since
by this point it's the very last stage of one already-large ffmpeg
command - isolating just this piece the way the map panel/gsensor
overlay each get their own try/except would mean running the final
encode a second time without subtitles as a fallback, real added cost
for what should be a rare failure mode (Christer's own ffmpeg build
already lists `--enable-libass`, confirmed from an earlier build-config
paste in this project).

**CLI**: `--stitch-subtitles` (bool), `--no-subtitles-bg` (bool,
`dest=subtitles_bg`, `action=store_false`, default True) - named to
match the existing `--no-movement`/`--no-duration` negative-flag
convention rather than a `--stitch-`-prefixed negative, per the
already-agreed spec.

Tested: real end-to-end verification that a background bar darkens a
meaningfully larger fraction of the bottom-of-frame region than bare
outlined text does (both variants have *some* dark pixels near the
bottom even with the bar off, since libass's default style already
draws a thin outline around the glyphs - the comparison is relative,
not an absolute pixel-color assertion, which turned out to be the
fragile approach on a first attempt: comparing average brightness
across the whole bottom strip barely moved, since the box only covers
the text's own width, not the full frame - counting the fraction of
genuinely dark pixels in that same region was the signal that actually
separated the two variants cleanly, roughly 2x). Also confirmed the top
of the frame stays completely untouched (plain footage, no dark
pixels), and that combining subtitles with both a gsensor overlay and a
map panel in the same render produces correct final dimensions with no
warnings (the real thing being tested there: correct clause/label
bookkeeping through all three optional pieces chained together). 7 new
test_stitch.py tests (escaping unit test; background-vs-no-background
comparison; top-of-frame untouched; combined with gsensor+map; ignored
for the single-camera fallback; no-op when subtitles_path isn't given).
4 new test_trip_export.py tests (uses this run's own trip.srt with no
separate render step; options forwarded; skipped without the flag even
though trip.srt still gets written; warns when there's no transcript
data at all). 4 new test_bv_export.py CLI tests (flag gating,
--no-subtitles-bg, one real end-to-end render burning an actual
trip.srt into stitch.mp4). All genuinely executed via the harness, all
green: test_stitch 68 passed, test_trip_export 34 passed, test_bv_export
52 passed, 0 failed.

Not confirmed against a real archive - only synthetic solid-color clips
and a hand-written .srt. The interaction between subtitle placement and
a bottom-side map panel (both wanting the same visual real estate) is
the most likely thing to actually look wrong on Christer's own footage
- worth checking together first, before rearview_mirror or auto-pick.

## --stitch: implement the rearview_mirror camera layout (done, this session)

Fifth piece of the --stitch spec, and the last of the three camera
`--stitch-layout` options: `rearview_mirror` - front stays full-frame
(the primary content), rear is flipped horizontally (a real rearview
mirror shows things reversed, not raw footage) and shrunk into an
inset overlaid top-center, sized `--stitch-mirror-size` percent
(10-50, default 25) of the composite's own width, per the already
-agreed spec.

**Different in kind from side_by_side/top_down, not just a third
STACK_LAYOUTS entry.** The other two are a plain ffmpeg hstack/vstack
of two full-size cameras, both pre-scaled to match on the shared axis
before combining. rearview_mirror never combines two full-size
cameras at all - front decodes untouched (or fit-and-padded to
`--stitch-resolution` alone, the same way the hstack/vstack composite
is, just applied to front by itself since there's no second full-size
camera to combine it with first) and rear also decodes untouched; the
inset's actual scale-down happens inside the filter_complex, based on
the *composite's* own already-known width, not at decode time.
`rearview_mirror` is tracked as its own `_MIRROR_LAYOUT` name, kept
out of `STACK_LAYOUTS` (which stays exactly the two hstack/vstack
entries), with a new `ALL_LAYOUTS` tuple as the actual full set
`stitch_cameras()`/`--stitch-layout` accept - `STACK_LAYOUTS` is
narrowly about "what ffmpeg stack filter does this layout use", not
"what layouts exist".

**Filter chain**: `[1:v]scale=<mirror_width>:-2,hflip[mirrored]` then
`[front][mirrored]overlay=x=(main_w-overlay_w)/2:y=<margin>[withmirror]`
- reuses ffmpeg's own `main_w`/`overlay_w` runtime variables for
horizontal centering (no Python-side math needed there), a small
precomputed `margin_y` (2% of composite height, same purely-visual
-polish role and value as the gsensor overlay's own margin, kept as
its own separate `_MIRROR_MARGIN_FRACTION` constant since the two
features are conceptually distinct even though they share a number).
Reuses ffmpeg input index 1 (rear) directly for the inset - unlike
gsensor.mp4/the map panel, which are separate already-rendered files
each needing their own new input index.

Once the front+inset composite exists (`camera_label`), everything
downstream - gsensor overlay, map panel, subtitle burn-in - treats it
exactly like the hstack/vstack composite would, unchanged. The
existing `comp_width`/`comp_height` computation (previously gated
behind "only worth an extra ffprobe call when gsensor/map panel are
actually requested") now also runs unconditionally for
`rearview_mirror`, since the mirror inset's own size needs it too,
before its overlay clause even exists.

**Map panel gets a tighter clamp in this layout specifically**: capped
at 30% of the composite's width/height rather than the general 50%
(`_REARVIEW_MAP_PANEL_MAX_FRACTION` vs. `_MAX_MAP_PANEL_FRACTION`) -
per the agreed spec, most of a rearview_mirror frame still needs to
stay the primary front view, with the mirror inset already claiming
some of it too. `_map_panel_dimensions()` gained a `max_fraction`
parameter (default `_MAX_MAP_PANEL_FRACTION`) so `_stack()` can pass
the tighter one only for this layout. `_DEFAULT_MAP_SIDE_FOR_LAYOUT`
gained `"rearview_mirror": "down"` - **my own pick**, not specified
beyond the spec's "left or down" - front is the whole composite here
(no rear column/row to be perpendicular to the way side_by_side/
top_down have), so there's no geometric argument either way; `down`
just matches side_by_side's own default.

**A genuinely bogus test caught by the real render, not by review**:
the pre-existing `test_stitch_cameras_rejects_an_unknown_layout` used
`layout="rearview_mirror"` as its "this should be rejected" example -
correct before this session, now a false failure once the layout
actually exists. Fixed by switching it to a truly nonexistent layout
name (`"diagonal"`) and adding a companion
`test_all_layouts_includes_rearview_mirror` alongside the renamed
`test_stack_layouts_has_the_two_hstack_vstack_layouts` (kept as-is,
still true - `STACK_LAYOUTS` deliberately never gained a third entry).

**CLI**: `--stitch-layout` now accepts `rearview_mirror` (choices
sourced from `ALL_LAYOUTS` directly, so the CLI and stitch.py can't
drift apart on what's valid). `--stitch-mirror-size PERCENT`
(argparse-level range validation, 10-50, default 25 - same pattern as
`--stitch-gsensor-size`).

Tested: real end-to-end verification that hflip is actually happening
(not just scale+overlay) - a rear source with red on its own left half
and green on its own right half ends up with green on the inset's left
and red on its right after compositing, sampled at exact computed
pixel coordinates, same rigor as the gsensor colorkey verification
earlier this session. Also verified: inset width scales correctly with
a non-default `--stitch-mirror-size`; front stays exactly full-frame
with no mirror inset requested elsewhere in the frame; a requested
`--stitch-resolution` still produces exactly that final size; the 30%
map-panel cap actually engages (a sharply north-south trip fixture
that would ask for far more than 30% under the general clamp lands
exactly at comp_height*0.3 instead); single-camera fallback ignores
`rearview_mirror` entirely, same convention as every other stitch
feature. 8 new/changed test_stitch.py tests, 2 new test_trip_export.py
tests (produces a video, `stitch_mirror_size` forwarded), 6 new
test_bv_export.py CLI tests (layout choice, default/explicit mirror
size, range rejection, one real end-to-end render). All genuinely
executed via the harness (in three batches this time - test_stitch
alone now runs past the harness's single-command time budget with 74
real-ffmpeg tests in the file), all green: test_stitch 74 passed,
test_trip_export 36 passed, test_bv_export 57 passed, 0 failed.

Not confirmed against a real archive - only synthetic solid-color/
color-block clips. The `--stitch-mirror-size` 10-50%/25% range comes
directly from the agreed spec this time (not one of this session's own
picks, unlike the gsensor/map-panel ranges) - but the actual visual
result (is a 25% top-center inset the right size/position against a
real dashcam's real field of view) is still worth Christer's own eyes
once he's back at his archive.

## --stitch: auto-pick --stitch-layout from trip geometry (done, this session)

Sixth and last piece of the original --stitch spec: when
`--stitch-layout` isn't given explicitly, pick `side_by_side` or
`top_down` from the trip's own real-world GPS extent instead of a
fixed default - an east-west trip (wider than tall) picks
`side_by_side` (front | rear, itself a wide row); a north-south trip
(taller than wide) picks `top_down` (front / rear, itself a tall
column) - each camera arrangement matching the trip's own overall
shape. `rearview_mirror` is never auto-picked - it's a distinct visual
style someone opts into by name, not something the trip's shape alone
should decide (also true of the map panel's own left/down auto-pick,
which was always keyed off the *camera* layout regardless of how that
layout itself got chosen - unaffected by this change).

**The sentinel design**: `stitch_layout: str | None` already had a
meaning before this session - `None` means "skip --stitch entirely",
checked throughout trip_export.py (`if stitch_layout is not None:`,
`if stitch_gsensor and stitch_layout is not None`, etc.). Auto-pick
needed a *third* state ("--stitch is happening, but pick the camera
layout for me") without disturbing that existing None-means-skip
convention. Solved with a new `stitch.AUTO_LAYOUT = "auto"` sentinel
string - a real, non-None value, so every existing "are we stitching
at all" gate keeps working unchanged - resolved to a concrete
`side_by_side`/`top_down` in trip_export.py's `export_trip()`, right
before the `stitch_cameras()` call, via the new
`stitch.pick_stitch_layout(fixes)`. `AUTO_LAYOUT` never reaches
`stitch_cameras()`/`_stack()` itself - deliberately kept out of
`ALL_LAYOUTS`, so passing it there directly still raises the same
`ValueError` any other made-up layout name would (verified with a real
test, not just by inspection - the kind of invariant that's easy to
silently break in a later refactor without a test pinning it down).

**`pick_stitch_layout(fixes) -> str | None`** (stitch.py, next to
`_map_panel_dimensions()` - same aspect-ratio-from-GPS math via
`bounding_box_for_fixes()`/`aspect_ratio_of()`): returns `None` if
there isn't enough GPS data to compute a bounding box at all, mirroring
every other "nothing to bound" convention already in this module - the
caller (trip_export.py) degrades that to a `warnings` entry and a
fixed `side_by_side` fallback, the same "degrade, don't fail" pattern
the map panel/gsensor overlay/subtitle burn-in all already follow. A
perfectly square real-world extent (aspect ratio exactly 1.0) picks
`side_by_side` - an arbitrary, harmless tie-break, not a meaningful
threshold.

**CLI**: `--stitch-layout`'s `choices` now include `AUTO_LAYOUT`
alongside `ALL_LAYOUTS` (sourced directly from stitch.py's own
constants, so the CLI can't silently drift out of sync with what's
actually valid), and its `default` changed from a fixed
`"side_by_side"` string to `AUTO_LAYOUT` - so a bare `--stitch` with no
`--stitch-layout` at all now auto-picks, while any explicit
`--stitch-layout` value (including `side_by_side` typed out by hand)
is always honored exactly as given, per the spec's "always
overridable" language.

**A pre-existing test caught by the changed default, not by
review**: `test_main_uses_the_default_stitch_layout_when_stitch_flag_given`
asserted `captured["stitch_layout"] == "side_by_side"` for a bare
`--stitch` - correct before this session, a false failure now that the
CLI's own default is `"auto"` instead. Fixed by updating the
assertion (with a comment explaining *why* the expectation changed,
not just what it changed to) rather than treating it as a real
regression - a deliberate, understood consequence of the new default,
not a bug.

Tested: `pick_stitch_layout()` directly against an east-west fixture,
a north-south fixture, and no GPS data at all (3 tests). A real
`export_trip()`-level end-to-end check that `stitch.AUTO_LAYOUT`
actually produces a `side_by_side`-shaped file for an east-west trip
and a `top_down`-shaped one for a north-south trip (checked via real
ffprobe dimensions, not just which string got passed internally),
plus the no-GPS-data warning+fallback path, plus confirmation that an
*explicit* `stitch_layout="top_down"` on an east-west trip is honored
exactly (never silently overridden by auto-pick) - 4 new
test_trip_export.py tests. 3 new test_bv_export.py CLI tests (default
is now `"auto"`, a genuinely real end-to-end run through `main()` with
no `--stitch-layout` at all against a real east-west GPS archive,
landing on the expected `side_by_side` dimensions) plus the one
existing test's assertion fix described above. All genuinely executed
via the harness (test_stitch split across three batches again, same
reason as last time - too many real-ffmpeg tests now to fit one
harness invocation's time budget), all green: test_stitch 78 passed,
test_trip_export 40 passed, test_bv_export 59 passed, 0 failed.

Not confirmed against a real archive - the aspect-ratio-based pick
logic is straightforward and directly mirrors the already-agreed spec
text, but whether it actually produces a *pleasant-looking* stitch.mp4
on Christer's own real trips (as opposed to just "technically the
requested layout") is still worth his own eyes.

This closes out the original --stitch spec's six-item roadmap end to
end: camera-layout composition, resolution/bitrate controls, the map
panel, the g-sensor overlay, subtitle burn-in, and now layout auto
-pick. `rearview_mirror` remains explicit-only by design. No further
--stitch work is currently planned - future requests would be new
scope, not spec items still outstanding.

---

## bv-export: per-trip trip.log (done, this session)

Christer reported three things in one message: bv-export needs a log
file per trip (start time, the full invoking command, why each
recording was judged to belong to the trip, what each phase is doing,
end time); a trip pulled in a recording that shouldn't belong to it
(originally described as "almost 6 days earlier", corrected to "6
days later"); and stitch.mp4 has no audio. Order confirmed via
AskUserQuestion: build the log first (so the trip-detection bug can be
diagnosed against Christer's real archive instead of guessed at from
code alone), then audio.

**`TripBuilder.build()` gained an optional `reasons` out-param**
(`export/trip_log.py`'s design needed *something* to log for "why does
this recording belong here" - reconstructing that after the fact from
the final trip list would mean re-deriving TripBuilder's own gap/
bridge logic a second time, which could drift from what it actually
decided). Same idiom the codebase already uses for `warnings: list[str]
| None = None` throughout: `build(recordings, *, reasons:
dict[RecordingId, str] | None = None)`, populated in place, one entry
per recording, explaining whether it starts a new trip or continues
one, the exact gap and threshold compared, and (if bridged) what
evidence bridged it. Confirmed via grep this doesn't break anything -
only `bv_export.py` and `bv_ls.py` call `TripBuilder.build()`, and
`bv_ls.py` doesn't pass `reasons` at all, so it's unaffected.

Feeding real evidence text into `reasons` needed `movement_bridges_gap()`
(and its helper `_recording_shows_movement()`, telemetry/movement.py)
to return more than a bare bool - changed from `bool` to `str | None`:
a short human-readable description ("GPS speed at/above 5 km/h near
the end of ...", "g-sensor variance near the start of ... exceeded its
own stationary baseline") or `None` for no evidence. Backward
compatible for both existing callers, which only ever check
truthiness (`TripBuilder.build()`'s `if gap > threshold and
self.bridge:` / `if ... and not bridge_reason:`) - never the exact
type. Three existing tests that asserted `is True`/`is False` on the
old bool return were renamed and rewritten to check `is not None` +
the reason text, or `is None` - a deliberate, disclosed API-contract
change, not a bug fix.

**`TripBuilder._describe_gap(gap)`**, a new static method, renders a
gap for `reasons` messages - and explicitly flags a *negative* gap
("... BEFORE the previous recording's own end (overlapping or
out-of-order timestamps)") rather than printing a bare, easy-to-miss
negative number. This exists because a negative gap is exactly the
shape a sort-order or duration-parsing bug would take - worth being
loud about on principle, even though it turned out not to be this
bug (see below).

**`export/trip_log.py`** (new module) - `TripLog`, one instance per
trip, opened via `TripLog.open(destination, trip_label=..., command=
...)` right after `export_trip()` creates the destination folder.
Writes `trip.log` incrementally: every `step()`/`membership()`/
`warning()` call flushes to disk immediately, not buffered until
close() - this is the direct answer to Christer's own "it hang on
output: map phase took 181.3s" report. A log only written at the end
would show nothing at all for a run stuck mid-phase; flushing per line
means whatever's on disk when a hung process is checked is the real,
current state. A `threading.Lock` guards every write, since front/
rear/audio concatenation runs in three concurrent threads (see
`export_trip()`) that can all call into the log around the same
moment. Three sections, written in order, each header appearing only
once and only if actually used (a run that fails immediately still
produces a clean, readable trip.log rather than one with empty
sections): a header (start time, trip label, invoking command) written
immediately on `open()`; trip membership (`membership(recording_id,
reason)`, sourced straight from `TripBuilder.build()`'s new `reasons`
dict - never re-derived, so the log can't disagree with the actual
decision); and export steps (`step(message, elapsed_seconds=None)`,
timestamped `HH:MM:SS`). `close(failed=False)` writes the footer
(finished/did-not-finish-cleanly, elapsed seconds) - also usable as a
context manager (`with TripLog.open(...) as log:`) so `__exit__` sets
`failed=True` automatically on an unhandled exception.

**Bracketing slow phases with a "starting" line, not just a
completion line** - the part of the design that actually serves the
hang-diagnosis goal. A phase that only logs on success tells Christer
nothing if the process is still inside it; `_render_map_variant()`,
the gsensor.mp4 render, and the stitch.mp4 render (in `trip_export.py`)
all now log `"starting <phase>"` immediately *before* the slow call,
so a stuck run's trip.log shows exactly which phase it entered and how
long ago, even without waiting for that phase to ever finish.

**Wiring into `export_trip()`**: every phase (concatenation,
text-asset merge, srt/lrc merge, GPX write, map/map-zoom render,
g-sensor merge/render, stitch resolve+render) got a paired `log.step()`
call for both the "did it" and "skipped, here's why" outcomes - not
just the ones Christer explicitly listed, on the theory that a log
with gaps in it ("why didn't gsensor run") is nearly as confusing as
no log at all. Every `warnings.append(...)` call site also gets a
matching `log.warning(...)` (itself just `step()` prefixed
`"WARNING: "`), including a diff-based catch for warnings
`stitch_cameras()` appends internally (map panel/gsensor overlay/
subtitle issues) - captured by comparing the shared `warnings` list's
length before/after the call, rather than threading `TripLog` into
`stitch.py` itself, to keep this change scoped to trip_export.py/
trip_log.py as agreed. `_concatenate_asset()`/`_load_trip_roads()`/
`_render_map_variant()` all gained an optional `log` parameter for
this same reason. `export_trip()` itself doesn't wrap its body in a
try/finally around `log.close()` - every sub-call that could plausibly
raise (`MediaToolError` from ffmpeg/OSM fetches) already catches its
own exception internally and degrades to a warning rather than
propagating, so an uncaught exception escaping `export_trip()` would
be a genuine bug elsewhere, not an expected path; if that ever
happens, trip.log still has every step logged up to that point (already
flushed), just without a footer - a known, accepted small gap rather
than restructuring the whole function's control flow for a case that
shouldn't occur.

**`bv_export.py`**: `TripBuilder(...).build(archive.recordings,
reasons=reasons)` now passes a `reasons` dict through, forwarded
unchanged to every trip's own `export_trip()` call (each trip only
logs entries for its own recordings via `reasons.get(recording.id)`,
so passing the whole archive-wide dict to every trip is fine - no
per-trip filtering needed). `main()` reconstructs the exact invoking
command from `argv`/`sys.argv[1:]` via `shlex.join()` (not from the
already-parsed `args` Namespace, which has been through argparse's own
defaulting and wouldn't necessarily read back as what Christer actually
typed) and threads it through `bv_export()` as `command_line`.

Tested: 11 new `test_trip_log.py` tests directly against `TripLog`
(header/membership/step/warning/close/failed-close/context-manager
behavior, including a mid-run read proving lines are flushed before
`close()`). 5 new `test_trip_export.py` tests (`trip.log` always gets
written; the given `command_line` and `reasons` show up verbatim;
concatenation/GPX steps get logged with the right skip messages; a
"starting stitch.mp4 render" line appears *before* "rendered
stitch.mp4", not just after). 2 new `test_bv_export.py` CLI-level
tests (`main()`'s reconstructed command line - path, `--target`,
`--overwrite` - shows up in a real trip's `trip.log`; two recordings
close enough to share a trip produce a real "continues the trip"
membership line, not just the first-recording one). Also ran a real,
non-test end-to-end `bv-export --stitch` against a tiny synthetic
archive and read the resulting `trip.log` by eye to confirm it's
actually readable, not just assertion-passing - it is (see this
session's transcript for the full example output). All genuinely
executed via the harness: `test_trip_log` 11 passed, `test_trip_export`
45 passed, `test_bv_export` 61 passed, `test_trip_builder` 23 passed,
`test_movement` 12 passed, `test_stitch` 78 passed (unchanged - this
session never touched stitch.py, run anyway as a regression check
since `_render_map_variant()`'s call site into it moved around),
0 failed anywhere.

**The trip-detection bug fix itself is still deferred** (see task
list) - the log feature was step one specifically so the actual root
cause (suspected: `movement_bridges_gap()`'s g-sensor sub-check
producing a false-positive bridge when a recording's self-calibrated
"stationary baseline" comes out to exactly 0, combined with no upper
bound on how large a gap `bridge` is allowed to close) can be
confirmed against Christer's real archive via the new `reasons`/
trip.log output, not fixed blind. Christer corrected the direction
mid-session: the stray recording was 6 days *later*, not earlier -
this rules out a secondary sort-order/negative-gap hypothesis (which
would need an earlier stray recording) but doesn't affect the primary
bridge-with-no-ceiling hypothesis, which works in either direction.
Next step on this: Christer re-runs `bv-export` for real and shares
(or reads himself) the `trip.log` for the trip that grabbed the wrong
recording.

---

## --stitch: mux the trip's own audio into stitch.mp4 (done, this session)

Third and last item from Christer's original bug/feature message:
stitch.mp4 had no sound. `stitch_cameras()`/`_stack()` (stitch.py)
gained an `audio_path: Path | None = None` param - when given, muxed
into the final output as a stream copy (`-c:a copy`, no re-encode -
the source is already a compressed AAC stream, and this stage's job
is combining video, not touching audio at all). Wired as an extra
`-i` input added last, right before the final `encode_with_nvenc_
fallback()` call, so it always claims whichever input index is left
over after the gsensor overlay and/or map panel have claimed theirs
(both of which, unlike audio, only exist when actually requested, so
audio can't just be hardcoded to a fixed index). A second `-map`
entry (`{index}:a`) sits alongside the existing `-map [output_label]`
video map.

Scoped to the two-camera `_stack()` path only, matching every other
--stitch add-on's own scope this session (map panel/gsensor overlay/
subtitles all skip the single-camera fallback too) - `stitch_cameras()`'s
docstring documents this as a known, deliberate gap rather than an
oversight: the single-camera path is a plain stream copy or single
-source re-encode with no filter_complex to hang a second `-map` off
of, and Christer's own trips normally have both cameras anyway.

`trip_export.py`'s `export_trip()` needed no new parameter at all -
the trip's own concatenated `audio` (the `audio.aac` local variable,
already sitting there from the front/rear/audio concatenation block
earlier in the same function) is simply forwarded into the existing
`stitch_cameras()` call as `audio_path=audio`. Not a new opt-in flag:
there's no reason anyone would want a silently muted stitch.mp4 when
the trip's own audio is already sitting right there having just been
concatenated moments earlier in the same run.

Tested: 3 new `test_stitch.py` tests directly against `stitch_cameras()`
(a real synthesized sine-wave AAC source ends up as a genuine `aac`
audio stream in the output, confirmed via ffprobe, not just "ffmpeg
didn't error"; no `audio_path` given produces no audio stream at all;
`audio_path` is accepted but silently ignored for the single-camera
fallback, matching the map panel/gsensor overlay's own "ignored for
single camera" tests). 2 new `test_trip_export.py` tests (a trip with
real front/rear/audio recordings produces a stitch.mp4 with an actual
audio stream; a trip with no audio at all still produces a silent
stitch.mp4, not a broken one). Also ran a real non-test `bv-export
--stitch` end-to-end through `main()` against a synthetic front/rear/
audio archive and confirmed via ffprobe that the resulting stitch.mp4
genuinely has both an h264 video stream and an aac audio stream. All
green: `test_stitch` 81 passed (78 existing + 3 new, run in batches
again - too many real-ffmpeg tests for one harness call), `test_trip_
export` 47 passed (45 + 2 new), `test_bv_export` 61 passed (unchanged -
regression check only, --stitch's CLI surface itself didn't change),
0 failed anywhere.

---

## Trip detection: movement-based bridging disabled by default (done, this session)

Christer re-ran `bv-export` and shared the real `trip.log` line for
the trip that grabbed a recording days apart from the rest:

    20260721_124108_N: continues the trip - gap since 20260715_144844_N
    was 510744.0s, over the 610.0s max_gap+gap_tolerance threshold,
    but bridged by: GPS speed at/above 5 km/h near the start of
    20260721_124108_N

This confirms the primary hypothesis from the earlier investigation
exactly: a single GPS fix showing ~5+ km/h right at the start of a
recording bridged a 510744-second (~5.9 day) gap into one trip - the
`bridge` mechanism has no ceiling on how large a gap it's willing to
close, so *any* amount of GPS/g-sensor movement evidence at either
edge overrides the entire `--max-gap` rule, regardless of how far
apart the two recordings actually are.

Asked Christer what ceiling would be right; his answer: he doesn't
know a good number, and estimates the camera is offline (not
recording at all) roughly 80% of the time between trips - meaning long
gaps between recordings are the *normal* case for his archive, not a
rare brief-stop exception the heuristic's whole premise assumes. Given
that, picking any specific ceiling number would be a guess dressed up
as a fix. His decision: disable movement-based bridging by default
rather than invent a number neither of us was confident in.

**The fix**: flipped the sense of the CLI flag in both `bv-export` and
`bv-ls` - `--no-movement` (opt-out, previously default-on) became
`--movement` (opt-in, default off). `bv_export()`'s and `bv_ls()`'s own
`movement`/`use_movement` parameter defaults changed from `True` to
`False` to match. With no flag given, `bridge` is `None` and
`TripBuilder.build()` falls back to its pure time-gap rule
(`--max-gap` + `--gap-tolerance`, optionally duration-adjusted via
`--duration`, which stays on by default and is unaffected by any of
this) - the same reliable, well-tested logic this session's earlier
`_describe_gap()`/`reasons` work was built on. `movement_bridges_gap()`
itself (telemetry/movement.py) is untouched - still available, still
correct for what it does, just no longer consulted unless explicitly
asked for.

Applied to **both** `bv-export` and `bv-ls --trips`, not just
bv-export - both call sites share the exact same `TripBuilder`+
`movement_bridges_gap()` machinery and the exact same flaw, and
leaving them inconsistent (bv-export's default trips disagreeing with
bv-ls's) would be confusing for the same archive. Not something
Christer asked for explicitly, but a direct, same-root-cause
consequence of his answer, called out here rather than left silent.

Verified against the exact real scenario: a synthetic two-recording
archive ~6 days apart, the later recording carrying a GPS fix showing
movement at its start (the identical shape of Christer's real trip.log
line above) - confirmed via a real `bv-export` run through `main()`
that the two recordings now land in separate trip folders by default,
where before this fix they'd have merged.

Tested: `test_bv_ls.py` - renamed/fixed the one existing test that
relied on the old default-on behavior (now passes `movement=True`
explicitly), added a new default-off test, and two new `main()`-level
CLI tests for `--movement`. `test_bv_export.py` - two new `bv_export()`
-level tests (default doesn't bridge even with real GPS evidence;
`movement=True` does) plus a `main()`-level `--movement` CLI test.
`movement_bridges_gap()`/`TripBuilder` themselves are unchanged, so
`test_movement.py` (12) and `test_trip_builder.py` (23) still pass
unmodified - this was purely a default-value/CLI-flag change at the
two command layers, not a change to the underlying detection logic.
All green: `test_bv_ls` 17 passed (0 failed - some PASS lines don't
print to the visible harness log for tests using the `capsys`
fixture, a known harness quirk, not a test failure), `test_bv_export`
64 passed (61 + 3 new), `test_trip_export`/`test_trip_builder`/
`test_movement` unaffected and re-run clean anyway, 0 failed
anywhere.

The root cause is now fixed for real trip detection, not just
diagnosed. `movement_bridges_gap()` remains available behind
`--movement` for whoever wants to opt back in (e.g. for a shorter,
more confident gap where GPS evidence genuinely helps, like a long
traffic light or a tunnel) - just off by default until there's a
ceiling number worth trusting.

---

## gsensor.mp4 render: fix an O(samples x frames) interpolation hang (done, this session)

Christer reported bv-export appearing stuck again, this time at a
different `trip.log` line: `starting gsensor.mp4 render`, with no
completion after a long wait. Same investigative discipline as the
trip-detection bug: read the actual code rather than guess, via a
research-only subagent pass over `gsensor_video.py`, `gsensor_render.py`,
and `gsensor_reader.py`.

**Confirmed cause**: `render_gsensor_video()`'s frame loop called
`interpolate_sample()` once per output frame; that function does a
full linear rescan of *every* g-sensor sample from the start on every
single call. Both the sample count and the frame count scale with
trip duration (g-sensor samples land roughly every 100ms - see
gsensor_reader.py - and the render deliberately matches that at
`DEFAULT_FPS = 10`), so the loop's total cost was O(samples x frames)
- quadratic in trip length. A short test trip never surfaces this; a
real multi-hour trip (very plausible per the previous section -
Christer estimated the camera is offline/idle a majority of the time,
implying long continuous or parking-mode recordings aren't rare)
turns into billions of inner-loop timedelta comparisons, indistinguishable
from a hang without watching CPU usage.

**The fix**: added `_advance_search_index()`/`_interpolate_from_index()`
(gsensor_video.py) - a forward-only two-pointer approach exploiting
the one property `interpolate_sample()`'s public, general-purpose
signature doesn't get to assume: within `render_gsensor_video()`'s own
loop, `elapsed` only ever increases frame over frame. Carrying a
single `search_index` across iterations (never resetting it) means
each frame's lookup only scans forward past whatever the previous
frame already passed, turning the loop's cost from O(samples x frames)
into O(samples + frames). `interpolate_sample()` itself is untouched
- still correct, still public, still used by its own tests below, just
no longer called by the hot per-frame loop.

Also added the sample count to the "starting gsensor.mp4 render"
`trip.log` line (`trip_export.py`) - so a future run that looks stuck
at this same step can tell straight from the log whether it's a
genuinely huge trip or something worth investigating further, the
same reasoning behind bracketing every slow phase with a "starting"
line in the first place.

**Known related risk, not fixed here**: `map_video.py`'s
`interpolate_position()` has the identical unindexed-rescan shape.
It's not currently painful in practice - GPS fixes land around 1Hz and
`map_video.py`'s own `DEFAULT_FPS` is 5, so both `n` and frame count
stay far smaller than g-sensor's 10Hz/10fps combination - but the same
class of bug is latent there for a long enough trip. Flagged here
rather than fixed proactively, to keep this change scoped to the
actual reported problem; worth revisiting if a similarly slow map.mp4
render is ever reported.

Tested: 4 new `test_gsensor_video.py` tests confirming
`_advance_search_index()`/`_interpolate_from_index()` reproduce
`interpolate_sample()`'s exact clamp-before/clamp-after/midpoint
behavior, plus a monotonic-sweep test that walks both the fast
indexed path and the original full-rescan function across the same
elapsed values and asserts identical results at every step (the real
correctness guarantee - "faster" is only worth having if it's still
exactly right). One new performance regression test: simulates a
synthetic 4-hour trip at g-sensor's native 10Hz (14,400 samples/
frames - old path would be on the order of 2x10^8 inner-loop
iterations) using just the two new functions directly (not the full
PIL/ffmpeg render, which has its own real, expected per-frame cost
unrelated to this bug) and asserts it finishes in well under 5
seconds. Also ran a real end-to-end `render_gsensor_video()` call for
a realistic 10-minute/6,000-sample trip outside the test suite -
completed in ~31s (real PIL+ffmpeg cost, not a hang) - and confirmed
the existing `test_render_gsensor_video_centers_positions_on_the_trips_
median_reading` and `test_render_gsensor_video_produces_a_real_video_
end_to_end` tests (which exercise the actual frame loop's output, not
just the isolated helpers) still pass unchanged, confirming the fix
didn't alter rendered output. All green: `test_gsensor_video` 14
passed (10 existing + 4 new), `test_trip_export` 47 passed (unaffected,
regression check only), 0 failed.

## map.mp4/gsensor.mp4: fix timeline desync when a recording lacks GPS/g-sensor data (done, this session)

Christer reported: a 3-recording trip (~3 min each) where the first
recording has no GPS data but the other two do produces a map.mp4 only
~6 minutes long that "starts from the beginning" out of sync with
front/rear.mp4 - and guessed the same was probably true for
gsensor.mp4.

Root cause, map.mp4: `render_map_video()` anchored both frame 0 and
the total render duration purely to the GPS fixes' own span
(`positioned[0].timestamp` / `positioned[-1].timestamp`), with no
knowledge of the trip's real start time or real video length. A
leading gap (early recording with no GPS) shrinks the fix span from
the left, so the render starts late and runs short at both ends
relative to front/rear.mp4.

Root cause, gsensor.mp4: only half of this bug applied.
`trip_export.py`'s `_merge_gsensor()` already rebases each recording's
g-sensor offsets against the trip start (`rebase = recording.id.
timestamp - trip_start`), so a *leading* gap (no g-sensor data on the
first recording) was already handled correctly - the samples that do
exist already carry the right trip-relative offset. But
`render_gsensor_video()`'s total duration was computed as
`samples[-1].offset.total_seconds()`, so a *trailing* gap (no
g-sensor data on the last recording) still cut the render short.

Fix: threaded the trip's real start timestamp and its real probed
video duration (`video_duration_seconds`, already computed in
`export_trip()` for subtitle padding) into both renderers as new
optional parameters, defaulting to the old fixes/samples-derived
behavior when omitted.

- `map_video.py`: `render_map_video()` gained `video_start` and
  `video_duration_seconds` params. `start` now falls back to
  `video_start` when given (instead of always `positioned[0].
  timestamp`), and `total_seconds` to `video_duration_seconds` when
  given. `interpolate_position()`'s existing clamp-to-nearest-fix
  behavior does the rest for free: frames rendered during a real gap
  just freeze on the nearest known fix instead of needing new clamp
  logic.
- `gsensor_video.py`: `render_gsensor_video()` gained
  `duration_seconds`, used in place of `samples[-1].offset.
  total_seconds()` when given.
- `trip_export.py`: both map.mp4/map_zoom_*.mp4 render call sites and
  the gsensor.mp4 call site now pass `trip.start_timestamp` /
  `video_duration_seconds` through. `stitch_cameras()` call site
  passes the same through as `map_video_start`/
  `map_video_duration_seconds`.
- `stitch.py`: `_render_map_panel()`, `stitch_cameras()`, and
  `_stack()` all gained the same two params (prefixed `map_` at the
  `stitch_cameras()`/`_stack()` level to disambiguate from other
  stitch concepts) and forward them into the panel's own
  `render_map_video()` call, so `--stitch-map` gets the identical fix.

Scope decision: fixed both the standalone map.mp4/map_zoom.mp4 outputs
and the `--stitch-map` panel path, not just whichever Christer's
literal example most directly describes - leaving either half fixed
and the other not would be an obviously incomplete fix for the same
root cause.

Test fixture fix: `test_trip_export.py`'s `_trip_with_two_gps_fixes()`
used a hardcoded GPS epoch (`1700000000000`, Nov 2023) unrelated to
its own `RecordingId` values (July 2026) - harmless before this
change since nothing compared the two, but once `video_start=trip.
start_timestamp` was wired in, the mismatch drove `total_seconds`
deeply negative and tripped the `<= 0: return None` guard. Fixed with
a new `_epoch_ms()` helper that derives GPS fix timestamps from the
fixture's own `RecordingId` values instead of an arbitrary epoch.

Tested: `test_map_video` 24 passed (20 existing + 4 new: video_start
extends render across a leading gap, position clamps correctly during
that gap, video_duration_seconds extends past a trailing gap, and
default/no-params behavior is unchanged), `test_gsensor_video` 16
passed (14 + 2 new: duration_seconds extends past a trailing gap,
default behavior unchanged), `test_trip_export` 47 passed (5 of which
only started passing again after the fixture epoch fix), `test_stitch`
81 passed, `test_bv_export` 64 passed, 0 failed. Also ran a real,
non-mocked end-to-end verification: a synthetic 3-recording archive
shaped exactly like Christer's report (first recording with no GPS,
next two with GPS, ~3s each) through the real `bv-export --map` CLI
path (network calls to the OSM Overpass API monkeypatched out, since
the sandbox has no network access) - confirmed via `ffprobe` that
`front.mp4` (9.0s) and `map.mp4` (9.2s, the extra 0.2s being the
render's own "+1 frame" convention) now match, where before the fix
map.mp4 would have been ~6s long and started 3 seconds late relative
to front.mp4.

## --stitch-gsensor: fix misleading warning when a trip has no g-sensor data (done, this session)

Christer's trip.log showed:

```
no g-sensor data for this trip - trip.3gf skipped
WARNING: stitch gsensor overlay: gsensor.mp4 not found - run bv-export --gsensor-video first
```

He'd guessed correctly that the trip simply had no g-sensor data at
all. The bug: `--stitch-gsensor`'s "gsensor.mp4 not found" check in
`trip_export.py` only ever looked at whether the file existed on disk
- it had no way to tell "not rendered yet, but could be" apart from
"can never be rendered, there's no data for this trip" (`samples`,
from `_merge_gsensor(trip)`, is already computed earlier in
`export_trip()` for the trip.3gf write). The former case's advice
("run bv-export --gsensor-video first") is actively wrong for the
latter - running with that flag would still produce nothing, since
`render_gsensor_video()` is only ever called `if render_gsensor and
samples`.

Fix: `trip_export.py`'s stitch-gsensor check now branches three ways
instead of two - existing gsensor.mp4 found (unchanged), no g-sensor
data for this trip at all (new: "stitch gsensor overlay: no g-sensor
data for this trip - skipped"), or data exists but hasn't been
rendered yet (unchanged "gsensor.mp4 not found - run bv-export
--gsensor-video first" message, now only shown when it's actually
correct advice).

Test fixture note: the pre-existing
`test_export_trip_stitch_gsensor_warns_when_no_gsensor_mp4_exists` used
`_trip_with_front_and_rear()`, which has no GSENSOR asset at all - so
it was actually already exercising the "no data" case, just asserting
the old (now-wrong-for-this-case) message. Renamed to
`test_export_trip_stitch_gsensor_warns_when_trip_has_no_gsensor_data`
and updated its assertion. Added a new sibling test,
`test_export_trip_stitch_gsensor_warns_when_gsensor_mp4_not_yet_rendered`,
using a trip that does have a GSENSOR asset (so `samples` is
non-empty) but no rendered gsensor.mp4 on disk, to cover the case
where the original "go run --gsensor-video" message is still correct.

Tested: `test_trip_export` 48 passed (47 + 1 new test), 0 failed.

## Trip detection: default --max-gap lowered from 10 to 5 minutes (done, this session)

Christer asked to change the default gap threshold to 5 minutes.
`trip_builder.py`'s `DEFAULT_MAX_GAP` changed from `timedelta(minutes=
10)` to `timedelta(minutes=5)`. `gap_tolerance`'s 10-second noise
margin on top is unchanged, so the real effective threshold is now
5m10s (was 10m10s). `bv-export --max-gap`/`bv-ls --max-gap`'s help
text render the default from this same constant, so nothing needed
changing there.

Two `test_bv_ls.py` tests hardcoded timing that depended on the exact
old default rather than just testing generic gap behavior, and had to
be updated to still exercise what they were meant to:

- `test_trips_default_gap_tolerance_absorbs_a_few_seconds` used
  recordings 10m5s apart (just inside the old 10m10s threshold) - now
  5m5s apart (just inside the new 5m10s threshold).
- `test_trips_defaults_to_a_ten_minute_gap` (recordings 9 minutes
  apart, under the old 10-minute default) - renamed
  `test_trips_defaults_to_a_five_minute_gap`, recordings now 4 minutes
  apart.

Everything else referencing the old default was either a stale
comment (fixed for accuracy) or a test whose actual gap was far enough
from the boundary (30 minutes, or a negative duration-aware gap) that
the exact default value never mattered to the assertion - confirmed by
running the full `test_trip_builder`/`test_bv_ls`/`test_bv_export`
suites rather than trying to eyeball every case.

Tested: `test_trip_builder` 23 passed (all pass `max_gap` explicitly,
unaffected by the constant), `test_bv_ls` 17 passed, `test_bv_export`
64 passed, 0 failed.

## --stitch default layout: subtitle scoping, --stitch-map-size, gsensor-vs-padding bug (done, this session)

Christer shared a real stitch.mp4 screenshot (top_down, auto-picked,
`--stitch-map --stitch-gsensor --stitch-subtitles --stitch-resolution
1920x1080`) with three visible problems: the subtitle bar spans the
full frame width including underneath the map panel; the map panel
reads as too thin with no way to ask for more; the gsensor gauge
floats alone in empty space, disconnected from the footage. Confirmed
via the actual trip.log's captured command line that `--stitch
-resolution 1920x1080` (16:9 landscape) was combined with an
auto-picked `top_down` layout (a portrait-shaped front/rear stack) -
the exact mismatch behind problem three.

**1. Subtitles now confined to the camera footage, not the map panel.**
`stitch.py`'s subtitle burn-in used to apply to the *final* composed
frame (camera + map panel combined) - moved to apply to the camera
composite alone, right after any gsensor overlay/--stitch-resolution
padding and *before* the map panel is ever hstacked/vstacked
alongside it. Same scoping the gsensor overlay already had. This was
a deliberate earlier design decision (the old code had a comment
explicitly defending "the whole video, not one region") - reversed
with Christer's go-ahead after seeing it in practice.

**2. New `--stitch-map-size PERCENT` flag.** The map panel's free
dimension was always auto-sized from the trip's own real-world
geographic aspect ratio, clamped to 20-50% (30% for
`rearview_mirror`) of the camera composite's matching dimension - a
near-straight-line trip can land right at that 20% floor with no way
to ask for more. `_map_panel_dimensions()` gained a `size_fraction`
param that, when given, is used directly with no clamping (an
explicit request isn't second-guessed) - threaded through as
`map_size` (`stitch.py`'s `_stack()`/`stitch_cameras()`),
`stitch_map_size` (`trip_export.py`'s `export_trip()`), and
`--stitch-map-size` (`bv_export.py`, range-validated via
`_parse_map_size()`/`MIN_MAP_SIZE_PERCENT`-`MAX_MAP_SIZE_PERCENT`,
5-80, same pattern as `--stitch-gsensor-size`).

**3. Fixed gsensor overlay (and rearview_mirror inset) landing in
--stitch-resolution's padding instead of on real footage.** Root
cause: `_fit_and_pad()` letterboxes/pillarboxes the camera composite
to fit a mismatched `resolution` - but the gsensor overlay's own
sizing/position math (and the mirror inset's own `mirror_size`) used
to run against `comp_width`/`comp_height`, which *is* `resolution`
itself once padding is in play, not the real content size inside it.
A "top-right" position computed against the full padded box lands in
the black bars, nowhere near the actual footage. Fixed by introducing
`content_width`/`content_height` (the composite's real pre-pad pixel
size - probed from the decoded intermediates for hstack/vstack,
reused from the already-known native front dims for
`rearview_mirror`) and reordering `_stack()`'s filter graph: the
gsensor overlay (or mirror inset) is now composited *before* any
`--stitch-resolution` fit-and-pad runs, sized/positioned against
`content_width`/`content_height` - so ffmpeg's own overlay `main_w`/
`main_h` runtime variables, and this module's own Python-side pixel
math, always see the real content, never the padding. `comp_width`/
`comp_height` keep their old meaning (the padded/final camera-portion
size) for the map panel, which genuinely does need to match the
file's own eventual size since it sits alongside it, outside the pad.

Tested: `test_stitch` 87 passed (81 existing + 6 new: two
`_map_panel_dimensions()` `size_fraction` unit tests plus one
confirming it still requires GPS data, an end-to-end `map_size=`
stitch_cameras() test, a subtitle-confined-to-camera-region regression
test - map panel's own bottom strip stays bright while the camera
region's darkens, precise pixel math - and a gsensor-lands-on-real
-footage regression test reproducing the exact `--stitch-resolution`
+ `top_down` mismatch, confirming the overlay's red test-box lands
inside the real visible content bounds and *not* where the old,
buggy math would have placed it, deep in the pillarbox), `test_trip_
export` 49 passed (48 + 1 new: `stitch_map_size` forwarding),
`test_bv_export` 67 passed (64 + 3 new: `--stitch-map-size` parsing,
default-None, and out-of-range rejection), 0 failed. Also ran a real,
non-mocked end-to-end encode (320x180 front/rear, top_down,
`resolution=(960, 540)` - a real aspect mismatch, same shape as
Christer's report) outside the test suite and confirmed via direct
pixel inspection: the gsensor overlay's red test-box lands at
x∈[660,686], comfortably inside the real visible footage bounds
(x∈[240,720) for this resolution/content combination) - not in the
pillarbox to its right, where the pre-fix math would have placed it.

## gsensor.mp4: ring/crosshair lines were too thin to survive the real --stitch pipeline (done, this session)

After the anchor fix above, Christer looked at a real stitch.mp4 and
could see the gsensor gauge's dot but said "i am missing the rings
around" - the reference rings and crosshair that are supposed to
frame it (gsensor_render.py's own docstring: "a circular dial with a
dot ... and a short fading trail").

Root cause: `render_frame()` drew both the three reference rings and
the crosshair axes at `width=1` - a single pixel, on the source's own
480x480 canvas. That's fine viewed on its own, but gsensor.mp4 is
never watched on its own - by the time it reaches the screen it's
been through --stitch's own downscale to a fraction of the camera
composite's width (`gsensor_size`, default 15%) and a real H.264
encode, both of which blur/discard single-pixel detail. Confirmed
empirically outside the test suite: built a real gsensor.mp4 from
`render_frame()`, ran it through the actual `stitch_cameras()` overlay
(colorkey + scale, same as production) at a realistic overlay size,
and sampled the composited result - a 1px ring line survived at
essentially 0% of its own outline (2 stray pixels out of 9,216
samples, indistinguishable from encoder noise), while the much bolder
8px-radius dot came through fine. Re-ran the same test at ring widths
2/3/4: 2px alone jumped survival to ~3.1%, clearly visible.

Fix: new `RING_LINE_WIDTH = 2` constant in `gsensor_render.py`, used
by both the three ring `draw.ellipse()` calls and the two crosshair
`draw.line()` calls in `render_frame()` (previously hardcoded
`width=1` in all five places).

Tested: `test_gsensor_render` 11 passed (10 + 1 new -
`test_render_frame_ring_lines_are_at_least_two_pixels_thick`, which
walks outward along a ray from the gauge's center - offset 30 degrees
to avoid the crosshair itself - and confirms the outermost ring's own
run of consecutive non-background pixels is at least `RING_LINE_WIDTH`
thick, not just one), `test_gsensor_video` 16 passed (unaffected,
regression check only), 0 failed.

## map.mp4: fix the same O(fixes x frames) interpolation risk gsensor.mp4 had (done, this session)

Flagged as a latent risk when gsensor.mp4's identical bug was fixed
earlier this session ("worth revisiting if a similarly slow map.mp4
render is ever reported") - Christer's own trip.log showed map.mp4
taking ~2m54s for a 5,402-fix trip, close enough to the scale where
this starts to matter, so fixed proactively rather than waiting for
an actual hang report.

Root cause, identical to gsensor's: `render_map_video()`'s frame loop
called `interpolate_position()` once per frame, which does a full
linear rescan of every GPS fix on every call - O(fixes x frames),
quadratic in trip duration. GPS's own ~1Hz rate is slower than
g-sensor's ~10Hz, which is why this hadn't yet turned into an obvious
"hang" the way gsensor's did - but the shape of the bug is the same,
and a long enough/fix-dense-enough trip would eventually hit it too.

Fix: mirrors gsensor_video.py's own fix exactly. New
`_advance_fix_index()`/`_interpolate_position_from_index()` (forward
-only index advance + interpolation from an already-known bracketing
index, same two-function split) replace the per-frame
`interpolate_position()` call; a `position_index` cursor is carried
across the frame loop's iterations, kept deliberately separate from
the loop's pre-existing `fix_index` (which tracks a different thing -
how many fixes have been folded into `route_so_far`, not the
interpolation bracket). `interpolate_position()` itself is untouched
and still used by its own tests and any one-off lookups - just no
longer called by the hot per-frame loop, same "old function stays,
new one takes over the loop" pattern as gsensor's fix.

Tested: `test_map_video` 30 passed (24 existing + 6 new: exact
-timestamp/midpoint/clamp-before/clamp-after tests for the new indexed
functions, a monotonic-sweep test confirming they match
`interpolate_position()`'s own answer at every step across a full
sweep, and a performance regression test - a synthetic 4-hour trip at
a real ~1Hz GPS rate, 14,400 fixes x ~72,000 frames at map.mp4's
default 5fps - old path would be on the order of 3x10^8 inner-loop
iterations; new path finishes in well under 5 seconds), `test_trip_
export` 49 passed (unaffected, regression check only), 0 failed.

## bv-export: gsensor.mp4 render phase had no timing output (done, this session)

Christer: "i dont se output from bv-export about timing for gsensor
rendering". Root cause: concat/map/stitch phases in `export_trip()`
each wrap their work in `time.monotonic()` and print `"bv-export:
{phase} phase took {X}s"` to stderr under `--debug` - gsensor's render
block never got the same treatment when it was written, so a `--debug`
run gave no visibility into how long gsensor.mp4 took, and `trip.log`'s
own `TripLog.step()` already has an `elapsed_seconds` parameter for
exactly this (its docstring even names "map/stitch rendering" as the
motivating case) but nothing called it anywhere in `trip_export.py`.

Fix, in `trip_export.py`'s gsensor render block: added
`gsensor_start = time.monotonic()` (unconditional, like the
concatenation phase - trip.log should always get this, not just
`--debug` runs) right after the existing "starting gsensor.mp4 render"
log line; on success, `log.step("rendered gsensor.mp4", elapsed_
seconds=...)` now records the duration into trip.log; a `--debug`
-gated stderr print ("bv-export: gsensor phase took Xs") mirrors the
map/stitch pattern exactly, placed after the try/except so it prints
whether the render succeeded or was caught and turned into a warning.
`export_trip()`'s own docstring updated to list gsensor alongside
concat/map/stitch in the `--debug` phases it times. Scoped to gsensor
only, per Christer's specific report - map/stitch/concat's own
`log.step()` calls still don't pass `elapsed_seconds` (their `--debug`
stderr timing already existed and is unaffected).

Tested: `test_trip_export` 52 passed (49 existing + 3 new: a `--debug`
-gated stderr test asserting "bv-export: gsensor phase took" appears,
a silent-by-default test with `--debug` omitted, and a trip.log test
asserting the "rendered gsensor.mp4 (X.Xs)" line's elapsed-seconds
suffix), 0 failed.

## map.mp4: the real render bottleneck was re-drawing roads every frame, not interpolation (done, this session)

Christer, after the interpolation fix above: "map phase took 186.2s /
Still slow" - the O(fixes x frames) interpolation fix didn't meaningfully
help, so the interpolation theory was wrong and needed real profiling,
not another guess.

Profiled `render_map_video()` with cProfile against a synthetic trip at
Christer's real scale (5,402 fixes, 3,000 roads x 15 points, a 600
-frame slice): the dominant cost by far was `render_frame()` itself -
specifically `_project()`, called ~27 million times for that one
slice alone. Root cause: `render_map_video()`'s default (non-`--map-
zoom`) mode draws the exact same `bbox` and `roads` on every single
frame - the whole-trip overview never changes - but `render_frame()`
was re-projecting and re-drawing every one of those roads from scratch
on every frame regardless, paying (roads x frames) cost for an answer
that's identical every time. `--map-zoom` mode is unaffected - it
already gets a fresh bbox/road-set every frame (see task #51's
`index_roads()`/`roads_within_bbox()` filtering), so there's nothing
static there to cache.

Fix: new `render_base_map()` in `map_render.py` draws just the
background + road network once, returning a plain `Image`.
`render_frame()` gained an optional `base_image` parameter - when
given, it's copied (never mutated - a route/marker baked into frame 1
must not leak into frame 2) as the starting canvas instead of drawing
`roads` from scratch, and the old road-drawing loop is skipped
entirely. `render_map_video()` now calls `render_base_map()` once,
before the frame loop, only when not in `--map-zoom` mode, and passes
the same image object to every `render_frame()` call. Profiling the
same 600-frame slice after the fix: 36.0s -> 10.2s (~3.5x), with the
remaining cost now dominated by PNG encode/save (unavoidable per-frame
I/O) rather than wasted road math.

Tested: `test_map_render` 13 passed (10 existing + 3 new: render_
base_map() draws roads onto an otherwise-blank background,
render_frame(base_image=...) reuses it instead of the `roads` argument
passed alongside it, and confirms the base image itself is never
mutated), `test_map_video` 33 passed (30 existing + 3 new: render_
base_map() is called exactly once and the same object is handed to
every frame in static mode, it's never called at all in `--map-zoom`
mode, and an end-to-end 1,000-road/150-frame render finishes well
under a generous 15s bound), `test_trip_export` 52 passed and
`test_stitch` 87 passed (both regression-only, map panel path
unaffected), 0 failed.

## bv-export: --stitch-gsensor's reuse path gave no --debug output (done, this session)

Christer: "gsensor file doesn't give any output when the video already
exist". Root cause: when `--stitch-gsensor` finds an existing
gsensor.mp4 already sitting in the destination folder (render_gsensor
=False this run), trip_export.py's reuse branch only calls
`log.step("using existing gsensor.mp4 for stitch overlay")` - a
trip.log line, with no matching `--debug` stderr print. Every other
phase (concat/map/gsensor render/stitch) prints something to stderr
under `--debug`; this one path was silent, which read as "did this
get skipped by mistake" rather than "this was intentionally reused".

Fix: one `if debug: print(...)` added to the reuse branch -
"bv-export: gsensor.mp4 already exists - reusing for stitch overlay
(render skipped)" - stderr only, trip.log's own line is unchanged.

Tested: `test_trip_export` 54 passed (52 existing + 2 new: a `--debug`
test asserting the reuse message appears, and a silent-by-default test
with `--debug` omitted), 0 failed.

## --stitch: the --stitch-map panel's own render time was invisible (done, this session)

Christer, after confirming the panel is always rendered fresh (never
reused from an existing map.mp4 - see the design decision two entries
up): "but it doesnt report time for the map video build". True - the
panel render (`_render_map_panel()`, inside `_stack()` in stitch.py)
had no timing of its own; its cost was entirely folded into the
overall "stitch phase took Xs" line from trip_export.py, with no way
to tell how much of that was the panel specifically versus the camera
decode/encode work.

Fix: `_stack()`'s map-panel block now times the `_render_map_panel()`
call with `time.monotonic()` and prints "stitch: map panel render took
Xs" to stderr under `--debug` - same message style and stderr-only
convention as stitch.py's existing `_report_decode_timing()` breadcrumb
for NVDEC/CPU decode. Silent by default, same as everything else under
`--debug`.

Tested: `test_stitch` 89 passed (87 existing + 2 new: a `--debug` test
asserting "stitch: map panel render took" appears, a silent-by-default
test), 0 failed.

## --stitch: --stitch-scale/--stitch-max-width/--stitch-max-height, a padding-free way to shrink stitch.mp4 (done, this session)

Christer: a native `--stitch` with neither `--stitch-resolution` nor
`--stitch-bitrate` given came out 3.5GB at 5422x4320, 20 minutes to
render - wanted a way to reduce the resolution "without specifying a
--stitch-resolution that might add extra black spaces in the video".
Real concern: `--stitch-resolution`'s exact-WxH fit-and-pad
letterboxes/pillarboxes whenever the requested resolution doesn't
happen to match the natural composite's own aspect ratio (the exact
bug fixed for the default layout earlier this session).

Asked Christer to confirm the shape of the fix (AskUserQuestion):
a direct percentage scale, a max-dimension cap, or both. Answer: build
all three - `--stitch-scale PERCENT`, `--stitch-max-width PIXELS`, and
`--stitch-max-height PIXELS`.

**Design.** All three are always-proportional - they scale the whole
final frame (camera composite plus any `--stitch-map` panel) down by
a uniform factor, computed *after* everything else in `_stack()`'s
filter graph is assembled (camera stack, any `--stitch-resolution`
fit-and-pad, gsensor overlay, map panel), so the aspect ratio is
preserved exactly and no black bars are ever introduced - a
fundamentally different mechanism from `--stitch-resolution`'s
exact-WxH padding, not a wrapper around it. A single final
`scale=-2:H` ffmpeg filter is enough regardless of which bound ends up
tightest, since ffmpeg auto-derives the other, unspecified dimension
to the nearest even number while preserving whatever aspect ratio the
input already has.

`--stitch-scale` (1-100, validated at the CLI layer - downscale only,
matching "reduce resolution", not left open for upscaling) is a direct
percentage of the natural size. `--stitch-max-width`/`--stitch-max
-height` (positive pixel counts) instead cap one or both dimensions,
scaling down - never up - just enough to fit. All three combine
freely as independent upper bounds on one `output_scale_factor`
(whichever produces the smallest result wins, floored at 1.0 so
nothing ever upscales) - deliberately no mutual-exclusion validation
between them, since "tightest cap wins" needs none.

**Where `final_width`/`final_height` come from.** `_stack()` already
computed the camera composite's own real pixel size (`content_width`/
`content_height`) for the gsensor-overlay/mirror-inset/map-panel
anchoring fix earlier this session, but only when one of those
features was actually in play - conditional, not unconditional, on
purpose (see the next paragraph). Widened that same condition to also
cover scale/max_width/max_height, and added `final_width`/
`final_height`, seeded from the camera portion's own size and grown by
the map panel's own dimensions if one gets added alongside it -
tracking the *whole* final frame, not just the camera portion, so
`--stitch-scale` genuinely shrinks the panel too, confirmed by a test
comparing scaled camera-only vs. scaled-with-panel output widths.

**A real regression caught by the test suite, not just the manual
verification script.** First pass made the `content_width`/
`comp_width` computation fully unconditional (simpler code, and
`final_width`/`final_height` need it regardless of whether gsensor/
mirror/map are used) - immediately broke 7 `test_stack_*` tests with
`ffprobe failed for front.mp4: moov atom not found`. Root cause: those
tests mock `encode_with_nvenc_fallback` to write empty (0-byte)
intermediate files and never needed a real `_video_dimensions()` probe
on them before - the exact same failure mode already documented in
this file's `--stitch-map` entry from an earlier session ("a real bug
caught by actually running the test suite"), reintroduced by this
change and caught again the same way. Fixed by keeping the computation
conditional (now on scale/max_width/max_height too, not just gsensor/
mirror/map), not making it unconditional - two cheap ffprobe calls
skipped is still better than none when nothing downstream needs them.

**Manual verification before writing formal tests** (real ffmpeg, a
throwaway script, not the test suite): confirmed `scale=50` on a
640x240 natural composite produces exactly 320x120 (aspect ratio
preserved), `max_width=400` produces 400x150, `max_width=1000`
(above natural) is a true no-op (640x240 unchanged), and
`scale=90,max_width=200` correctly lets the tighter `max_width` win
(202x76, not ~576x216) - before committing to the design.

Tested: `test_stitch` 96 passed (89 existing + 7 new: scale halves
both dimensions preserving aspect ratio, scale=100 is a no-op,
max_width/max_height each cap without upscaling, a cap above the
natural size is a no-op, scale+max_width combine with the tighter
cap winning, and the panel is included in what gets scaled).
`test_bv_export` 71 passed (67 existing + 4 new: `--stitch-scale`/
`--stitch-max-width`/`--stitch-max-height` parse and forward
correctly, default to None, an out-of-range scale and a zero
max-width are both rejected). `test_trip_export` 55 passed (54
existing + 1 new: all three forwarded to `stitch_cameras()` as
`scale`/`max_width`/`max_height`). 0 failed across all three.

## --stitch-scale was only shrinking the final file, not speeding up the render (done, this session)

Christer's report after trying task #82's new flags: "is --stitch-scale
applied in the end, because rear, front, panel and stitch are still slow
even with --stitch-scale 10". He was right. The task #82 design applied
`scale`/`max_width`/`max_height` as a single trailing `scale=` filter on
the already-fully-composited final frame. That produces a genuinely
smaller output file with the correct aspect ratio, which is what task #82
promised, but every expensive step upstream of that final filter -
decode-time scaling of the front/rear sources, the intermediate
front/rear re-encode, and the map panel's own PIL render size - still ran
at full native resolution regardless of `--stitch-scale`. Shrinking a
5422x4320 render to 10% only shrank the file at the very last moment; it
did nothing for render time, matching exactly what Christer observed.

Fix: `_stack()` now computes an `effective_resolution` up front, before
any decode happens, whenever `resolution` wasn't given explicitly but
`scale`/`max_width`/`max_height` were (and the layout isn't
`rearview_mirror`, which never gets decode-time scaling regardless -
matching `--stitch-resolution`'s own pre-existing limitation there).  It
probes the front/rear *source* files (not the eventual composite) via the
existing `_video_dimensions()` helper to get their natural pre-decode
size, works out what the natural composited width/height would be for the
chosen layout (`hstack` or `vstack`), then applies the same
tightest-cap-wins logic task #82 already used (`min` across scale as a
fraction, `max_width / natural_width`, `max_height / natural_height`) to
derive a target resolution. That derived `effective_resolution` is then
fed through the *exact same* code paths `--stitch-resolution` already
uses: `_ideal_shared_dimension()` for the decode-time `scale=` filters,
`comp_width`/`comp_height` for the map panel's render size, and
`_fit_and_pad()` for the final combine. Nothing new was built for the
actual speedup - the redesign's job was just getting `scale`/
`max_width`/`max_height` to produce a real target resolution early enough
to reuse `--stitch-resolution`'s already-fast path, instead of bolting a
filter on at the very end.

A `pre_decode_scale_applied` flag tracks whether `effective_resolution`
ended up different from the literal `resolution` argument (i.e. was
auto-derived from scale/max_width/max_height rather than given directly).
This guards the old trailing-scale block from double-applying: it still
runs, unchanged, for `rearview_mirror` layouts and for the case where an
explicit `--stitch-resolution` was combined with `--stitch-scale` (a
deliberate "shrink further on top of an explicit resolution" combo that
predates this task), but is skipped whenever the pre-decode path already
produced the target size.

One accepted imprecision, documented in the code rather than solved: when
a map panel is combined with an auto-derived `effective_resolution`, the
target is computed from the camera composite alone - the panel's own
width/height isn't known yet at that point, since deriving it would need
a full pre-decode size estimate for the panel too. So the final frame
(camera + panel) can end up somewhat larger than `max_width`/
`max_height` by roughly the panel's own share. This mirrors an existing,
already-documented simplification in `_map_panel_dimensions()` (sizing
off the composite alone, not composite+panel), so it's consistent with
how the codebase already treats this class of problem rather than a new
compromise invented for this task.

What actually got faster: decode-time `scale=` filters on the front/rear
sources now target the real (small) `effective_resolution` instead of
native size, so ffmpeg decodes/scales less data per frame; the
intermediate front/rear re-encode writes smaller files; and
`_render_map_panel()` is called with the smaller `comp_width`/
`comp_height`, so PIL renders fewer pixels per frame. What did NOT get
faster, by design: raw source decode itself remains proportional to the
source video's length regardless of target size, per the pre-existing,
unchanged docstring note in `stitch.py` - `--stitch-scale` was never
going to make that part faster, only the steps whose cost scales with
output resolution.

Tested: `test_stitch` 98 passed (96 existing from task #82 + 2 new: one
confirms the decode-time `scale=` filter no longer contains the native
"2160" dimension when `--stitch-scale 10` is given on 3840x2160 sources,
i.e. that decode itself is now targeting the small size instead of just
the final combine step; the other confirms `_render_map_panel()` is
called with a smaller width/height when `--stitch-scale 25` is given vs.
no scale at all). `test_trip_export` 55 passed and `test_bv_export` 71
passed, both unchanged from task #82 since this was purely an internal
`stitch.py` redesign - no CLI or `export_trip()` signature changes were
needed. 0 failed across all three, 224 tests total.

## rearview_mirror was left out of the --stitch-scale decode speedup, plus a request for rounded corners and mirror zoom (this session)

Christer, right after confirming task #83's fix: "Same time for
rendering / i would like that the mirror have round edges and a zoom in
percent". Two things in one message - the second sentence gave away the
first: he's exporting with `--stitch-layout rearview_mirror`, and task
#83's `effective_resolution` fix explicitly excluded `is_mirror` from
decode-time scaling (documented at the time as "front is never
decode-time-scaled for that layout, resolution or not"). So his mirror
exports genuinely didn't get faster - not a regression, but a real gap
task #83 left open by design, now closed.

Fix (task #84): `effective_resolution`'s own natural-size computation
now branches on `is_mirror` - front IS the whole composite for
rearview_mirror (rear never contributes to the frame's own dimensions,
it's just a small inset overlaid on top), so the "natural size" is
simply front's own native size, no front+rear summing needed the way
hstack/vstack requires. When `scale`/`max_width`/`max_height` (and not
an explicit `--stitch-resolution`, same carve-out as before) derive a
smaller `effective_resolution`, front's own decode now targets that
size directly via `scale={width}:{height}` - exact, not `-2`-derived,
since the target was already computed preserving front's own aspect
ratio.

Separately, and unconditionally (not gated behind scale/max_width/
max_height at all): the rear inset is now always decoded pre-scaled to
`mirror_size` percent of front's own width and pre-flipped, instead of
decoding at full native resolution only to be shrunk down in the final
combine pass. This was real waste even in the plain default case with
no shrink flags at all - Christer's "rear ... still slow" was pointing
partly at this. The final combine's own `is_mirror` filter_complex
clause is now just a plain overlay of `[0:v][1:v]` - no more
`scale=...,hflip` step there, since input 1 (rear_decoded) already
arrives in exactly the shape needed.

Two new tests: `test_stitch_cameras_rearview_mirror_scale_shrinks_front_decode_time_scaling`
confirms front's decode-time scale filter no longer contains the native
"2160" dimension when `--stitch-scale 10` is given on 3840x2160 sources
under `rearview_mirror`, mirroring the equivalent hstack/vstack test
from task #83. `test_stitch_cameras_rearview_mirror_rear_is_always_decoded_pre_scaled`
confirms rear's own decode-time filter is exactly `scale=160:-2,hflip`
(25% of a 640-wide front, mirror_size's own default) even with no scale/
resolution flags given at all. All 5 existing rearview_mirror pixel
-level tests (flip direction, inset placement, configurable mirror_size,
resolution scaling, map-panel-capped-at-30%) still pass unchanged,
confirming the decode-time refactor didn't shift any visible output.

Also requested this same message, tracked as their own follow-up tasks
rather than folded into this fix: `--stitch-mirror-radius` (rounded
corners on the mirror inset, as a percent of the inset's own size) and
`--stitch-mirror-zoom` (a center-crop zoom on the mirror inset before
it's scaled in, as a percent - 0 meaning today's full rear frame,
higher cropping in tighter). Design confirmed with Christer via
AskUserQuestion (percent-of-inset-size for the radius, "0=full frame,
higher=tighter crop" for the zoom - both his own recommended options).

Tested: `test_stitch` 100 passed (98 existing from task #83 + 2 new).
`test_trip_export` 55 passed and `test_bv_export` 71 passed, both
unchanged - no CLI or `export_trip()` signature changes, purely an
internal `stitch.py` decode-path refactor. 0 failed across all three,
226 tests total.

## --stitch-mirror-radius: rounded corners on the mirror inset (this session)

First of the two follow-up requests from task #84's own message
("i would like that the mirror have round edges"). Design confirmed via
AskUserQuestion (percent of the inset's own size, Christer's own
recommended option) before writing any code, per the working agreement.

`mirror_radius` (0-100, default 0 - see MIN_/MAX_/DEFAULT_MIRROR_RADIUS_PERCENT)
rounds the rear inset's four corners to a radius of `mirror_radius`
percent of the inset's own min(width, height)/2. 0 leaves them square -
the layout's original, unchanged look; 100 rounds each corner all the
way to a quarter-circle of that radius, producing a "stadium"/pill shape
for a non-square inset or a full circle for a square one.

Can't be baked into the rear inset's own decode-time intermediate the
way task #84's scale+hflip optimization was - an H.264 intermediate has
no alpha channel to carry a transparency mask through. So this happens
in the final combine's own filter_complex instead, right before the
`[0:v][1:v]overlay=...` clause: `[1:v]format=rgba,geq=r='r(X,Y)':
g='g(X,Y)':b='b(X,Y)':a='<rounded-corner expression>'[mirror_rounded]`,
then the overlay reads from `[mirror_rounded]` instead of `[1:v]`
directly. Only added when `mirror_radius > 0` - a plain `[0:v][1:v]`
overlay otherwise, matching task #84's default-case behavior exactly
(no perf cost for anyone not using this).

The alpha expression itself (`_mirror_radius_alpha_expr()`) is a classic
four-corner rounded-rectangle mask: for each of the four `radius`x
`radius` corner squares, a pixel is transparent only if it's also
farther than `radius` from that corner's own rounding-circle center;
everything else stays opaque. Distances compared as squared values
(`pow(...,2)`) rather than via `hypot`/`sqrt`, both to avoid a square
root per pixel and because `hypot` isn't universally available across
ffmpeg builds' eval parsers. Wrapped in single quotes when embedded in
the geq option value - same escaping idiom `_subtitles_filter()`'s own
`force_style='...'` already uses, protecting the expression's internal
commas from ffmpeg's top-level filter-chain comma splitting without
needing to backslash-escape each one.

Wired through the same three layers every --stitch flag goes through:
`_stack()`/`stitch_cameras()` (stitch.py), `export_trip()`
(trip_export.py, `stitch_mirror_radius` param), and `--stitch-mirror
-radius PERCENT` (bv_export.py, `_parse_mirror_radius()` validator,
range-checked at the CLI layer same as every other percent flag here).

Two new pixel-level tests in test_stitch.py, both against a 320x320
solid-red rear scaled to a 256x256 square inset (mirror_size=40% of a
640-wide front) so the corner math is easy to reason about by hand:
`test_stitch_cameras_rearview_mirror_radius_zero_leaves_square_corners`
confirms a pixel right at the inset's corner is still solid red with
the unchanged default. `test_stitch_cameras_rearview_mirror_radius_rounds_the_corners`
confirms that same corner pixel is now transparent (front's blue shows
through) at `mirror_radius=100`, while the inset's own center pixel
stays solid red - the rounding only carves away the four corners, not
the whole shape.

Tested: `test_stitch` 102 passed (100 existing from task #84 + 2 new).
`test_trip_export` 56 passed (55 existing + 1 new: `mirror_radius` is
forwarded to `stitch_cameras()`). `test_bv_export` 74 passed (71
existing + 3 new: default/explicit-value/out-of-range-rejection for
`--stitch-mirror-radius`, mirroring `--stitch-mirror-size`'s own three).
0 failed across all three, 232 tests total.

## --stitch-mirror-zoom: center-crop zoom on the mirror inset (this session)

Second of the two follow-up requests from task #84's own message
("a zoom in percent"). Design confirmed via the same AskUserQuestion
call as `--stitch-mirror-radius` (0=full rear frame, higher=tighter
crop, Christer's own recommended reading).

`mirror_zoom` (0-95, default 0 - see MIN_/MAX_/DEFAULT_MIRROR_ZOOM_PERCENT)
crops the rear source toward its own center, by that percent of each
edge, before it's scaled into the inset - 0 (the default) shows the
whole rear frame unchanged; higher values show progressively less of
it. Capped below 100 (95) since cropping the entire frame away is
degenerate, not a meaningful "maximum zoom."

Unlike `mirror_radius`, this is a plain crop with no alpha-channel
concerns, so it's baked straight into rear's own decode-time
intermediate from task #84 - prepended to the scale+hflip filter
already built there: `crop=w=iw*{keep_fraction}:h=ih*{keep_fraction},
scale={mirror_width}:-2,hflip`, where `keep_fraction = 1 -
mirror_zoom/100`. A uniform fraction off both `iw`/`ih` preserves
rear's own aspect ratio exactly, so it doesn't interact with
`mirror_width`'s own `-2` auto-height sizing - no reordering or
recomputation needed elsewhere. A no-op (no crop clause at all, not
even a redundant `crop=w=iw:h=ih`) when `mirror_zoom` is 0, the
unchanged default.

Wired through the same three layers as `mirror_radius`: `_stack()`/
`stitch_cameras()` (stitch.py), `export_trip()` (trip_export.py,
`stitch_mirror_zoom` param), and `--stitch-mirror-zoom PERCENT`
(bv_export.py, `_parse_mirror_zoom()` validator).

Two new pixel-level tests in test_stitch.py, using a new
`_make_rear_zoom_probe()` helper (a 320x320 yellow frame with a solid
blue center, 20px border) so cropping is easy to verify by sampling a
pixel right at the inset's own edge: `test_stitch_cameras_rearview_mirror_zoom_zero_is_a_no_op`
confirms that edge pixel is still the source's yellow border at
`mirror_zoom=0`. `test_stitch_cameras_rearview_mirror_zoom_crops_toward_the_center`
confirms the same edge pixel becomes solid blue at `mirror_zoom=30`
(keep_fraction 0.7 removes 48px from each side of a 320px source,
comfortably past the 20px yellow border, so none of it survives into
the cropped/scaled inset).

Tested: `test_stitch` 104 passed (102 existing from the radius task +
2 new). `test_trip_export` 57 passed (56 existing + 1 new:
`mirror_zoom` is forwarded to `stitch_cameras()`). `test_bv_export` 77
passed (74 existing + 3 new: default/explicit-value/out-of-range
-rejection for `--stitch-mirror-zoom`). 0 failed across all three, 238
tests total.

## --stitch-mirror-icon: composite a real mirror photo as the inset frame (this session)

Christer pasted a photo of his own physical rearview mirror and asked
"Can you use that image as a realistic mirror" - after confirming via
AskUserQuestion that he wanted the photo genuinely composited (not
just used as loose visual inspiration) and getting the actual file
(`mirror.avif`, converted to PNG via ImageMagick since the sandbox's
ffmpeg 4.4.2 build can't decode AVIF), built `--stitch-mirror-icon`: a
path to a plain product-style photo of a real mirror, composited so
the rear camera's footage reads as playing inside that photo's own
glass, with the photo's own frame/mount drawn on top - a replacement
for the plain procedural rounded-rectangle inset, not an addition to
it.

Segmentation (new `blackvue/export/mirror_icon.py`, `load_mirror_frame()`):
a plain-photo-on-light-background source (Christer's own reference
image) splits cleanly into three regions with nothing but a luminance
threshold and a flood fill - no ML, no user-drawn mask. Any pixel
below `_DARK_LUMINANCE_THRESHOLD` (120, picked from the reference
photo's own sharply bimodal histogram) is "dark" - the frame/bezel
/mount. A BFS flood fill from every border pixel, through light
territory only, marks every light pixel reachable from the image's
own edge as "background." Whatever light territory the flood fill
never reaches - fully enclosed by the dark frame - is "glass." A real
photo's frame can enclose small light spots that aren't the actual
glass (a reflective logo on Christer's own mount's label segmented
this way too, confirmed visually during this feature's own mockup
phase) - `_largest_connected_component()` keeps only the biggest
enclosed blob, discarding the rest as noise. The result is cropped to
its own content bounding box (dark ∪ glass, discarding the source
photo's white margin) and returned as a `MirrorFrame`: an RGBA
`frame_overlay` (opaque original color where dark, transparent
elsewhere), an `L`-mode `glass_mask` (white where glass, black
elsewhere), and the glass region's own `glass_bbox` within that shared
canvas.

Compositing (stitch.py, `_stack()`'s `is_mirror` branch): rear footage
has to land inside the glass's own non-rectangular silhouette, not
just its bounding rectangle, and an H.264 decode-time intermediate
can't carry an alpha channel - so this reuses `mirror_radius`'s own
established rule (task #85) of keeping every alpha-dependent step
(`format=rgba`, `pad` with a transparent color, `alphamerge`) in the
FINAL combine's filter_complex only. New `_mirror_icon_layout()` scales
the icon's own native size up to `mirror_size`'s target content width
(same percent-of-front-width sizing every mirror inset uses) and maps
the glass bbox into that scaled canvas. New `_cover_crop_filter()`
center-crops the rear source to the glass bbox's own aspect ratio
before an exact `scale=` - fitting without distortion, the same
philosophy `_fit_and_pad()`/`--stitch-resolution` already follow -
expressed as ffmpeg runtime `iw`/`ih` fractions rather than Python
-computed literal pixels, so it composes safely after `mirror_zoom`'s
own upstream crop (still respected here - `mirror_zoom` zooms the rear
source same as always, `_cover_crop_filter()` just handles the
leftover aspect mismatch). The final combine: scale+crop rear to the
glass bbox's exact size, `pad` it out to the full icon canvas at the
bbox's own offset (transparent padding), `alphamerge` against the
glass mask to clip away everything outside the true silhouette, then
overlay the icon's own `frame_overlay` on top so the frame/mount reads
as physically in front of the footage. `mirror_radius` is a no-op once
`mirror_icon` is given (the photo's own shape replaces the rounded
-rectangle math entirely) - documented in `stitch_cameras()`'s own
docstring and confirmed by a dedicated test.

The mount needing to look physically attached to the top of the frame
(not floating) took an extra offset beyond the plain content-bbox
crop: a new `_MIRROR_ICON_TOP_NUDGE_FRACTION = 15/358` constant, empirically
derived from the approved mockup (a small negative y-offset,
proportional to the inset's own height, compensating for the mount's
rounded dome shape leaving a thin curved sliver visible even at a
literal y=0 placement). Before landing on the real pipeline, iterated
on this and several other visual decisions (50% wider without
changing height, moved up "a couple more pixels," "20 percent bigger"
overall) across 7 rounds against a disposable PIL-based mockup - cheap
to reshoot per round, versus the much more expensive real ffmpeg
pipeline - only building the real thing once Christer said "perfect."

Wired through the same three layers as every other `--stitch-mirror-*`
flag: `_stack()`/`stitch_cameras()` (stitch.py, `mirror_icon: Path |
None = None`), `export_trip()` (trip_export.py, `stitch_mirror_icon`
param, forwarded as-is - already caught inside stitch.py's own
`is_mirror` branch so no separate try/except needed at this layer),
and `--stitch-mirror-icon PATH` (bv_export.py - a plain string/Path
arg with no custom validator, following `--map-icon`'s own established
pattern: `metavar="PATH"`, default `None`, converted to `Path` inside
`bv_export()`'s own body). A bad or missing path degrades to a
`warnings` entry ("stitch mirror icon: ...") and falls back to the
plain procedural inset rather than failing the export - the same
"don't fail the whole export over an optional cosmetic input"
convention `--map-icon` already follows, though notably gentler than
`--map-icon`'s own failure mode (a bad map icon fails the whole map;
a bad mirror icon just loses the fancy frame, stitch.mp4 still comes
out complete).

New `tests/blackvue/export/test_mirror_icon.py` (7 tests) covers
`load_mirror_frame()`/`_largest_connected_component()` directly against
small synthetic PIL-drawn images (a main frame+glass block plus a
disconnected stray "logo" block, mirroring the real photo's own logo
-artifact issue) - content-bbox cropping, only-the-largest-component
survives, frame pixels painted opaque, glass pixels marked in the
mask, a missing file raises `MediaToolError`, an all-light image with
no enclosed glass raises `MediaToolError` too. Four new pixel-level
tests in test_stitch.py, against a simple synthetic square icon (solid
black 40x40 with a white 20x20 hole cut dead center, so the on-screen
math is reproducible by hand and was cross-checked against an actual
rendered frame before being written into the assertions):
`test_stitch_cameras_rearview_mirror_icon_composites_rear_into_the_glass`
confirms front shows outside the inset, rear shows inside the glass,
and the icon's own frame paints opaque black over both;
`test_stitch_cameras_rearview_mirror_icon_falls_back_with_a_warning_on_a_bad_path`
confirms a missing icon file produces exactly one warning and still
renders the plain procedural inset; `test_stitch_cameras_rearview_mirror_icon_ignores_mirror_radius`
confirms the icon's own frame corner stays opaque (not rounded away)
even with `mirror_radius=100`; `test_stitch_cameras_rearview_mirror_icon_still_respects_mirror_zoom`
confirms a bordered rear probe's edge color still changes between
`mirror_zoom=0` and `mirror_zoom=30`, same as the plain-inset zoom
tests from task #86. Also manually verified the full real pipeline
end-to-end with synthetic 1920x1080/1280x720 test-pattern front/rear
videos and the actual reference mirror photo (not just the synthetic
test assets) - confirmed by direct pixel inspection that the mount
sits flush to the top edge, the frame/mount render opaque, and the
rear test pattern (including its own checkerboard corner marker) is
visible clipped to the glass's real oval silhouette rather than a
plain rectangle.

Tested: `test_mirror_icon` 7 passed (new file). `test_stitch` 112
passed (108 existing from the zoom task + 4 new). `test_trip_export`
59 passed (57 existing + 2 new: `mirror_icon` is forwarded to
`stitch_cameras()`, and a bad path warns without failing the whole
export). `test_bv_export` 81 passed (77 existing + 4 new:
default/explicit-value for `--stitch-mirror-icon`, produces-a-video,
and warns-without-failing-on-a-bad-path). 0 failed across all four
modules, 259 tests total.

## --stitch-mirror-icon crashed instead of warning on an all-dark source image (this session)

Christer ran the real feature against his own trip footage and hit an
unhandled `ValueError: min() iterable argument is empty` crashing the
whole export, from `load_mirror_frame()`'s own `glass_bbox = (min
(glass_xs), ...)` line. Root cause: he'd pointed `--stitch-mirror-icon`
at `mirror_frame_overlay_final.png` - a debug preview I'd shared of
`load_mirror_frame()`'s own *output* (an RGBA image, opaque frame,
fully transparent "glass" region) - not the original source photo.
`Image.open(...).convert("RGB")` strips alpha and keeps the underlying
RGB, which for a region built via `Image.new("RGBA", ..., (0, 0, 0,
0))` is solid black - so the reloaded image read as entirely "dark,"
with plenty of content (the earlier `not content_xs` guard didn't
catch it, since dark pixels count as content) but zero glass pixels.

That's a real gap regardless of how the bad input arose: `glass_xs`/
`glass_ys` could end up empty while `content_xs` wasn't, and nothing
guarded against it before the `min()`/`max()` calls. Added a second,
more specific check right before `glass_bbox` is built - `if not
glass_xs: raise MediaToolError(...)` - with a message that also
explicitly calls out the "did you point this at one of the tool's own
outputs instead of the source photo" mistake, since that's the exact
shape of error a user is likely to hit. This raises the same
`MediaToolError` stitch.py's own `is_mirror` branch already catches
and downgrades to a `warnings` entry - so this class of bad input now
degrades gracefully (falls back to the plain procedural inset) instead
of crashing the whole export, matching every other bad-`--stitch
-mirror-icon` case.

New `test_load_mirror_frame_raises_a_clear_error_when_the_image_is_entirely_dark`
in test_mirror_icon.py reproduces this exact scenario (a plain solid
-black source image - content, no glass) and confirms `MediaToolError`
is raised with the expected message instead of an unguarded `ValueError`
escaping. Manually reproduced the original crash against the actual
file Christer used (`mirror_frame_overlay_final.png`, loaded via a
real `Path` the way `bv_export.py` always calls it) and confirmed the
fix now raises the intended `MediaToolError` there too.

Tested: `test_mirror_icon` 8 passed (7 existing + 1 new). `test_stitch`
112 passed (only the 4 mirror_icon-specific tests re-run directly,
since this fix only touches mirror_icon.py's own error path).
`test_trip_export` 59 passed (full module, no regressions). 0 failed.
