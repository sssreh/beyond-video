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
FileResponse) plus GPX/SRT download links. On top of that, the
owner can now also trigger bv-config, bv-gps, bv-generate, bv-export,
bv-download, bv-ls, bv-scribe, and bv-search as background jobs from
the browser (see jobs.py for how - a real job-runner infrastructure,
since any of these can run for a while and bv-config's wizard needs
to ask questions back). Every bv-* pipeline command now has a browser
trigger.

Copyright (C) 2026 Christer R. (sssreh)

SPDX-License-Identifier: GPL-3.0-or-later
"""

from __future__ import annotations

import re
import tempfile
from argparse import ArgumentTypeError
from datetime import datetime
from pathlib import Path

from fastapi import Depends
from fastapi import FastAPI
from fastapi import File
from fastapi import Form
from fastapi import HTTPException
from fastapi import Query
from fastapi import Request
from fastapi import UploadFile
from fastapi import status
from fastapi.exception_handlers import http_exception_handler
from fastapi.responses import FileResponse
from fastapi.responses import HTMLResponse
from fastapi.responses import JSONResponse
from fastapi.responses import RedirectResponse
from fastapi.responses import Response
from fastapi.responses import StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from ..adapters.registry import get_adapter
from ..adapters.telemetry_bridge import recording_gps_available
from ..adapters.telemetry_bridge import resolve_recording_gps_span
from .archive_browser import ArchiveRecording
from .archive_browser import ArchiveRecordingCache
from .archive_browser import filter_recordings
from .archive_browser import find_recording
from .archive_browser import group_by_day
from .archive_browser import kind_options
from .archive_browser import scan_archive
from .auth import SESSION_COOKIE_NAME
from .auth import THEME_COOKIE_NAME
from .auth import SessionStore
from .auth import require_login
from .auth import require_owner
from .auth import require_viewer_or_owner
from ..history import HistoryFilter
from ..history import NumberedEntry
from ..history import all_entries
from ..history import filtered_entries
from ..history import matching_log_lines
from ..history import tail
from .jobs import BvExportArgError
from .jobs import Job
from .jobs import JobRunner
from .jobs import JobStatus
from .trips import GPX_FILENAME
from .trips import TripAssets
from .trips import TripCache
from .trips import first_gpx_point
from .trips import scan_all_trips
from .users import User
from .users import UsersConfig
from ..core.camera_config import CameraConfigCache
from ..core.camera_config import CameraConfigError
from ..core.camera_config import config_path
from ..core.camera_config import default_config_dir
from ..core.camera_config import list_camera_ids
from ..core.camera_config import load_camera_config
from ..core.lock import LOCKABLE_ASSETS
from ..export.geocoding import load_or_reverse_geocode
from ..telemetry.gps_reader import GpsFix
from ..export.hevc_preview import open_hevc_preview_stream
from ..export.kml_writer import gpx_to_kml
from ..generate.media import MediaToolError
from ..generate.mp4_repair import load_or_repair_parking_video
from ..generate.speech import transcribe
from ..lexicaltimeparser import LexicalTimeParser

TEMPLATES_DIR = Path(__file__).parent / "templates"

# Bundled, fixed assets (currently just the two theme background
# images - see base.html's page-bg layer) - not the trip archive
# itself, which is served through its own authenticated routes
# instead of a blanket StaticFiles mount. Packaged via
# [tool.setuptools.package-data]'s own "blackvue.web" entry in
# pyproject.toml; see that file's comments for why non-.py assets
# need to be listed there explicitly or a real `pip install .` drops
# them silently (bit us twice before, with templates and then with
# the bundled font/mirror-icon).
STATIC_DIR = Path(__file__).parent / "static"

# bv-search's job output prints each matching recording's bare id on
# its own unindented line (see cli/bv_search.py's _run(): `say(str(
# recording.id))`), sandwiched between the blank line before it and
# the indented match-detail lines after it - a Recording.id is always
# "<8-digit date>_<6-digit time>_<one-letter kind>" (see domain/
# recording.py's kind/is_normal/is_event/is_manual properties, and
# e.g. test_archive_browser.py's "20260715_140212_N"). job_detail.html
# uses this to turn just those lines into links to the recording's
# archive detail page, without touching any other job's output or any
# of bv-search's own indented match/status lines.
RECORDING_ID_RE = re.compile(r"^\d{8}_\d{6}_[A-Za-z]$")

# Quick-tail view (task #687 in WORKING_CONTEXT.md): job_detail()'s
# ?tail=1 option renders only the most recent TAIL_LINE_COUNT output
# lines of a still-running job, instead of the full (potentially
# thousands-of-lines) history that grows every 2s auto-refresh tick.
# Originally 200, dropped to 30 after Christer measured his own
# job-output panel (pre.job-output's max-height: 60vh) actually shows
# about 24 lines before it scrolls - 200 lines buffered behind a
# ~24-line window was mostly wasted re-render/re-send cost on every
# 2s refresh, not extra visible context. 30 keeps a small amount of
# scroll-back slack above that visible window without reintroducing
# the growing-payload problem this feature exists to solve.
TAIL_LINE_COUNT = 30


def create_app(target: Path, users_config: UsersConfig) -> FastAPI:
    """Build the bv-web FastAPI app.

    `target` is a bv-export --target directory (the same one passed
    to `bv-export --target ...`) - but no longer the *only* place
    trips are found. Every camera set up via bv-config may have its
    own configured Target (CameraConfig.target - see bv-config's own
    "Target" prompt) that bv-export writes to by default, and there's
    no requirement they share one; `target` here now serves as the
    *fallback*, scanned flat alongside every configured camera's own
    Target directory (see trips.scan_all_trips(), and the
    "Thats a dilemma"/task #762 discussion in WORKING_CONTEXT.md this
    resolves). Trips are discovered freshly on every request rather
    than cached, so a trip bv-export finishes writing while the app
    is already running shows up without a restart.

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
    # Display-only capitalization for usernames shown in the UI (header
    # account bar, welcome page greeting) - Christer: "make the first
    # letter of username uppercase". Usernames themselves are stored and
    # compared exactly as entered (see users.py) - this only affects how
    # one is rendered. Deliberately not Jinja's built-in `capitalize`
    # filter, which also lowercases the rest of the string (e.g. turning
    # "christerR" into "Christerr") - this only touches the first
    # character and leaves everything else exactly as stored.
    templates.env.filters["capitalize_first"] = (
        lambda s: (s[0].upper() + s[1:]) if s else s
    )
    # HistoryEntry.started_at is stored as a UTC ISO-8601 string with
    # microseconds (see core/history.py's own docstring); the history
    # templates used to render that raw value straight into the page -
    # a wall of digits like "2026-08-16T14:23:07.482913+00:00", worse
    # than useless for a human glancing at the table. bv-history (the
    # CLI)'s own _format_row() already solved this - convert to local
    # time, drop the microseconds/offset - so this filter mirrors that
    # exact formatting rather than inventing a second convention.
    templates.env.filters["local_time"] = lambda iso: datetime.fromisoformat(
        iso
    ).astimezone().strftime("%Y-%m-%d %H:%M:%S")
    # See RECORDING_ID_RE's own comment above - job_detail.html calls
    # this per output line to decide whether to render it as a link
    # rather than plain text.
    templates.env.globals["is_recording_id"] = (
        lambda s: RECORDING_ID_RE.match(s) is not None
    )

    # Unauthenticated on purpose: just the two theme background
    # images (cosmetic, not trip data), served the same way for
    # anyone including the /login page itself, which also wants the
    # background applied.
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

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

    @app.post("/theme")
    async def set_theme(theme: str = Form(...), next: str = Form("/")):
        # Manual light/dark toggle (Christer chose "manual" over
        # auto-follow-system when asked) - a plain preference cookie,
        # not tied to login. base.html reads it directly via
        # request.cookies rather than every route passing a theme
        # variable through its own TemplateResponse context, the same
        # way the tab nav already reads request.url.path directly.
        #
        # `next` guarded against becoming an open redirect (e.g.
        # "//evil.example.com", which browsers treat as
        # protocol-relative) since, unlike /login's next, this route
        # doesn't require a login and so is reachable by anyone who
        # can get a victim's browser to POST here.
        if not next.startswith("/") or next.startswith("//"):
            next = "/"

        response = RedirectResponse(url=next, status_code=status.HTTP_303_SEE_OTHER)
        response.set_cookie(
            THEME_COOKIE_NAME,
            "dark" if theme == "dark" else "light",
            max_age=60 * 60 * 24 * 365,
            httponly=True,
            samesite="lax",
        )
        return response

    @app.get("/", response_class=HTMLResponse)
    async def welcome(request: Request, user: User = Depends(require_login)):
        # The landing page after login used to be the trip list itself
        # (this route used to be trip_list()) - Christer's own feedback:
        # "I don't think we should start with trips after login, yes trips
        # are the end goal, but not the starting." Trips moved to their
        # own GET /trips below; this route now renders a short welcome/
        # orientation page instead, with quick links into the rest of the
        # app rather than immediately dropping the owner into a (possibly
        # empty) trip table.
        return templates.TemplateResponse(request, "welcome.html", {"user": user})

    @app.get("/trips", response_class=HTMLResponse)
    async def trip_list(request: Request, user: User = Depends(require_login)):
        trips = scan_all_trips(default_config_dir(), target)
        return templates.TemplateResponse(
            request, "trip_list.html", {"user": user, "trips": trips}
        )

    @app.get("/trips/{trip_id:path}", response_class=HTMLResponse)
    async def trip_detail(
        request: Request, trip_id: str, user: User = Depends(require_login)
    ):
        trip = _find_trip(
            app.state.trip_cache, app.state.camera_config_cache, target, trip_id
        )
        return templates.TemplateResponse(
            request, "trip_detail.html", {"user": user, "trip": trip}
        )

    @app.get("/trips/{trip_id:path}/location", response_class=HTMLResponse)
    async def trip_location(
        request: Request, trip_id: str, user: User = Depends(require_login)
    ):
        trip = _find_trip(
            app.state.trip_cache, app.state.camera_config_cache, target, trip_id
        )

        coordinates = None
        google_maps_url = None
        address = None
        address_error = None
        error = None

        if not trip.gpx:
            error = "This trip has no GPS track."
        else:
            point = first_gpx_point(trip.folder / GPX_FILENAME)
            if point is None:
                error = (
                    "No valid GPS fix found in this trip's GPS track "
                    "(no signal)."
                )
            else:
                latitude, longitude = point
                coordinates = f"{latitude},{longitude}"
                google_maps_url = f"https://www.google.com/maps?q={coordinates}"

                # Same reverse-geocode cache dir the archive browser's
                # own /location route already uses (see
                # archive_recording_location() below) - one shared
                # cache under bv-web's own writable scratch space
                # rather than a second one, since both routes are
                # geocoding the exact same kind of thing (a single
                # lat/lon point) and would otherwise cold-miss each
                # other's already-cached lookups for no reason.
                geocode_cache_dir = default_config_dir() / ".osm_cache"
                try:
                    address = load_or_reverse_geocode(
                        latitude, longitude, geocode_cache_dir
                    )
                except MediaToolError as exc:
                    address_error = str(exc)

        return templates.TemplateResponse(
            request,
            "trip_location.html",
            {
                "user": user,
                "trip_id": trip_id,
                "coordinates": coordinates,
                "google_maps_url": google_maps_url,
                "address": address,
                "address_error": address_error,
                "error": error,
            },
        )

    @app.get("/trips/{trip_id:path}/kml")
    async def trip_kml(
        trip_id: str, user: User = Depends(require_login)
    ):
        # A dedicated route rather than an addition to trip_file()
        # below: trip.kml doesn't exist as a real file in the trip's
        # folder (unlike trip.gpx, which trip_file() just serves
        # straight off disk) - it's generated on demand from
        # trip.gpx, same as trip_location() already generates its own
        # page from trip.gpx rather than reading a pre-written file.
        # Google Earth Pro opens trip.gpx directly (File > Open), but
        # Google Earth Web only accepts KML/KMZ via its own Import
        # flow - see kml_writer.gpx_to_kml()'s own docstring.
        trip = _find_trip(
            app.state.trip_cache, app.state.camera_config_cache, target, trip_id
        )
        if not trip.gpx:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="this trip has no GPS track",
            )

        kml = gpx_to_kml(trip.folder / GPX_FILENAME, name=trip.label)
        if kml is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="no valid GPS fix found in this trip's GPS track",
            )

        # trip_id may now be "camera-id/trip-folder" (see
        # scan_all_trips()'s own docstring) - the download filename
        # only wants the trip's own folder name, not a path, so take
        # the last "/" segment.
        kml_filename = trip_id.rsplit("/", 1)[-1]
        return Response(
            content=kml,
            media_type="application/vnd.google-earth.kml+xml",
            headers={
                "Content-Disposition": (
                    f'attachment; filename="{kml_filename}.kml"'
                )
            },
        )

    @app.get("/trips/{trip_id:path}/files/{filename}")
    async def trip_file(
        trip_id: str, filename: str, user: User = Depends(require_login)
    ):
        trip = _find_trip(
            app.state.trip_cache, app.state.camera_config_cache, target, trip_id
        )
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
        include_no_video: bool = Query(default=False),
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
        adapter_id = _find_camera_adapter_id(app.state.camera_config_cache, camera_id)
        recordings = scan_archive(archive_path, camera_id, adapter_id)

        # An empty `mode` (nothing checked) means "don't filter by
        # mode at all", not "show nothing" - see
        # archive_browser.filter_recordings()'s own docstring on why.
        selected_modes = set(mode)

        # See _archive_filter_flags()'s own docstring for the full
        # reasoning - short version: the checkbox is framed as the
        # opt-in "show me the video-less ones too" rather than an
        # opt-out "only show ones with video" that defaults to
        # checked, so an unchecked/unsubmitted checkbox correctly means
        # "no" whether this is a fresh page load or a real resubmission.
        videos_only, filters_active, show_clear_filters = _archive_filter_flags(
            include_no_video=include_no_video,
            selected_modes=selected_modes,
            timestamp=timestamp,
            from_=from_,
            until=until,
        )

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
                "include_no_video": include_no_video,
                "filters_active": filters_active,
                "show_clear_filters": show_clear_filters,
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
        adapter_id = _find_camera_adapter_id(app.state.camera_config_cache, camera_id)
        adapter = get_adapter(adapter_id)
        # A real per-recording probe (real telemetry, falling back to a
        # photo's EXIF tag or a video's own container location tag - see
        # recording_gps_available()'s own docstring), not the old
        # recording.has_gps property, which only ever checked for a
        # BlackVue .gps sidecar and so never lit up this link for a
        # folder/GoPro-adapter recording with a real GPS fix from EXIF
        # or a container location tag.
        gps_available = recording_gps_available(adapter, recording.recording)
        return templates.TemplateResponse(
            request,
            "archive_recording_detail.html",
            {
                "user": user,
                "camera_id": camera_id,
                "recording": recording,
                "gps_available": gps_available,
            },
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
        adapter_id = _find_camera_adapter_id(app.state.camera_config_cache, camera_id)
        adapter = get_adapter(adapter_id)

        start_coordinates = None
        start_google_maps_url = None
        start_address = None
        start_address_error = None
        start_error = None

        stop_coordinates = None
        stop_google_maps_url = None
        stop_address = None
        stop_address_error = None
        stop_error = None

        error = None

        # resolve_recording_gps_span() replaces the old two-branch
        # "real GPS log, or bust straight to EXIF/container-tag
        # fallback only when there's no GPS source at all" logic with
        # one shared call: real telemetry first, falling through to a
        # photo's EXIF GPS tag or a video's own ISO 6709 container
        # `location` tag whenever real telemetry comes up with zero
        # valid fixes - not just when recording_has_gps() is False
        # outright. That extra case (a GoPro-adapter recording that
        # declares GPS support but has no real GPMF track - a
        # stock/downloaded clip mixed into the archive) used to leave
        # this page stuck showing "no GPS log" even though a usable
        # fallback fix existed; see recording_gps_available()'s own
        # docstring in adapters/telemetry_bridge.py for the same gap
        # already fixed in cli/bv_ls.py's GPS column and bv-web's
        # archive detail page link (tasks #974-977, #998-999).
        #
        # The old first_valid_gps_fix()/last_valid_gps_fix() calls were
        # each wrapped in try/except MediaToolError, but
        # read_recording_gps() (which both of those, and
        # resolve_recording_gps_span(), ultimately call) already
        # swallows MediaToolError internally and returns an empty
        # tuple - that wrapper never actually fired, so it's dropped
        # here rather than carried forward.
        start_fix, stop_fix = resolve_recording_gps_span(adapter, recording.recording)

        if start_fix is None and stop_fix is None:
            error = "This recording has no GPS log."
        else:
            # Reverse-geocoded and cached under default_config_dir() -
            # bv-web's own writable scratch space (the same directory
            # CameraConfigCache/the job runner already use), NOT next to
            # the camera's archive the way trip_export.py's trip_info.txt
            # caches (destination.parent / ".osm_cache"): that convention
            # assumed a writable archive path, true for bv-cli's
            # container (which mounts /data/archive read-write) but
            # false for bv-web's own container - docker-compose.yml
            # mounts /data/archive read-only there (the archive browser
            # only ever reads recordings), so writing a cache anywhere
            # under it 500s with "Read-only file system" the moment
            # reverse geocoding is actually used. Real bug hit on
            # Christer's NAS - see WORKING_CONTEXT.md.
            geocode_cache_dir = default_config_dir() / ".osm_cache"

            if start_fix is None:
                start_error = (
                    "No valid GPS fix found in this recording's GPS "
                    "log (no signal)."
                )
            else:
                (
                    start_coordinates,
                    start_google_maps_url,
                    start_address,
                    start_address_error,
                ) = _describe_gps_fix(start_fix, geocode_cache_dir)

            if stop_fix is None:
                stop_error = (
                    "No valid GPS fix found in this recording's GPS "
                    "log (no signal)."
                )
            else:
                (
                    stop_coordinates,
                    stop_google_maps_url,
                    stop_address,
                    stop_address_error,
                ) = _describe_gps_fix(stop_fix, geocode_cache_dir)

        return templates.TemplateResponse(
            request,
            "archive_recording_location.html",
            {
                "user": user,
                "camera_id": camera_id,
                "recording_id": recording_id,
                "start_coordinates": start_coordinates,
                "start_google_maps_url": start_google_maps_url,
                "start_address": start_address,
                "start_address_error": start_address_error,
                "start_error": start_error,
                "stop_coordinates": stop_coordinates,
                "stop_google_maps_url": stop_google_maps_url,
                "stop_address": stop_address,
                "stop_address_error": stop_address_error,
                "stop_error": stop_error,
                "error": error,
            },
        )

    @app.get(
        "/archive/{camera_id}/{recording_id}/watch/{filename}",
        response_class=HTMLResponse,
    )
    async def archive_recording_watch(
        request: Request,
        camera_id: str,
        recording_id: str,
        filename: str,
        user: User = Depends(require_login),
    ):
        """A small page wrapping a single Front/Rear video in its own
        <video> element, with a normal in-page back link to the
        recording detail page - what the detail page's per-direction
        asset links point at now, instead of straight at
        archive_recording_file() itself. Christer's own reasoning:
        clicking a raw video URL plays it full-page with nothing but
        the browser's own back button (no Escape, no link) to get back
        to the recording; this page gives that a proper "back" link
        like every other page in the archive browser already has."""

        recording = _find_archive_recording(
            app.state.archive_recording_cache,
            app.state.camera_config_cache,
            camera_id,
            recording_id,
        )

        label = _video_label_for_filename(recording, filename)
        if label is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="file not found"
            )

        return templates.TemplateResponse(
            request,
            "archive_recording_watch.html",
            {
                "user": user,
                "camera_id": camera_id,
                "recording": recording,
                "filename": filename,
                "label": label,
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
        archive_root = _find_camera_archive(app.state.camera_config_cache, camera_id)
        path = recording.thumbnail_path(direction, archive_root=archive_root)
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

        is_video_file = filename in {
            video_filename for _, video_filename in recording.videos
        }

        # Parking-mode recordings' own video files fail ffmpeg's/
        # browsers' strict MP4 container validation outright (a known
        # BlackVue quirk - see WORKING_CONTEXT.md, "Correction: the
        # ffprobe failures aren't per-file corruption, they're a known
        # BlackVue container quirk" and the follow-up entry confirming
        # the fix against one of Christer's own real recordings), which
        # silently breaks playback here even though the file itself is
        # fine. Transparently swap in a repaired, cached copy for just
        # this one case - load_or_repair_parking_video() falls back to
        # `path` itself unchanged for anything outside the one narrow,
        # confirmed pattern it knows how to fix, so this is always safe
        # to try. Only for a Parking (P) recording's own video files,
        # never its GPS/g-sensor sidecars or another kind's video,
        # which were never affected by this quirk in the first place.
        if recording.recording.id.kind == "P" and is_video_file:
            cache_dir = default_config_dir() / ".parking_repair_cache"
            path = load_or_repair_parking_video(path, cache_dir)

        # Some recordings - a handful from when Christer's camera was
        # new and he was experimenting with HEVC/H.265 (see
        # WORKING_CONTEXT.md, task #704) - are HEVC, which Chrome/
        # Firefox's built-in <video> decoder can't play at all
        # (regardless of OS codec packs); the browser still plays the
        # file's audio track fine, which is exactly the "sound only,
        # no picture" symptom he reported. Transparently swap in a
        # transcoded H.264 copy for just this case -
        # open_hevc_preview_stream() falls back to `path` itself
        # unchanged for anything that isn't HEVC (the normal case for
        # the rest of the archive), so this is always safe to try.
        # Unlike a plain cache lookup, a fresh transcode comes back as
        # a live async byte stream instead of a Path - Christer asked
        # for playback to start before the whole file finishes
        # converting ("Can you convert the first 10 to 20%, start
        # playing that and during that time convert the rest?"), so
        # this streams the transcode straight to the browser instead
        # of blocking the request until it's fully done (see that
        # function's own docstring, and hevc_preview.py's "Progressive
        # (streaming) preview transcode" section, for the full story).
        if is_video_file:
            preview_cache_dir = default_config_dir() / ".hevc_preview_cache"
            result = await open_hevc_preview_stream(path, preview_cache_dir)
            if isinstance(result, Path):
                path = result
            else:
                return StreamingResponse(result, media_type="video/mp4")

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

    @app.get("/jobs/bv-generate", response_class=HTMLResponse)
    async def new_bv_generate_form(
        request: Request, user: User = Depends(require_owner)
    ):
        # "Reuse a previous run" (extended from bv-scribe's own pilot,
        # tasks #692-696, to bv-export/bv-generate/bv-search per
        # Christer: "why choose, when i can have both" - both the
        # in-page picklist here *and* bv-history's own Rerun links).
        # See new_bv_scribe_form()'s own comment for the full mechanism.
        recent_runs = _recent_web_runs("bv-generate")
        defaults, active_reuse_number = _reuse_defaults(
            recent_runs, request.query_params.get("reuse")
        )
        return templates.TemplateResponse(
            request,
            "job_new_bv_generate.html",
            {
                "user": user,
                "cameras": _camera_options(),
                "error": None,
                "defaults": defaults,
                "recent_runs": recent_runs,
                "active_reuse_number": active_reuse_number,
            },
        )

    @app.post("/jobs/bv-generate")
    async def new_bv_generate_submit(
        request: Request,
        id: str = Form(...),
        extract_audio: bool = Form(False),
        get_duration: bool = Form(False),
        thumbnail: bool = Form(False),
        transcribe: bool = Form(False),
        translate: str = Form(""),
        language: str = Form(""),
        model_size: str = Form(""),
        diarize: bool = Form(False),
        hf_token: str = Form(""),
        srt: bool = Form(False),
        describe_scene: bool = Form(False),
        scene_model: str = Form(""),
        camera: str = Form("front"),
        overwrite: bool = Form(False),
        dry_run: bool = Form(False),
        ignore_lock: bool = Form(False),
        from_: str = Form(""),
        until: str = Form(""),
        timestamp: str = Form(""),
        user: User = Depends(require_owner),
    ):
        # Blank optional text fields arrive as "" (HTML forms always
        # send a value for a present <input>, never omit it) - "" and
        # "not given at all" mean the same thing here, so normalize to
        # None the same way bv_generate.parse_args() itself defaults
        # them, rather than passing an empty string through to argv.
        # Raw, un-cleaned field values, keyed exactly like this route's
        # own Form(...) parameters - snapshotted for the "reuse a
        # previous run" feature (see Job.params's own docstring in
        # jobs.py, and new_bv_scribe_submit()'s own raw_params for the
        # same pattern). Captured before the stripping/None-ing below
        # so the GET form can prefill every field with exactly what was
        # actually typed/checked.
        raw_params = {
            "id": id,
            "extract_audio": extract_audio,
            "get_duration": get_duration,
            "thumbnail": thumbnail,
            "transcribe": transcribe,
            "translate": translate,
            "language": language,
            "model_size": model_size,
            "diarize": diarize,
            "hf_token": hf_token,
            "srt": srt,
            "describe_scene": describe_scene,
            "scene_model": scene_model,
            "camera": camera,
            "overwrite": overwrite,
            "dry_run": dry_run,
            "ignore_lock": ignore_lock,
            "from_": from_,
            "until": until,
            "timestamp": timestamp,
        }

        translate = translate.strip() or None
        language = language.strip() or None
        model_size = model_size.strip() or None
        hf_token = hf_token.strip() or None
        scene_model = scene_model.strip() or None
        from_ = from_.strip() or None
        until = until.strip() or None
        timestamp = timestamp.strip() or None

        # Mirrors bv_generate.parse_args()'s own cross-field checks
        # (see that module's docstring reasoning in jobs.py's
        # start_bv_generate) - re-checked here so a bad web form
        # re-renders with a friendly error instead of parse_args()
        # raising SystemExit(2) inside this route.
        error = None
        if not (
            extract_audio
            or get_duration
            or thumbnail
            or transcribe
            or translate
            or describe_scene
        ):
            error = (
                "Select at least one action: extract audio, compute "
                "duration, thumbnail, transcribe, translate, or "
                "describe scene."
            )
        elif diarize and not (transcribe or translate):
            error = "Label speakers requires transcribe or translate."
        elif srt and not (transcribe or translate):
            error = "SRT requires transcribe or translate."

        if error is not None:
            return templates.TemplateResponse(
                request,
                "job_new_bv_generate.html",
                {
                    "user": user,
                    "cameras": _camera_options(),
                    "error": error,
                    "defaults": {},
                    "recent_runs": [],
                    "active_reuse_number": None,
                },
                status_code=status.HTTP_400_BAD_REQUEST,
            )

        archive_path = _find_camera_archive(app.state.camera_config_cache, id)

        job = app.state.job_runner.start_bv_generate(
            camera_id=id,
            archive_path=archive_path,
            params=raw_params,
            from_=from_,
            until=until,
            timestamp=timestamp,
            extract_audio=extract_audio,
            get_duration=get_duration,
            thumbnail=thumbnail,
            transcribe=transcribe,
            translate=translate,
            language=language,
            model_size=model_size,
            diarize=diarize,
            hf_token=hf_token,
            srt=srt,
            describe_scene=describe_scene,
            scene_model=scene_model,
            camera=camera,
            overwrite=overwrite,
            dry_run=dry_run,
            ignore_lock=ignore_lock,
            username=user.username,
        )
        return RedirectResponse(
            url=f"/jobs/{job.id}", status_code=status.HTTP_303_SEE_OTHER
        )

    @app.get("/jobs/bv-export", response_class=HTMLResponse)
    async def new_bv_export_form(
        request: Request, user: User = Depends(require_owner)
    ):
        # "Reuse a previous run" - see new_bv_generate_form()'s own
        # comment above for the full story.
        recent_runs = _recent_web_runs("bv-export")
        defaults, active_reuse_number = _reuse_defaults(
            recent_runs, request.query_params.get("reuse")
        )
        return templates.TemplateResponse(
            request,
            "job_new_bv_export.html",
            {
                "user": user,
                "cameras": _camera_options(),
                "error": None,
                "defaults": defaults,
                "recent_runs": recent_runs,
                "active_reuse_number": active_reuse_number,
            },
        )

    @app.post("/jobs/bv-export")
    async def new_bv_export_submit(
        request: Request,
        id: str = Form(...),
        prefix: str = Form(""),
        from_: str = Form(""),
        until: str = Form(""),
        timestamp: str = Form(""),
        max_gap_minutes: str = Form(""),
        movement: bool = Form(False),
        gps_split: bool = Form(False),
        no_duration: bool = Form(False),
        duration_heal_archive: bool = Form(False),
        gap_tolerance_seconds: str = Form(""),
        max_parking_duration_minutes: str = Form(""),
        render_map: bool = Form(False),
        map_icon: str = Form(""),
        map_zoom_meters: str = Form(""),
        map_track_up: bool = Form(False),
        render_map_intro: bool = Form(False),
        map_intro_seconds: str = Form(""),
        render_gsensor: bool = Form(False),
        render_gsensor_graph: bool = Form(False),
        gsensor_graph_x: bool = Form(False),
        stitch: bool = Form(False),
        stitch_layout: str = Form("auto"),
        # stitch_mirror_size/_radius/_zoom/_pan_x/_pan_y and
        # stitch_gsensor_size below used to default to bv-export's own
        # CLI default (e.g. Form("40")) - matching the template's own
        # value="40" so the field showed a sensible starting point.
        # Christer, looking at a real job's shown replicate command:
        # "Why show all option, we should only show non default, or?"
        # The bug: once a field's own Form() default equals the CLI's
        # default, _clean() below can never tell "the user left this
        # untouched" apart from "the user typed the default value on
        # purpose" - both arrive here as that same non-empty string, so
        # start_bv_export()'s own `if x is not None: argv += [...]`
        # check always adds the flag, even for a field nobody touched.
        # Blank here (like every other optional field already was) plus
        # a `placeholder=` (not `value=`) in the template - a real hint
        # text, not a submitted value - fixes it: an untouched field now
        # actually arrives here empty, so _clean() turns it into None
        # and the flag is correctly omitted.
        stitch_mirror_size: str = Form(""),
        stitch_mirror_radius: str = Form(""),
        stitch_mirror_zoom: str = Form(""),
        stitch_mirror_pan_x: str = Form(""),
        stitch_mirror_pan_y: str = Form(""),
        stitch_mirror_icon: str = Form(""),
        stitch_resolution: str = Form(""),
        stitch_bitrate: str = Form(""),
        stitch_scale: str = Form(""),
        stitch_max_width: str = Form(""),
        stitch_max_height: str = Form(""),
        stitch_map: str = Form(""),
        stitch_map_side: str = Form(""),
        stitch_map_size: str = Form(""),
        stitch_map_circle: bool = Form(False),
        stitch_gsensor: bool = Form(False),
        stitch_gsensor_size: str = Form(""),
        stitch_gsensor_pos: str = Form(""),
        stitch_gsensor_xy: str = Form(""),
        stitch_graph: bool = Form(False),
        stitch_graph_side: str = Form(""),
        stitch_graph_size: str = Form(""),
        stitch_subtitles: bool = Form(False),
        no_subtitles_bg: bool = Form(False),
        include_parking: bool = Form(False),
        parking_speed: str = Form(""),
        trip_summary: bool = Form(False),
        scene_model: str = Form(""),
        scene_cpu: bool = Form(False),
        overwrite: bool = Form(False),
        dry_run: bool = Form(False),
        debug: bool = Form(False),
        user: User = Depends(require_owner),
    ):
        # Every text/number field arrives as a plain string (HTML forms
        # always send *something* for a present <input>, even an empty
        # one) - normalize blank to None uniformly here rather than
        # parsing each one to int/float in this route. jobs.py's
        # start_bv_export() just str()s whatever isn't None straight
        # into argv, so a numeric field's real parsing/range-checking
        # happens exactly once, inside bv_export.parse_args() itself
        # (via its own `type=` validators) - not duplicated here and
        # not left to FastAPI's automatic int/float coercion, which
        # would 422 on a blank optional field instead of re-rendering
        # this form the same friendly way an invalid value does (see
        # BvExportArgError below).
        def _clean(value: str) -> str | None:
            value = value.strip()
            return value or None

        # Raw, un-cleaned field values, keyed exactly like this route's
        # own Form(...) parameters - snapshotted for the "reuse a
        # previous run" feature, same pattern as new_bv_scribe_submit()'s
        # own raw_params. `target` is deliberately not included - it's
        # never a form field to begin with (see start_bv_export()'s own
        # docstring in jobs.py), so there's nothing to snapshot.
        raw_params = {
            "id": id,
            "prefix": prefix,
            "from_": from_,
            "until": until,
            "timestamp": timestamp,
            "max_gap_minutes": max_gap_minutes,
            "movement": movement,
            "gps_split": gps_split,
            "no_duration": no_duration,
            "duration_heal_archive": duration_heal_archive,
            "gap_tolerance_seconds": gap_tolerance_seconds,
            "max_parking_duration_minutes": max_parking_duration_minutes,
            "render_map": render_map,
            "map_icon": map_icon,
            "map_zoom_meters": map_zoom_meters,
            "map_track_up": map_track_up,
            "render_map_intro": render_map_intro,
            "map_intro_seconds": map_intro_seconds,
            "render_gsensor": render_gsensor,
            "render_gsensor_graph": render_gsensor_graph,
            "gsensor_graph_x": gsensor_graph_x,
            "stitch": stitch,
            "stitch_layout": stitch_layout,
            "stitch_mirror_size": stitch_mirror_size,
            "stitch_mirror_radius": stitch_mirror_radius,
            "stitch_mirror_zoom": stitch_mirror_zoom,
            "stitch_mirror_pan_x": stitch_mirror_pan_x,
            "stitch_mirror_pan_y": stitch_mirror_pan_y,
            "stitch_mirror_icon": stitch_mirror_icon,
            "stitch_resolution": stitch_resolution,
            "stitch_bitrate": stitch_bitrate,
            "stitch_scale": stitch_scale,
            "stitch_max_width": stitch_max_width,
            "stitch_max_height": stitch_max_height,
            "stitch_map": stitch_map,
            "stitch_map_side": stitch_map_side,
            "stitch_map_size": stitch_map_size,
            "stitch_map_circle": stitch_map_circle,
            "stitch_gsensor": stitch_gsensor,
            "stitch_gsensor_size": stitch_gsensor_size,
            "stitch_gsensor_pos": stitch_gsensor_pos,
            "stitch_gsensor_xy": stitch_gsensor_xy,
            "stitch_graph": stitch_graph,
            "stitch_graph_side": stitch_graph_side,
            "stitch_graph_size": stitch_graph_size,
            "stitch_subtitles": stitch_subtitles,
            "no_subtitles_bg": no_subtitles_bg,
            "include_parking": include_parking,
            "parking_speed": parking_speed,
            "trip_summary": trip_summary,
            "scene_model": scene_model,
            "scene_cpu": scene_cpu,
            "overwrite": overwrite,
            "dry_run": dry_run,
            "debug": debug,
        }

        job_runner = app.state.job_runner
        archive_path = _find_camera_archive(app.state.camera_config_cache, id)

        try:
            job = job_runner.start_bv_export(
                camera_id=id,
                archive_path=archive_path,
                params=raw_params,
                target=app.state.target,
                prefix=_clean(prefix),
                from_=_clean(from_),
                until=_clean(until),
                timestamp=_clean(timestamp),
                max_gap_minutes=_clean(max_gap_minutes),
                movement=movement,
                gps_split=gps_split,
                no_duration=no_duration,
                duration_heal_archive=duration_heal_archive,
                gap_tolerance_seconds=_clean(gap_tolerance_seconds),
                max_parking_duration_minutes=_clean(max_parking_duration_minutes),
                render_map=render_map,
                map_icon=_clean(map_icon),
                map_zoom_meters=_clean(map_zoom_meters),
                map_track_up=map_track_up,
                render_map_intro=render_map_intro,
                map_intro_seconds=_clean(map_intro_seconds),
                render_gsensor=render_gsensor,
                render_gsensor_graph=render_gsensor_graph,
                gsensor_graph_x=gsensor_graph_x,
                stitch=stitch,
                stitch_layout=_clean(stitch_layout) or "auto",
                stitch_mirror_size=_clean(stitch_mirror_size),
                stitch_mirror_radius=_clean(stitch_mirror_radius),
                stitch_mirror_zoom=_clean(stitch_mirror_zoom),
                stitch_mirror_pan_x=_clean(stitch_mirror_pan_x),
                stitch_mirror_pan_y=_clean(stitch_mirror_pan_y),
                stitch_mirror_icon=_clean(stitch_mirror_icon),
                stitch_resolution=_clean(stitch_resolution),
                stitch_bitrate=_clean(stitch_bitrate),
                stitch_scale=_clean(stitch_scale),
                stitch_max_width=_clean(stitch_max_width),
                stitch_max_height=_clean(stitch_max_height),
                stitch_map=_clean(stitch_map),
                stitch_map_side=_clean(stitch_map_side),
                stitch_map_size=_clean(stitch_map_size),
                stitch_map_circle=stitch_map_circle,
                stitch_gsensor=stitch_gsensor,
                stitch_gsensor_size=_clean(stitch_gsensor_size),
                stitch_gsensor_pos=_clean(stitch_gsensor_pos),
                stitch_gsensor_xy=_clean(stitch_gsensor_xy),
                stitch_graph=stitch_graph,
                stitch_graph_side=_clean(stitch_graph_side),
                stitch_graph_size=_clean(stitch_graph_size),
                stitch_subtitles=stitch_subtitles,
                no_subtitles_bg=no_subtitles_bg,
                include_parking=include_parking,
                parking_speed=_clean(parking_speed),
                trip_summary=trip_summary,
                scene_model=_clean(scene_model),
                scene_cpu=scene_cpu,
                overwrite=overwrite,
                dry_run=dry_run,
                debug=debug,
                username=user.username,
            )
        except BvExportArgError as exc:
            return templates.TemplateResponse(
                request,
                "job_new_bv_export.html",
                {
                    "user": user,
                    "cameras": _camera_options(),
                    "error": str(exc),
                    "defaults": {},
                    "recent_runs": [],
                    "active_reuse_number": None,
                },
                status_code=status.HTTP_400_BAD_REQUEST,
            )

        return RedirectResponse(
            url=f"/jobs/{job.id}", status_code=status.HTTP_303_SEE_OTHER
        )

    @app.get("/jobs/bv-download", response_class=HTMLResponse)
    async def new_bv_download_form(
        request: Request, user: User = Depends(require_owner)
    ):
        return templates.TemplateResponse(
            request,
            "job_new_bv_download.html",
            {
                "user": user,
                "cameras": _camera_options(),
                "kind_options": kind_options(),
                "error": None,
            },
        )

    @app.post("/jobs/bv-download")
    async def new_bv_download_submit(
        request: Request,
        id: str = Form(...),
        timeout: int = Form(5, ge=1),
        mode: list[str] = Form([]),
        from_: str = Form(""),
        until: str = Form(""),
        timestamp: str = Form(""),
        dry_run: bool = Form(False),
        files: bool = Form(False),
        verbose: bool = Form(False),
        trace: bool = Form(False),
        user: User = Depends(require_owner),
    ):
        # Mirrors bv_download.parse_args()'s own "--files requires
        # --dry-run" check (see jobs.py's start_bv_download docstring
        # for why this one condition gets a plain pre-check here
        # rather than a BvExportArgError-style exception class) - so a
        # bad web form re-renders with a friendly error instead of
        # parse_args() raising SystemExit(2) inside this route.
        error = None
        if files and not dry_run:
            error = "List every file requires dry run."

        if error is not None:
            return templates.TemplateResponse(
                request,
                "job_new_bv_download.html",
                {
                    "user": user,
                    "cameras": _camera_options(),
                    "kind_options": kind_options(),
                    "error": error,
                },
                status_code=status.HTTP_400_BAD_REQUEST,
            )

        job = app.state.job_runner.start_bv_download(
            id_=id,
            timeout=timeout,
            modes=mode,
            from_=from_.strip() or None,
            until=until.strip() or None,
            timestamp=timestamp.strip() or None,
            dry_run=dry_run,
            files=files,
            verbose=verbose,
            trace=trace,
            username=user.username,
        )
        return RedirectResponse(
            url=f"/jobs/{job.id}", status_code=status.HTTP_303_SEE_OTHER
        )

    @app.get("/jobs/bv-ls", response_class=HTMLResponse)
    async def new_bv_ls_form(
        request: Request, user: User = Depends(require_owner)
    ):
        return templates.TemplateResponse(
            request,
            "job_new_bv_ls.html",
            {"user": user, "cameras": _camera_options(), "error": None},
        )

    @app.post("/jobs/bv-ls")
    async def new_bv_ls_submit(
        request: Request,
        id: str = Form(...),
        all: bool = Form(False),
        full: bool = Form(False),
        from_: str = Form(""),
        until: str = Form(""),
        timestamp: str = Form(""),
        source: str = Form(""),
        trips: bool = Form(False),
        max_gap_minutes: str = Form(""),
        movement: bool = Form(False),
        gps_split: bool = Form(False),
        no_duration: bool = Form(False),
        gap_tolerance_seconds: str = Form(""),
        user: User = Depends(require_owner),
    ):
        # max_gap_minutes/gap_tolerance_seconds are the only two
        # numeric fields bv-ls's own CLI has, and both are genuinely
        # optional (a blank field means "use bv-ls's own default", not
        # zero) - a plain int(...) with a friendly re-render on
        # ValueError is enough here, unlike bv-export's much larger
        # numeric surface (see jobs.py's BvExportArgError docstring),
        # since neither of these has a range to also enforce beyond
        # "is this a whole number at all".
        error = None
        max_gap_minutes_value: int | None = None
        gap_tolerance_seconds_value: int | None = None

        if max_gap_minutes.strip():
            try:
                max_gap_minutes_value = int(max_gap_minutes.strip())
            except ValueError:
                error = "Max gap must be a whole number of minutes."

        if error is None and gap_tolerance_seconds.strip():
            try:
                gap_tolerance_seconds_value = int(gap_tolerance_seconds.strip())
            except ValueError:
                error = "Gap tolerance must be a whole number of seconds."

        if error is not None:
            return templates.TemplateResponse(
                request,
                "job_new_bv_ls.html",
                {"user": user, "cameras": _camera_options(), "error": error},
                status_code=status.HTTP_400_BAD_REQUEST,
            )

        archive_path = _find_camera_archive(app.state.camera_config_cache, id)

        job = app.state.job_runner.start_bv_ls(
            camera_id=id,
            archive_path=archive_path,
            all=all,
            full=full,
            from_=from_.strip() or None,
            until=until.strip() or None,
            timestamp=timestamp.strip() or None,
            source=source.strip() or None,
            trips=trips,
            max_gap_minutes=max_gap_minutes_value,
            movement=movement,
            gps_split=gps_split,
            duration=not no_duration,
            gap_tolerance_seconds=gap_tolerance_seconds_value,
            username=user.username,
        )
        return RedirectResponse(
            url=f"/jobs/{job.id}", status_code=status.HTTP_303_SEE_OTHER
        )

    @app.get("/jobs/bv-lock", response_class=HTMLResponse)
    async def new_bv_lock_form(
        request: Request, user: User = Depends(require_owner)
    ):
        return templates.TemplateResponse(
            request,
            "job_new_bv_lock.html",
            {
                "user": user,
                "cameras": _camera_options(),
                "lockable_assets": sorted(LOCKABLE_ASSETS),
                "error": None,
            },
        )

    @app.post("/jobs/bv-lock")
    async def new_bv_lock_submit(
        request: Request,
        id: str = Form(...),
        mode: str = Form("lock"),
        from_: str = Form(""),
        until: str = Form(""),
        timestamp: str = Form(""),
        assets: list[str] = Form([]),
        assets_all: bool = Form(False),
        user: User = Depends(require_owner),
    ):
        # "list" ignores the range/asset fields entirely, same as
        # bv-lock's own CLI --list does - nothing to validate for it.
        # "lock"/"unlock" need at least one asset name, same
        # requirement cli/bv_lock.py's own --lock-assets/--unlock-
        # assets enforce (a bare "" would otherwise silently lock/
        # unlock nothing). assets_all is its own checkbox rather than
        # just another item in the `assets` list, so the "select all
        # seven individually" and "check the one All box" cases can't
        # both need to be handled by the template's own JS - it maps
        # straight onto --lock-assets/--unlock-assets' own "all" alias
        # (see cli/bv_lock.py's _split_assets()).
        error = None
        if mode not in ("lock", "unlock", "list"):
            error = "Unknown mode."
        elif mode != "list" and not assets_all and not assets:
            error = "Choose at least one asset type, or All."

        if error is not None:
            return templates.TemplateResponse(
                request,
                "job_new_bv_lock.html",
                {
                    "user": user,
                    "cameras": _camera_options(),
                    "lockable_assets": sorted(LOCKABLE_ASSETS),
                    "error": error,
                },
                status_code=status.HTTP_400_BAD_REQUEST,
            )

        archive_path = _find_camera_archive(app.state.camera_config_cache, id)

        job = app.state.job_runner.start_bv_lock(
            camera_id=id,
            archive_path=archive_path,
            mode=mode,
            from_=from_.strip() or None,
            until=until.strip() or None,
            timestamp=timestamp.strip() or None,
            assets=["all"] if assets_all else assets,
            username=user.username,
        )
        return RedirectResponse(
            url=f"/jobs/{job.id}", status_code=status.HTTP_303_SEE_OTHER
        )

    @app.get("/jobs/bv-scribe", response_class=HTMLResponse)
    async def new_bv_scribe_form(
        request: Request, user: User = Depends(require_owner)
    ):
        # "Reuse a previous run's parameters" (task following #691 -
        # Christer: "i would like to have a button or something like
        # in bv-web to get the latest run parameters filled in for
        # bv-web or maybe a list of the latest"). recent_runs feeds the
        # picklist; _reuse_defaults() picks the active one - whatever
        # ?reuse=<N> names if it's real, else the most recent run, else
        # {} (falls back to this form's own ordinary hardcoded
        # defaults, unchanged from before this feature existed).
        recent_runs = _recent_web_runs("bv-scribe")
        defaults, active_reuse_number = _reuse_defaults(
            recent_runs, request.query_params.get("reuse")
        )
        return templates.TemplateResponse(
            request,
            "job_new_bv_scribe.html",
            {
                "user": user,
                "cameras": _camera_options(),
                "error": None,
                "defaults": defaults,
                "recent_runs": recent_runs,
                "active_reuse_number": active_reuse_number,
            },
        )

    @app.post("/jobs/bv-scribe")
    async def new_bv_scribe_submit(
        request: Request,
        id: str = Form(...),
        from_: str = Form(""),
        until: str = Form(""),
        timestamp: str = Form(""),
        task: str = Form("both"),
        camera: str = Form("front"),
        model: str = Form(""),
        # Advanced sampling/model (job_new_bv_scribe.html's "Advanced
        # sampling & model" <details>, collapsed by default) - full
        # parity, same "collapsed but not curated away" treatment
        # bv-export's own advanced sections got. See start_bv_scribe()'s
        # docstring in jobs.py for the full story behind this change.
        fps: str = Form(""),
        max_frames: str = Form(""),
        max_pixels: str = Form(""),
        resized_width: str = Form(""),
        resized_height: str = Form(""),
        crop_top: str = Form(""),
        crop_bottom: str = Form(""),
        max_new_tokens: str = Form(""),
        repetition_penalty: str = Form(""),
        no_repeat_ngram_size: str = Form(""),
        do_sample: bool = Form(False),
        temperature: str = Form(""),
        top_p: str = Form(""),
        top_k: str = Form(""),
        # Advanced zoom detection (its own <details>)
        no_zoom_signs: bool = Form(False),
        zoom_frames: str = Form(""),
        zoom_detect_width: str = Form(""),
        zoom_padding: str = Form(""),
        zoom_ocr_width: str = Form(""),
        zoom_max_new_tokens: str = Form(""),
        zoom_detect_max_new_tokens: str = Form(""),
        zoom_repetition_penalty: str = Form(""),
        zoom_no_repeat_ngram_size: str = Form(""),
        no_zoom_plate_confidence_check: bool = Form(False),
        cpu: bool = Form(False),
        overwrite: bool = Form(False),
        dry_run: bool = Form(False),
        verbose: bool = Form(False),
        user: User = Depends(require_owner),
    ):
        # Every numeric field is optional text, cleaned to str | None
        # here and passed straight through - jobs.py's start_bv_scribe()
        # just str()s whatever isn't None into argv, so the real
        # parsing/range-checking happens exactly once, inside
        # bv_scribe.parse_args() itself. Same convention
        # new_bv_export_submit()'s own _clean() helper already uses.
        def _clean(value: str) -> str | None:
            value = value.strip()
            return value or None

        # Raw, un-cleaned field values, keyed exactly like this route's
        # own Form(...) parameters (and this template's own <input
        # name=...> attributes) - snapshotted for the "reuse a
        # previous run's parameters" feature (see Job.params's own
        # docstring in jobs.py). Captured here, before _clean()/the
        # zoom_signs-style inversions below, so the GET form can
        # prefill every field with exactly what was actually typed
        # /checked, not a reprocessed version of it.
        raw_params = {
            "id": id,
            "from_": from_,
            "until": until,
            "timestamp": timestamp,
            "task": task,
            "camera": camera,
            "model": model,
            "fps": fps,
            "max_frames": max_frames,
            "max_pixels": max_pixels,
            "resized_width": resized_width,
            "resized_height": resized_height,
            "crop_top": crop_top,
            "crop_bottom": crop_bottom,
            "max_new_tokens": max_new_tokens,
            "repetition_penalty": repetition_penalty,
            "no_repeat_ngram_size": no_repeat_ngram_size,
            "do_sample": do_sample,
            "temperature": temperature,
            "top_p": top_p,
            "top_k": top_k,
            "no_zoom_signs": no_zoom_signs,
            "zoom_frames": zoom_frames,
            "zoom_detect_width": zoom_detect_width,
            "zoom_padding": zoom_padding,
            "zoom_ocr_width": zoom_ocr_width,
            "zoom_max_new_tokens": zoom_max_new_tokens,
            "zoom_detect_max_new_tokens": zoom_detect_max_new_tokens,
            "zoom_repetition_penalty": zoom_repetition_penalty,
            "zoom_no_repeat_ngram_size": zoom_no_repeat_ngram_size,
            "no_zoom_plate_confidence_check": no_zoom_plate_confidence_check,
            "cpu": cpu,
            "overwrite": overwrite,
            "dry_run": dry_run,
            "verbose": verbose,
        }

        archive_path = _find_camera_archive(app.state.camera_config_cache, id)

        job = app.state.job_runner.start_bv_scribe(
            camera_id=id,
            archive_path=archive_path,
            params=raw_params,
            from_=_clean(from_),
            until=_clean(until),
            timestamp=_clean(timestamp),
            task=task,
            camera=camera,
            model=_clean(model),
            fps=_clean(fps),
            max_frames=_clean(max_frames),
            max_pixels=_clean(max_pixels),
            resized_width=_clean(resized_width),
            resized_height=_clean(resized_height),
            crop_top=_clean(crop_top),
            crop_bottom=_clean(crop_bottom),
            max_new_tokens=_clean(max_new_tokens),
            repetition_penalty=_clean(repetition_penalty),
            no_repeat_ngram_size=_clean(no_repeat_ngram_size),
            do_sample=do_sample,
            temperature=_clean(temperature),
            top_p=_clean(top_p),
            top_k=_clean(top_k),
            zoom_signs=not no_zoom_signs,
            zoom_frames=_clean(zoom_frames),
            zoom_detect_width=_clean(zoom_detect_width),
            zoom_padding=_clean(zoom_padding),
            zoom_ocr_width=_clean(zoom_ocr_width),
            zoom_max_new_tokens=_clean(zoom_max_new_tokens),
            zoom_detect_max_new_tokens=_clean(zoom_detect_max_new_tokens),
            zoom_repetition_penalty=_clean(zoom_repetition_penalty),
            zoom_no_repeat_ngram_size=_clean(zoom_no_repeat_ngram_size),
            zoom_plate_confidence_check=not no_zoom_plate_confidence_check,
            cpu=cpu,
            overwrite=overwrite,
            dry_run=dry_run,
            verbose=verbose,
            username=user.username,
        )
        return RedirectResponse(
            url=f"/jobs/{job.id}", status_code=status.HTTP_303_SEE_OTHER
        )

    @app.get("/jobs/bv-search", response_class=HTMLResponse)
    async def new_bv_search_form(
        request: Request, user: User = Depends(require_viewer_or_owner)
    ):
        # "Reuse a previous run" - see new_bv_generate_form()'s own
        # comment above for the full story.
        recent_runs = _recent_web_runs("bv-search")
        defaults, active_reuse_number = _reuse_defaults(
            recent_runs, request.query_params.get("reuse")
        )
        return templates.TemplateResponse(
            request,
            "job_new_bv_search.html",
            {
                "user": user,
                "cameras": _camera_options(),
                "error": None,
                "defaults": defaults,
                "recent_runs": recent_runs,
                "active_reuse_number": active_reuse_number,
            },
        )

    @app.post("/jobs/bv-search")
    async def new_bv_search_submit(
        request: Request,
        id: str = Form(...),
        from_: str = Form(""),
        until: str = Form(""),
        timestamp: str = Form(""),
        text: str = Form(""),
        asset: str = Form("all"),
        regex: bool = Form(False),
        case_sensitive: bool = Form(False),
        near: str = Form(""),
        place: str = Form(""),
        radius: str = Form(""),
        trace: bool = Form(False),
        user: User = Depends(require_viewer_or_owner),
    ):
        # Raw, un-cleaned field values, keyed exactly like this route's
        # own Form(...) parameters - snapshotted for the "reuse a
        # previous run" feature, same pattern as new_bv_scribe_submit()'s
        # own raw_params. Captured before the stripping/None-ing below.
        raw_params = {
            "id": id,
            "from_": from_,
            "until": until,
            "timestamp": timestamp,
            "text": text,
            "asset": asset,
            "regex": regex,
            "case_sensitive": case_sensitive,
            "near": near,
            "place": place,
            "radius": radius,
            "trace": trace,
        }

        text = text.strip() or None
        near = near.strip() or None
        place = place.strip() or None
        radius = radius.strip()

        # Small number of conditions to re-check (bv-search has
        # nowhere near bv-export's dozens of validators) - a plain
        # pre-check, same "small number of conditions -> a plain
        # pre-check" approach start_bv_ls()/start_bv_download()'s own
        # routes already use, rather than a BvExportArgError-style
        # exception class built for a much larger validator surface.
        # Mirrors bv-search's own _run() "give at least one criterion"
        # check and parse_args()'s --near/--place mutually-exclusive
        # group.
        error = None
        if not (text or near or place):
            error = "Give at least one of: text, near coordinates, or place."
        elif near and place:
            error = "Near coordinates and place are mutually exclusive."
        elif near is not None:
            # Deferred import - app.py otherwise never imports a cli.*
            # module directly (that's jobs.py's job); this is a single
            # small, private parsing helper reused as-is rather than
            # duplicated, not a reason to import the whole module at
            # app.py's own top level.
            from ..cli.bv_search import _parse_coordinates

            try:
                _parse_coordinates(near)
            except ArgumentTypeError as exc:
                error = str(exc)

        radius_value: float | None = None
        if error is None and radius:
            try:
                radius_value = float(radius)
            except ValueError:
                error = "Radius must be a number."

        if error is not None:
            return templates.TemplateResponse(
                request,
                "job_new_bv_search.html",
                {
                    "user": user,
                    "cameras": _camera_options(),
                    "error": error,
                    "defaults": {},
                    "recent_runs": [],
                    "active_reuse_number": None,
                },
                status_code=status.HTTP_400_BAD_REQUEST,
            )

        archive_path = _find_camera_archive(app.state.camera_config_cache, id)

        job = app.state.job_runner.start_bv_search(
            camera_id=id,
            archive_path=archive_path,
            params=raw_params,
            from_=from_.strip() or None,
            until=until.strip() or None,
            timestamp=timestamp.strip() or None,
            text=text,
            asset=asset,
            regex=regex,
            case_sensitive=case_sensitive,
            near=near,
            place=place,
            radius=radius_value,
            trace=trace,
            username=user.username,
        )
        return RedirectResponse(
            url=f"/jobs/{job.id}", status_code=status.HTTP_303_SEE_OTHER
        )

    @app.post("/jobs/bv-search/transcribe")
    async def transcribe_voice_search(
        audio: UploadFile = File(...),
        user: User = Depends(require_viewer_or_owner),
    ):
        """Quick, synchronous voice-to-text for the bv-search form's
        "Search by voice" button - deliberately NOT a JobRunner
        background Job the way bv-search itself is. Same "on-demand
        action, not a multi-minute run" precedent
        archive_recording_thumbnail()/archive_recording_file()'s HEVC-
        preview branch already set: a few-second spoken query
        transcribes in well under a second to a few seconds on
        model_size="base", nowhere near needing the Job/polling/
        history machinery built for bv-generate/bv-export's multi-
        minute runs.

        Returns the raw transcript as plain text - the frontend fills
        it into the #text search field rather than submitting it
        directly. A first-pass Whisper transcript of a spoken query
        (place names, camera-specific terms, ...) is exactly the kind
        of thing a user should get to correct before it becomes a
        literal search string, especially with --regex/--case-
        sensitive available right next to it.
        """

        suffix = Path(audio.filename or "").suffix or ".webm"
        with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp:
            tmp_path = Path(tmp.name)
            tmp.write(await audio.read())

        try:
            result = transcribe(tmp_path, model_size="base")
        except MediaToolError as exc:
            return JSONResponse(
                {"error": str(exc)}, status_code=status.HTTP_422_UNPROCESSABLE_ENTITY
            )
        finally:
            tmp_path.unlink(missing_ok=True)

        return JSONResponse({"text": result.text.strip()})

    @app.get("/jobs/{job_id}", response_class=HTMLResponse)
    async def job_detail(
        request: Request, job_id: str, user: User = Depends(require_viewer_or_owner)
    ):
        job = _find_job(app.state.job_runner, job_id)
        _authorize_job_view(job, user)
        job_status, output, prompt = job.snapshot()
        # A "paused" query param lets the owner freeze the page on its
        # current snapshot - same server-rendered-link pattern the
        # archive browser's filters already use - and resume by
        # dropping it. This now also gates job_detail.html's own JS
        # poll loop (task #772-776, WORKING_CONTEXT.md), not just the
        # old full-page <meta refresh> it replaced: the template only
        # starts polling /jobs/{id}/poll when status == "running" and
        # not paused, exactly the same condition that used to gate the
        # meta tag.
        paused = request.query_params.get("paused") == "1"
        # Quick-tail view (task #687): a long-running job (a 902-
        # recording bv-scribe batch is the real example that prompted
        # this) accumulates thousands of output lines. `?tail=1`
        # renders only the most recent TAIL_LINE_COUNT lines instead of
        # the full history - both this initial render and every /poll
        # tick after it (see _sliced_job_output()) honor the same flag,
        # carried forward via the poll URL job_detail.html builds. A
        # finished job's full output is comparatively cheap to keep
        # around/re-render (no more poll ticks coming), so tailing only
        # actually matters - and is only offered - while running.
        tail_requested = request.query_params.get("tail") == "1"
        tail_active, displayed_output, tail_truncated_count = _sliced_job_output(
            job_status, output, tail_requested
        )
        camera_id = _job_camera_id(job)
        response = templates.TemplateResponse(
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
                "output": displayed_output,
                "prompt": prompt,
                "paused": paused,
                "camera_id": camera_id,
                "tail_active": tail_active,
                "tail_truncated_count": tail_truncated_count,
                "tail_line_count": TAIL_LINE_COUNT,
                # Links built here rather than string-concatenated in
                # the template, so toggling one of paused/tail never
                # silently drops the other's own query param.
                "tail_on_url": f"/jobs/{job.id}?tail=1" + ("&paused=1" if paused else ""),
                "tail_off_url": f"/jobs/{job.id}" + ("?paused=1" if paused else ""),
                "pause_url": f"/jobs/{job.id}?paused=1" + ("&tail=1" if tail_requested else ""),
                "resume_url": f"/jobs/{job.id}" + ("?tail=1" if tail_requested else ""),
            },
        )
        # Christer noticed that navigating away from a running job (e.g.
        # to start bv-ls) and then hitting the browser's Back button
        # showed stale output until he manually reloaded. That's the
        # browser's back/forward cache (bfcache) restoring the exact DOM
        # snapshot from when he left instead of asking the server for
        # anything - the <meta refresh> above never gets a chance to
        # fire again since no new page load happens. no-store tells the
        # browser this response (and therefore Back to it) is never
        # allowed to come from cache, only from a fresh request - so
        # Back now shows current status/output the same as a reload.
        response.headers["Cache-Control"] = "no-store"
        return response

    @app.get("/jobs/{job_id}/poll")
    async def job_poll(
        request: Request, job_id: str, user: User = Depends(require_viewer_or_owner)
    ):
        """AJAX sibling of job_detail() (task #772-776, WORKING_CONTEXT.md).

        job_detail.html used to auto-refresh via a full <meta
        http-equiv="refresh"> page reload every 2s while a job ran.
        Christer hit two real problems with that: it snapped scroll back
        to the top on every tick, and - worse - resizing/restoring the
        browser window while a GPU-heavy bv-generate job (NVENC encode
        and/or CUDA Whisper/scene-description inference) was running
        could line up with a reload and tip an already GPU-starved
        display driver into a TDR reset (screen corruption, audio
        glitch). This route returns just the current status and freshly
        rendered output HTML so job_detail.html's own poll loop can
        patch #job-output in place instead - same slicing/permission
        rules as job_detail() itself (see _authorize_job_view() and
        _sliced_job_output()'s own docstrings for why those had to be
        shared helpers, not just copied), so a poll tick can never
        disagree with how the page first rendered.
        """
        job = _find_job(app.state.job_runner, job_id)
        _authorize_job_view(job, user)
        job_status, output, _prompt = job.snapshot()
        tail_requested = request.query_params.get("tail") == "1"
        tail_active, displayed_output, tail_truncated_count = _sliced_job_output(
            job_status, output, tail_requested
        )
        camera_id = _job_camera_id(job)
        output_html = templates.env.get_template("_job_output_lines.html").render(
            output=displayed_output, camera_id=camera_id
        )
        response = JSONResponse(
            {
                "status": job_status.value,
                "output_html": output_html,
                "tail_truncated_count": tail_truncated_count,
            }
        )
        # Same reasoning as job_detail()'s own no-store header - a
        # bfcache-restored poll response would show stale output/status
        # forever since nothing re-triggers the fetch loop.
        response.headers["Cache-Control"] = "no-store"
        return response

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

    @app.get("/history", response_class=HTMLResponse)
    async def history_list(
        request: Request,
        user: User = Depends(require_owner),
        command: str | None = Query(default=None),
        camera: str | None = Query(default=None),
        timestamp: str | None = Query(default=None),
        from_: str | None = Query(default=None, alias="from"),
        until: str | None = Query(default=None, alias="until"),
        failed_only: bool = Query(default=False),
        search: str | None = Query(default=None),
        source: str | None = Query(default=None),
        show_all: bool = Query(default=False, alias="all"),
    ):
        # Same "" vs None GET-form normalization archive_recording_list()
        # above already needs and explains in its own comment - a text
        # box left blank still arrives as "", not an absent param.
        command = command or None
        camera = camera or None
        timestamp = timestamp or None
        from_ = from_ or None
        until = until or None
        search = search or None
        source = source or None

        error = None
        matches: list = []
        try:
            matches = filtered_entries(
                HistoryFilter(
                    command=command,
                    camera=camera,
                    since=from_,
                    until=until,
                    timestamp=timestamp,
                    failed_only=failed_only,
                    search=search,
                    source=source,
                ),
                entries=all_entries(),
            )
        except ValueError as exc:
            error = str(exc)

        shown = matches if show_all else tail(matches)
        truncated = not show_all and len(shown) < len(matches)
        # bv-history (the CLI) keeps oldest-first, matching bash/pwsh's
        # own `history` convention (see blackvue/history.py's module
        # docstring) - but Christer wants the *web* page the other way
        # round: "i would like to show history in a descending order
        # so the latest commands are on top." tail() above still picks
        # the most recent `count` entries first, oldest-first within
        # that slice; this just flips the final display order, it
        # doesn't change which entries get shown or their `.number`s.
        shown = list(reversed(shown))

        return templates.TemplateResponse(
            request,
            "history_list.html",
            {
                "user": user,
                "entries": shown,
                "truncated": truncated,
                "total_matches": len(matches),
                "command_value": command or "",
                "camera_value": camera or "",
                "timestamp_value": timestamp or "",
                "from_value": from_ or "",
                "until_value": until or "",
                "search_value": search or "",
                "source_value": source or "",
                "failed_only": failed_only,
                "show_all": show_all,
                "error": error,
                "reuse_supported_commands": _REUSE_SUPPORTED_COMMANDS,
            },
        )

    @app.get("/history/{number}", response_class=HTMLResponse)
    async def history_detail(
        request: Request, number: int, user: User = Depends(require_owner)
    ):
        match = next((e for e in all_entries() if e.number == number), None)
        if match is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"No history entry numbered {number}.",
            )

        lines = matching_log_lines(match.entry)

        return templates.TemplateResponse(
            request,
            "history_detail.html",
            {
                "user": user,
                "numbered": match,
                "lines": lines,
                "reuse_supported_commands": _REUSE_SUPPORTED_COMMANDS,
            },
        )

    return app


