"""
FastAPI app factory for bv-web.

Server-rendered (Jinja2), not a JSON API + separate frontend build -
there's no client-side app to build/deploy, and the pages here
(a trip list, a trip detail/player page, a login form) don't need
anything richer. bv-web's CLI (cli/bv_web.py) is the only thing that
imports this module, and only inside `bv-web serve` - see
web/__init__.py's docstring for why that matters.

The core of this app is still browse/watch: login (owner/viewer
roles), the trip list, and a trip detail page with video playback
(range-request support comes for free from Starlette's own
FileResponse) plus GPX/SRT/LRC download links. On top of that, the
owner can now also trigger bv-config and bv-gps as background jobs
from the browser (see jobs.py for how - a real job-runner
infrastructure, since either can run for a while and bv-config's
wizard needs to ask questions back). bv-download/bv-generate/bv-export
aren't wired in yet - see WORKING_CONTEXT.md for the increment plan.

Copyright (C) 2026 Christer R. (sssreh)

SPDX-License-Identifier: GPL-3.0-or-later
"""

from __future__ import annotations

from pathlib import Path

from fastapi import Depends
from fastapi import FastAPI
from fastapi import Form
from fastapi import HTTPException
from fastapi import Query
from fastapi import Request
from fastapi import status
from fastapi.exception_handlers import http_exception_handler
from fastapi.responses import FileResponse
from fastapi.responses import HTMLResponse
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates

from .archive_browser import ArchiveRecording
from .archive_browser import ArchiveRecordingCache
from .archive_browser import filter_recordings
from .archive_browser import find_recording
from .archive_browser import first_valid_gps_fix
from .archive_browser import group_by_day
from .archive_browser import kind_options
from .archive_browser import scan_archive
from .auth import SESSION_COOKIE_NAME
from .auth import SessionStore
from .auth import require_login
from .auth import require_owner
from .jobs import Job
from .jobs import JobRunner
from .trips import TripAssets
from .trips import TripCache
from .trips import scan_trips
from .users import User
from .users import UsersConfig
from ..core.camera_config import CameraConfigCache
from ..core.camera_config import CameraConfigError
from ..core.camera_config import config_path
from ..core.camera_config import default_config_dir
from ..core.camera_config import list_camera_ids
from ..core.camera_config import load_camera_config
from ..export.geocoding import load_or_reverse_geocode
from ..generate.media import MediaToolError
from ..lexicaltimeparser import LexicalTimeParser

TEMPLATES_DIR = Path(__file__).parent / "templates"


