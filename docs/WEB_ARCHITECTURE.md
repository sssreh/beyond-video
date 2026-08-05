# bv-web architecture (side project)

`bv-web` is a small, separate side project from the main `bv-*` pipeline documented in `docs/ARCHITECTURE.md`: a login-protected web app for *browsing and watching* trips the pipeline's `bv-export` step already produced. It doesn't download from a camera, doesn't generate anything, and doesn't touch the source archive at all - it only ever reads `bv-export --target`'s own output folders. For the one piece that crosses over between the two projects, see "Relationship to bv-live" below.

## Why it's a separate project, not a feature of the pipeline

The pipeline (`bv-config` through `bv-export`, plus `bv-live`) is built to be run by one person, from their own machine, with no login at all. `bv-web` exists for a different situation entirely: letting other people - family members, say - see trips after the fact, from a browser, without shell access to the machine that ran the pipeline. That needs accounts, roles, and sessions, none of which the rest of the project has any use for - keeping it a separate top-level package (`blackvue.web`, not folded into `blackvue.export`/`blackvue.cli`) means none of that login machinery leaks into tools that were never meant to need it.

It also has its own, heavier optional-dependency group (`fastapi`, `uvicorn`, `python-multipart`, `Jinja2` - see `pyproject.toml`'s `web` extra) and, in deployment, its own Docker image (`Dockerfile`) separate from the pipeline's own (`Dockerfile.cli`) - see "Deployment" below.

## What it does today

Started as deliberately browse/watch only; a second increment (below) now also lets the owner trigger `bv-config` and `bv-gps` from the browser. Triggering `bv-download`/`bv-generate`/`bv-export` this way is still not part of this yet (see `WORKING_CONTEXT.md`) - the job-runner infrastructure below was built with those in mind, but only bv-config/bv-gps are actually wired up so far.

- **Login** - username/password, session cookie. Two roles: `owner` (currently just Christer) and `viewer` (everyone else). Browsing/watching trips works the same for both roles; triggering a job (below) is `owner`-only.
- **Trip list** - every trip folder under the configured `--target` directory, scanned fresh on every request (not cached), so a trip `bv-export` finishes writing while `bv-web` is already running shows up without a restart.
- **Trip detail** - video playback (range-request support comes for free from Starlette's own `FileResponse`, so seeking/scrubbing works), plus GPX/SRT/LRC download links, whichever of those a given trip actually has (a trip only has the files the `bv-export` run that produced it actually asked for - no `map.mp4` without `--map`, etc.).
- **Jobs (owner-only)** - the owner can trigger `bv-config` (set up or edit a camera) or `bv-gps` (get a live GPS fix) as a background job from the browser, watch its output, and answer `bv-config`'s wizard questions as they come up. See "Job runner" below for how.

## Job runner

Both `bv-config`'s wizard and `bv-gps` can run for a while and, in `bv-config`'s case, need to ask questions back - neither fits the request/response shape of an ordinary route. `web/jobs.py` (`JobRunner`, `Job`, `JobStatus`) runs each triggered command in a background `threading.Thread`, **in-process, not a subprocess**: the job runner calls the target CLI module's own already-tested `_run()` function directly (`cli/bv_config.py`, `cli/bv_gps.py`), passing it `ask`/`say`/`warn` callables that write into that job's own `Job.output` list instead of a real terminal.

This sidesteps a real problem a subprocess approach would have: a subprocess's stdout can't reliably tell "waiting for input" apart from "more output is still coming", since `input()`'s own prompt text has no trailing newline - scraping raw bytes for that would need fragile heuristics. Calling the real Python function directly means the job runner controls exactly when `ask()`/`say()` fire, no scraping needed. It also means each job's `ask`/`say` closures only ever touch that one job's own output list, not the real process-wide `sys.stdout` - safe for jobs to run concurrently without a global-redirect hazard.

`bv-config`'s `prompt()`/`edit_endpoints()`/`run_wizard()`/`_run()` and `bv-gps`'s `_run()` all gained injectable `ask`/`say`/`warn` keyword-only parameters for this, defaulting to the real `input`/`print`/stderr-`print` - real terminal use is unaffected. When `ask()` is called, the job's status flips to `waiting_for_input`, the prompt text is recorded on the `Job`, and the call blocks on a `queue.Queue` until the browser POSTs an answer (`Job.submit_answer()`); the job page (`templates/job_detail.html`) shows a form for that prompt and polls via `<meta http-equiv="refresh" content="2">` rather than a websocket - no client-side JS, consistent with the rest of `bv-web`'s server-rendered approach. Jobs are held in an in-memory `dict` on `JobRunner` - the same restart-loses-state trade-off `SessionStore` already accepts (a job mid-run doesn't survive a restart regardless of whether it's tracked).

All job routes (`GET`/`POST /jobs/bv-config`, `GET`/`POST /jobs/bv-gps`, `GET /jobs/{job_id}`, `POST /jobs/{job_id}/answer`) are gated by the existing `require_owner` dependency rather than a granular per-command permission system - simpler, and there's currently no real semi-trusted third party that would need finer-grained access.

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
    jobs.py         JobRunner/Job/JobStatus - runs bv-config/bv-gps as
                   background threads (in-process, not subprocesses) and
                   tracks each one's output/status/pending-prompt. See
                   "Job runner" above.
    templates/      Server-rendered Jinja2 templates (base/login/trip_list/
                   trip_detail/forbidden/job_new_bv_config/job_new_bv_gps/
                   job_detail) - no client-side app to build, no JSON API;
                   the job_detail page polls via a <meta refresh>, not a
                   websocket.
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