# Commands the "reuse a previous run" feature supports - both the
# in-page picklist (see new_bv_scribe_form()'s own comment) and the
# "Rerun with these options" links on /history and /history/{number}
# below. Matches _recent_web_runs()'s own callers exactly; bv-download/
# bv-ls/bv-lock/bv-config/bv-gps are deliberately excluded (either too
# few reusable fields to be worth it, or - bv-config/bv-gps - not
# really "reusable" runs at all).
_REUSE_SUPPORTED_COMMANDS = {"bv-scribe", "bv-export", "bv-generate", "bv-search"}


def _recent_web_runs(command: str) -> list[NumberedEntry]:
    """Every past bv-web-triggered run of `command` that has a params
    snapshot (see Job.params/HistoryEntry.params's own docstrings),
    newest first - the "reuse a previous run's parameters" feature's
    data source (Christer: "i would like to have a button or something
    like in bv-web to get the latest run parameters filled in for
    bv-web or maybe a list of the latest"). Not capped here, and every
    caller now shows the full list uncapped too (Christer: "i dont
    want to be restricted to 5" - the earlier `[:5]` slice at each
    call site is gone; the reuse panel's `.reuse-list` CSS scrolls
    instead of growing the page once there are many entries).

    A CLI-sourced entry has no web form to have snapshotted, and a
    bv-web entry recorded before this feature existed has
    entry.params == None - both filtered out, since there's nothing
    to reuse from either.
    """

    matches = filtered_entries(HistoryFilter(command=command, source="bv-web"))
    with_params = [numbered for numbered in matches if numbered.entry.params]
    with_params.reverse()
    return with_params