def create_app(target: Path, users_config: UsersConfig) -> FastAPI:
    """Build the bv-web FastAPI app.

    `target` is a bv-export --target directory (the same one passed
    to `bv-export --target ...`) - trips are discovered by scanning
    its subfolders for trip.log (see trips.scan_trips()), freshly on
    every request rather than cached, so a trip bv-export finishes
    writing while the app is already running shows up without a
    restart.

    `users_config` is the already-loaded set of accounts (see
    users.load_users_config()) - this app itself never creates or
    edits accounts; that's `bv-web adduser`'s job.
    """

    app = FastAPI(title="Beyond Video")
    app.state.target = target
    app.state.users_config = users_config
    app.state.session_store = SessionStore()
    app.state.job_runner = JobRunner()
    # See TripCache's own docstring: collapses the burst of stat()
    # calls a video player's HTTP range requests would otherwise
    # repeat on every single chunk while seeking/buffering.
    app.state.trip_cache = TripCache()

    # See ArchiveRecordingCache's own docstring - same reasoning as
    # trip_cache above, applied to the archive browser's detail/
    # thumbnail/file-serving routes instead of the trip player.
    app.state.archive_recording_cache = ArchiveRecordingCache()

    # See CameraConfigCache's own docstring (core/camera_config.py) -
    # same reasoning again, one layer further out: every one of those
    # archive-browser requests also has to resolve which camera's
    # config to read first, and that resolution was itself being
    # redone from scratch (a fresh file read + TOML parse) on every
    # single request.
    app.state.camera_config_cache = CameraConfigCache()

    templates = Jinja2Templates(directory=str(TEMPLATES_DIR))

    @app.exception_handler(HTTPException)
    async def _handle_http_exception(request: Request, exc: HTTPException):
        if exc.status_code == status.HTTP_401_UNAUTHORIZED:
            return RedirectResponse(
                url=f"/login?next={request.url.path}",
                status_code=status.HTTP_303_SEE_OTHER,
            )
        if exc.status_code == status.HTTP_403_FORBIDDEN:
            return templates.TemplateResponse(
                request,
                "forbidden.html",
                {"detail": exc.detail},
                status_code=status.HTTP_403_FORBIDDEN,
            )
        # Anything else (404s in particular) keeps FastAPI's normal
        # JSON error body - only 401/403 need browser-friendly
        # handling here.
        return await http_exception_handler(request, exc)

    @app.get("/login", response_class=HTMLResponse)
    async def login_form(request: Request, next: str = "/"):
        return templates.TemplateResponse(
            request, "login.html", {"next": next, "error": None}
        )

    @app.post("/login")
    async def login_submit(
        request: Request,
        username: str = Form(...),
        password: str = Form(...),
        next: str = Form("/"),
    ):
        user = users_config.authenticate(username, password)
        if user is None:
            return templates.TemplateResponse(
                request,
                "login.html",
                {"next": next, "error": "Wrong username or password."},
                status_code=status.HTTP_401_UNAUTHORIZED,
            )

        session_id = app.state.session_store.create(user.username)
        response = RedirectResponse(
            url=next or "/", status_code=status.HTTP_303_SEE_OTHER
        )
        response.set_cookie(
            SESSION_COOKIE_NAME,
            session_id,
            httponly=True,
            samesite="lax",
        )
        return response

    @app.post("/logout")
    async def logout(request: Request):
        session_id = request.cookies.get(SESSION_COOKIE_NAME)
        app.state.session_store.destroy(session_id)
        response = RedirectResponse(
            url="/login", status_code=status.HTTP_303_SEE_OTHER
        )
        response.delete_cookie(SESSION_COOKIE_NAME)
        return response

    @app.get("/", response_class=HTMLResponse)
    async def trip_list(request: Request, user: User = Depends(require_login)):
        trips = scan_trips(target)
        return templates.TemplateResponse(
            request, "trip_list.html", {"user": user, "trips": trips}
        )

    @app.get("/trips/{trip_id}", response_class=HTMLResponse)
    async def trip_detail(
        request: Request, trip_id: str, user: User = Depends(require_login)
    ):
        trip = _find_trip(app.state.trip_cache, target, trip_id)
        return templates.TemplateResponse(
            request, "trip_detail.html", {"user": user, "trip": trip}
        )

    @app.get("/trips/{trip_id}/files/{filename}")
    async def trip_file(
        trip_id: str, filename: str, user: User = Depends(require_login)
    ):
        trip = _find_trip(app.state.trip_cache, target, trip_id)
        if filename not in trip.known_filenames:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="file not found"
            )

        path = trip.folder / filename
        if not path.is_file():
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="file not found"
            )

        return FileResponse(path)

    @app.get("/archive", response_class=HTMLResponse)
    async def archive_camera_list(
        request: Request, user: User = Depends(require_login)
    ):
        return templates.TemplateResponse(
            request,
            "archive_camera_list.html",
            {"user": user, "cameras": _camera_options()},
        )

    @app.get("/archive/{camera_id}", response_class=HTMLResponse)
    async def archive_recording_list(
        request: Request,
        camera_id: str,
        user: User = Depends(require_login),
        mode: list[str] = Query(default=[]),
        timestamp: str | None = Query(default=None),
        from_: str | None = Query(default=None, alias="from"),
        until: str | None = Query(default=None, alias="until"),
        videos_only: bool = Query(default=False),
    ):
        # A GET form always submits every named field, even ones the
        # user left blank - an empty text box arrives here as "", not
        # an absent query param, so Query(default=None) never actually
        # kicks in for it. LexicalTimeParser.parse() checks `is not
        # None` (not truthiness) to detect a --timestamp/--from
        # combination, since a CLI caller's unset argparse flags are
        # real None - normalize "" to None here so a page load with
        # only "Exact" filled in doesn't get treated as if "From"/
        # "Until" were filled in too. Confirmed as a real bug, not
        # just theoretical: Christer hit this leaving From/Until
        # untouched and got "cannot be combined" anyway.
        timestamp = timestamp or None
        from_ = from_ or None
        until = until or None

        archive_path = _find_camera_archive(app.state.camera_config_cache, camera_id)
        recordings = scan_archive(archive_path, camera_id)

        # An empty `mode` (nothing checked) means "don't filter by
        # mode at all", not "show nothing" - see
        # archive_browser.filter_recordings()'s own docstring on why.
        selected_modes = set(mode)

        error = None
        time_interval = None
        if timestamp or from_ or until:
            try:
                time_interval = LexicalTimeParser(
                    timestamp=timestamp, from_=from_, until=until
                ).parse()
            except ValueError as exc:
                # A bad/conflicting filter (e.g. --timestamp combined
                # with --from) shouldn't 500 the page or silently show
                # an unfiltered list - show every recording alongside
                # the error so the owner can see what to fix, the same
                # way a bad CLI flag combination would just print an
                # error rather than doing something unexpected.
                error = str(exc)

        if error is None:
            recordings = filter_recordings(
                recordings,
                modes=selected_modes or None,
                time_interval=time_interval,
                videos_only=videos_only,
            )

        days = group_by_day(recordings)
        return templates.TemplateResponse(
            request,
            "archive_recording_list.html",
            {
                "user": user,
                "camera_id": camera_id,
                "days": days,
                "kind_options": kind_options(),
                "selected_modes": selected_modes,
                "timestamp_value": timestamp or "",
                "from_value": from_ or "",
                "until_value": until or "",
                "videos_only": videos_only,
                "error": error,
            },
        )

    @app.get("/archive/{camera_id}/{recording_id}", response_class=HTMLResponse)
    async def archive_recording_detail(
        request: Request,
        camera_id: str,
        recording_id: str,
        user: User = Depends(require_login),
    ):
        recording = _find_archive_recording(
            app.state.archive_recording_cache,
            app.state.camera_config_cache,
            camera_id,
            recording_id,
        )
        return templates.TemplateResponse(
            request,
            "archive_recording_detail.html",
            {"user": user, "camera_id": camera_id, "recording": recording},
        )

    @app.get(
        "/archive/{camera_id}/{recording_id}/location", response_class=HTMLResponse
    )
    async def archive_recording_location(
        request: Request,
        camera_id: str,
        recording_id: str,
        user: User = Depends(require_login),
    ):
        recording = _find_archive_recording(
            app.state.archive_recording_cache,
            app.state.camera_config_cache,
            camera_id,
            recording_id,
        )

        coordinates = None
        google_maps_url = None
        address = None
        address_error = None
        error = None

        if recording.gps_path is None:
            error = "This recording has no GPS log."
        else:
            # first_valid_gps_fix() calls read_gps(), which raises
            # MediaToolError on an unreadable .gps file (permissions,
            # a truncated/corrupt file, etc.) - trip_export.py's own
            # _merge_gps() guards the exact same read_gps() call the
            # same way (skip and move on) rather than letting it
            # propagate, and this route needs the same guard: without
            # it, a single bad .gps file 500s the page instead of
            # showing a friendly message.
            try:
                fix = first_valid_gps_fix(recording.gps_path)
            except MediaToolError as exc:
                fix = None
                error = f"could not read this recording's GPS log: {exc}"

            if fix is None:
                if error is None:
                    error = (
                        "No valid GPS fix found in this recording's GPS "
                        "log (no signal)."
                    )
            else:
                coordinates = f"{fix.latitude},{fix.longitude}"
                google_maps_url = f"https://www.google.com/maps?q={coordinates}"

                # Reverse-geocoded and cached under default_config_dir()
                # - bv-web's own writable scratch space (the same
                # directory CameraConfigCache/the job runner already
                # use), NOT next to the camera's archive the way
                # trip_export.py's trip_info.txt caches (destination.
                # parent / ".osm_cache"): that convention assumed a
                # writable archive path, true for bv-cli's container
                # (which mounts /data/archive read-write) but false for
                # bv-web's own container - docker-compose.yml mounts
                # /data/archive read-only there (the archive browser
                # only ever reads recordings), so writing a cache
                # anywhere under it 500s with "Read-only file system"
                # the moment reverse geocoding is actually used. Real
                # bug hit on Christer's NAS - see WORKING_CONTEXT.md.
                geocode_cache_dir = default_config_dir() / ".osm_cache"
                try:
                    address = load_or_reverse_geocode(
                        fix.latitude, fix.longitude, geocode_cache_dir
                    )
                except MediaToolError as exc:
                    address_error = str(exc)

        return templates.TemplateResponse(
            request,
            "archive_recording_location.html",
            {
                "user": user,
                "camera_id": camera_id,
                "recording_id": recording_id,
                "coordinates": coordinates,
                "google_maps_url": google_maps_url,
                "address": address,
                "address_error": address_error,
                "error": error,
            },
        )

    @app.get("/archive/{camera_id}/{recording_id}/thumbnail/{direction}")
    async def archive_recording_thumbnail(
        camera_id: str,
        recording_id: str,
        direction: str,
        user: User = Depends(require_login),
    ):
        recording = _find_archive_recording(
            app.state.archive_recording_cache,
            app.state.camera_config_cache,
            camera_id,
            recording_id,
        )
        path = recording.thumbnail_path(direction)
        if path is None or not path.is_file():
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="thumbnail not found"
            )
        return FileResponse(path)

    @app.get("/archive/{camera_id}/{recording_id}/files/{filename}")
    async def archive_recording_file(
        camera_id: str,
        recording_id: str,
        filename: str,
        user: User = Depends(require_login),
    ):
        recording = _find_archive_recording(
            app.state.archive_recording_cache,
            app.state.camera_config_cache,
            camera_id,
            recording_id,
        )
        if filename not in recording.known_filenames:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="file not found"
            )

        path = recording.file_path(filename)
        if path is None or not path.is_file():
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="file not found"
            )

        return FileResponse(path)

    @app.get("/jobs/bv-config", response_class=HTMLResponse)
    async def new_bv_config_form(
        request: Request, user: User = Depends(require_owner)
    ):
        return templates.TemplateResponse(
            request,
            "job_new_bv_config.html",
            {"user": user, "cameras": _camera_options()},
        )

    @app.post("/jobs/bv-config")
    async def new_bv_config_submit(
        request: Request,
        id: str = Form(...),
        user: User = Depends(require_owner),
    ):
        job = app.state.job_runner.start_bv_config(id_=id, username=user.username)
        return RedirectResponse(
            url=f"/jobs/{job.id}", status_code=status.HTTP_303_SEE_OTHER
        )

    @app.get("/jobs/bv-gps", response_class=HTMLResponse)
    async def new_bv_gps_form(
        request: Request, user: User = Depends(require_owner)
    ):
        return templates.TemplateResponse(
            request,
            "job_new_bv_gps.html",
            {"user": user, "cameras": _camera_options()},
        )

    @app.post("/jobs/bv-gps")
    async def new_bv_gps_submit(
        request: Request,
        id: str = Form(...),
        timeout: int = Form(5, ge=1),
        no_address: bool = Form(False),
        user: User = Depends(require_owner),
    ):
        job = app.state.job_runner.start_bv_gps(
            id_=id,
            timeout=timeout,
            no_address=no_address,
            username=user.username,
        )
        return RedirectResponse(
            url=f"/jobs/{job.id}", status_code=status.HTTP_303_SEE_OTHER
        )

    @app.get("/jobs/{job_id}", response_class=HTMLResponse)
    async def job_detail(
        request: Request, job_id: str, user: User = Depends(require_owner)
    ):
        job = _find_job(app.state.job_runner, job_id)
        job_status, output, prompt = job.snapshot()
        return templates.TemplateResponse(
            request,
            "job_detail.html",
            {
                "user": user,
                "job": job,
                # .value, not the raw JobStatus - a `str, Enum` member's
                # own __str__ renders as "JobStatus.RUNNING", not the
                # plain "running" the template's CSS classes and
                # {% if %} checks below actually need.
                "status": job_status.value,
                "is_finished": job_status.is_finished,
                "output": output,
                "prompt": prompt,
            },
        )

    @app.post("/jobs/{job_id}/answer")
    async def job_answer(
        job_id: str,
        answer: str = Form(""),
        user: User = Depends(require_owner),
    ):
        # A missing job or one that isn't actually waiting for input
        # (job_runner.answer() returns False either way) is treated as
        # a harmless no-op redirect back to the job page, not an
        # error - the most likely real cause is a double form submit
        # or a stale browser tab, not something the owner needs an
        # error page for.
        app.state.job_runner.answer(job_id, answer)
        return RedirectResponse(
            url=f"/jobs/{job_id}", status_code=status.HTTP_303_SEE_OTHER
        )

    @app.post("/jobs/{job_id}/cancel")
    async def job_cancel(
        job_id: str,
        user: User = Depends(require_owner),
    ):
        # Same no-op-if-already-finished treatment as job_answer()
        # above - a stale tab's Cancel button hitting an already-done
        # job isn't an error.
        app.state.job_runner.cancel(job_id)
        return RedirectResponse(
            url=f"/jobs/{job_id}", status_code=status.HTTP_303_SEE_OTHER
        )

    return app


