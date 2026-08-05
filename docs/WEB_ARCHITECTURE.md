# bv-web architecture (side project)

`bv-web` is a small, separate side project from the main `bv-*` pipeline documented in `docs/ARCHITECTURE.md`: a login-protected web app for *browsing and watching* trips the pipeline's `bv-export` step already produced. It doesn't download from a camera, doesn't generate anything, and doesn't touch the source archive at all - it only ever reads `bv-export --target`'s own output folders. For the one piece that crosses over between the two projects, see "Relationship to bv-live" below.

## Why it's a separate project, not a feature of the pipeline

The pipeline (`bv-config` through `bv-export`, plus `bv-live`) is built to be run by one person, from their own machine, with no login at all. `bv-web` exists for a different situation entirely: letting other people - family members, say - see trips after the fact, from a browser, without shell access to the machine that ran the pipeline. That needs accounts, roles, and sessions, none of which the rest of the project has any use for - keeping it a separate top-level package (`blackvue.web`, not folded into `blackvue.export`/`blackvue.cli`) means none of that login machinery leaks into tools that were never meant to need it.

It also has its own, heavier optional-dependency group (`fastapi`, `uvicorn`, `python-multipart`, `Jinja2` - see `pyproject.toml`'s `web` extra) and, in deployment, its own Docker image (`Dockerfile`) separate from the pipeline's own (`Dockerfile.cli`) - see "Deployment" below.

## What it does today

Started as deliberately browse/watch only; a second increment (below) now also lets the owner trigger `bv-config` and `bv-gps` from the browser. Triggering `bv-download`/`bv-generate`/`bv-export` this way is still not part of this yet (see `WORKING_CONTEXT.md`) - the job-runner infrastructure below was built with those in mind, but only bv-config/bv-gps are actually wired up so far.

- **Login** - username/password, session cookie. Two roles: `owner` (currently just Christer) and `viewer` (everyone else). Browsing/watching trips and the raw archive works the same for both roles; triggering a job (below) is `owner`-only.
- **Trip list** - every trip folder under the configured `--target` directory, scanned fresh on every request (not cached), so a trip `bv-export` finishes writing while `bv-web` is already running shows up without a restart.
- **Trip detail** - video playback (range-request support comes for free from Starlette's own `FileResponse`, so seeking/scrubbing works), plus GPX/SRT/LRC download links, whichever of those a given trip actually has (a trip only has the files the `bv-export` run that produced it actually asked for - no `map.mp4` without `--map`, etc.).
- **Archive browser** - the raw per-camera archive `bv-download` actually writes to disk, before `bv-export` groups anything into trips: a camera picker, a day-grouped thumbnail grid per camera, and a per-recording detail page. See "Archive browser" below for how.
- **Jobs (owner-only)** - the owner can trigger `bv-config` (set up or edit a camera) or `bv-gps` (get a live GPS fix, against an already-configured camera id only) as a background job from the browser, watch its output, answer `bv-config`'s wizard questions as they come up, and cancel a job that's running or stuck. See "Job runner" below for how.

## Archive browser

Trips only exist once `bv-export` has processed them; the archive browser shows what's on disk before that - every raw recording `bv-download` has already saved for a camera, with a thumbnail per recording so a long archive is easy to scan visually. `web/archive_browser.py` (`ArchiveRecording`, `scan_archive()`, `find_recording()`, `group_by_day()`) is deliberately thin, the same way `trips.py` is thin: it reuses `blackvue.archive.Archive`/`ArchiveReader` - the exact same reader `bv-ls`/`bv-export` already use to enumerate recordings from a flat, unstructured archive directory - rather than adding any new disk-scanning logic of its own.

A camera id in a URL (`/archive/{camera_id}`) is resolved to its archive directory via `CameraConfig.target` - the directory `bv-download` writes raw recordings to. This is a different directory from `bv-export --target`/`app.state.target` (the trips directory `trips.py` reads) - a camera id names a *source* archive, a trip id names *processed* output. Don't confuse the two; `app.py`'s `_find_camera_archive()` helper is the only place this resolution happens, precisely to avoid that mix-up spreading across routes.

The camera list (`/archive`) reuses `_camera_options()` - the same pick-list `bv-config`/`bv-gps`'s job forms already build (see "Camera pick-list" below). The per-camera page (`/archive/{camera_id}`) groups recordings by calendar day, newest day and newest recording first, and shows each recording's thumbnail if it has one - real `.thm` files `bv-download` already downloads (see the sidecar-probing feature in `WORKING_CONTEXT.md`), preferring front, then rear, then interior. A recording with no thumbnail at all (an older archive, or a camera/firmware that doesn't serve `.thm` files) shows a plain "No thumbnail" placeholder instead of breaking the grid - thumbnail presence was never guaranteed, and there's no ffmpeg-based thumbnail generation anywhere in this project to fall back to. The detail page (`/archive/{camera_id}/{recording_id}`) plays the recording's best available video and links every file it actually has (front/rear/interior video, GPS/g-sensor sidecars).

Two file-serving routes (`/archive/{camera_id}/{recording_id}/thumbnail/{direction}`, `/archive/{camera_id}/{recording_id}/files/{filename}`) follow the same allow-list pattern `trips.py`'s `trip_file` route already established: `ArchiveRecording.known_filenames` is the frozenset of real filenames a recording actually owns, checked before ever touching the filesystem, and `camera_id`/`recording_id` path segments are rejected outright if they contain `/`, `\`, or a `.`/`..` segment - the same guard `_find_trip()` applies to `trip_id`.

**Filtering** (mode + lexical time range): a long archive can have thousands of recordings, so `/archive/{camera_id}` also accepts filter query params - `mode` (repeatable, N/E/M/P/A), plus `timestamp`/`from`/`until`. The time filter reuses `LexicalTimeParser`/`TimeInterval` from `lexicaltimeparser.py` - the same lexical-prefix matcher `bv-ls`/`bv-export`/`bv-download`/`bv-generate` already filter recordings with on the CLI side (a prefix like `2026`, `20260715`, or `20260715_14` expands to an inclusive range; `--timestamp` can't be combined with `--from`/`--until`). `archive_browser.filter_recordings()` applies both filters together over an already-scanned list; `kind_options()` is the canonical N/E/M/P/A list the filter bar's checkboxes are built from. No mode checked means no mode filter (show every kind) - `app.py`'s route turns an empty checkbox selection into `modes=None` rather than `modes=set()`, since the latter would mean "match nothing." A bad/conflicting time filter shows the unfiltered list plus an error message rather than a 500 or a silently-ignored filter. The filter bar is a plain `GET` form (bookmarkable/shareable URL, no JS) rendered above the thumbnail grid on `archive_recording_list.html`.

Access is `require_login` (any logged-in user), the same as trip browsing - not `require_owner` like the job-trigger routes. Like `trips.py`, there's no caching: every request rescans the archive directory fresh.

## Job runner

Both `bv-config`'s wizard and `bv-gps` can run for a while and, in `bv-config`'s case, need to ask questions back - neither fits the request/response shape of an ordinary route. `web/jobs.py` (`JobRunner`, `Job`, `JobStatus`) runs each triggered command in a background `threading.Thread`, **in-process, not a subprocess**: the job runner calls the target CLI module's own already-tested `_run()` function directly (`cli/bv_config.py`, `cli/bv_gps.py`), passing it `ask`/`say`/`warn` callables that write into that job's own `Job.output` list instead of a real terminal.

This sidesteps a real problem a subprocess approach would have: a subprocess's stdout can't reliably tell "waiting for input" apart from "more output is still coming", since `input()`'s own prompt text has no trailing newline - scraping raw bytes for that would need fragile heuristics. Calling the real Python function directly means the job runner controls exactly when `ask()`/`say()` fire, no scraping needed. It also means each job's `ask`/`say` closures only ever touch that one job's own output list, not the real process-wide `sys.stdout` - safe for jobs to run concurrently without a global-redirect hazard.

`bv-config`'s `prompt()`/`edit_endpoints()`/`run_wizard()`/`_run()` and `bv-gps`'s `_run()` all gained injectable `ask`/`say`/`warn` keyword-only parameters for this, defaulting to the real `input`/`print`/stderr-`print` - real terminal use is unaffected. When `ask()` is called, the job's status flips to `waiting_for_input`, the prompt text is recorded on the `Job`, and the call blocks on a `queue.Queue` until the browser POSTs an answer (`Job.submit_answer()`); the job page (`templates/job_detail.html`) shows a form for that prompt and polls via `<meta http-equiv="refresh" content="2">` rather than a websocket - no client-side JS, consistent with the rest of `bv-web`'s server-rendered approach. Jobs are held in an in-memory `dict` on `JobRunner` - the same restart-loses-state trade-off `SessionStore` already accepts (a job mid-run doesn't survive a restart regardless of whether it's tracked).

`bv-gps`'s job trigger only accepts an already-configured camera id, not its CLI's own `--host` option - `--host` is a terminal-only "probe a bare IP that hasn't been set up with bv-config yet" escape hatch, not something the browser-facing job form should expose.

**Cancellation** (`Job.cancel()`/`JobRunner.cancel()`, a "Cancel job" button on `job_detail.html` whenever a job isn't finished) is honest about what Python can and can't do to a running thread. A job currently `waiting_for_input` is unblocked immediately: `cancel()` pushes a sentinel onto the job's own answer queue, `ask()` recognizes it and raises `_JobCancelled`, which unwinds `_run()` right away - the same as any other exception escaping it. A job that's `running` with no prompt open (e.g. `bv-gps` blocked inside a socket call) can't be interrupted mid-call - `cancel()` instead flips the job's status to `CANCELLED` immediately so the browser stops trusting its output, while the background thread itself may keep running invisibly (it's a daemon thread, so it can't block process shutdown either way) until whatever it's blocked on returns or times out on its own; `JobRunner._spawn()`'s completion handler checks for this and won't overwrite an already-`CANCELLED` status with a stale success/failure result if the thread does eventually finish.

All job routes (`GET`/`POST /jobs/bv-config`, `GET`/`POST /jobs/bv-gps`, `GET /jobs/{job_id}`, `POST /jobs/{job_id}/answer`, `POST /jobs/{job_id}/cancel`) are gated by the existing `require_owner` dependency rather than a granular per-command permission system - simpler, and there's currently no real semi-trusted third party that would need finer-grained access.

**Camera pick-list**: most people only ever have one camera configured, but nothing stops there being more, so the job-trigger forms don't make the owner remember/retype an id from memory. `core/camera_config.py`'s `list_camera_ids(config_dir)` scans `default_config_dir()` for `*.cfg` files and returns their ids (filenames only - deliberately doesn't parse each file, so one corrupt config can't break the listing). `app.py`'s `_camera_options()` then loads each id's config just to get a friendly `"<name> (<id>)"` label, falling back to the bare id if that load fails. `job_new_bv_config.html` offers this as a `<datalist>` alongside its free-text id field (bv-config can still create a brand-new id, so the field stays free text - the list is a suggestion, not a constraint); `job_new_bv_gps.html` uses a `<select>` instead, since bv-gps only ever works against an already-configured id - if none exist yet, the page points at bv-config instead of showing an empty dropdown.

**Form layout - Required / Defaults / Optional, plus help tooltips**: each job form's fields are grouped into up to three `.option-group` sections (a plain `<h3>` heading, no JS) - `Required` for fields with no sensible default (bv-config's camera id, bv-gps's camera pick-list), `Defaults` for a field that's already filled in with a sane value the owner can still override (bv-gps's `--timeout`, added to the job form specifically to give this group a real example), and `Optional` for an off-by-default toggle (bv-gps's "skip reverse-geocoded address lookup"). A command doesn't have to fill all three - bv-config's job form today only has a `Required` group, since its one other real flag (`--config-dir`) is deliberately not exposed (would conflict with the camera-picker's assumption of a single `default_config_dir()`). Each field also gets a small circular "?" - a CSS-only tooltip (`.help-tip`/`.tooltip-text`, shown on hover or keyboard focus, no JavaScript) explaining what it does in a sentence or two, plus a `.command-help` box at the top of the page explaining what the command as a whole does. This grouping/tooltip pattern is meant to scale to a much larger flag surface (bv-export's 47 flags, if/when that's wired into bv-web) without redesigning the page - it's just more `.option-group` sections.

## Structure

```
src/blackvue/web/
    app.py        FastAPI app factory (create_app()) - routes, the
                   HTTPException handler that turns a 401 into a redirect
                   to /login and a 403 into a rendered "forbidden" page.
    auth.py        SessionStore (in-memory session-id -> username map,
                   not a signed cookie/JWT - see its own docstring for the
                   restart-loses-sessions trade-off this accepts), plus the
                   require_login/require_owner FastAPI dependencies routes
                   use to gate access.
    users.py        User accounts: username, PBKDF2-HMAC-SHA256 password
                   hash, role. A plain hand-editable TOML file (same pattern
                   as core/camera_config.py), not a database - there are
                   only ever a handful of accounts.
    trips.py        Scans a bv-export --target directory for trip folders -
                   a trip is identified by containing trip.log, the first
                   file export_trip() always writes. Deliberately doesn't
                   touch blackvue.archive/blackvue.trip at all: those model
                   the source recordings before a trip exists as a folder;
                   this only ever looks at bv-export's already-written
                   output.
    archive_browser.py
                   ArchiveRecording wrapper + scan_archive()/
                   find_recording()/group_by_day()/filter_recordings()/
                   kind_options() - browses a camera's raw bv-download
                   archive (CameraConfig.target), reusing
                   blackvue.archive.Archive rather than scanning the
                   filesystem itself, and lexicaltimeparser.py for the
                   time-range filter. See "Archive browser" above.
    jobs.py         JobRunner/Job/JobStatus - runs bv-config/bv-gps as
                   background threads (in-process, not subprocesses) and
                   tracks each one's output/status/pending-prompt. See
                   "Job runner" above.
    templates/      Server-rendered Jinja2 templates (base/login/trip_list/
                   trip_detail/archive_camera_list/archive_recording_list/
                   archive_recording_detail/forbidden/job_new_bv_config/
                   job_new_bv_gps/job_detail) - no client-side app to
                   build, no JSON API; the job_detail page polls via a
                   <meta refresh>, not a websocket.
```

`cli/bv_web.py` is the actual entry point (`bv-web` in `pyproject.toml`'s `[project.scripts]`), with two subcommands: `bv-web serve` (run the app against a `--target` directory and a users file) and `bv-web adduser` (create/update an account without needing the app itself running).

## Relationship to bv-live

`bv-live` - documented in full in `docs/ARCHITECTURE.md` - is the one piece that touches both projects. It shares `bv-web`'s `web` dependency extra in `pyproject.toml` ("bv-live shares this group rather than getting its own: it needs the exact same fastapi/uvicorn stack, just for a different app"), but it is not part of `blackvue.web` - it's its own top-level package, `blackvue.live`, with no login, no accounts, no session store, and no connection to `bv-export`'s trip folders at all. It talks directly to the camera's own live feed instead. Don't confuse "shares a dependency group with bv-web" with "is part of bv-web" - the two are related only by that one shared Python dependency, not by shared code, data, or purpose.

## Deployment

`bv-web` is the piece of this project actually meant to run always-on, reachable by more than one person - see `docs/DEPLOY.md` for the concrete Synology NAS setup: its own `Dockerfile`, its own entry in `docker-compose.yml`, a mounted `--target` directory (the same one `bv-export` writes to, wherever that happens to run), and a mounted users file. The heavier pipeline steps (`bv-generate --transcribe`, `bv-export`'s own rendering) can run on the NAS too via the separate `Dockerfile.cli`/`bv-cli` image, or faster on a PC with a GPU - either way, `bv-web` itself only ever needs read access to whatever `--target` directory ends up on the NAS.

## See also

- `docs/ARCHITECTURE.md` - the main project (pipeline + bv-live)
- `docs/DEPLOY.md` - NAS deployment, including bv-web's own Dockerfile/compose entry
- `WORKING_CONTEXT.md` - the dated history of how bv-web actually got built (search for "Scaffold blackvue.web" and the deploy-scaffolding entries that follow it)