def _reuse_defaults(
    recent_runs: list[NumberedEntry], reuse_param: str | None
) -> tuple[dict, int | None]:
    """Resolve a job-trigger form's `defaults` dict plus which entry
    (if any) is the active one - `?reuse=<N>` if it names a real entry
    in `recent_runs`, otherwise the most recent entry (the "prefill
    with the latest run automatically" half of the feature), otherwise
    `({}, None)` when there's no history yet - the form then just
    falls back to its own ordinary hardcoded defaults, unchanged from
    before this feature existed.
    """

    if reuse_param is not None:
        try:
            reuse_number = int(reuse_param)
        except ValueError:
            reuse_number = None
        if reuse_number is not None:
            match = next(
                (n for n in recent_runs if n.number == reuse_number), None
            )
            if match is not None:
                return match.entry.params or {}, match.number

    if recent_runs:
        latest = recent_runs[0]
        return latest.entry.params or {}, latest.number

    return {}, None


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


def _archive_filter_flags(
    *,
    include_no_video: bool,
    selected_modes: set[str],
    timestamp: str | None,
    from_: str | None,
    until: str | None,
) -> tuple[bool, bool, bool]:
    """Resolve archive_recording_list()'s three filter-related values
    from its raw query params: `videos_only` (the actual value
    filter_recordings() wants), `filters_active` (does *any* filter -
    including the default video-only view - explain a possibly-empty
    result), and `show_clear_filters` (has the user actually deviated
    from the bare default, i.e. is there anything real to clear).

    The form's checkbox is `include_no_video` ("Show all recordings
    (including ones without video)") rather than a `videos_only`
    checkbox that defaults to checked - Christer initially got a
    `videos_only` checkbox that was checked by default, and found that
    confusing to reason about ("i wanted the option to be 'Show all
    recordings' or something better"). Framing the checkbox as the
    opt-in ("include the video-less ones") instead of the opt-out
    ("only show ones with video") means an unticked/unsubmitted
    checkbox - which is indistinguishable from a fresh page load
    either way, since HTML never submits an unchecked box - correctly
    reads as "no, don't include them" in both cases. That sidesteps
    the whole fresh-visit-vs-explicit-uncheck disambiguation problem
    the old `videos_only`-checked-by-default design needed a hidden
    `filtered` marker field for; this version needs no such marker.

    `filters_active` and `show_clear_filters` diverge only on the
    video-only view being the default: showing "Clear filters" or "No
    recordings match these filters." on every ordinary page load (just
    because the default view itself counts as "filtered") would be
    misleading - `show_clear_filters` only counts `include_no_video`
    when it's actually been checked, since leaving it unchecked is
    just the default.
    """

    videos_only = not include_no_video

    filters_active = bool(
        selected_modes or timestamp or from_ or until or videos_only
    )
    show_clear_filters = bool(
        selected_modes or timestamp or from_ or until or include_no_video
    )

    return videos_only, filters_active, show_clear_filters