def _camera_options() -> list[dict[str, str]]:
    """List every camera already set up via bv-config, for the
    bv-config/bv-gps job-trigger forms to offer as a pick-list instead
    of making the owner remember/retype an id - most people only ever
    have one, but nothing stops there being more.

    Scans the same default_config_dir() the job runner itself actually
    uses (see jobs.py's start_bv_config()/start_bv_gps(), neither of
    which take a --config-dir override - "curated subset, not every
    CLI flag"). Each id's own config is loaded just to get a friendly
    `label` ("<name> (<id>)", or just the id if the name and id are
    the same) - a config that fails to load (corrupt, hand-edited
    wrong) still shows up by its bare id rather than disappearing from
    the list or breaking the whole page.
    """

    config_dir = default_config_dir()
    options = []
    for id_ in list_camera_ids(config_dir):
        try:
            config = load_camera_config(config_path(config_dir, id_))
        except CameraConfigError:
            options.append({"id": id_, "label": id_})
            continue
        label = id_ if config.name == id_ else f"{config.name} ({id_})"
        options.append({"id": id_, "label": label})
    return options


def _find_camera_archive(cache: CameraConfigCache, camera_id: str) -> Path:
    """Resolve a camera id (from the URL) to its archive directory -
    CameraConfig.target, the directory bv-download writes raw
    recordings to. This is NOT the same thing as bv-export --target
    (app.state.target, the trips directory trips.py reads) - a camera
    id names a *source* archive, a trip id names *processed* output;
    don't conflate the two.

    `camera_id` comes straight from the URL path and is therefore
    untrusted - reject anything that could walk outside
    default_config_dir() before it ever reaches config_path(), same
    guard _find_trip() applies to trip_id below. A camera id that
    doesn't have a config file at all (never set up, or a typo) 404s
    the same way a bad trip id does.

    Goes through `cache` (see CameraConfigCache's own docstring in
    core/camera_config.py) rather than calling load_camera_config()
    directly.
    """

    if (
        "/" in camera_id
        or "\\" in camera_id
        or camera_id in (".", "..")
    ):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="camera not found"
        )

    try:
        config = cache.get(default_config_dir(), camera_id)
    except CameraConfigError:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="camera not found"
        )

    return config.target


