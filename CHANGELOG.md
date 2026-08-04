# Changelog

## Unreleased

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