def _find_camera_archive(cache: CameraConfigCache, camera_id: str) -> Path:
    """Resolve a camera id (from the URL) to its archive directory -
    CameraConfig.archive, the directory bv-download writes raw
    recordings to. This is NOT the same thing as bv-export --target
    (app.state.target, this app's own --target, the trips directory
    trips.py reads - a separate concept from CameraConfig.target,
    which is just the *default value* that flag is populated from
    when a camera config has one set) - a camera id names a *source*
    archive, a trip id names *processed* output; don't conflate the
    two.

    `camera_id` comes straight from the URL path and is therefore
    untrusted - reject anything that could walk outside
    default_config_dir() before it ever reaches config_path(), same
    guard _find_trip() applies to each segment of trip_id below. A
    camera id that doesn't have a config file at all (never set up,
    or a typo) 404s the same way a bad trip id does.

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

    return config.archive


def _find_camera_adapter_id(cache: CameraConfigCache, camera_id: str) -> str:
    """Resolve a camera id to its CameraConfig.adapter (see
    core/camera_config.py and docs/CAMERA_ADAPTERS.md) - "blackvue"
    (DEFAULT_ADAPTER_ID) for an un-migrated config with no `adapter`
    key. A sibling to _find_camera_archive() rather than folded into
    it: only the archive-browser routes (scan_archive()/
    find_recording(), both adapter-aware) need this - the other
    _find_camera_archive() call sites (bv-export/bv-generate/etc. job
    forms) just want the raw path and stay untouched by the adapter
    abstraction for now, per docs/CAMERA_ADAPTERS.md's roadmap.

    Goes through the same `cache` as _find_camera_archive() - no
    extra config-file read, just a second field off the same cached
    CameraConfig. Reuses _find_camera_archive()'s own camera-id/404
    guard rather than duplicating it.
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

    return config.adapter