def _find_archive_recording(
    recording_cache: ArchiveRecordingCache,
    camera_config_cache: CameraConfigCache,
    camera_id: str,
    recording_id: str,
) -> ArchiveRecording:
    """Resolve a (camera id, recording id) pair to an ArchiveRecording,
    404ing if either the camera or the recording within it doesn't
    exist. `recording_id` is as untrusted as `camera_id` - same
    path-separator/dot-segment guard as everywhere else URL segments
    reach the filesystem.

    Goes through `recording_cache` (see ArchiveRecordingCache's own
    docstring) rather than calling find_recording() directly, and
    passes `camera_config_cache` through to _find_camera_archive() -
    the detail page, its thumbnail, and every HTTP range request while
    its video plays all resolve the same recording (and the same
    camera config) through here."""

    if (
        "/" in recording_id
        or "\\" in recording_id
        or recording_id in (".", "..")
    ):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="recording not found"
        )

    archive_path = _find_camera_archive(camera_config_cache, camera_id)
    recording = recording_cache.get(archive_path, camera_id, recording_id)
    if recording is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="recording not found"
        )
    return recording


def _find_job(job_runner: JobRunner, job_id: str) -> Job:
    """Resolve a job id to a Job, 404ing if it doesn't exist - covers
    both a genuinely bad id and a job_runner that's been restarted
    since the id was handed out (see jobs.py's own docstring on why
    jobs don't survive a restart)."""

    job = job_runner.get(job_id)
    if job is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="job not found"
        )
    return job


def _find_trip(cache: TripCache, target: Path, trip_id: str) -> TripAssets:
    """Resolve a trip id (its folder name) to a TripAssets inside
    `target`, 404ing if it doesn't exist or isn't actually a trip
    folder. `trip_id` comes straight from the URL path and is
    therefore untrusted - reject anything that could walk outside
    `target` (a component like ".." or a path separator) before ever
    touching the filesystem with it.

    Goes through `cache` (see TripCache's own docstring) rather than
    calling scan_trip() directly - trip_file() calls this once per
    HTTP range request, and a video player issues many of those per
    second while seeking/buffering."""

    if (
        "/" in trip_id
        or "\\" in trip_id
        or trip_id in (".", "..")
    ):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="trip not found"
        )

    trip = cache.get(target, trip_id)
    if trip is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="trip not found"
        )
    return trip
