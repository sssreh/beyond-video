# Changelog

## Unreleased

## [0.4.0] - 2026-08-09

Most of this release is `bv-generate`'s transcription path getting faster and
more reliable on real GPU hardware, plus a Ken-Burns rewrite that fixed a
20x render-time regression in `--map-intro`. A few smaller Whisper/CI fixes
round it out.

### Added

- `bv-generate`: opt-in Intel NPU (OpenVINO GenAI) Whisper backend
  (`--npu-model-dir`) for machines with an Intel Core Ultra-class NPU, as an
  alternative to the default faster-whisper/CTranslate2 path. Not yet
  confirmed working end-to-end on real NPU hardware.
- `bv-generate --model-size` now defaults to `large` automatically when a
  CUDA GPU is detected, `small` otherwise - a `large` model transcribes in
  about the same time a `small` one used to take on a real GPU, so the
  more accurate model is now the default for free. New `--cpu` flag forces
  CPU even on a GPU machine, e.g. to compare `--model-size small --cpu`
  against the GPU-default `large` run.
- `bv-export --map-intro`: opt-in flyover-intro clip before the main map
  video, with a widened OSM fetch so the wide establishing shot has real
  map data, and an optional title-card caption.
- `bv-export`: KML export for trip GPS tracks, so a trip opens directly in
  Google Earth without a format conversion step.
- `STORY.md`: the narrative history behind why this project exists.

### Changed

- `bv-export --parking-speed` range widened from 0.10x-5x to 0.10x-10x.
- GitHub Actions CI bumped off `actions/checkout@v4`/`actions/setup-python@v5`
  (both pinned to a now-forced, deprecation-warned Node 20 runtime) to `@v6`.

### Fixed

- `--map-intro`'s map-render phase was up to 20x slower than before its own
  wide-fetch feature shipped (16s -> 335s on a real trip). Root cause: the
  wider road/area pool the intro needed was being reused by every other map
  render on the export, not just the intro itself, and the intro redrew
  every frame from scratch instead of cropping one raster. Fixed by scoping
  the widened fetch to the intro only and rewriting the intro as a single
  high-res raster cropped per frame (a proper Ken-Burns zoom).
- faster-whisper's classic repetition-loop hallucination on dashcam audio
  (a garbled phrase repeating itself indefinitely) - non-speech road/wind/
  engine noise was being transcribed as speech and then feeding back into
  later segments. Fixed with voice-activity-detection filtering and
  disabling prior-text conditioning by default.
- A cuBLAS DLL search-path gap on Windows that could make faster-whisper's
  CUDA path fail with "Library cublas64_12.dll is not found or cannot be
  loaded" even with the right NVIDIA packages installed - pip installing
  them doesn't register their DLL directory anywhere Windows will look.
  Fixed by registering both `os.add_dll_directory()` and a `PATH` prepend
  for every found NVIDIA component directory.
- Intel NPU model-conversion instructions dropped an outdated
  `--disable-stateful` flag that's wrong for current (2025.1+) OpenVINO
  GenAI and caused a hard load-time failure.
- Trip-detail page light-theme readability (header, tabs, back-links, page
  headings) against the background photo.
- Username display now capitalized consistently in `bv-web`.

## [0.3.0] - 2026-08-08

`bv-web` grew from a read-only trip browser into a control panel for the
whole pipeline; `bv-export` gained playback-speed control and track-up map
rotation. Most of the rest of this release is real bugs Christer found
running the toolkit on his own trips, mostly around Parking-mode footage.

### Added

- `bv-web`: a job runner that can trigger every `bv-*` step from the
  browser - `bv-config`, `bv-download`, `bv-gps`, `bv-generate`,
  `bv-export`, and `bv-ls` - with live streaming output and a per-job
  Cancel button, not just browsing trips `bv-export` had already produced.
- `bv-web`: an archive browser for the raw `bv-download` output -
  thumbnail grid, mode and lexical time-range filters, a "show only with
  videos" toggle, a red-cross overlay on thumbnails missing their video,
  and a "Show start location" GPS link on trip/recording detail pages.
- `bv-web`: a welcome landing page (the trip list moved to `/trips`), a
  full-app background photo (light/dark matched pair) with a manual
  light/dark theme toggle, and a real Beyond Video logo/wordmark and
  favicon.
- `bv-web`: progressive disclosure on the `bv-export` job form -
  collapsible advanced sections so the common case isn't buried under
  every flag.
- `bv-export --parking-speed`: play Parking-mode footage back at an
  adjustable speed (0.10x-5x) instead of the camera's own real-time pace.
- `bv-export --map-track-up`: rotate `--map`/`--map-zoom`/`--stitch-map`'s
  panel so the vehicle's current heading always points "up," like a phone
  turn-by-turn app, instead of the default fixed north-up orientation.
  Opt-in - costs real extra render time on the whole-trip overview map
  specifically.