def _describe_gps_fix(
    fix: GpsFix, geocode_cache_dir: Path
) -> tuple[str, str, str | None, str | None]:
    """Turn one GpsFix into the (coordinates, google_maps_url, address,
    address_error) tuple archive_recording_location() renders for both
    its start and stop fix - factored out so that route doesn't
    duplicate the coordinate-formatting/reverse-geocode-with-fallback
    logic twice. `address_error` is the reverse-geocode failure
    message (MediaToolError, e.g. Nominatim unreachable) if any -
    `address` stays None in that case rather than raising, same
    "degrade to a message, don't 500" handling this route's own
    docstring/history already established for gps_path being missing
    or unreadable."""

    coordinates = f"{fix.latitude},{fix.longitude}"
    google_maps_url = f"https://www.google.com/maps?q={coordinates}"

    address = None
    address_error = None
    try:
        address = load_or_reverse_geocode(
            fix.latitude, fix.longitude, geocode_cache_dir
        )
    except MediaToolError as exc:
        address_error = str(exc)

    return coordinates, google_maps_url, address, address_error


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
    adapter_id = _find_camera_adapter_id(camera_config_cache, camera_id)
    recording = recording_cache.get(archive_path, camera_id, recording_id, adapter_id)
    if recording is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="recording not found"
        )
    return recording


