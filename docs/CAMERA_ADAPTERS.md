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

## What's built so far (2026-08-16, second pass)

Christer: "yes start" - real implementation, not just design, following
the re-sequenced order above. Built:

- `CameraConfig.adapter: str = "blackvue"` (`core/camera_config.py`) -
  round-trips through save/load, defaults to `"blackvue"` for every
  existing config (no migration step needed, since that's what an unset
  field always implicitly meant). Nothing reads it yet.
- `adapters/base.py` - the `CameraAdapter` Protocol every adapter class
  implements: `open_archive()`, `read_gps()`, `read_gsensor()`,
  `connect()`, `config_snapshot_seconds()`. Not a full 1:1 mapping of
  every `code_hooks_required` entry to its own method - `camera_client`/
  `config_snapshot_parser`/`sidecar_prober` are bv-download's concern
  (a later step below), so `connect()`/`config_snapshot_seconds()` exist
  as their eventual home without every hook needing separate methods yet.
  `AdapterCapabilityError` is the named exception a method raises when
  its own manifest declares the capability unsupported.
- `adapters/registry.py` - a plain, explicit `adapter_id -> class` dict
  (deliberately not import-time self-registration magic).
  `get_adapter(id)`/`load_adapter_manifest(id)` both raise
  `AdapterNotFoundError` with the list of what *is* registered, rather
  than a bare `KeyError`.
- `adapters/blackvue/adapter.py` - `BlackVueAdapter`, a **pure delegation
  wrapper**: `open_archive()` returns a real `Archive(path)` unchanged,
  `read_gps()`/`read_gsensor()`/`connect()`/`config_snapshot_seconds()`
  each forward straight to the exact existing functions
  (`telemetry.gps_reader.read_gps()`, `telemetry.gsensor_reader.
  read_gsensor()`, `core.connection.connect()`, `archive.configuration.
  parse_record_time_seconds()`) with zero reimplementation. This is the
  "prove the interface" step from the roadmap below - built and verified
  precisely because it *isn't* allowed to change behavior.

**Still not built** (as of the plumbing-only first half of this pass):
no `FolderAdapter`, no wiring of `bv-ls`/the web archive browser through
any of this, no `bv-download` SD-card import path, no vocabulary
de-duplication (the "cross-cutting cleanup" section above - flagged, not
fixed). See "Suggested next steps" below for the remaining, re-sequenced
order - #4 and #5 were completed later the same day, see the next section.

Verified: every new module `py_compile`s; a standalone script (no pytest
available in the sandbox this was built in - see WORKING_CONTEXT.md's
standing note) exercised the registry lookup, a synthetic archive read
through `BlackVueAdapter.open_archive()` compared against calling
`Archive(path)` directly, and every delegation method via monkeypatching
the real function each one calls, confirming args/return values cross
the boundary unchanged; real pytest test files were also written
(`tests/blackvue/adapters/`) matching this exact coverage for Christer's
own machine/CI to run, and were confirmed to at least import/collect
cleanly here via a minimal fake `pytest` shim.

## What's built so far (2026-08-16, third pass - FolderAdapter + wiring)

Christer's own GoPro test archive (`X:\gopro`, a real "GP" camera config
with `adapter = "folder"`, hand-set since `bv-config`'s wizard has no
prompt for `adapter` yet) made this the natural next step instead of a
synthetic exercise. Built:

- `adapters/folder/adapter.py` - `FolderAdapter`, **not** a delegation
  wrapper (there's nothing BlackVue-specific to delegate to for a plain
  folder of videos). Recursively walks `archive_layout: "recursive"`,
  resolving each video's timestamp via ffprobe's `creation_time` tag
  first, file mtime second (matching `manifest.timestamp_source`
  exactly), and synthesizes a `RecordingId` in BlackVue's own
  `"YYYYMMDD_HHMMSS_K"` shape (kind code `"V"`) from that timestamp -
  deliberately reusing `RecordingId` as-is rather than inventing a
  parallel id type, so `bv-ls`'s `--from`/`--until`/`--timestamp`
  filters, `TripBuilder`'s gap grouping, and bv-web's URL routing all
  keep working for a folder-adapter camera with no changes of their
  own. Two videos landing on the same wall-clock second are
  disambiguated by bumping the later one forward a second at a time.
  Each video is stored under `Asset.FRONT` - the single-video-per-
  recording equivalent of BlackVue's front camera, since
  `direction_vocabulary` here has exactly one (primary) code - which is
  what lets `recordings_with_front_video()` (trip building) and every
  existing Front-column/video-serving code path work unmodified.
  Same-stem sibling files (`clip.mp4` + `clip.transcript.txt` in the
  same folder) are picked up against `manifest.asset_suffix_table`'s 8
  generated-asset suffixes - keyed by the video's own filename stem,
  *not* by the synthesized recording id, since that id doesn't exist as
  a stable name anyone could target in advance the way BlackVue's
  filename-embedded one does.
- `FolderArchive` (same file) - an `Archive`-duck-typed container
  (`.recordings`, `.configuration()`) built directly from the scan, no
  `ArchiveReader` involved. `.configuration()` always returns
  `Configuration.fallback()` (300s, matching
  `manifest.default_trip_gap_seconds`) silently - no "no configuration
  snapshot" warning, since a folder adapter camera never has one *by
  design* (`config_snapshot` capability is `false`), not as a degraded
  state.
