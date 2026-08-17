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

## GoPro adapter: GPMF parser + GoProAdapter implementation (2026-08-16)

Step 8's implementation pass (#905), building on the design above. Real,
tested, registered - `"gopro"` now resolves via `registry.get_adapter()`
the same as `"blackvue"`/`"folder"`.

**Shared scan logic factored out.** Per Christer's steer on the design
pass's open question ("Share them as long as it is possible, in worst
case you make a branch later"): before writing `GoProAdapter`,
`FolderAdapter`'s recursive-scan/timestamp-resolution/synthesized-
`RecordingId`/generated-asset-discovery logic was extracted from
`folder/adapter.py` into a new `adapters/_recursive_scan.py` (leading
underscore - shared internal machinery, not an adapter in its own
right). `FolderAdapter` now just delegates to
`scan_recursive_archive()`/`find_recording_in_recursive_archive()` with
its own manifest and `_KIND_CODE = "V"`; `test_folder_adapter.py`'s full
17-test suite passes unmodified against the refactor, confirming
byte-identical behavior. `GoProAdapter` delegates to the exact same two
functions - the two adapters now differ only in their manifest and in
`read_gps()`/`read_gsensor()` (real GPMF telemetry vs. `folder`'s
`AdapterCapabilityError`).

**No ffmpeg/ffprobe dependency for GPMF extraction - pure-Python MP4 box
parsing instead**, a change from the design pass's original plan (which
assumed an `ffprobe`-then-`ffmpeg -map -c copy` extraction). Reason:
muxing a synthetic `gpmd`-tagged stream via this sandbox's ffmpeg 4.4.2
to build a test fixture failed outright (`Tag gpmd incompatible with
output codec id '0'`), and no `MP4Box`/`gpac` alternative was available
either. Rather than depend on one ffmpeg build's tag-handling behavior in
production too, `adapters/gopro/gpmf.py`'s `locate_gpmf_stream()` walks
the MP4's own `moov`/`trak`/`mdia`/`minf`/`stbl` sample-table boxes
directly (`stsd` for the `'gpmd'` fourcc identifying the GPMF track,
`stsz`/`stsc`/`stco`/`co64` for per-sample byte ranges - the standard
ISO-BMFF algorithm every demuxer uses), reusing
`generate/mp4_box_reader.py`'s existing private box-walking helpers
(`_find_top_level_box`, `_iter_boxes`, `_find_box`, `_parse_hdlr_type`) -
the same reuse pattern `generate/mp4_repair.py` already established.
More robust for real-world files, and fully testable with hand-built
synthetic MP4 box structures in pure Python with no external tool
dependency at all.