def _video_label_for_filename(
    recording: ArchiveRecording, filename: str
) -> str | None:
    """The direction label ("Front"/"Rear"/"Interior") for `filename`
    within `recording`, or None if it isn't one of this recording's
    actual video files - what archive_recording_watch() uses to both
    validate the filename (404 on anything else, including sidecars
    like .gps/.3gf that were never meant to be "watched") and title the
    page. Deliberately checked against recording.videos rather than
    just recording.known_filenames, which also covers non-video
    sidecars this route has no business serving a <video> player for."""

    for label, video_filename in recording.videos:
        if video_filename == filename:
            return label
    return None


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


def _authorize_job_view(job: Job, user: User) -> None:
    """Shared by job_detail() and its /poll AJAX sibling (task
    #772-776, WORKING_CONTEXT.md) - only bv-search is open to
    viewers (see require_viewer_or_owner's own docstring). A viewer
    hitting either route for any other job type (e.g. by guessing/
    reusing a job id) gets the usual 403 either way."""

    if user.role != "owner" and not job.command.startswith("bv-search "):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="owner role required"
        )


def _sliced_job_output(
    job_status: JobStatus, output: list[str], tail_requested: bool
) -> tuple[bool, list[str], int]:
    """Apply ?tail=1's slicing (task #687, WORKING_CONTEXT.md) - shared
    by job_detail() and /poll so a poll tick can never disagree with
    how the page first decided to slice. See TAIL_LINE_COUNT's own
    comment for why 30. Returns (tail_active, displayed_output,
    tail_truncated_count)."""

    tail_active = tail_requested and not job_status.is_finished
    displayed_output = output
    tail_truncated_count = 0
    if tail_active and len(output) > TAIL_LINE_COUNT:
        tail_truncated_count = len(output) - TAIL_LINE_COUNT
        displayed_output = output[-TAIL_LINE_COUNT:]
    return tail_active, displayed_output, tail_truncated_count