- **`CameraAdapter.find_recording(path, recording_id)`** - added to the
  Protocol (`adapters/base.py`) after the fact, not in the original
  design: bv-web's archive browser needs a *targeted* single-recording
  lookup (one per thumbnail request, one per HTTP video-range request -
  see `archive_browser.find_recording()`'s own docstring) separate from
  `open_archive()`'s full scan, and that need only became concrete once
  actually wiring the browser through the adapter layer, not at design
  time. `BlackVueAdapter.find_recording()` delegates to
  `ArchiveReader.read_recording()` unchanged (same fixed-stat-count
  lookup, zero perf regression). `FolderAdapter.find_recording()` has no
  equivalent fast path - a folder adapter's ids are computed at scan
  time from resolved timestamps, not derivable from a filename the way
  BlackVue's are - so it does a full rescan filtered by id. Accepted as
  a real, documented cost for this kind of archive (matching the
  `recursive_scanner` code hook already declared), not silently glossed
  over; can be revisited if a large real folder-adapter archive ever
  makes it a felt problem, the same way BlackVue's own O(archive size)
  lookup bug got fixed only once it actually bit (see WORKING_CONTEXT.md).
- **`bv-ls` wired through the adapter abstraction** (`cli/bv_ls.py`) -
  `bv_ls()` gained an `adapter_id: str = DEFAULT_ADAPTER_ID` parameter
  and now calls `registry.get_adapter(adapter_id).open_archive(path)`
  instead of constructing `Archive(path)` directly; `_run()` passes
  `camera_config.adapter` when `path` resolved to a configured camera
  (falls back to `DEFAULT_ADAPTER_ID` for a literal directory path).
  Confirmed byte-for-byte identical output for existing BlackVue
  archives (same delegation as before, just via the registry).
- **bv-web's archive browser wired through the adapter abstraction**
  (`web/archive_browser.py`, `web/app.py`) - `scan_archive()` and
  `find_recording()` both gained an `adapter_id` parameter (defaulting
  to `DEFAULT_ADAPTER_ID`); `ArchiveRecordingCache.get()` threads it
  through too. `web/app.py` gained `_find_camera_adapter_id()` (a
  sibling to the existing `_find_camera_archive()`, reusing the same
  cached `CameraConfig` lookup - no extra file read) and wires it into
  the archive list route and `_find_archive_recording()` (which backs
  the detail page, thumbnails, and video playback). Every *other*
  `_find_camera_archive()` call site (the bv-export/bv-generate/etc. job
  forms) is untouched - only the two archive-browsing functions Christer
  scoped this step to needed adapter awareness.

**Deliberately not built in this pass**: on-demand thumbnail generation
for folder-adapter recordings (`capabilities.thumbnails: "generated"` -
the `thumbnail_generator` code hook is still just a manifest entry, not
code; a folder-adapter recording with no thumbnail degrades the same way
an older/incomplete BlackVue archive already does throughout bv-web -
`ArchiveRecording.thumbnail_direction` returns `None`, the grid shows no
image, nothing errors). `bv-download` SD-card import (next roadmap step)
and further adapter variants (GoPro, drone) remain unstarted.

Verified: every new/changed module `py_compile`s; standalone scripts
exercised `FolderAdapter.open_archive()`/`find_recording()` against real
temp-directory fixtures (nested subfolders, mixed extensions/case,
same-second collisions, same-stem generated assets), `bv_ls()` against
both a real BlackVue-shaped archive (output byte-identical to before)
and a folder-shaped one, and `scan_archive()`/`find_recording()` at the
`archive_browser.py` layer for both adapters including the
`ArchiveRecordingCache` path. Real pytest test files
(`tests/blackvue/adapters/test_folder_adapter.py`, plus additions to
`test_registry.py`, `tests/blackvue/cli/test_bv_ls.py`, and
`tests/blackvue/web/test_archive_browser.py`) were written matching this
coverage and confirmed to actually pass, function-by-function, via a
minimal fake `pytest`+`tomllib` harness (no real pytest available in
this sandbox - see WORKING_CONTEXT.md's standing note) for Christer's
own machine/CI to run for real.

## GPS/g-sensor pipeline rewired through the adapter (2026-08-16, fourth pass)

A gap surfaced while scoping the GoPro adapter (see "Adapter families"
below): every GPS/g-sensor read in the pipeline - bv-export's trip-gap
movement-bridging heuristic and prebuffer-trim/merge logic, bv-search's
`--near`/`--place` proximity search, and bv-web's archive-detail "Show
start and stop location" link - read BlackVue's `.gps`/`.3gf` sidecars
directly (`Asset.GPS`/`Asset.GSENSOR` + `telemetry.gps_reader.read_gps()`/
`telemetry.gsensor_reader.read_gsensor()`), bypassing the adapter
abstraction entirely. This wasn't a folder-adapter-specific gap -
**BlackVue's own pipeline bypassed its own adapter** for every telemetry
read except `open_archive()`/`find_recording()`, which task #4 (third
pass) wired through. A `GoProAdapter` with GPMF telemetry embedded in the
video itself (not a separate sidecar) would have hit this immediately;
fixing it now, decoupled from the GoPro adapter's own work, keeps that
adapter a real GPMF-parsing exercise rather than also being the thing
that discovers this architectural hole.

Christer's call when asked "adapter-only fix, or fix the whole pipeline":
**option 2, the full rewire** - confirmed after asking "are we skipping
the plugin thing" (no; the adapter/plugin architecture stays the
intended shape, this closes a gap in following through on it).

Built:

- `AdapterManifest.gps_source_asset` / `.gsensor_source_asset` (`adapters/
  manifest.py`, `manifest.schema.json`) - two new optional manifest
  fields naming the `Asset` enum member (by member name, e.g. `"GPS"`)
  whose on-disk file holds that adapter's telemetry, or `null` if the
  adapter doesn't have it as a discrete asset at all. BlackVue's manifest
  sets `"GPS"`/`"GSENSOR"` (its existing sidecars); the folder adapter
  sets both `null` (declares no gps/gsensor capability at all). A future
  GoPro adapter would point both at `"FRONT"`, since GPMF is embedded in
  the video stream rather than a separate file - the same manifest field
  covers both shapes without the many call sites needing to know which
  case applies.