**GPMF KLV decoding**, also in `gpmf.py`: a generic `_iter_klv()` walker
over the FourCC/type-char/size/repeat/payload shape (nested containers
have type char `'\x00'`), `_iter_devc_blocks()`/`_iter_strm_blocks()` to
walk `DEVC`→`STRM` structure, `_unpack_rows()` to unpack a leaf item's
payload against a type-character-to-`struct`-format table.
`extract_gps_fixes()` reads each `STRM`'s `GPS5`/`SCAL`/`GPSF`/`GPSU`;
`extract_gsensor_samples()` reads `ACCL`/`STMP` (raw, unscaled x/y/z -
same "unit unconfirmed, use relative variance" contract
`gsensor_reader.py` already has for BlackVue's `.3gf`). Known,
documented gaps (see `gpmf.py`'s own module docstring): GPS9 (Hero11+'s
replacement for GPS5) isn't parsed, only GPS5; GPS5 has no
heading/course field so every `GpsFix` this module returns has
`course=None`; within-block row timestamps/offsets are linearly
interpolated across one assumed second per `DEVC` block rather than
derived from an explicit per-sample rate.

**Per-recording degradation, not all-or-nothing** - the second design
constraint Christer added mid-pass ("The worst archive case would be a
mix of everything video/picture but then it should regress to plain
folder and have minimal options"): this falls out for free from two
already-established contracts working together, not new code.
`open_archive()` never touches GPMF at all - only reading a specific
recording's telemetry does - so a mixed-content folder scans in full
regardless of how many clips turn out to have no usable GPMF stream.
`locate_gpmf_stream()` raises `MediaToolError` for a video with no
`'gpmd'` track (a re-encoded/trimmed clip, a plain video that happens to
sit in a GoPro folder) or an unparseable sample table, and
`telemetry_bridge.py`'s `read_recording_gps()`/`read_recording_gsensor()`
already catch `MediaToolError` and return `()` rather than propagating
it (the same "missing/bad telemetry is absent, not fatal" contract every
other adapter's telemetry already gets) - so `GoProAdapter` needed no
special-casing here at all. At the time this was written, non-video
files (photos, screenshots) were excluded by `video_extensions`
matching, same as `FolderAdapter`; photos are now scanned in deliberately
(see "Photo support" below) - a genuine screenshot/junk-file exclusion
still holds, just no longer for every still image. `test_gopro_adapter.py`'s
`test_mixed_content_folder_scans_fully_with_per_recording_telemetry_degradation`
exercises this directly: a real GPMF-shaped clip, a video with no GPMF
track, a photo, and a text file in one folder - the scan returns both
videos, the clean one's telemetry reads normally via
`read_recording_gps()`/`read_recording_gsensor()`, the other's reads back
as `()` rather than raising.

**Tests.** `test_gopro_gpmf.py` (12 tests) - `locate_gpmf_stream()`
against a hand-built, minimal-but-real synthetic MP4 (real `moov`/`stbl`
box structure, real KLV-encoded `DEVC` samples in `mdat` - no ffmpeg
needed to build it, see that file's own module docstring), covering the
happy path, no-`gpmd`-track, and non-MP4-file error cases;
`extract_gps_fixes()`/`extract_gsensor_samples()` against raw GPMF bytes
directly, covering scaled-value decoding, block-to-block/row-to-row
timestamp interpolation, `GPSF`-gated invalidity, and a block with no
GPS5/ACCL stream at all. `test_gopro_adapter.py` (10 tests) - manifest/
registration sanity, a spot-check of the shared scan path (full coverage
already lives in `test_folder_adapter.py`), real end-to-end
`read_gps()`/`read_gsensor()` against a synthetic GPMF video, the
capability guards for `connect()`/`config_snapshot_seconds()`, and the
mixed-content-folder degradation test described above. Also added:
`test_gopro_manifest_loads()` to `test_manifest.py`,
`test_gopro_is_registered_by_default()`/
`test_get_adapter_returns_a_gopro_adapter_instance()` to
`test_registry.py`. `bv-config`'s own test file needed a small
mechanical update - `registered_adapter_ids()` is sorted, so the
wizard's "Adapter (blackvue/folder): " prompt text became "Adapter
(blackvue/folder/gopro): " once `gopro` registered; every scripted-ask
dict in `test_bv_config.py` referencing that literal string was updated
to match (22/22 tests still pass).

Not yet done: testing against Christer's real `X:\gopro` footage (#906)
- GPMF's exact on-disk shape is camera/firmware-dependent (GPS5 vs.
GPS9, which fields are actually populated, real `SCAL`/`GPSU` framing)
and worth confirming against a real file before trusting the synthetic-
fixture-only test suite above as the last word.

## GoPro adapter: tested against real footage, TICK fallback fix (2026-08-16)

Step #906. This sandbox has no access to `X:\gopro`, so Christer copied
four real sample clips (Hero5, Hero6, "Karma" - a Karma drone's onboard
camera, and a Max in HERO mode) into a git-ignored `.sample_footage/`
scratch folder in the repo root (`/.sample_footage/` added to
`.gitignore` - never meant to be committed, real personal footage).
`GoProAdapter`/`gpmf.py` were run directly against these, then through
the full `open_archive()`/`telemetry_bridge.read_recording_gps()`/
`read_recording_gsensor()` path end to end.

**Confirmed working as designed:** all four clips' `moov`/`gpmd`-track/
sample-table shape parses cleanly via the pure-Python box walker; GPS5
decodes correctly on the three clips that have it (Hero5: 618 fixes,
Hero6: 417, Max: 191 - lat/lon/speed values sane, e.g. Hero5's opening
fixes cluster around a fixed point as expected for a stationary start).
The Karma clip has zero GPS5 fixes - not a bug: its GPMF stream is
structured as two devices, a `"Camera"` DEVC (image sensor telemetry
only - `ACCL`/`GYRO`/`ISOG`/`SHUT`, no GPS at all) and a
`"GoPro Karma v1.0"` DEVC (the drone controller's own telemetry -
`GPRI`/`ATTD`/`GLPI`/`BPOS`/etc., an entirely different, drone-specific
FourCC vocabulary this module was never designed to parse). Correctly
falls through to "no GPS for this recording" via the existing
per-recording degradation contract rather than erroring - exactly
right, out of scope to chase further (Christer's own camera is a
Hero-series action cam, not a Karma drone controller).

**Real bug found and fixed: `extract_gsensor_samples()` required
`STMP`, but Hero5/Hero6/Karma's firmware never writes it.** All three
returned 0 g-sensor samples despite having real `ACCL` data present and
otherwise-parseable - only the Max (newer firmware) has `STMP` and
worked. Inspecting the older clips' raw KLV directly showed their
`ACCL` `STRM` blocks carry `TICK` (a millisecond-resolution, free-
running device-clock reading - confirmed identical whether read from
the `STRM` level or hoisted to the parent `DEVC`) instead of `STMP`
(GPMF's stream-relative microsecond timestamp). `extract_gsensor_samples()`
now falls back to `TICK` when `STMP` is absent: the first `TICK`-bearing
block in the stream anchors to offset zero (matching `STMP`'s own "time
since stream start" semantics), every later block's offset is
`(this block's TICK - anchor TICK)` converted to a `timedelta`. After
the fix, all three older clips now return real per-recording g-sensor
sample counts (Hero5: 6870, Hero6: 4667, Karma: 2397 - correctly still
under the `"Camera"` DEVC, unaffected by the GPS-vs-no-GPS distinction
above), sequential offsets increasing in step with each `DEVC` block's
`TICK` delta as expected. `test_gopro_gpmf.py` gained
`test_extract_gsensor_samples_falls_back_to_tick_when_stmp_is_missing`
(2 synthetic Hero5/6-style TICK-only blocks, no `STMP` at all),
confirming zero-anchored first-block offset and correct block-to-block
delta; full suite now 80/80 (was 79/79).

Verified end to end through `open_archive()` + `telemetry_bridge`'s
`read_recording_gps()`/`read_recording_gsensor()` (the real call path
every pipeline consumer uses, not just the raw `gpmf` module): all four
clips scan into recordings, GPS/g-sensor counts above reproduce through
that full path, and the Karma clip's GPS-less recording degrades to `()`
rather than raising - the mixed-content-folder contract holding for
real files, not just the synthetic fixture that already exercised it.

## bv-config wizard: real adapter selection (2026-08-16)

Closes the gap the GoPro design section above and the third-pass note
higher up both flagged: every camera config's `adapter` field had to be
hand-edited into the `.cfg` file after running `bv-config`, since the
wizard itself never asked. Now a real question, `cli/bv_config.py`'s
`run_wizard()`:

- New "Adapter (blackvue/folder): " prompt, right after Name and before
  Archive - listed from `adapters.registry.registered_adapter_ids()`
  (so it grows automatically once `gopro` registers itself in the
  implementation pass), validated against that same list with a
  reprompt-and-explain loop on an unrecognized answer (same shape as
  the existing Name-validation loop). Defaults to the existing config's
  own `adapter` when editing (so re-running the wizard never silently
  changes it - consistent with every other field's own default
  behavior) and to `DEFAULT_ADAPTER_ID` ("blackvue") for a brand-new
  config, matching `CameraConfig.adapter`'s own dataclass default.
- Network endpoints are now conditional on the chosen adapter's own
  manifest: `load_adapter_manifest(adapter_id).requires_network` gates
  whether `edit_endpoints()` is even called. A folder/gopro-style
  camera has nothing to connect to, so the wizard no longer asks
  "Endpoint 1 address?" for one - it prints a one-line "Adapter
  '<id>' doesn't use network endpoints - skipping endpoint setup."
  instead. An existing config's endpoints, if any, are left untouched
  rather than cleared when switched to a non-network adapter -
  harmless unused data if it stays that way, still there unmodified if
  switched back.
- `CameraConfig(..., adapter=adapter_id)` - the wizard's own answer now
  actually reaches the saved config, instead of every config silently
  keeping whatever `CameraConfig.adapter`'s dataclass default was.

Files: `src/blackvue/cli/bv_config.py`, `src/blackvue/core/
camera_config.py` (refreshed the `adapter` field's own docstring, which
had gone stale describing a registry that exists now), `tests/blackvue/
cli/test_bv_config.py` (all 9 existing `run_wizard()`-driving tests
updated for the new prompt in their scripted-ask scripts; 5 new tests:
unknown-adapter reprompt, endpoint setup skipped for a non-network
adapter, existing endpoints preserved-not-cleared when switched to one,
adapter defaults to the existing config's own value on edit, plus an
explicit `config.adapter` assertion on the main new-config test).

Verified: all 22 functions in `test_bv_config.py` (17 existing + 5 new)
confirmed passing via a standalone harness (a fake `pytest` shim
providing `raises`/`fixture`/`approx`, a fake `monkeypatch` fixture
object, and `tempfile.mkdtemp()` for `tmp_path` - no real pytest in
this sandbox, see this file's own "Verified" notes elsewhere and
WORKING_CONTEXT.md's standing note on why); `py_compile` on both
changed source modules.

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
8. ~~Make `bv-download --sdcard` adapter-aware, so a GoPro-configured
   camera's real on-camera filenames (`GH010123.MP4`) are recognized
   too~~ - **done** (2026-08-16). Step 6 above shipped `--sdcard`
   BlackVue-only - `SdCardCamera`'s recognizer was the strict
   `YYYYMMDD_HHMMSS_KD.ext` filename regex, matching nothing on
   Christer's real GoPro card (`bv-download GP --sdcard X:\SD_card`
   found "0 BlackVue-named recordings" despite 19 real files).
   `core/sdcard_camera.py`'s `_scan()` now takes an optional
   `AdapterManifest`: given one, it switches to a generic recognizer
   (`_matches_generic_video()` - extension match against the
   manifest's `video_extensions`, since GoPro's own filenames carry no
   timestamp) with each match's capture time read from the file's
   mtime rather than parsed from its name. `cli/bv_download.py`'s
   `_run()` reads the target camera's `CameraConfig.adapter` and picks
   the recognizer accordingly - byte-identical `SdCardCamera(root)`
   call for BlackVue/default, `SdCardCamera(root, manifest=...)` for
   anything else. Two more BlackVue-shaped assumptions had to give
   too: `domain.Recording.kind` unconditionally did
   `self.id.rsplit("_", 1)[1]`, which raised `IndexError` for an
   underscore-less id like `GH010123` - now returns `""` instead. And
   the default download-selection logic (`select_by_mode()`/
   `select_by_context()`, plus `TimeInterval.__contains__()`'s lexical
   date-range check) is entirely BlackVue-kind/timestamp-shaped, so a
   generic/manifest-driven `--sdcard` import now bypasses both:
   every recognized file is included and downloads unconditionally,
   matching `gopro/manifest.json`'s own "no --mode filtering... every
   file is just 'a video'" contract. (The interval bypass mattered in
   practice - GoPro ids like `GH010123` sort lexically *after* the
   default range's `99991231_235959` upper bound, since `'G' > '9'`,
   so without it every recording was silently filtered out even after
   filename recognition was fixed.) 3 new tests in
   `tests/blackvue/cli/test_bv_download.py`, 9 new tests in
   `tests/blackvue/core/test_sdcard_camera.py`, 2 new tests in
   `tests/blackvue/domain/test_recording.py`.
9. Build further adapter variants (drone footage, ...) as real
   need/footage shows up, informed by whatever steps 4-8 taught about the
   interface.
10. Build `bv-analyze` (sketched below) once at least two real adapters
   exist to test it against - an inference tool tuned against a single
   example (BlackVue) risks just re-deriving BlackVue's own pattern rather
   than genuinely generalizing.

## `--sdcard` renamed to `--media` (2026-08-16)

Christer: "May be SD card should be renamed to external source since many
cameras support a usb connection." Steps 6 and 8 above shipped and
extended `bv-download --sdcard`, but by step 8 the flag already covered
non-SD-card sources too - a USB-connected GoPro exposing itself as mass
storage, not literally an SD card in a reader. Renamed the flag to
`--media` (and `core/sdcard_camera.py` to `core/media_camera.py`,
`SdCardCamera` to `MediaCamera`) to match what it actually covers.
`--sdcard` is kept working as a hidden alias (`help=argparse.SUPPRESS`
in `cli/bv_download.py`'s `parse_args()`) for the same `dest="media"`,
since it was a documented, released (v1.0.0) flag name - existing
scripts or muscle memory typing `--sdcard` still work, just no longer
shown in `--help`. Pure rename otherwise - no behavior change to the
scan/download/RecordTime-capture logic described in steps 6 and 8.
`docs/man/bv-download.md` rewritten for `--media` as primary, noting the
alias.

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

## bv-generate wired through the adapter registry (2026-08-17)

Christer ran `bv-generate gp --describe-scene --get-duration
--extract-audio --srt --transcribe` against his real GoPro archive
(`X:\gopro\archive`, camera config `gp` with `adapter="gopro"`) and got
`no recordings found in range, nothing to do` for every single
recording - not a GPS-specific gap, a total failure. Root cause:
`cli/bv_generate.py`'s `_run()` constructed the raw, BlackVue-only
`archive.Archive` class directly instead of going through
`registry.get_adapter(adapter_id).open_archive()` the way `bv-ls` was
already fixed to do (see "`bv-ls` wired through the adapter
abstraction" above, back when `bv-ls` was the only command with this
gap). `archive.Archive`'s underlying `ArchiveReader.read()` requires
BlackVue's literal `YYYYMMDD_HHMMSS_K` filename shape -
`RecordingId.parse()` returns `None`, and the file is silently
skipped, for anything else - so a GoPro's own on-camera names like
`GH010123.MP4` never matched, and every recording in a non-BlackVue
archive was invisible before the "no recordings" range check even ran.

This gap existed only in `bv_generate.py`; `bv-search` and `bv-export`
(step 7 above) had already been rewired for GPS/g-sensor reads via
`telemetry_bridge.py`, but nobody had gone back and checked whether
every *other* command that scans an archive - `bv-generate` chief
among them, since it's the command that actually calls
`--describe-scene`/`--get-duration`/etc. - was wired through the
registry at all. It wasn't.

Fix: `_run()` now calls `registry.get_adapter(adapter_id).open_archive
(archive_path)`, with `adapter_id` taken from `camera_config.adapter`
when `args.path` resolved through a configured camera, falling back to
`DEFAULT_ADAPTER_ID` ("blackvue") for a literal path - same pattern as
`bv-ls`. This gives `GoProAdapter.open_archive()` (and `FolderAdapter`'s,
for a plain `adapter="folder"` config) a chance to assign each file a
synthetic BlackVue-shaped id from its own resolved timestamp
(`adapters/_recursive_scan.py`'s `assign_recording_ids()`) - already
sortable by the interval filter and safe for `recording.id.is_parking`,
with no further changes needed anywhere else in `bv_generate.py`.

Regression test (`test_main_resolves_a_camera_id_to_its_configured_folder_adapter`
in `tests/blackvue/cli/test_bv_generate.py`) mirrors `bv-ls`'s own
equivalent test: a camera config with `adapter="folder"` pointed at a
recursively-nested, arbitrarily-named video file, run end-to-end through
`main()` with `--get-duration`, asserting the recording is actually
found and processed (not "no recordings found"). Full suite: 103/103 in
`test_bv_generate.py` (up from 102; the 6 unrelated `monkeypatch.setattr
("builtins.input", ...)` failures are a pre-existing fake-pytest harness
limitation, confirmed present before this change too via `git stash`).

## Three GoPro follow-up fixes: generated-asset visibility, id stability, source filenames (2026-08-17)

Christer reported three related problems in one message after actually
running `bv-generate`/`bv-ls` against his real GoPro archive: bv-generate's
own output files (audio/duration/transcript/scene description) weren't
showing up in `bv-ls`'s asset columns even though they existed on disk;
some synthesized recording ids reflect when a GoPro clip was downloaded
onto his machine rather than when it was actually recorded, risking two
different physical clips colliding into the same id; and there was no way
to see a recording's real on-disk filename from `bv-ls` to notice or
untangle such a collision if it happened.

**Fix 1 - `generated_assets_for()` root-fallback
(`adapters/_recursive_scan.py`).** Root-caused as a path-convention
mismatch, not a wiring gap: every write site in `cli/bv_generate.py`
builds its destination as `archive_path / f"{recording.id}.<suffix>"` - a
flat path at the archive *root*, keyed by the synthetic recording id - but
`generated_assets_for()` only ever checked same-stem-next-to-the-original-
video, which for a GoPro archive is nested arbitrarily deep
(`archive_layout: "recursive"`). `FolderAdapter`'s own same-stem tests
never caught this because its existing tests only exercise the same-stem
path. Fixed by having the read side check both locations - same-stem
first (unchanged, still wins on the rare theoretical collision), then
`root / f"{recording_id}{suffix}"` - with `root`/`recording_id` threaded
through `_scan()`'s existing call site, no changes needed to
`bv_generate.py` itself. New regression tests in `test_folder_adapter.py`
(`test_root_id_named_generated_assets_are_discovered`,
`test_same_stem_generated_asset_wins_over_root_id_named_one`) cover both
adapters at once since they share this code path.

**Fix 2 - GPMF GPSU timestamp fallback (`adapters/gopro/gpmf.py`,
`_recursive_scan.py`, `gopro/adapter.py`, `gopro/manifest.json`).** Before
this fix, a GoPro recording's timestamp chain was `ffprobe creation_time`
-> file mtime, with no telemetry-aware middle tier - so a clip whose
`creation_time` tag was missing or stripped (a re-encode, a copy tool that
drops metadata) fell straight to mtime, which reflects when the file was
copied/downloaded onto Christer's machine, not when it was recorded.
GPMF's own `GPSU` field is a real device-clock UTC anchor written by the
camera every DEVC block *whether or not GPS actually had a lock that
second* - meaningfully truer than mtime even on footage with no real GPS
fix at all. Added `gpmf.first_creation_time(path) -> datetime | None`
(stops at the first block with a usable `GPSU`, unlike the full
`extract_gps_fixes()` walk) and threaded it through
`_resolve_timestamp()`/`_scan()`/`scan_recursive_archive()`/
`find_recording_in_recursive_archive()` as a new keyword-only
`telemetry_timestamp` hook, wired into `GoProAdapter.open_archive()`/
`find_recording()`. `FolderAdapter` passes nothing (`None`), so its
two-tier chain is unchanged - confirmed via its own 19/19 green run.
`manifest.json`'s `timestamp_source` gained the new
`"gpmf_gpsu_anchor"` middle entry (also added to
`manifest.schema.json`'s enum, doc-only since the schema isn't
runtime-validated). New tests: `test_gopro_gpmf.py` gained a
`first_creation_time()` section (first-DEVC-wins, no-GPMF-track ->
`None`, non-MP4 -> `None`, skips a GPS-less block to the next one);
`test_gopro_adapter.py` gained
`test_open_archive_prefers_gpmf_gpsu_anchor_over_file_mtime`. This fix
also exposed and fixed a stale assumption in the pre-existing
`test_mixed_content_folder_scans_fully_with_per_recording_telemetry_degradation`
test: it identified "the clip with telemetry" vs. "the clip without" by
sorted-id order, which implicitly assumed mtime order - now that the
telemetry clip's id can legitimately sort anywhere (its GPSU anchor no
longer has to agree with its mtime), the test identifies each recording
by its actual GPS-read result instead of by id order.

**Fix 3 - Source column in `bv-ls` (`cli/display_group.py`,
`cli/bv_ls.py`).** Added `DisplayGroup.source_label(root)` (real on-disk
`FRONT` filename, relative to the archive root when possible so two
same-named files in different subfolders - e.g. a GoPro card's
`100GOPRO/GH010001.MP4` and `101GOPRO/GH010001.MP4` - stay
distinguishable) and a new `_source_column_needed()` helper in
`bv_ls.py` that decides once per table whether the column is worth
showing at all: a BlackVue archive's filenames are themselves id-derived
(`20260715_133255_NF.mp4` for id `20260715_133255_N`), so showing this
column there would just repeat the Recording column on every row -
checked via "does the real filename start with the recording id string"
across every recording behind every row, without `bv_ls.py` needing to
import adapter-type metadata directly. Column is conditionally inserted
into both header rows and every data row, with its own width computed
alongside the existing Recording/asset/Size columns. New tests:
`test_bv_ls_shows_source_column_for_a_folder_adapter_archive`,
`test_bv_ls_hides_source_column_for_a_blackvue_archive` in
`test_bv_ls.py`.

**Verification.** `test_folder_adapter.py`: 19/19. `test_gopro_adapter.py`:
11/11. `test_gopro_gpmf.py`: 17/17. `test_bv_ls.py`: 22/24 (the 2
failures - `test_main_movement_flag_enables_gps_bridging`,
`test_trips_bridges_a_gap_when_gps_shows_movement_and_movement_flag_given`
- are a pre-existing, unrelated `movement_bridges_gap() missing 1
required keyword-only argument: 'adapter'` bug in `trip_builder.py`,
confirmed present before any of these changes too via `git stash`/`git
stash pop`, left untouched as out of scope).

## Photo support

Christer's own framing, verbatim: "yes i have photos, my thought is that
in a trip they should be shown for a specified time that defaults to 5
second. If i want to play with words i would say a picture is also a
video, but 1 frame only. ;*" - taken literally as the design: a photo
gets no new `Asset` enum member, no new `RecordingId` kind code. It is
stored under `Asset.FRONT` exactly like a real video, and the rest of the
pipeline treats it as an ordinary FRONT-only recording with no REAR/no
AUDIO - a case every layer already handles for a front-only camera
setup. The only new primitive is "is this file a photo," answered purely
by file extension.

**`archive/photo.py`** (new module) - `PHOTO_EXTENSIONS` (`.jpg`,
`.jpeg`, `.png`, `.heic`, `.gpr` - Christer's own answer when asked which
extensions should count was "all of them"), `DEFAULT_PHOTO_DURATION_SECONDS
= 5`, `is_photo_path(path)` (suffix match, case-insensitive), and
`recording_is_photo(recording)` (true iff the recording's FRONT asset
file's path is a photo path). Every other layer imports one of these two
functions rather than re-implementing extension matching.

**Scanning.** `_recursive_scan.py` - shared by `FolderAdapter` and
`GoProAdapter` only, never `BlackVueAdapter` - now scans
`manifest.video_extensions | PHOTO_EXTENSIONS`, so a photo rides through
the exact same timestamp-resolution/id-assignment/same-stem-asset-
discovery path a video does and lands in `Asset.FRONT` the same way. This
one-line change is what scopes photo support to GoPro + folder archives
only, matching Christer's answer ("GoPro + folder") without a
manifest-level opt-in flag.

**Duration.** `generate/media.py`'s new `photo_aware_duration(inner,
*, photo_duration_seconds=5)` wraps any `RecordingDuration` callable
(`read_duration_seconds`, `load_or_compute_duration`) and intercepts
photo recordings before they ever reach ffprobe/the box-reader fallback,
returning the fixed duration directly. Wired into `bv-ls`'s
`--duration`/`--full` and `bv-export`'s duration-driven trip detection
(both the plain and `--duration-heal-archive` variants).

**Rendering.** `export/media.py`'s new `render_image_as_video(source_image,
destination, duration_seconds, *, width, height, fps)` shells out to
ffmpeg (`-loop 1 -framerate fps -i photo -t duration -vf scale+pad`) -
not PIL, since PIL doesn't universally read HEIC, and ffmpeg is already a
hard dependency everywhere else in the pipeline. Scale+pad (letterbox),
not crop, so nothing the user chose to keep in frame gets cut off; sized
against the trip's own real video dimensions/frame rate when one exists
in the trip (so the encoded clip splices into `front.mp4` without a
scale step at concat time), falling back to the photo's own pixel
dimensions when the trip is all-photo.

**Splicing into the concat pipeline.** `trip_export.py`'s new
`_photo_clip_overrides(trip, work_dir, warnings, log, *,
photo_duration_seconds)` renders every photo recording's clip and returns
a `dict[(RecordingId, Asset), Path]` - the exact same shape
`_repair_parking_sources()`, `_apply_parking_speed()`, and
`_align_front_rear_durations()` already produce, merged last into
`export_trip()`'s combined `duration_overrides` map. No new splicing
mechanism was needed - a photo clip is just another override the
existing concat pipeline substitutes in transparently. A photo that fails
to render (a corrupt/unreadable image) is warned about and left out of
`front.mp4` entirely, same failure contract as a corrupted video source.

**CLI.** `bv-export --photo-duration SECONDS` (default 5, must be > 0)
threads `photo_duration_seconds` through `bv_export()` down to both the
duration callback and `_photo_clip_overrides()`.

**bv-generate.** `_do_extract_audio()`/`_do_transcribe_and_translate()`
both gained a `recording_is_photo()` guard, right alongside the existing
Parking-mode guard, printing "photo has no audio, skipping" and doing
nothing rather than trying to extract/transcribe silence from a still
image. `--describe-scene` was deliberately left untouched - it calls
`describe_scene(video_path, ...)` directly with no Parking-style
video-repair detour, so it runs on a photo's own FRONT file unmodified;
a vision-language model has no trouble describing a still photo.

**Archive-browser thumbnail.** `web/archive_browser.py`'s
`ArchiveRecording.thumbnail_path("front")` falls back to the photo's own
FRONT file when no `*_THUMBNAIL` sidecar exists for it - true for every
photo today, since nothing generates one - rather than trying to
ffmpeg-extract a frame from what's already a still image.

**Tests.** `test_photo.py` (9 tests, new file) - `is_photo_path()`/
`recording_is_photo()` covering all five extensions, case-insensitivity,
non-photo extensions, and the no-FRONT-asset edge case.
`test_folder_adapter.py` gained a photo-scanning section (6 tests) -
photos alongside videos, `Asset.FRONT` storage, all five extensions,
`recording_is_photo()` true/false, and the `"V"` kind code (unchanged -
no new `RecordingId` kind). `test_media.py` gained 3 tests for
`photo_aware_duration()` (default, custom seconds, delegates to inner for
a real video). `test_export_media.py` gained 3 tests for
`render_image_as_video()` (exact duration/size, letterbox on a mismatched
aspect ratio, raises `MediaToolError` when ffmpeg itself is missing).
`test_trip_export.py` gained 5 tests for `_photo_clip_overrides()` (empty
trip, sized against a real video, configured duration, falls back to the
photo's own size when the trip is all-photo, warns-and-skips a render
failure). `test_bv_generate.py` gained 2 tests mirroring the existing
Parking-mode skip tests, for the extract-audio and transcribe/translate
guards. `test_archive_browser.py` gained 3 tests for the thumbnail
fallback (falls back to the photo, a real `*_THUMBNAIL` sidecar still
wins if one exists, "rear" never falls back since the mechanism is
front-only).

## GIF classification and EXIF metadata

Christer, following up on photo support: "Exactly, how do you define a
gif file, a picture or a silent video? Maybe we need exif now." Two
independent additions on top of the "Photo support" section above,
both scoped to `FolderAdapter`/`GoProAdapter` the same way photo
support is.

**GIF: animated vs. static.** A `.gif` can genuinely be either - an
animated GIF already has its own real per-frame timing baked in, so
it's treated as an ordinary silent video; a static, single-frame GIF
is a photo, held for `--photo-duration` like any other still. Extension
alone can't tell the two apart, so `.gif` is deliberately kept out of
`PHOTO_EXTENSIONS` and given its own `GIF_EXTENSIONS = {".gif"}` in
`archive/photo.py`, scanned in alongside `PHOTO_EXTENSIONS` in
`_recursive_scan.py`'s extension set. The actual classification is a
real ffprobe call: `count_gif_frames(path)` (`archive/photo.py`) runs
`ffprobe -select_streams v:0 -count_frames -show_entries
stream=nb_read_frames` and returns the frame count (or `None` if
ffprobe fails/is missing). `recording_is_photo(recording)` now checks
`is_photo_path()` first (unchanged, for jpg/png/heic/gpr), then falls
through to `is_gif_path()` + `count_gif_frames(...) == 1` for a `.gif`
FRONT - a GIF whose frame count can't be determined at all is treated
as `False` (an ordinary video), the same conservative default the rest
of the export pipeline's corrupted-source handling already relies on,
rather than risk mis-classifying an unreadable file as a photo it was
never confirmed to be.

Rendering a static GIF can't reuse `render_image_as_video()`'s `-loop 1`
approach directly: a `.gif` suffix makes ffmpeg pick its native "gif"
demuxer instead of "image2", and that demuxer doesn't support `-loop`
(`ffmpeg -loop 1 -i x.gif` fails outright with "Option loop not
found."). `export/media.py`'s new `extract_first_frame(source_gif,
destination)` sidesteps this: `ffmpeg -i source.gif -frames:v 1
dest.png` pulls frame 0 out to a plain PNG first, which
`render_image_as_video()` then renders exactly like any other photo,
with no gif-specific branch of its own. `trip_export.py`'s
`_photo_clip_overrides()` calls `extract_first_frame()` when the FRONT
path is a `.gif`, before the (also new) EXIF-orientation step below.

**EXIF: timestamp, orientation, GPS.** All three read via Pillow
(`archive/exif.py`, new module) - already a base, non-optional
dependency, so no new install requirement. Every function degrades to
"nothing found" (`None`/`False`) rather than raising on a file with no
EXIF block, a format Pillow can't open at all (HEIC/GPR need the
optional `pillow-heif`/`rawpy` plugins, neither a project dependency),
or a corrupt file - the same "missing telemetry is absent, not fatal"
policy already applied to GPS/g-sensor reads.

- `exif_datetime_original(path)` reads tag 36867 (EXIF's own
  `"YYYY:MM:DD HH:MM:SS"` format). Wired into
  `_recursive_scan.py`'s `_resolve_timestamp()` as the *first* source
  tried for a photo path (ahead of even ffprobe's own `creation_time`,
  which is an unreliable, inconsistent source for a still image) -
  falling through to the existing ffprobe/telemetry/mtime chain
  unchanged if the photo has no usable EXIF.
- `normalize_photo_orientation(source, destination)` bakes a photo's
  EXIF Orientation tag into its actual pixel data via
  `PIL.ImageOps.exif_transpose()`, since ffmpeg does not auto-rotate
  EXIF-oriented image input on its own (confirmed directly: the exact
  `-loop 1`/`scale,pad` command `render_image_as_video()` uses decodes
  a portrait, Orientation=6 JPEG as a raw, unrotated landscape frame -
  a portrait phone photo would otherwise render sideways in the
  exported clip with no warning). Returns `True` only when a real,
  non-identity correction was written to `destination`; `trip_export.py`'s
  `_photo_clip_overrides()` calls this on every photo (including a
  gif-extracted PNG frame) right before `render_image_as_video()`,
  using the corrected file when one was written and the original
  otherwise.
- `exif_gps_fix(path, *, timestamp)` reads the GPS sub-IFD (tag 34853 -
  note `exif.get_ifd(34853)` is required, not `exif.get(34853)`, which
  only returns a raw IFD pointer in Pillow's API) and converts the
  degrees/minutes/seconds tuples to signed decimal degrees, returning a
  `telemetry.gps_reader.GpsFix` (`valid=True`, `speed_kmh`/`course`
  always `None` - a still photo is a single instant, nothing to compute
  motion from). `timestamp` is the caller's own already-resolved
  recording timestamp, reused as-is rather than re-derived from the GPS
  sub-IFD's own date/time tags. Wired into `web/app.py`'s
  `/archive/{camera_id}/{recording_id}/location` route: when
  `recording_has_gps()` is `False` and `recording_is_photo()` is
  `True`, the route tries `exif_gps_fix()` on the photo's FRONT path and,
  if found, uses the single fix for both the start and stop location
  display (a photo has no separate start/stop - it's one instant).

**Tests.** `test_photo.py` gained a GIF-classification section (8
tests) - `is_gif_path()`, `GIF_EXTENSIONS`, `count_gif_frames()` on
real static/animated/corrupt GIF fixtures, and `recording_is_photo()`
across all three. `test_exif.py` (new file, 15 tests) covers
`read_exif()`, `exif_datetime_original()`, `exif_gps_fix()` (DMS
conversion, south/west sign, missing-IFD/missing-EXIF), and
`normalize_photo_orientation()` (real rotation, already-normal
orientation, no tag, unreadable file) - all against real EXIF written
via Pillow's own `Image.Exif`/`save(exif=...)` round trip, not
hand-rolled bytes. `test_export_media.py` gained 4 tests for
`extract_first_frame()`. `test_folder_adapter.py` gained 2 tests for
EXIF-preferred photo timestamps (prefers a real DateTimeOriginal tag
over a deliberately different mtime; falls back to mtime unchanged
without EXIF). `test_trip_export.py` gained 2 `_photo_clip_overrides()`
tests - a static GIF rendering end-to-end via frame extraction, and an
EXIF-oriented photo rendering without error.

## Timestamp reliability tracking + TripBuilder isolation (2026-08-17)

Christer's own report, verbatim: "The problem is the following from
bv-ls\n20260816_144130_V\n20260816_144131_V\n20260816_144150_V\n20260816_144151_V\nWe
need to get correct created timstamp instead of downloaded timestamp.
Otherwise the will go on a trip together." A real `ffprobe -v error
-show_format -show_streams` dump on the underlying source files
(`13532784_1080_1920_60fps.mp4` and siblings, in the `GP` archive)
confirmed there was no bug in `_resolve_timestamp()` itself - these files
have no `creation_time` tag anywhere (FORMAT or STREAM level), only a
GPS `location` tag and `encoder=Lavc60.31.102 libx264` /
`Lavf60.16.100` tags showing they'd been re-encoded at some point - so
the mtime fallback was already firing correctly. The real defect was
downstream: `TripBuilder` treated a meaningless mtime-derived timestamp
identically to a trustworthy device-clock one. mtime reflects when a
file was last *written* to disk, not when it was recorded - for a
batch-downloaded folder of unrelated stock/sample clips, that's purely a
function of when the copy happened, so unrelated recordings can land a
second apart and get silently merged into one fake "trip."

Presented Christer with three fix approaches (fix mtime resolution
somehow, drop these files from grouping entirely, or track per-recording
confidence and only exclude unreliable ones from auto-grouping); he
picked the third, recommended option.

**Implementation.** `_resolve_timestamp()` in `adapters/_recursive_scan.py`
now returns a new `ResolvedTimestamp(value: datetime, reliable: bool)`
`NamedTuple` instead of a bare `datetime` - `reliable` is `True` for EXIF,
container `creation_time`, or adapter telemetry (GoPro's GPMF `GPSU`
anchor), and `False` only for the final mtime-fallback branch. `_scan()`
threads `reliable` into a new `Recording.timestamp_reliable: bool = True`
field (`archive/recording.py`) - always `True` for a BlackVue archive,
since its timestamp is encoded in the camera's own device-clock filename.
`TripBuilder.build()` (`trip/trip_builder.py`) gained an always-on check
(unlike the opt-in `max_parking_duration` cap) that force-splits a trip
whenever either the previous or current recording has
`timestamp_reliable=False`, checked before the ordinary gap/bridge logic
and never offered to the `bridge` callback - a gap measurement built from
a meaningless mtime value gives movement-bridging evidence nothing real
to weigh in on, same reasoning as the parking cap's forced split. Each
isolated recording still stands as its own single-recording trip and
remains fully usable via `bv-export --target`; it's just never silently
merged with a neighbor. Checked via `getattr(recording, "timestamp_reliable",
True)` throughout, so any recording (real or a minimal test double) that
doesn't define the attribute at all is treated as reliable - unaffected,
exactly as if the check didn't exist.

**Tests.** `test_trip_builder.py` gained a 7-test "timestamp_reliable
isolation" section: two unreliable recordings a second apart never group
even within the gap threshold; an unreliable recording alone still forces
a split on both sides; reliable clusters on either side of a single
unreliable recording still group normally among themselves; the split is
never offered to `bridge` even when `bridge` returns `True`; the split
reason names the culprit recording; and the pre-existing minimal
`FakeRecording` test double (no `timestamp_reliable` attribute at all)
still groups normally, confirming the `getattr(..., True)` default.
`test_folder_adapter.py` gained 2 tests: a plain data file with no
metadata falls back to mtime and is flagged `timestamp_reliable=False`;
a real ffmpeg-encoded fixture with a real embedded `creation_time` tag is
flagged `timestamp_reliable=True`. Full regression sweep: `test_trip_builder.py`
48/48, `test_folder_adapter.py` 29/29, `test_recording.py` 5/5,
`test_gopro_gpmf.py` 17/17, `test_gopro_adapter.py` 10/11,
`test_trip_export.py` 173/175, `test_trip.py` 12/12 - 342 passed, 3
pre-existing/unrelated failures (a stale test-comment assumption in
`test_gopro_adapter.py` predating photo support, and two unrelated
trip-summary export tests in `test_trip_export.py`), zero new
regressions.

Files changed: `src/blackvue/archive/recording.py`,
`src/blackvue/adapters/_recursive_scan.py`, `src/blackvue/trip/trip_builder.py`,
`tests/blackvue/trip/test_trip_builder.py`,
`tests/blackvue/adapters/test_folder_adapter.py`.

## Container-tag GPS fallback + photo scene description (2026-08-17)

Two follow-up reports from Christer on the same real ffprobe dump
(the stock/downloaded clips mixed into his GoPro `GP` archive that
motivated the timestamp-reliability feature above). Verbatim:

    This looks like gps coordinates
    TAG:location-{=+05.0448-073.7965/
    TAG:location=+05.0448-073.7965/
    not found by bv-generate.
    pictures dont get scene asset

**Fix 1 - container-tag GPS fallback
(`archive/container_gps.py`, new; `web/app.py`).** He's right - that's
a real single-point GPS fix in ISO 6709's "typical" representation (a
signed latitude, a signed longitude, an optional signed altitude, no
separators, a trailing `/`), muxed into the container by whatever
tool originally produced or re-encoded the file. Nothing read it.
Added `container_gps.py`, mirroring `archive/exif.py`'s `exif_gps_fix()`
closely: `_probe_container_location()` reads the ffprobe
`format_tags=location` entry and parses it via a digit-width-agnostic
signed-number regex (`[+-]\d+(?:\.\d+)?`) rather than a strict
fixed-width pattern, since ISO 6709 doesn't mandate one precision;
`container_location_fix(path, timestamp=...)` wraps the result as a
`telemetry.gps_reader.GpsFix` (`valid=True`, `speed_kmh`/`course`
always `None` - a container tag is one static point, not a track).
Wired into `web/app.py`'s `/archive/{camera_id}/{recording_id}/location`
route as a second fallback, checked after the existing EXIF-for-photo
fallback (task #957) when the adapter reports no GPS at all: a photo
tries EXIF first, then (if that also comes up empty, or the recording
is a real video) the container tag. Both fallbacks are single-point
fixes, so the same value now serves as both "start" and "stop" for
either source, not just the EXIF case.

**Fix 2 - photo scene description (`generate/scene.py`).**
`describe_scene()` built a `{"type": "video", ...}` message
unconditionally, for every input - so a photo recording's FRONT file
got handed straight to qwen_vl_utils' video-decoding path (decord),
which can't open a still image at all. `_do_describe_scene()`
(bv-generate) and `_describe_recording()` (bv-scribe) both pass
photo paths straight through with no photo-aware skip of their own
(unlike the audio/transcribe actions, which do skip photos - see task
#946 - scene description was always meant to run on photos too, a
vision model reading a still image being at least as sensible as
reading a video frame). Fixed at the shared root: `describe_scene()`
now branches on `archive/photo.py`'s `is_photo_path()` and builds a
real `{"type": "image", ...}` element instead, via a new
`_photo_as_pil_image()` helper - decodes through an `ffmpeg`
subprocess piped to a PNG in memory rather than handing the path to
PIL directly, since PIL doesn't reliably cover every
`PHOTO_EXTENSIONS` member (HEIC needs a plugin this project doesn't
ship; GPR is GoPro's own RAW format) - the same reasoning
`export/media.py`'s `render_image_as_video()` already uses ffmpeg
over PIL for. `_extract_full_res_frames()` (the zoom-into-signs
sub-pipeline's own frame source) got the same branch: a photo has no
timeline to sample, so it returns a single `(0.0, image)` entry via
`_photo_as_pil_image()` instead of reaching `decord.VideoReader` at
all. `crop_top`/`crop_bottom` overlay cropping (previously
video-tensor-only, via `_crop_top_bottom()`) now applies to the
decoded PIL image directly for a photo, via the same
`_crop_overlay_from_image()` the zoom pipeline already had.

**Tests.** `test_container_gps.py` (new, 6 tests, real ffmpeg
fixtures): reads back Christer's exact real tag shape
(`+05.0448-073.7965/`, no altitude); a tag with an altitude field;
returns `None` for a plain video with no tag and for an unreadable
file; `container_location_fix()` builds a valid single-point `GpsFix`
and returns `None` without a tag. `test_scene.py` gained a "photo
support" section (5 tests): `_photo_as_pil_image()` decodes a real
JPEG via ffmpeg and raises `MediaToolError` on an unreadable one;
`_extract_full_res_frames()` returns exactly one frame for a photo
without ever reaching decord (not even installed in this sandbox - a
`ModuleNotFoundError` here would mean the video branch was wrongly
taken); `describe_scene()` builds a real `"image"` content element
(a decoded PIL Image, not a path string) for a photo, and still
builds the original `"video"` element for an ordinary path - a
regression check. Full sweep: `test_container_gps.py` 6/6,
`test_scene.py` 29/29, `test_exif.py` 15/15, `test_photo.py` 18/18,
`test_archive_browser.py` 58/58, `test_bv_generate.py` 109/111 (2
pre-existing/unrelated - a sandbox-only `tomllib`-stub config-write
quirk, confirmed present before this session's changes too via `git
stash`), `test_bv_scribe.py` 25/26 (1 pre-existing/unrelated, same
cause), `test_trip_export.py` 173/175 (2 pre-existing/unrelated,
already documented above). `web/app.py` itself can't be imported in
this sandbox (no `fastapi` installed), so the route change was
verified via `ast.parse()` and manual review only, not a live
request.

Files changed: `src/blackvue/archive/container_gps.py` (new),
`src/blackvue/archive/__init__.py`, `src/blackvue/web/app.py`,
`src/blackvue/generate/scene.py`,
`tests/blackvue/archive/test_container_gps.py` (new),
`tests/blackvue/generate/test_scene.py`.

## See also

- `docs/ARCHITECTURE.md` - main project overview; documents the earlier,
  abandoned adapter framework this design deliberately avoids repeating.
- `src/blackvue/adapters/manifest.schema.json` - the formal JSON Schema.
- `src/blackvue/adapters/manifest.py` - the loader/validator.
- `src/blackvue/adapters/blackvue/manifest.json`,
  `src/blackvue/adapters/folder/manifest.json` - the two example manifests.