def _job_camera_id(job: Job) -> str | None:
    """bv-search is the only job type whose output prints recording
    ids (see RECORDING_ID_RE's own comment) - None for every other
    job type, so the output partial only ever links lines for a
    bv-search job. There's no dedicated Job.camera_id field (see
    jobs.py's Job dataclass) - start_bv_search() sets job.command to
    exactly f"bv-search {camera_id}", so the second whitespace-
    separated token is the camera id whenever the first is
    "bv-search"."""

    command_parts = job.command.split(maxsplit=1)
    if command_parts and command_parts[0] == "bv-search" and len(command_parts) == 2:
        return command_parts[1]
    return None


def _resolve_camera_target(cache: CameraConfigCache, camera_id: str) -> Path | None:
    """Resolve a camera id to its configured Target directory
    (CameraConfig.target - the same directory bv-export --target
    defaults to for that camera), or None if the camera doesn't have
    a config at all, or has one but never set a Target (see
    bv-config's own "Target" prompt, which is optional).

    Mirrors _find_camera_archive()'s own pattern (same cache, same
    default_config_dir()) - but returns None instead of 404ing on a
    miss, since callers here (_find_trip() below) use it to
    distinguish "no such camera-scoped trip" from "malformed id",
    not to serve a dedicated per-camera route of its own."""

    try:
        config = cache.get(default_config_dir(), camera_id)
    except CameraConfigError:
        return None
    return config.target