- `adapters/telemetry_bridge.py` (new module) - the bridging layer
  between that declarative field and a real read: `read_recording_gps(
  adapter, recording)` / `read_recording_gsensor(adapter, recording)`
  resolve `recording.file(Asset[manifest.gps_source_asset])` and call
  `adapter.read_gps()`/`.read_gsensor()` on it, returning `()` (not
  raising) if the adapter lacks the capability, the manifest field is
  `None`, the recording has no file for that asset, or the read raises
  `MediaToolError` - the same "missing/bad telemetry is absent, not
  fatal" contract every direct sidecar read already had. `recording_has_
  gps()`/`recording_has_gsensor()` are the cheap existence-check
  counterparts (no read, just confirms a file exists for the asset).
- Every call site rewired to go through these instead of `Asset.GPS`/
  `Asset.GSENSOR` + `read_gps()`/`read_gsensor()` directly:
  `export/trip_export.py` (`_merge_gps()`, `_merge_gsensor()`,
  `_trim_prebuffers()` - all three gained a required keyword-only
  `adapter` parameter; `export_trip()` itself gained an optional
  `adapter: CameraAdapter | None = None`, defaulting to
  `get_adapter(DEFAULT_ADAPTER_ID)` so every existing BlackVue call site
  keeps working unchanged), `cli/bv_export.py` (resolves the real
  adapter from `CameraConfig.adapter` and threads it through, including
  into the movement-bridge callable via `functools.partial(
  movement_bridges_gap, adapter=adapter)`), `telemetry/movement.py`
  (`_recording_shows_movement()`/`movement_bridges_gap()` both gained a
  required keyword-only `adapter`), `search.py`/`cli/bv_search.py`
  (`search_near()` gained a required keyword-only `adapter`; `bv_search.
  py`'s `_run()` also picked up the same `resolve_archive_path()` ->
  `get_adapter()` -> `adapter.open_archive()` pattern `bv_ls.py`/
  `bv_download.py` already used - it had previously discarded the
  resolved `camera_config`/adapter id entirely), `web/archive_browser.py`
  (`first_valid_gps_fix()`/`last_valid_gps_fix()` changed from taking a
  bare `.gps` path to `(adapter, recording)`), `web/app.py`
  (`archive_recording_location()` resolves the camera's adapter via the
  existing `_find_camera_adapter_id()` helper and threads it into all
  three).
- **Deliberately left alone**: `web/archive_browser.py`'s
  `ArchiveRecording.has_gps`/`.gps_path` properties still read
  `Asset.GPS` directly rather than going through the manifest field. For
  BlackVue this is correct (`Asset.GPS` is always where its GPS data
  lives); for a future GoPro adapter (`gps_source_asset: "FRONT"`) these
  two properties would incorrectly report "no GPS" even though
  `recording_has_gps()`/`read_recording_gps()` would find it fine. A
  real, narrow, documented gap, deferred rather than pulled into this
  rewire's scope - nothing in this pass's callers actually depends on
  those two properties being adapter-aware yet.

Verified: every changed module `py_compile`s; functional smoke tests
(synthetic fixtures via a fake-`tomllib` import shim, no real pytest in
this sandbox - see WORKING_CONTEXT.md's standing note) confirmed
`_merge_gps()`/`_merge_gsensor()` produce identical `GpsFix`/
`GSensorSample` tuples via `BlackVueAdapter()` as the old direct reads
did, `movement_bridges_gap()` still returns its expected "GPS speed..."
reason both called directly and via the `functools.partial`-wrapped
callable `bv_export.py` actually constructs, and `search_near()`
produces identical `GeoMatch` results with the adapter's `read_gps()`
monkeypatched in place of the old module-level fake. Existing pytest
files updated to match the new signatures - `tests/blackvue/telemetry/
test_movement.py`, `tests/blackvue/test_search.py`, `tests/blackvue/cli/
test_bv_search.py` (its `_FakeArchive`/`Archive`-monkeypatch pattern
became a `_FakeAdapter(BlackVueAdapter)` wrapping the same fake archive,
since `_run()` now resolves through `get_adapter()` rather than
constructing `Archive()` directly), `tests/blackvue/export/
test_trip_export.py` (`_trim_prebuffers()`/`_merge_gsensor()`'s direct
unit tests needed an explicit `adapter=BlackVueAdapter()` - these are
the one place the rewire wasn't backward-compatible by default, since
only `export_trip()` itself gained a defaulting `adapter` parameter, not
its private helpers), and `tests/blackvue/web/test_archive_browser.py`
(`first_valid_gps_fix()`/`last_valid_gps_fix()`'s tests now build a real
`Recording`+`AssetFile` and pass a real `BlackVueAdapter()` instead of a
bare path) - all confirmed passing via the same fake-`tomllib`/fake-
`pytest` harness. `tests/blackvue/cli/test_bv_export.py` needed no
changes: its `_fake_bv_export(**kwargs)`/`_fake_export_trip(*args,
**kwargs)` fakes already accept arbitrary kwargs, and `export_trip()`'s
own default-adapter design meant every existing `export_trip(trip,
dest_dir)` call site across that ~5500-line test file kept working
unmodified - the intended "zero behavior change for BlackVue by
default" outcome, confirmed rather than assumed.

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

## GoPro adapter: manifest + code-hook interface design (2026-08-16)

First real step on step 8 below, now that the GPS/g-sensor pipeline rewire
above means this adapter's own work is purely a GPMF-parsing exercise, not
also the thing that has to fix a pipeline gap along the way. Design-only
pass - no `GoProAdapter` class yet, not registered, nothing wired.

**New `adapters/gopro/manifest.json`.** Structurally closest to `folder`
(no BlackVue-style filename convention, `filename_pattern: null`,
`timestamp_source: ["ffprobe_creation_time", "file_mtime"]`,
`archive_layout: "recursive"`, a single `kind_vocabulary`/
`direction_vocabulary` entry each, `thumbnails: "generated"`,
`config_snapshot: false`, `multi_direction_video: false`) but with real
telemetry: `capabilities.gps`/`capabilities.gsensor` both `true`, and
`gps_source_asset`/`gsensor_source_asset` both `"FRONT"` - GPMF lives
inside the video's own stream, not a sidecar file, so both point at the
same asset the video itself is stored under, exactly the shape
`manifest.schema.json`'s own `gps_source_asset` docstring anticipated back
when the GPS/g-sensor rewire added that field. `asset_suffix_table` only
lists the shared generated-asset suffixes (transcript/audio/subtitles/
scene description) - byte-identical to `folder`'s - since there's no
GPMF-specific sidecar suffix to register; the video itself carries its own
telemetry. `code_hooks_required` adds three GPMF-specific hooks beyond
`folder`'s `recursive_scanner`/`timestamp_resolver`/`thumbnail_generator`:
`gpmf_stream_locator`, `gpmf_gps_parser`, `gpmf_gsensor_parser` (see below).

`unsupported_notes` records what's deliberately out of scope for a first
real pass rather than guessed at: GPMF's own fix-type/precision fields
(`GPSF`/`GPSP`) will only gate fix validity, not be surfaced as accuracy
data; chaptered >4GB recordings (camera-split across
`GH010001.MP4`/`GH020001.MP4`-style files) are treated as separate
recordings, the same limitation `FolderAdapter` already has for any
multi-part video; no 360/dual-lens (GoPro MAX) support; GoPro's own
same-stem `.THM` sidecar isn't read for thumbnails yet since it isn't
confirmed to survive a plain file copy onto Christer's archive - worth
revisiting once `X:\gopro` is actually scanned in #906/step 8's
implementation pass.

**Planned code-hook interface** (for the implementation pass, not built
yet): a new `adapters/gopro/gpmf.py` module, parallel to how
`telemetry/gps_reader.py`/`telemetry/gsensor_reader.py` are the parsing
layer `BlackVueAdapter` delegates to -

- `locate_gpmf_stream(path: Path) -> bytes` - find and extract the raw
  GPMF ('gpmd'-tagged) data stream from an MP4 container. GPMF isn't a
  separate file the way `.gps`/`.3gf` are, so this is the `gpmf_stream_
  locator` hook: almost certainly an `ffprobe`-then-`ffmpeg -map -c copy`
  extraction (the same subprocess pattern `generate/media.py` already
  uses elsewhere in this project) rather than hand-parsing MP4 box
  structure, since the pure-Python `mp4_box_reader.py` fallback built
  earlier for duration/dimensions doesn't need to (and wasn't designed
  to) walk into stream *payloads*, only container-level boxes.
- `parse_gpmf(data: bytes) -> ...` - a generic KLV-style tree walker over
  GPMF's nested `DEVC`/`STRM` container format (FourCC + type char +
  structure size + repeat count + payload, repeating) - the shared
  low-level parser both telemetry extractors below sit on top of.
- `extract_gps_fixes(data: bytes) -> tuple[GpsFix, ...]` - the
  `gpmf_gps_parser` hook: reads a `STRM` container's `GPS5`/`GPS9`
  payload (lat/lon/altitude/speed) plus its `STMP` (sample timing) and
  `GPSU` (UTC anchor) siblings to produce `GpsFix` timestamps directly
  comparable to `RecordingId.timestamp`/BlackVue's own `.gps` fixes,
  `GPSF` gating `.valid` the same way BlackVue's NMEA mode indicator
  does today (see gps_reader.py's own docstring on why that field, not
  the older status one, is the right validity signal).
- `extract_gsensor_samples(data: bytes) -> tuple[GSensorSample, ...]` -
  the `gpmf_gsensor_parser` hook: reads `ACCL`'s payload plus `STMP`/
  `SCAL` to produce `GSensorSample`s offset from recording start, same
  shape `.3gf`'s reader already returns (raw axis units unconfirmed
  either way, per that reader's own docstring - relative variance, not a
  calibrated g-force threshold, either way).
- `GoProAdapter.read_gps(path)`/`.read_gsensor(path)` (in the not-yet-
  written `adapters/gopro/adapter.py`) each call `locate_gpmf_stream()`
  then the matching extractor - `path` here is the *video's own* path,
  since `gps_source_asset`/`gsensor_source_asset` are both `"FRONT"`,
  exactly the shape `adapters/telemetry_bridge.py` already resolves
  correctly for an embedded-telemetry adapter without needing any change
  of its own (built and verified generically enough for this case back
  in the GPS/g-sensor rewire pass, before this adapter existed).
- `open_archive()`/`find_recording()` are expected to mirror
  `FolderAdapter`'s recursive-scan/synthesized-`RecordingId`/single-
  `Asset.FRONT`-slot shape closely enough that the implementation pass
  should decide whether to factor the shared scanning logic out (both
  adapters would then differ only in their manifest and telemetry code
  hooks) or duplicate it (simpler, but the two copies drift) - a real
  call worth making once `GoProAdapter` is actually being written
  against real footage, not guessed at here.

Verified: `load_manifest()` loads and structurally validates the new
manifest.json cleanly (`gps_source_asset`/`gsensor_source_asset` both
resolve to `"FRONT"`, `capabilities.gps`/`.gsensor` both `True`,
`primary_direction` resolves to the single `"V"` entry); the file is
valid JSON. No GPMF-parsing code exists yet - that's step 8's
implementation pass (#905), to be tested against Christer's real `X:\gopro`
footage (#906) rather than synthetic fixtures alone, since GPMF's exact
on-disk shape (GPS5 vs. the newer GPS9 stream present on Hero11+, which
fields are actually populated) is camera/firmware-dependent and worth
confirming against a real file before committing the parser to one shape.

## Suggested next steps (re-sequenced 2026-08-16; #1-3 done, see above)

Re-sequenced per Christer's steer (2026-08-16): read paths first (lowest
risk, no existing behavior to regress), then the "easy" half of writing
(local file copy, no network protocol), letting later steps - more adapter
variants, `bv-analyze` - fall out of having two real, exercised adapters
to generalize from rather than one.

1. ~~Add `CameraConfig.adapter: str = "blackvue"` plus a load-time
   migration default~~ - **done**, see "What's built so far" above.
2. ~~Write `adapters/base.py`: a `CameraAdapter` Protocol, plus the
   registry~~ - **done**, see above.
3. ~~Implement `BlackVueAdapter` as a thin wrapper delegating to the
   existing `core`/`parser`/`telemetry` code~~ - **done**, see above.
4. ~~Wire `bv-ls` and the `bv-web` archive browser through the adapter
   abstraction~~ - **done**, see "What's built so far (third pass)"
   above.
5. ~~Implement `FolderAdapter` for real (the recursive scanner, the
   ffprobe/mtime timestamp fallback)~~ - **done**, see above. On-demand
   thumbnail generation specifically was deferred - see that section's
   "Deliberately not built" note.
6. ~~Add SD-card import to `bv-download`~~ - **done** (2026-08-16). New
   `--sdcard DIR` flag: no CGI wire protocol at all, just a recursive
   filesystem scan of `DIR` for files matching BlackVue's own on-camera
   filename convention (`YYYYMMDD_HHMMSS_KD.ext`), fed straight into the
   same `domain.Recording`/`VodEntry` model the network path already
   uses - so the rest of `bv-download` (mode selection, dry-run
   listing, the download loop, RecordTime capture) works unmodified.
   New `core/sdcard_camera.py`: `SdCardCamera`, a filesystem-backed
   counterpart to `BlackVueCamera` (same `recordings()`/
   `probe_missing_sidecars()`/`download()` shape, plus `scan_summary()`
   and `read_config_text()`). A file that doesn't match BlackVue's
   naming convention is silently skipped, not an error - reported as
   "N files scanned, 0 recognized" rather than a bare empty result, so
   a card like Christer's own emulated test card
   (`X:\SD_card`, non-BlackVue filenames) gets a clear explanation
   instead of looking broken. Combine `--sdcard` with `ID` to import
   into that camera's configured archive (RecordTime capture included,
   reading `config.ini` straight off the card instead of over HTTP -
   tried at `Config/config.ini` and the card root, since the real
   on-disk layout isn't confirmed yet), or with `--target` for a bare
   one-off import with no config, mirroring `--host`/`--target`. This
   turned out to be the first real user of the `removable_media`
   source-kind idea discussed above, though the schema itself wasn't
   touched in this pass - `--sdcard` lives entirely in `bv-download`,
   not in the adapter/manifest system. 34 new/updated tests across
   `tests/blackvue/core/test_sdcard_camera.py` and
   `tests/blackvue/cli/test_bv_download.py`, plus a
   `docs/man/bv-download.md` rewrite for the new three-way source
   choice.
7. ~~Rewire the GPS/g-sensor pipeline (bv-export, bv-search, bv-web's
   archive browser/location page) through `CameraAdapter.read_gps()`/
   `read_gsensor()` instead of BlackVue-specific `Asset.GPS`/`Asset.
   GSENSOR` sidecar reads~~ - **done** (2026-08-16), see "GPS/g-sensor
   pipeline rewired through the adapter" above. Surfaced while scoping
   step 8 below; fixed first so the GoPro adapter's own work is purely
   GPMF parsing, not also discovering this gap.
8. Build further adapter variants (GoPro, drone footage, ...) as real
   need/footage shows up, informed by whatever steps 4-7 taught about the
   interface.
9. Build `bv-analyze` (sketched below) once at least two real adapters
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
