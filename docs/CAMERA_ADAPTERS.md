# Camera adapters

**Status: design + schema only (2026-08-16).** Nothing described here is
wired into the running pipeline yet. `CameraConfig` has no `adapter`
field, there's no adapter registry, and no `BlackVueAdapter`/`FolderAdapter`
class exists. What *does* exist as of this document: a JSON Schema for an
adapter manifest, two real example manifests (BlackVue and a plain folder
of videos), and a small loader that validates both. See "Not done in this
pass" below for the explicit boundary, and "Suggested next steps" for what
implementing this for real would look like.

## Why

beyond-video is, honestly, a small-market tool - one person's BlackVue
dashcam, built out one `WORKING_CONTEXT.md` entry at a time into something
considerably more capable than that scope suggests. Christer's framing for
this pass: there's good, reusable stuff buried in here (trip detection,
transcription/translation, scene description, map/g-sensor rendering, the
whole `bv-web` browsing UI), and packaging it behind a pluggable "camera
adapter" - with a real, useful "plain folder of ordinary videos" adapter as
the second concrete example, not just BlackVue - both makes the reusable
parts actually reusable and gives the project a plausible path to support
other cameras later.

This isn't the first attempt: `docs/ARCHITECTURE.md` already notes that
day one of the project sketched an aspirational "Fleet -> Vehicle ->
Connection Manager -> Adapter -> Camera -> Jobs -> Storage" framework that
was never built, leaving behind an empty `src/blackvue/adapters/` package
that nothing references. That design was written before there was a real
second use case to design against. This pass is deliberately the opposite
approach: catalog what the *existing, working* BlackVue-only code actually
assumes, then design the adapter boundary from those real findings plus a
real second adapter (the plain folder one), instead of guessing up front.

## The core idea: exactly one active adapter at a time

Christer's framing, and the one this design follows: **a camera config
picks one adapter; the pipeline runs against that one adapter's manifest
and code hooks. Adapters are not all loaded/run simultaneously.** This
matches a plugin model more than a multi-tenant one - closer to how a
single VS Code workspace has one active set of extensions per project than
to a framework that fans a request out across every registered backend.

Concretely (future work, not built yet): `CameraConfig` gains an
`adapter: str` field (default `"blackvue"` so every existing config keeps
working unmodified), and a small registry maps that id to
`(manifest.json, an adapter class)`. Whatever command runs (`bv-ls`,
`bv-export`, the `bv-web` archive browser, ...) loads the one adapter named
by the camera config it's pointed at and uses only that adapter's
manifest/capabilities for the duration of the run.

## What's declarative (JSON) vs. what needs real code