def _find_trip(
    trip_cache: TripCache,
    camera_config_cache: CameraConfigCache,
    fallback_target: Path,
    trip_id: str,
) -> TripAssets:
    """Resolve a trip id to a TripAssets, 404ing if it doesn't exist
    or isn't actually a trip folder. `trip_id` comes straight from
    the URL path and is therefore untrusted - reject anything that
    could walk outside the resolved target directory before ever
    touching the filesystem with it.

    `trip_id` is either a plain folder name - resolved under
    `fallback_target`, same as before scan_all_trips() existed - or
    exactly one "camera-id/folder-name" segment pair, resolved
    through that camera's own configured Target via
    _resolve_camera_target() (see scan_all_trips()'s own docstring
    for why a trip can be identified this way). Each segment still
    goes through the same "no dots, no backslash" check a flat id
    always has. Anything with more than one "/", a backslash, or an
    empty/"."/".." segment is rejected outright - "\\" is blocked
    unconditionally since it's never a legitimate part of an id on
    any platform this runs on, only ever an attempted escape.

    Goes through `trip_cache` (see TripCache's own docstring) rather
    than calling scan_trip() directly - trip_file() calls this once
    per HTTP range request, and a video player issues many of those
    per second while seeking/buffering."""

    segments = trip_id.split("/")
    if (
        "\\" in trip_id
        or len(segments) > 2
        or any(segment in ("", ".", "..") for segment in segments)
    ):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="trip not found"
        )

    if len(segments) == 2:
        camera_id, trip_folder = segments
        camera_target = _resolve_camera_target(camera_config_cache, camera_id)
        if camera_target is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="trip not found"
            )
        trip = trip_cache.get(camera_target, trip_folder, id_=trip_id)
    else:
        trip = trip_cache.get(fallback_target, trip_id)

    if trip is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="trip not found"
        )
    return trip