- `--prefix` (bv-export's output-filename prefix) now shown on
  `bv-web`'s Trips list and detail pages.

### Changed

- `bv-web` and `bv-cli`'s separate Docker images merged into one
  full-toolchain image, simplifying the split-machine (NAS + PC)
  deployment.
- `bv-web`'s archive browser and job-detail pages no longer rescan the
  whole archive on every request (new `ArchiveRecordingCache`/
  `CameraConfigCache`/`TripCache`) - multi-second page loads on a large
  archive are back to instant.
- `map_zoom_METERSm.mp4` now matches the trip's own front/rear video
  shape instead of always rendering square.
- The g-sensor graph's opt-in third axis moved from Z to X, matching this
  project's own axis-meaning relabeling; its `--stitch-graph` panel now
  defaults to a sensible side/orientation for `top_down` layouts even
  with no map panel present.

### Fixed

- A container quirk in the camera's own Parking-mode MP4s, repaired at
  download/export time rather than worked around only in the archive
  browser - fixes several related issues: Parking-mode recordings not
  playing back in the archive browser, and front.mp4 duration corruption,
  audio desync, and vanishing subtitles when Parking footage was
  concatenated into a trip export (`--include-parking`/`--parking-speed`).
- An O(frames x fixes) hang in the map-rendering phase (`--map-zoom`) on
  trips with a long stationary Parking span.
- Trip folder naming and `trip_info.txt` now match what's actually
  exported when leading/trailing Parking footage is excluded.
- A harmless "no configuration snapshot" warning no longer fires on
  every `bv-export` run.
- Light-theme readability on the Trips list and archive browser (both
  were hard to read against the new background photo in light mode).
- Several smaller `bv-web` fixes: stale job-detail pages after browser
  Back, an archive filter treating empty From/Until fields as set, a
  Docker container seeing zero cameras, the archive browser only being
  mounted for `bv-cli`, `adduser` writing outside the mounted volume, and
  the geocode cache directory living under a read-only mount.

## [0.2.0] - 2026-08-04

First release meant for other people to actually try, not just Christer's own
NAS. `CHANGELOG.md` was never kept up to date before this point (the project
went from initial commit to this release in about a month, entirely as
`## Unreleased` placeholder text), so rather than a diff against v0.1.0, this
entry summarizes the toolkit as it stands today.

### Added

- `bv-config`: interactive wizard to set up a camera (endpoints, archive
  location).
- `bv-download`: download recordings from a camera into a local archive,
  with resumable/incremental downloads, sidecar (`.gps`/`.3gf`/thumbnail)
  probing and self-healing, and a `--host`/`--target` quick-connect mode
  that skips full camera config.
- `bv-ls`: list and inspect an archive's recordings, with a `--trips` view
  grouping recordings into detected trips.
- `bv-generate`: derive assets per recording - audio extraction, real
  duration, transcript (faster-whisper, GPU with automatic CPU fallback),
  speaker diarization (pyannote.audio), translation (offline, via
  `bv-lang`-managed language packs), and SRT/LRC subtitles.
- `bv-export`: detect trips in an archive (GPS- and duration-aware, with
  fuzzy gap tolerance and a bounded `build_around()` search) and assemble
  each one into its own folder:
  - Video, GPX track, and a per-trip `trip_info.txt` (distance, avg/max
    speed, moving/idle time, size, reverse-geocoded start/end via
    Nominatim).
  - `--map`: an OSM-based route map video (road-type coloring, street
    labels, water/green areas, live position marker with a GPS-signal
    badge, optional zoomed follow-camera mode).
  - G-sensor overlays: a dot-gauge video and a scrolling strip-chart graph
    (`--gsensor-graph-video`), both axis-labelled and independently scaled.
  - `--stitch`: a single composited video combining front/rear camera,
    map panel, g-sensor panel, and burned-in subtitles, with configurable
    layout (including an auto-picked `rearview_mirror` layout with a real
    mirror-photo inset), resolution/bitrate/scale controls, and NVENC/NVDEC
    hardware encode with automatic CPU fallback.
  - `--include-parking`: skip-and-replace mid-trip parking footage instead
    of dropping it silently.
  - Automatic detection and trimming of the pre-record buffer on
    Event/Manual recordings, using g-sensor cross-correlation.
- `bv-gps`: one-shot live GPS reading from a camera.
- `bv-live`: a live browser dashboard per camera - live video, a scrolling
  live map, and a scrolling g-sensor strip, auto-opening in the user's
  default (or `--browser`-chosen) browser.
- `bv-web`: a small multi-user web app (FastAPI) for browsing trips that
  `bv-export` has already produced, with role-based access.
- Docker images and `docker-compose.yml` for running the pipeline and
  `bv-web` split across a NAS (always-on) and a PC (for heavier
  `bv-generate`/`bv-export` steps), or both on one machine.
- `scripts/scan_blackvue_endpoints.py`: a read-only endpoint scanner
  contributors can run against their own camera to help extend model
  support without needing physical hardware on Christer's end.
- Camera compatibility: a BlackVue DR900S-2CH tested through the full
  pipeline (download/export/live); an Elite 10 plus nine DR750X/DR770X/
  DR900X/DR970X models confirmed via endpoint scan (not yet full pipeline).
- `docs/man/`: reference documentation for every `bv-*` command;
  `docs/PIPELINE.md`, `docs/ARCHITECTURE.md`, `docs/WEB_ARCHITECTURE.md`,
  and `docs/DEPLOY.md` for the bigger picture.
- Basic CI (pytest/ruff/mypy on push and PR).

### Changed

- GPLv3 licensing and public-release cleanup (broken markdown fixes, dead
  files dropped, `CONTRIBUTING.md` and issue templates added).

### Fixed

- 3 pytest collection errors and a follow-on sibling-import break, both
  surfaced by CI's first-ever run against this codebase.