Christer's guess was "probably a JSON file," and a lot of it genuinely is
- but not all of it, and being upfront about the split was part of the
ask ("I know that many of our special tricks won't work, then that will be
noted"). The investigation (full findings below) turned up a clean line:

| Concept | JSON-expressible? | Why |
|---|---|---|
| Filename regex + timestamp format | Yes | Just a pattern + a `strptime` format string |
| Recording-kind vocabulary (Normal/Event/Manual/Parking/...) | Yes | A small ordered table of letter -> label -> flags |
| Camera-direction vocabulary (Front/Rear/Interior/...) | Yes | Same shape as kind vocabulary |
| Filename-suffix -> Asset mapping | Yes | An ordered list (order matters on suffix overlap, e.g. `.diarized.transcript.txt` vs `.transcript.txt`) |
| Archive layout (flat vs. recursive) | Yes | One flag picks a *shared, generic* scanner - not adapter-specific code |
| Capability flags (has GPS? has live view? ...) | Yes | Booleans (or `"generated"` for "synthesized, not native") callers check before offering a feature |
| `.gps` NMEA-in-brackets parsing | **No** | Reverse-engineered text format, needs a real parser |
| `.3gf` raw accelerometer parsing | **No** | Proprietary fixed-width binary struct |
| Camera network protocol (CGI endpoints, never-closing multipart streams) | **No** | Real HTTP client code, BlackVue-firmware-specific |
| `config.ini` `RecordTime` schema | **No** | Real (small) INI-parsing code |
| Recursive-folder timestamp resolution (ffprobe/mtime fallback) | **No** | Needs to actually run `ffprobe`/`stat` |

So the design is a **manifest + code-hooks hybrid**: a JSON file carries
everything genuinely declarative (vocab tables, suffix mapping, capability
flags, the regex), and a short list of named "code hooks" in the manifest
(`code_hooks_required`) documents which real methods an adapter class has
to implement because JSON fundamentally can't express them. This is the
same shape a lot of plugin systems end up at (a manifest for identity/
capabilities, real code for the parts that need it) - not a novel idea,
just the right one here.

## The manifest schema

`src/blackvue/adapters/manifest.schema.json` - a JSON Schema (2020-12)
formally defining the shape below. `src/blackvue/adapters/manifest.py`
loads and structurally validates a `manifest.json` against the same rules
by hand (no new dependency added just for this - see that module's
docstring for why `jsonschema` wasn't pulled in).

Key fields:

- `adapter_id` / `display_name` / `description` - identity.
- `source.kind` (`network_camera` | `local_folder`) and
  `source.requires_network` - whether this adapter talks to a device at
  all; drives whether `bv-download`/`bv-config`'s endpoint setup/`bv-gps`/
  `bv-live` even apply.
- `archive_layout` (`flat` | `recursive`) - picks the on-disk scanning
  strategy. This is the single biggest structural blocker the
  investigation found for a folder-of-videos adapter: `ArchiveReader.read()`
  (`src/blackvue/archive/archive_reader.py:47-81`) does a non-recursive
  `os.scandir()` and nothing else in the archive layer walks subfolders.
  Making this a manifest flag (rather than hardcoding `scandir`) is enough
  for a shared scanner to handle both cases - it doesn't need to become
  per-adapter code.
- `video_extensions` - which file extensions count as video.
- `filename_pattern` (regex + `strptime` format, or `null`) and
  `timestamp_source` (ordered fallback chain: `filename` ->
  `ffprobe_creation_time` -> `file_mtime`) - how a recording's identity and
  timestamp get derived. BlackVue always has a pattern; a folder of
  arbitrary videos generally doesn't, so it falls through to the ffprobe/
  mtime chain instead.
- `kind_vocabulary` / `direction_vocabulary` - ordered tables (letter,
  label, and a couple of behavior flags: `low_signal` generalizes
  BlackVue's Parking-mode-as-a-long-uninformative-segment concept;
  `primary` marks which direction is the "front video" equivalent that
  trip-building/`--stitch`/thumbnails default to). Every adapter needs at
  least one entry in each, even a folder adapter with no real
  kind/direction concept - see the folder manifest for the trivial case.
- `asset_suffix_table` - an *ordered* suffix -> `Asset` mapping, directly
  modeled on the real `ArchiveReader.ASSETS` tuple (order-sensitive for the
  same reason it already is today: `.diarized.transcript.txt` must be
  checked before `.transcript.txt`).
- `capabilities` - the flags callers should check (`gps`, `gsensor`,
  `multi_direction_video`, `live_view`, `network_connect`, `download`,
  `config_snapshot`, and `thumbnails` which is `true`/`false`/`"generated"`)
  before offering a feature, instead of the feature silently no-op'ing or
  erroring on an adapter that can't do it.
- `code_hooks_required` - names of adapter-class responsibilities this
  manifest can't express (see the table above).
- `default_trip_gap_seconds` / `grouping_hint` - trip-building fallbacks
  for adapters without a `config_snapshot` capability (no per-camera
  auto-derived nominal segment length) or with a nested folder layout
  (`grouping_hint: "subfolder"` - recordings sharing an immediate parent
  folder are eligible for the same trip before gap-based splitting still
  applies within it).
- `unsupported_notes` - plain-language list of what this adapter
  deliberately can't do and why, meant to be surfaced in UI/CLI rather than
  left for the user to discover as a confusing error or silent gap.

## The two manifests shipped

### `src/blackvue/adapters/blackvue/manifest.json`

A snapshot of BlackVue's real, current conventions - not a new format,
a description of the one `src/blackvue/{archive,domain,parser,telemetry,
core,web}` already implement. The `asset_suffix_table` here is a direct,
order-preserving transcription of the real `ArchiveReader.ASSETS` tuple
(`src/blackvue/archive/archive_reader.py:17-45`), cross-checked against
that file rather than guessed. `code_hooks_required` lists the five real
things that stay genuine adapter code: GPS/g-sensor sidecar parsing, the
camera network client, `config.ini` parsing, and sidecar probing (the
`BlackVueCamera` compensation logic for `blackvue_vod.cgi` never listing
sidecars).

### `src/blackvue/adapters/folder/manifest.json`

The generic adapter, designed to be a **real, usable feature** (per
Christer - not just a proof that the boundary works): point it at a folder
tree of ordinary video files - phone clips, GoPro/action-cam footage,
downloaded videos, anything with no dashcam-specific metadata - and get a
working listing, gap-based trip grouping, and (since `bv-generate`/
`bv-scribe`'s transcription/translation/subtitle/scene-description work off
a video's own audio and frames, not off BlackVue conventions) real
transcripts/translations/subtitles/scene descriptions too.

Design choices worth calling out:

- `archive_layout: "recursive"` + `grouping_hint: "subfolder"` - handles
  "listings of ordinary videos in different folders" directly, the second
  thing Christer asked for. Files in the same immediate subfolder are
  treated as candidates for the same trip; gap-based splitting still
  applies inside that.
- `filename_pattern: null` + `timestamp_source: ["ffprobe_creation_time",
  "file_mtime"]` - no naming convention is assumed at all. This is real
  code that doesn't exist yet (`code_hooks_required` lists
  `timestamp_resolver`), and it's honestly the adapter's weakest link:
  embedded `creation_time` metadata and file mtimes are both less reliable
  than BlackVue's own filename-embedded timestamp (a file copy resets
  mtime; not every phone/camera writes `creation_time`). Noted directly in
  the manifest's `unsupported_notes` rather than glossed over.
- Every `kind_vocabulary`/`direction_vocabulary` still needs exactly one
  entry (schema-enforced - see `manifest.py`'s `primary_count != 1` check)
  even though there's no real concept of "kind" or "direction" for a
  generic video file - a single `"V"`/`"Video"` entry in each, marked
  primary, keeps the rest of the pipeline (which expects a primary
  direction and at least one kind) working unmodified.
- `thumbnails: "generated"` - no native `.thm` sidecar exists, so a
  thumbnail would need to come from an on-demand `ffmpeg` frame-grab
  instead (also not implemented yet - `code_hooks_required` lists
  `thumbnail_generator`).
- `asset_suffix_table` only lists the *generated* assets (audio, duration,
  transcript, translation, subtitles, scene description) - the same
  suffixes BlackVue uses, since those are this project's own filenames,
  not the camera's. There's no video/thumbnail/sidecar entry because a
  folder adapter's primary video asset isn't suffix-derived at all - any
  file matching `video_extensions` *is* the recording's one video track
  directly. That's a code-hook nuance the table alone doesn't capture,
  worth flagging for whoever implements `FolderAdapter` for real.

**What breaks, explicitly** (from the manifest's `unsupported_notes`, and
by design, not accident): no map.mp4/GPX/reverse-geocoding/start-stop-
location (no GPS), no gsensor.mp4/gsensor_graph.mp4/`--stitch-gsensor`
(no g-sensor), no `--stitch` camera-layout compositor or rearview-mirror
layout (no second camera direction - export is single-track passthrough),
no `--mode`/`--include-parking`/`max_parking_duration` (no kind vocabulary
beyond "it's a video"), and `bv-download`/`bv-config`'s endpoint setup/
`bv-gps`/`bv-live` simply don't apply (no network source at all). Trip
splitting on timestamp gaps still works; transcription/translation/
subtitles/scene description still work.

## Cross-cutting cleanup this unlocks (a bonus finding, not the point of
## this pass)

The investigation surfaced real duplication that a single adapter-owned
vocabulary would collapse, independent of the adapter project:

- The **kind-letter vocabulary** (N/E/M/P/A) is defined independently in
  at least four places: `RecordingId.kind`
  (`archive/recording_id.py:47-58`), `Recording.kind`
  (`domain/recording.py:23-27`), `bv_download.py`'s `ALL_KINDS`/
  `select_by_context()` (`cli/bv_download.py:53,174-200`), and
  `_KIND_LABELS` in the web archive browser
  (`web/archive_browser.py:156-164`).
- The **direction-letter vocabulary** (F/R/I) is similarly duplicated
  across `VodEntry` (`domain/vod_entry.py:30,42-60`), `ArchiveReader.ASSETS`
  (`archive/archive_reader.py:18-29`), `BlackVueCamera._DIRECTION_LETTERS`
  (`core/blackvue_camera.py:40`), and `web/archive_browser._DIRECTIONS`
  (`web/archive_browser.py:48-52`).
- **Filename-timestamp parsing** (`stem[:15]`, `"%Y%m%d_%H%M%S"`) is
  duplicated between `RecordingId.timestamp`
  (`archive/recording_id.py:41-45`) and `parser/vod.parse_timestamp()`
  (`parser/vod.py:30-33`).

None of this is fixed in this pass. It's flagged here because it's exactly
the kind of thing a real adapter implementation would want to fix as part
of consolidating these vocabularies behind `AdapterManifest.kind_vocabulary`/
`.direction_vocabulary` - free cleanup that falls out of doing the adapter
work properly, not a separate project.

## What's already adapter-friendly, unmodified

Worth naming so a future implementer doesn't waste time re-designing parts
that already work: `TripBuilder`'s core gap-based grouping algorithm
(`src/blackvue/trip/trip_builder.py`) only ever touches `recording.id.
timestamp` plus two already-optional/injected BlackVue wrinkles
(`is_parking` for the parking-duration cap, and `recordings_with_front_
video()`'s front-camera filter, applied by the *caller* rather than inside
`TripBuilder` itself) - it works on any timestamped recording sequence
regardless of camera brand today. GPS/g-sensor movement-bridging
(`telemetry/movement.py`) already treats a missing sidecar as "no
evidence" rather than an error, so a folder adapter with zero GPS/g-sensor
data needs no change there at all - it just never fires.
`core/camera_config.py`'s `CameraConfig`/`resolve_archive_path()`/
`default_archive_dir()` are already almost entirely generic; the one real
gap is that `CameraConfig.endpoints` assumes "a camera is reached over
HTTP," which would need to become optional for a source with no network
component.

## Not done in this pass

- No `CameraConfig.adapter` field, no migration for it.
- No adapter registry / `get_adapter(config)` lookup.
- No `BlackVueAdapter` or `FolderAdapter` class implementing the code
  hooks named in each manifest's `code_hooks_required`.
- No change to `ArchiveReader`, `RecordingId`, `Recording`, `VodEntry`,
  `bv_ls.py`, `bv_download.py`, or `web/archive_browser.py` - all of the
  real BlackVue-specific code cataloged above is untouched and still
  exactly as BlackVue-only as it was before this document.
- No vocabulary de-duplication (the "cross-cutting cleanup" section above)
  - flagged, not fixed.

This was deliberate, per Christer's "we start with number 1" - design doc
and schema only, reviewed before any of the above gets built.

## Adapter families: more than one "folder adapter"

Christer's steer (2026-08-16): don't expect one generic folder adapter to
cover every non-BlackVue source. GoPro and drone footage are the two he
named, and both have real structure the plain `folder` manifest
deliberately doesn't assume: GoPro embeds GPMF telemetry (GPS, g-sensor)
directly inside each MP4's own stream, and drones - DJI in particular -
commonly ship a companion `.srt` file per clip carrying per-frame GPS/
gimbal telemetry. Neither of those is "no metadata," they're just *not
BlackVue's* `.gps`/`.3gf` sidecar format - which is exactly what the
manifest+code-hooks split was designed for: a `gopro` or `dji_drone`
adapter would set `capabilities.gps: true`/`capabilities.gsensor: true`
like BlackVue does, but point `code_hooks_required` at a GPMF-stream
parser or an SRT-telemetry parser instead of the NMEA/`.3gf` ones. The
`folder` adapter shipped in this pass stays what it's meant to be: the
zero-assumptions baseline for footage with genuinely no embedded metadata,
not a stand-in for every other adapter that will exist. Each real source
gets its own `adapters/<id>/manifest.json` as evidence for it shows up,
same as `blackvue`'s and `folder`'s were both built from real, checked
findings rather than guessed.

This also means `source.kind` (currently `network_camera` | `local_folder`
in `manifest.schema.json`) will likely need a third value once SD-card
import (next section) is built - something like `removable_media`:
`requires_network: false` like `local_folder`, but with a real
`download`/import step (copy files off a mounted card into the archive),
unlike `local_folder` where `download` is always `false` because there's
nothing to import - the files are already the archive. Not added to the
schema yet since it's speculative until bv-download's SD-card path is
actually being built (next section) and the real shape is known.

## Suggested next steps (future passes, not started)

Re-sequenced per Christer's steer (2026-08-16): read paths first (lowest
risk, no existing behavior to regress), then the "easy" half of writing
(local file copy, no network protocol), letting later steps - more adapter
variants, `bv-analyze` - fall out of having two real, exercised adapters
to generalize from rather than one.

1. Add `CameraConfig.adapter: str = "blackvue"` plus a load-time migration
   default, so every existing config keeps working unmodified. Prerequisite
   plumbing, zero behavior change.
2. Write `src/blackvue/adapters/base.py`: a `CameraAdapter` Protocol/ABC
   whose methods correspond 1:1 to each manifest's `code_hooks_required`
   entries, plus the registry (`adapter_id -> (manifest, adapter class)`).
3. Implement `BlackVueAdapter` as a thin wrapper delegating to the
   existing `core`/`parser`/`telemetry` code - no behavior change. This
   alone is a good regression-safe validation of the interface: if
   `BlackVueAdapter` can be built as a pure delegation layer with zero
   behavior change, the interface is right.
4. **Wire `bv-ls` and the `bv-web` archive browser through the adapter
   abstraction** - Christer's stated starting point. Both are read-only
   and display-heavy (per the investigation, `bv-ls`'s own BlackVue
   coupling is one filter-policy line plus `display_group.py`'s kind-
   letter/RecordTime comparisons; the archive browser's is a handful of
   small fixed tables - `_DIRECTIONS`, `_KIND_LABELS`, `_SIDECARS`) -
   good first real consumers of `AdapterManifest`, and a place a
   regression shows up immediately as a wrong table cell, not silent data
   loss.
5. Implement `FolderAdapter` for real (the recursive scanner, the
   ffprobe/mtime timestamp fallback, on-demand thumbnails) so step 4 has
   a second, genuinely different adapter to prove itself against, not
   just `BlackVueAdapter` in a trench coat.
6. **Add SD-card import to `bv-download`** - Christer's stated second
   step, and rightly called "the easy one": no CGI wire protocol, no
   never-closing multipart streams, no `blackvue_vod.cgi` sidecar-missing
   workaround - just a mounted filesystem with the camera's own file
   layout (BlackVue's SD card mirrors what `bv-download` already produces)
   to filter and copy into the archive. Likely the first real user of the
   `removable_media` source kind discussed above.
7. Build further adapter variants (GoPro, drone footage, ...) as real
   need/footage shows up, informed by whatever steps 4-6 taught about the
   interface.
8. Build `bv-analyze` (sketched below) once at least two real adapters
   exist to test it against - an inference tool tuned against a single
   example (BlackVue) risks just re-deriving BlackVue's own pattern rather
   than genuinely generalizing.

## Future: `bv-analyze <archive>` - an adapter-authoring assistant

Christer's follow-on idea, not built, not started - a sketch for whoever
picks up step 8 above. The manifest schema makes writing an adapter by
hand tractable, but most of a manifest's *declarative* fields are exactly
the kind of thing pattern-matching over a real folder of files can infer
- so instead of a human starting from a blank `manifest.json`, `bv-analyze`
would point at an unknown archive and hand back a draft to review and
finish, the same way `bv-config`'s wizard suggests defaults rather than
asking for everything blind.

### CLI shape

```
bv-analyze <path> [--out manifest.json] [--sample N]
```

Read-only - it never modifies `<path>`, only writes the draft manifest
(and prints a summary to the terminal either way).

### What it would try to infer

1. **Layout** (`archive_layout`): flat if every video file is a direct
   child of `<path>`, `recursive` if any live in subfolders.
2. **Video extensions**: the extensions of files `ffprobe` recognizes as
   video, sampled rather than probing every file for a large archive.
3. **Filename pattern**: over a sample of filenames, look for a shared
   structural prefix - runs of digits that parse as a plausible date/time
   - and, among files that share that prefix, a trailing single letter
   that varies (a direction-letter candidate) or one just before the
   extension that splits the sample into a small number of groups (a
   kind-letter candidate). This is the part most likely to need a human's
   eyes - BlackVue's own scheme was reverse-engineered by inspection, not
   guessed from a pattern-matcher, and a bad candidate pattern would
   silently mis-group recordings.
4. **Candidate validation**: for an inferred direction-letter set, check
   that files sharing a timestamp prefix actually differ *only* in that
   letter and have broadly consistent sizes/durations within each letter
   - a sanity check against a false-positive pattern match, not a
   guarantee.
5. **Timestamp reliability**: parse the candidate `filename_pattern`
   against a sample and cross-check the result against each file's mtime
   and `ffprobe` `creation_time`. Rough agreement -> keep the filename
   pattern. Disagreement, or no pattern found at all -> draft
   `filename_pattern: null` with the `timestamp_source` fallback chain
   instead (the folder adapter's approach), and say so in the printed
   summary rather than silently picking one.
6. **Sidecars**: any non-video extension sitting next to video files gets
   listed with its size and a text-vs-binary guess - but never
   auto-assigned to `GPS`/`GSENSOR`/a thumbnail. Guessing that wrong is
   the kind of mistake that corrupts trip data quietly; a "found 3 unknown
   sidecar types, please classify" list is safer than a confident wrong
   answer.
7. **`grouping_hint`**: for a recursive layout, check whether subfolders'
   timestamp ranges are non-overlapping (each folder is its own time
   window) - if so, suggest `grouping_hint: "subfolder"`.
8. **Capabilities**: everything defaults to `false`/unknown except what
   step 4 actually confirmed (e.g. `multi_direction_video` only if the
   direction split validated). `thumbnails` is always left as a TODO for
   a human, same reasoning as sidecars.
9. **Known-adapter match**: compare the inferred pattern and extensions
   against already-registered manifests first - if the archive looks like
   an existing `adapter_id` (say, it's just another BlackVue archive),
   report that instead of drafting a redundant new manifest.

### Output

A `manifest.json` matching `manifest.schema.json`: confidently-inferred
fields filled in directly, uncertain ones left at safe defaults (an
unclassified sidecar list, a single collapsed kind/direction entry as the
folder adapter does when nothing was confirmed) with the reasoning
recorded in `unsupported_notes`, plus a terminal summary of exactly which
`code_hooks_required` entries have zero automatic support and need a
human to write real code - sidecar binary/text parsing and the camera's
network protocol can never be inferred from static files alone, no matter
how good the pattern-matching gets.

### Non-goals

Doesn't write the adapter class or any of the code hooks themselves - only
drafts the manifest half. Doesn't try to identify the camera make/model
that produced the files (no vendor database) - a nice-to-have someday, not
required for the draft manifest to already save real time.

## See also

- `docs/ARCHITECTURE.md` - main project overview; documents the earlier,
  abandoned adapter framework this design deliberately avoids repeating.
- `src/blackvue/adapters/manifest.schema.json` - the formal JSON Schema.
- `src/blackvue/adapters/manifest.py` - the loader/validator.
- `src/blackvue/adapters/blackvue/manifest.json`,
  `src/blackvue/adapters/folder/manifest.json` - the two example manifests.
