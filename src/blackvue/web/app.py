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

import asyncio
import json
import re
import tempfile
import time
from argparse import ArgumentTypeError
from datetime import date
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
from .archive_browser import _SCENE_ASSET_BY_DIRECTION
from .archive_browser import _frame_viewer_timestamps
from .archive_browser import _nominal_frame_timestamps
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
from .elevenlabs_tts import ElevenLabsError
from .elevenlabs_tts import api_key as elevenlabs_api_key
from .elevenlabs_tts import list_voices as elevenlabs_list_voices
from .elevenlabs_tts import synthesize_with_timestamps as elevenlabs_synthesize
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
from .voice_asr import known_places_from_learned
from .voice_asr import known_places_from_params
from .voice_asr import transcribe_voice_query
from .voice_llm import VALID_MODEL_CHOICES as VOICE_LLM_MODEL_CHOICES
from .voice_llm import extract_voice_query_llm
from .voice_query import parse_spoken_query
from .voice_time import parse_spoken_timerange
from ..core.camera_config import CameraConfigCache
from ..core.camera_config import CameraConfigError
from ..core.camera_config import config_path
from ..core.camera_config import default_config_dir
from ..core.camera_config import list_camera_ids
from ..core.camera_config import load_camera_config
from ..core.blackvue_client import SNAPSHOT_DIRECTIONS
from ..core.lock import LOCKABLE_ASSETS
from ..export.geocoding import load_or_reverse_geocode
from ..telemetry.gps_reader import GpsFix
from ..export.hevc_preview import open_hevc_preview_stream
from ..export.kml_writer import gpx_to_kml
from ..generate.media import MediaToolError
from ..generate.media import extract_video_thumbnail
from ..generate.media import load_or_compute_duration
from ..generate.mp4_repair import load_or_repair_parking_video
from ..lexicaltimeparser import LexicalTimeParser
from ..stats_report import DEFAULT_FIELDS
from ..stats_report import GPS_DEPENDENT_FIELDS
from ..stats_report import GROUPINGS
from ..stats_report import STAT_FIELDS
from ..stats_report import aggregate_recording_stats
from ..stats_report import count_recordings_without_gps
from ..stats_report import load_recording_stats
from ..cli.bv_stats import _format_value as _format_stat_value
from ..trip.driver_detect import add_driver
from ..trip.driver_detect import default_driver_profiles_path
from ..trip.driver_detect import load_driver_profiles
from ..trip.driver_detect import rename_driver
from ..trip.driver_detect import save_driver_profiles
from ..trip.place_knowledge import bulk_assign_undecided_trips
from ..trip.place_knowledge import default_driver_knowledge_path
from ..trip.place_knowledge import group_trips_by_place
from ..trip.place_knowledge import load_knowledge_base
from ..trip.place_knowledge import reresolve_trip_drivers
from ..trip.place_knowledge import save_knowledge_base
from ..trip.place_knowledge import smoothness_score
from ..trip.place_knowledge import suggest_closest_decided_trip
from ..trip.place_knowledge import undecided_places
from ..trip.place_knowledge import undecided_trips
from dataclasses import replace as _dc_replace

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

# bv-snap and bv-gps --snap both print exactly one "<direction>: saved
# <path>" line per direction that actually captured (see bv_snap.py's/
# bv_gps.py's own _run()/_run_snap()) - same prefix job_detail.html's
# own camera-click-sound JS already watches for (SNAP_LINE_RE there).
# _job_output_lines.html uses this to render the actual image inline
# for a snap-capable job (Christer: "Of course i want to see the
# snapshot pictures on bv-web") instead of just the path as text -
# see _job_snapshot_path()/job_snapshot_image() for how the path in
# group(2) gets resolved back to a real file, safely.
SNAP_SAVED_RE = re.compile(r"^([FRI]): saved (.+)$")

# bv-gps prints exactly one "Google Maps: <url>" line per successful
# fix (see cli/bv_gps.py's google_maps_url()/_report_gps_fix()) -
# job_detail.html used to render that whole line as plain escaped
# text, so the URL itself wasn't clickable (Christer: "bv-gps in
# bv-web does not create a link for google maps"). _job_output_lines.html
# uses this the same way it already does for RECORDING_ID_RE/
# SNAP_SAVED_RE above, to turn just the URL portion of a matching line
# into a real <a href> instead of leaving the whole thing as text.
GOOGLE_MAPS_LINE_RE = re.compile(r"^Google Maps: (https://\S+)$")

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

# How long tts_voices() trusts its in-memory copy of the ElevenLabs
# voice list before re-fetching - see that route's own docstring.
TTS_VOICE_CACHE_SECONDS = 300

# job_detail.html's own back-link (Christer: "i would like to have a
# return link after a search report"): every job type's Job.command
# starts with the exact same command name its "start a new job" route
# is registered under (see each start_bv_*() call's own
# `command=f"bv-... {...}"` in jobs.py, and the matching
# `@app.get("/jobs/bv-...")` routes below) - so job_detail() can build
# one generic "back to the form that started this" link for every job
# type, not just bv-search, just by taking the first word of
# job.command. This dict only supplies a friendlier label than the
# bare command name; it mirrors the wording already used for each
# command's tab in base.html's <nav class="tabs">.
JOB_NEW_LABELS = {
    "bv-config": "Set up camera",
    "bv-gps": "GPS Current loc",
    "bv-download": "Download recordings",
    "bv-ls": "List recordings",
    "bv-generate": "Generate assets",
    "bv-lock": "Lock ranges",
    "bv-scribe": "Describe scenes",
    "bv-export": "Export trips",
    "bv-search": "Search",
}


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

    # drivers_page()'s own pair of longer-lived caches (task #1390).
    # Christer, after task #1389's render-scoped fixes: "Assigning
    # driver still takes 30 seconds per trip." #1389 stopped one
    # render from doing the same geocode/has-video lookup twice, but
    # every trip assignment is its own fresh POST -> redirect -> GET,
    # and drivers_page() re-renders the whole page from zero each
    # time - the general-purpose archive_recording_cache's 2-second TTL
    # (tuned for video playback, where a download finishing mid-session
    # needs to be noticed quickly) had always expired well before the
    # *next* redirect, so all ~200 has-video lookups against Christer's
    # NAS-hosted archive were repeated, unchanged, on every single
    # click. These are historical, already-downloaded recordings -
    # "has video" for one of them only ever flips when a fresh
    # bv-download run finishes, never mid-/drivers-session - so a much
    # longer TTL is safe here in a way it wouldn't be for the archive
    # browser's own playback routes, which keep using the 2-second
    # archive_recording_cache above unchanged.
    #
    # Follow-up (task #1391) - Christer: "Cant the 5 minute cashed be
    # solved another way, its irritating that after 5 minutes i need
    # to wait 30 seconds." The 300s TTL just traded one problem for a
    # smaller, recurring one: every 5 minutes of active /drivers use
    # paid the same 30s cost the render-scoped fix was meant to avoid,
    # for no reason - nothing had actually changed. The real signal
    # for "safe to keep serving this" was never a clock, it was
    # "has a bv-download for this camera finished since this was
    # cached" - and JobRunner.start_bv_download() already knows
    # exactly when that happens, so it now calls this cache's own
    # clear() the moment a real (non-dry-run) download job succeeds
    # (see that method's own docstring). With that in place the TTL
    # below is just a distant backstop for the one case explicit
    # invalidation can't see - a bv-download run kicked off outside
    # bv-web entirely (a direct CLI call against the same archive) -
    # so it's raised from 5 minutes to 1 hour rather than removed
    # outright.
    app.state.drivers_page_recording_cache = ArchiveRecordingCache(ttl_seconds=3600.0)
    # Reverse-geocoded addresses get the same treatment, but with no
    # TTL at all - a (lat, lon) pair's own street address never
    # changes, so once resolved once in this process's lifetime
    # there's nothing to ever re-check. _reverse_geocode_or_none()'s
    # own on-disk cache (default_config_dir()/.osm_cache) avoids the
    # network round trip on repeat lookups, but still does a real
    # file-exists check + read + JSON parse on every single call with
    # no in-memory layer of its own - this dict is that missing layer,
    # shared across every /drivers render for this process's lifetime.
    app.state.drivers_page_geocode_cache: dict[tuple[float, float], str | None] = {}

    # See tts_voices()'s own docstring - a tiny process-lifetime cache
    # so the "Read aloud" voice picker doesn't hit the ElevenLabs API
    # on every single archive recording detail page view.
    app.state.tts_voice_cache = {"voices": None, "fetched_at": 0.0}

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
    # stats.html renders every StatBucket value through this - reuses
    # bv-stats' own _format_value() (duration as H:MM:SS via
    # timedelta, everything else fixed-precision + unit) so the web
    # dashboard matches the CLI report's own formatting exactly,
    # rather than a second implementation slowly drifting from it.
    templates.env.filters["stat_value"] = (
        lambda value, field_key: _format_stat_value(field_key, value)
    )
    # Bucket keys ("2026-08", "Monday", "2026-08-23") need to become a
    # safe HTML element id for the chart's click-to-scroll-to-row
    # behavior - see stats.html's own inline <script> for what reads
    # this id back out. _slugify() is a plain module-level function
    # (not a lambda here) so it's directly testable - see its own
    # docstring.
    templates.env.filters["slugify"] = _slugify
    # See RECORDING_ID_RE's own comment above - job_detail.html calls
    # this per output line to decide whether to render it as a link
    # rather than plain text.
    templates.env.globals["is_recording_id"] = (
        lambda s: RECORDING_ID_RE.match(s) is not None
    )
    # See SNAP_SAVED_RE's own comment above - _job_output_lines.html
    # calls these per output line, only for a snap-capable job
    # (snapshot_job=True in that partial's context), to render an
    # inline <img> after the matching line rather than just the
    # "<direction>: saved <path>" text on its own.
    templates.env.globals["is_snapshot_saved_line"] = (
        lambda s: SNAP_SAVED_RE.match(s) is not None
    )
    templates.env.globals["snapshot_direction"] = (
        lambda s: SNAP_SAVED_RE.match(s).group(1)
    )
    # See GOOGLE_MAPS_LINE_RE's own comment above -
    # _job_output_lines.html calls these per output line to render a
    # bv-gps "Google Maps: <url>" line with the URL itself as a real
    # link rather than plain text.
    templates.env.globals["is_google_maps_line"] = (
        lambda s: GOOGLE_MAPS_LINE_RE.match(s) is not None
    )
    templates.env.globals["google_maps_link_url"] = (
        lambda s: GOOGLE_MAPS_LINE_RE.match(s).group(1)
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

    # 2026-08-27: trip_detail() (the plain "/trips/{trip_id:path}"
    # route) is deliberately registered LAST among the four
    # "/trips/{trip_id:path}..." routes below, not first as it
    # originally was when task #759 switched this from a plain
    # {trip_id} to {trip_id:path}. FastAPI/Starlette tries routes in
    # registration order and picks the first one whose compiled regex
    # matches the request path - and a bare {trip_id:path} with no
    # trailing literal compiles to `^/trips/(?P<trip_id>.*)$`, which
    # (being fully greedy with nothing anchoring it short of the end)
    # matches ANY "/trips/..." URL, including one that was meant for
    # trip_location()'s "/location" suffix, trip_kml()'s "/kml"
    # suffix, or trip_file()'s "/files/{filename}" suffix. With
    # trip_detail() registered first (the original order), it silently
    # swallowed every one of those requests before the more specific
    # route ever got a chance - trip_id ended up including the literal
    # "/files/stitch.mp4" tail as part of itself, which _find_trip()'s
    # own segment-count guard then rejected as malformed, 404ing with
    # "trip not found" for every single video/location/KML request on
    # every trip, camera-prefixed or not (confirmed via Christer's real
    # NAS logs: "/trips/kirby_2019/.../files/map_zoom_120m_tu.mp4"
    # 404-ing while the plain trip page 200'd every time). Registering
    # the three suffixed routes first means their own more specific
    # patterns get tried - and match - before Starlette ever reaches
    # this catch-all, while a genuinely bare trip_id (no "/location",
    # "/kml", or "/files/..." tail) still falls through to here exactly
    # as before, since it can't match any of the three specific
    # patterns. Any *new* "/trips/{trip_id:path}/something" route added
    # in the future must go above this one for the same reason.
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

    # Deliberately registered LAST among the "/trips/{trip_id:path}..."
    # routes - see the comment above trip_location() (the first of the
    # three specific ones) for why order matters here.
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

    @app.get("/stats", response_class=HTMLResponse)
    async def stats_dashboard(
        request: Request,
        user: User = Depends(require_login),
        id: str | None = Query(default=None),
        group: str = Query(default="all"),
        fields: list[str] = Query(default=[]),
        graph_fields: list[str] = Query(default=[]),
        chart_type: str = Query(default="bar"),
        timestamp: str | None = Query(default=None),
        from_: str | None = Query(default=None, alias="from"),
        until: str | None = Query(default=None, alias="until"),
        estimate_gaps: bool = Query(default=False),
    ):
        # Read-only dashboard over stats_report.py's aggregation
        # (bv-stats' own library half - see that module's docstring:
        # "so a future bv-web stats tab can call the aggregation
        # directly instead of parsing this command's text output").
        # Deliberately built the same way archive_recording_list()
        # above is - a GET with query-string filters, no JobRunner
        # involved, since this is a read over already-computed
        # RECORDING_STATS assets, not something that runs a subprocess
        # or takes any real time.
        timestamp = timestamp or None
        from_ = from_ or None
        until = until or None

        selected_fields = _selected_stat_fields(fields)
        grouping = group if group in GROUPINGS else "all"
        graph_fields = _selected_graph_fields(graph_fields)
        aggregate_fields = _fields_for_aggregation(selected_fields, graph_fields)
        chart_type = chart_type if chart_type in ("bar", "line") else "bar"

        cameras = _camera_options()
        camera_id = id if id and any(c["id"] == id for c in cameras) else None

        error = None
        buckets: list = []
        summary_bucket = None
        skipped = 0
        total_in_range = 0
        no_gps = 0

        if camera_id:
            try:
                archive_path = _find_camera_archive(app.state.camera_config_cache, camera_id)
                adapter_id = _find_camera_adapter_id(app.state.camera_config_cache, camera_id)
                adapter = get_adapter(adapter_id)
                archive = adapter.open_archive(archive_path)

                time_interval = None
                if timestamp or from_ or until:
                    time_interval = LexicalTimeParser(
                        timestamp=timestamp, from_=from_, until=until
                    ).parse()

                recordings = [
                    recording for recording in archive.recordings
                    if time_interval is None or recording.id.value in time_interval
                ]
                total_in_range = len(recordings)

                entries: list = []
                for recording in recordings:
                    stats = load_recording_stats(recording)
                    if stats is None:
                        skipped += 1
                        continue
                    entries.append((recording.id, stats))

                if GPS_DEPENDENT_FIELDS.intersection(aggregate_fields):
                    no_gps = count_recordings_without_gps(entries)

                if entries:
                    buckets = aggregate_recording_stats(
                        entries, grouping=grouping, fields=aggregate_fields,
                        estimate_gaps=estimate_gaps,
                    )
                    if grouping != "all":
                        summary_bucket = aggregate_recording_stats(
                            entries, grouping="all", fields=aggregate_fields,
                            estimate_gaps=estimate_gaps,
                        )[0]
            except ValueError as exc:
                # A bad/conflicting LexicalTimeParser filter combo -
                # same "show the form again with the error" handling
                # archive_recording_list() above uses, rather than a
                # raw 500.
                error = str(exc)

        chart_data = _stats_chart_series(buckets, graph_fields)

        return templates.TemplateResponse(
            request,
            "stats.html",
            {
                "user": user,
                "cameras": cameras,
                "camera_id": camera_id,
                "groupings": GROUPINGS,
                "grouping": grouping,
                "all_fields": STAT_FIELDS,
                "selected_fields": selected_fields,
                "graph_fields": graph_fields,
                "chart_type": chart_type,
                "timestamp_value": timestamp or "",
                "from_value": from_ or "",
                "until_value": until or "",
                "estimate_gaps": estimate_gaps,
                "buckets": buckets,
                "summary_bucket": summary_bucket,
                "skipped": skipped,
                "total_in_range": total_in_range,
                "no_gps": no_gps,
                "chart_data_json": json.dumps(chart_data),
                "error": error,
            },
        )

    @app.get("/drivers", response_class=HTMLResponse)
    async def drivers_page(
        request: Request,
        user: User = Depends(require_login),
        min_visits: int = Query(default=2),
        driver_filter: str = Query(default=""),
    ):
        # Christer's common-places/undecided-trips driver-assignment
        # form (see trip/place_knowledge.py's own module docstring) - a
        # read over driver_knowledge.json, built/refreshed by the
        # "Build driver KB" job (bv-drivers, /jobs/bv-drivers). Global,
        # not per-camera, unlike /archive and /stats above:
        # driver_knowledge.json lives under the shared
        # default_config_dir(), not any one camera's own archive - a
        # single household vehicle, one knowledge base.
        def _build_context() -> dict:
            """Runs the actual page computation (knowledge/profile
            reads, geocode/has-video lookups, sorting) synchronously -
            called via asyncio.to_thread() below instead of directly
            inline in this async route.

            Christer, possibly while a HEVC preview was mid-transcode-
            and-streaming elsewhere: "Any change to pre partial hevc
            to h264, its slow and sluggish, it used to flow without
            pausing" - confirmed he might have had /drivers open at
            the same time ("I might have been"). Nothing in
            hevc_preview.py's own progressive-streaming code changed;
            the real cause is that bv-web runs a single uvicorn
            process with one event loop (no worker pool - see
            cli/bv_web.py's plain uvicorn.run(app, host, port)), and
            this route's own geocode/has-video lookups (see the "still
            slow adding driver" profiling comment below) ran as plain
            synchronous filesystem/network calls directly inside this
            async def - a cache-miss render measured at ~30s against
            Christer's real archive (tasks #1389/#1391). Since none of
            that ever awaits, it can't yield control back to the event
            loop for its entire duration, so it doesn't just make
            *this* request slow - it blocks every other concurrent
            request on the server too, including the async chunk-by-
            chunk delivery of a video that's mid-transcode-and-
            streaming to a browser elsewhere (open_hevc_preview_
            stream()'s _consume_broadcast() generator can't get
            scheduled to hand off its next chunk while this function
            has the server's only event-loop thread tied up). Same
            class of event-loop-stall bug task #1270 already fixed
            once for the HEVC codec probe - just resurfacing here in a
            much larger block. Wrapping the whole thing in
            asyncio.to_thread() moves it onto a worker thread, so it
            can take exactly as long as before without blocking
            anything else on the server meanwhile.
            """
            config_dir = default_config_dir()
            profiles = load_driver_profiles(default_driver_profiles_path(config_dir))
            knowledge = load_knowledge_base(default_driver_knowledge_path(config_dir))

            if knowledge is None or profiles is None:
                return {
                    "user": user,
                    "built": False,
                    "driver_choices": [],
                    "places": [],
                    "undecided_place_keys": set(),
                    "specific_trip_list": [],
                    "place_trips": {},
                    "place_trip_addresses": {},
                    "driver_display_by_label": {},
                    "smoothness_scores": {},
                    "closest_matches": {},
                    "driver_trip_counts": {},
                    "undecided_trip_count": 0,
                    "min_visits": min_visits,
                    "driver_filter": driver_filter,
                    "trip_count": 0,
                    "decided_count": 0,
                }

            trips, places, trip_overrides = knowledge
            driver_choices = [
                (driver.label, driver.display_name) for driver in profiles.drivers
            ]
            # Christer: "A specific trip on driver, should have a count
            # for no of trips" - the driver filter dropdown atop
            # Specific trips only ever showed each driver's bare name,
            # with no sense of how many trips are already resolved to
            # them (or how many are still Undecided) without actually
            # picking that option and looking at the table underneath.
            # Counted against every trip in the knowledge base (not
            # just specific_trip_list, which is only ever one filter's
            # worth) so every option's count is stable regardless of
            # which one is currently selected.
            driver_trip_counts = {
                driver.label: sum(
                    1 for entry in trips if entry.driver_label == driver.label
                )
                for driver in profiles.drivers
            }
            undecided_trip_count = len(undecided_trips(trips))

            # build_common_places() itself creates one CommonPlace per
            # distinct away-place grid cell regardless of visit_count (see
            # its own docstring - carrying forward existing labels/rules is
            # cheap and doesn't need a threshold) - but a place visited only
            # once isn't "common" by this page's own stated definition
            # ("every place the vehicle has been to more than once", see
            # the command-help text above) and is exactly the shape of
            # task #1355's bogus-Common-Place report: a one-off stop (a
            # traffic light, a roundabout, anywhere GPS dwell logic
            # mis-detected a stop) that will never repeat. Christer,
            # spotting these still in the table after the #1355 fix: "common
            # places har vissa trips = 1" - confirmed via AskUserQuestion he
            # wants them hidden entirely, not just left unflagged. Filtered
            # here (display only) rather than in build_common_places()
            # itself so a place that reaches a second visit on some future
            # rebuild still gets recomputed correctly - nothing is lost by
            # not showing it meanwhile, since undecided_trips() already
            # surfaces its (single) trip in the Specific trips table below.
            places_sorted = sorted(
                (place for place in places.values() if place.visit_count >= min_visits),
                key=lambda place: place.visit_count, reverse=True,
            )
            undecided_place_keys = {
                place.key for place in undecided_places(places, min_visits=min_visits)
            }
            # Christer: "pa specific trips hogst upp skulle jag vilja ha en
            # selection for varje driver samt aven undecided som default." -
            # a driver filter at the top of the Specific trips section,
            # separate from each row's own driver <select> (that one
            # assigns a driver, this one chooses which trips to look at).
            # Empty driver_filter (the default, "Undecided" in the
            # dropdown) keeps today's behavior - only trips with no
            # resolved driver at all. Picking a specific driver instead
            # shows every trip already resolved to them, regardless of how
            # (pattern-match/place-rule/manual-trip) - useful for
            # reviewing or correcting an already-made call, not just
            # making new ones.
            if driver_filter:
                specific_trip_list = sorted(
                    (entry for entry in trips if entry.driver_label == driver_filter),
                    key=lambda entry: entry.start_time, reverse=True,
                )
            else:
                specific_trip_list = sorted(
                    undecided_trips(trips), key=lambda entry: entry.start_time, reverse=True
                )

            # Reverse-geocoded address per place (keyed by CommonPlace.key)
            # and per undecided trip's start/stop point (keyed by
            # trip_label) - Christer's own follow-up asks ("i also need an
            # address for Place ... not for that specific address all of
            # them in the list" and "a link to first and last video with
            # the adress of start and stop"). Computed live here, the same
            # load_or_reverse_geocode()-with-on-disk-cache call the
            # archive browser's own /location route already makes (see
            # _describe_gps_fix()) rather than persisted into
            # driver_knowledge.json - place_knowledge.py stays a pure,
            # network-free module (see its own docstring) and the cache
            # itself already makes every address after the first page load
            # free. Only geocoded for specific_trip_list's own trips (not
            # every trip in the archive) - the only ones this page's
            # Specific trips table actually shows, whichever driver_filter
            # currently narrows it to.
            # Christer, after the loading-overlay/backup/HEVC fixes above:
            # "still slow adding driver" - the overlay only ever addressed
            # the *symptom* (a page that looks frozen), not this route's
            # actual cost. Real profiling against his own driver_knowledge.json
            # (164 trips, 37 places, 88 undecided) found two genuine waste
            # sources in this block: (1) place_trip_addresses/
            # place_trip_video_status below were computed for every trip
            # under *every* Common Place, including the 27 of 37 places
            # with visit_count==1 that task #1356 already hides from
            # places_sorted/the template entirely - 27 of 64 places-trips
            # (42%) worth of geocode + has_video lookups for rows that can
            # never be seen; (2) an undecided trip that also belongs to a
            # shown place got its start/stop geocoded and has_video-checked
            # twice - once for trip_addresses/trip_video_status (the
            # Specific trips table), once more for place_trip_addresses/
            # place_trip_video_status (that place's own expandable trip
            # list) - same (point) or (camera_id, recording_id) key, two
            # separate network/filesystem round trips. Neither
            # _reverse_geocode_or_none() nor _recording_has_video_or_none()
            # itself caches across the distinct keys this route calls them
            # with in one render (the on-disk geocode cache and the 2s
            # ArchiveRecordingCache TTL only dedupe a *repeated* key, not a
            # first-time lookup happening twice under two different dict
            # comprehensions), so both are now routed through small
            # helpers here instead of called directly. Backed by the two
            # process-lifetime caches set up in create_app() (see
            # app.state.drivers_page_recording_cache/_geocode_cache's own
            # comments there, task #1390) rather than a fresh per-request
            # dict, so the *next* /drivers render - the one every single
            # trip-assignment POST redirects straight into - reuses this
            # render's own lookups instead of repeating every one of them
            # from zero.
            geocode_cache_dir = default_config_dir() / ".osm_cache"
            _geocode_memo = app.state.drivers_page_geocode_cache

            def _geocode(point: tuple[float, float]) -> str | None:
                if point not in _geocode_memo:
                    _geocode_memo[point] = _reverse_geocode_or_none(point, geocode_cache_dir)
                return _geocode_memo[point]

            def _video_status(camera_id: str, recording_id: str) -> bool | None:
                return _recording_has_video_or_none(
                    app.state.drivers_page_recording_cache,
                    app.state.camera_config_cache,
                    camera_id,
                    recording_id,
                )

            # Reverse-geocoded address per place (keyed by CommonPlace.key)
            # and per undecided trip's start/stop point (keyed by
            # trip_label) - Christer's own follow-up asks ("i also need an
            # address for Place ... not for that specific address all of
            # them in the list" and "a link to first and last video with
            # the adress of start and stop"). Computed live here, the same
            # load_or_reverse_geocode()-with-on-disk-cache call the
            # archive browser's own /location route already makes (see
            # _describe_gps_fix()) rather than persisted into
            # driver_knowledge.json - place_knowledge.py stays a pure,
            # network-free module (see its own docstring) and the cache
            # itself already makes every address after the first page load
            # free. Only geocoded for specific_trip_list's own trips (not
            # every trip in the archive) - the only ones this page's
            # Specific trips table actually shows, whichever driver_filter
            # currently narrows it to.
            place_addresses = {
                place.key: _geocode(place.point) for place in places_sorted
            }
            trip_addresses = {
                entry.trip_label: (
                    _geocode(entry.start_point),
                    _geocode(entry.end_point),
                )
                for entry in specific_trip_list
            }
            # Whether the Start/Stop "Video" link's own recording actually
            # has a downloaded video - see _recording_has_video_or_none()'s
            # own docstring (Christer's "even if there is no video, it
            # should be named 'No video' but still keep the link"). Same
            # scope-to-what's-actually-shown reasoning as trip_addresses
            # above: only computed for specific_trip_list's own trips.
            trip_video_status = {
                entry.trip_label: (
                    _video_status(entry.camera_id, entry.first_recording_id),
                    _video_status(entry.camera_id, entry.last_recording_id),
                )
                for entry in specific_trip_list
            }

            # Every trip belonging to each Common Place, most-recent-first -
            # Christer's own follow-up ask ("common places should show each
            # trip with all what that means"): the place row's aggregate
            # visit/short/long counts don't say *which* trips they are, so
            # this expands each place into its own per-trip list (same
            # per-trip fields the Specific trips table shows - date,
            # weekday, time, stay length, driver/candidates, start/stop
            # addresses with Maps links, video links) rendered as a
            # collapsed <details> under that place's row. Unlike the
            # Specific trips table (undecided by default, or one driver's
            # own trips when filtered), this covers every trip at the
            # place regardless of driver - that's the whole point of "each
            # trip". Filtered down to places_sorted's own keys (visit_count
            # >= min_visits) right away - drivers.html only ever looks up
            # `place_trips.get(place.key, [])` for a `place in places_sorted`
            # loop, so a hidden (visit_count==1, task #1356) place's own
            # entries here would never be read by the template; per the
            # profiling above, on Christer's real archive that's 27 of 37
            # places whose trips there was previously no reason to touch.
            shown_place_keys = {place.key for place in places_sorted}
            place_trips = {
                key: entries
                for key, entries in group_trips_by_place(trips).items()
                if key in shown_place_keys
            }
            place_trip_addresses = {
                entry.trip_label: (
                    _geocode(entry.start_point),
                    _geocode(entry.end_point),
                )
                for entries in place_trips.values()
                for entry in entries
            }
            # Same has_video lookup as trip_video_status above, scoped to
            # every trip shown under a Common Place's own expandable list
            # instead of just the Specific trips table.
            place_trip_video_status = {
                entry.trip_label: (
                    _video_status(entry.camera_id, entry.first_recording_id),
                    _video_status(entry.camera_id, entry.last_recording_id),
                )
                for entries in place_trips.values()
                for entry in entries
            }
            driver_display_by_label = {
                driver.label: driver.display_name for driver in profiles.drivers
            }

            # Driving-smoothness score + "closest past match" suggestion -
            # Christer's own follow-up ask ("anything else you can do to
            # make it easier for me to decide driver"), both scoped to
            # specific_trip_list, same scope trip_addresses above already
            # uses. smoothness_population is every trip's own
            # smoothness_raw (not just the currently filtered ones) - a
            # trip's score is always its percentile rank against the whole
            # archive, regardless of which driver_filter is active.
            smoothness_population = [
                entry.smoothness_raw for entry in trips if entry.smoothness_raw is not None
            ]
            smoothness_scores = {
                entry.trip_label: smoothness_score(entry.smoothness_raw, smoothness_population)
                for entry in specific_trip_list
            }
            closest_matches = {
                entry.trip_label: suggest_closest_decided_trip(entry, trips)
                for entry in specific_trip_list
            }

            return {
                "user": user,
                "built": True,
                "driver_choices": driver_choices,
                "places": places_sorted,
                "place_addresses": place_addresses,
                "undecided_place_keys": undecided_place_keys,
                "specific_trip_list": specific_trip_list,
                "trip_addresses": trip_addresses,
                "trip_video_status": trip_video_status,
                "place_trips": place_trips,
                "place_trip_addresses": place_trip_addresses,
                "place_trip_video_status": place_trip_video_status,
                "driver_display_by_label": driver_display_by_label,
                "smoothness_scores": smoothness_scores,
                "closest_matches": closest_matches,
                "driver_trip_counts": driver_trip_counts,
                "undecided_trip_count": undecided_trip_count,
                "min_visits": min_visits,
                "driver_filter": driver_filter,
                "trip_count": len(trips),
                "decided_count": sum(
                    1 for entry in trips if entry.source != "undecided"
                ),
            }

        context = await asyncio.to_thread(_build_context)
        return templates.TemplateResponse(request, "drivers.html", context)

    @app.post("/drivers/places/{key}")
    async def drivers_update_place(
        key: str,
        label: str = Form(""),
        driver: str = Form(""),
        user: User = Depends(require_owner),
    ):
        # Updates one CommonPlace's label/driver, then re-resolves
        # every trip's driver via reresolve_trip_drivers() (no archive
        # re-scan needed - see that function's own docstring) and
        # saves the whole knowledge base back out. A blank driver
        # field clears that rule (reverts matching trips to
        # pattern-match/undecided).
        config_dir = default_config_dir()
        knowledge_path = default_driver_knowledge_path(config_dir)
        knowledge = load_knowledge_base(knowledge_path)
        profiles = load_driver_profiles(default_driver_profiles_path(config_dir))

        if knowledge is not None and profiles is not None and key in knowledge[1]:
            trips, places, trip_overrides = knowledge
            places = dict(places)
            places[key] = _dc_replace(
                places[key],
                label=label.strip() or places[key].label,
                driver=driver.strip() or None,
            )
            resolved = reresolve_trip_drivers(trips, places, profiles, trip_overrides)
            save_knowledge_base(
                knowledge_path, trips=resolved, places=places,
                trip_overrides=trip_overrides,
            )

        # #place-{key} fragment (matching drivers.html's own row id) so
        # the browser lands back on the row Christer just edited
        # instead of scrolling to the top of a possibly long page -
        # Christer: "i dont want to scroll down every time i save a
        # driver. And that also goes for common places." A fragment is
        # never sent to the server, but browsers still apply it
        # client-side after following a redirect.
        return RedirectResponse(
            url=f"/drivers#place-{key}", status_code=status.HTTP_303_SEE_OTHER
        )

    @app.post("/drivers/trips/{trip_label}")
    async def drivers_update_trip(
        trip_label: str,
        driver_label: str = Form(""),
        user: User = Depends(require_owner),
    ):
        # Sets (or, if blank, clears) one specific trip's manual
        # override - Christer's own "also for specific trips" ask,
        # for a one-off destination that never clustered into a
        # CommonPlace worth a rule. Same re-resolve-and-save path as
        # drivers_update_place() above.
        config_dir = default_config_dir()
        knowledge_path = default_driver_knowledge_path(config_dir)
        knowledge = load_knowledge_base(knowledge_path)
        profiles = load_driver_profiles(default_driver_profiles_path(config_dir))

        if knowledge is not None and profiles is not None:
            trips, places, trip_overrides = knowledge
            trip_overrides = dict(trip_overrides)
            driver_label = driver_label.strip()
            if driver_label:
                trip_overrides[trip_label] = driver_label
            else:
                trip_overrides.pop(trip_label, None)
            resolved = reresolve_trip_drivers(trips, places, profiles, trip_overrides)
            save_knowledge_base(
                knowledge_path, trips=resolved, places=places,
                trip_overrides=trip_overrides,
            )

        # NOT the same #trip-{label} pattern drivers_update_place() uses
        # above - unlike a CommonPlace row (which always stays in the
        # places table regardless of whether its driver rules are set),
        # resolving a trip's override removes it from undecided_trips()
        # entirely, so its own #trip-{label} row is gone from the very
        # next render. Anchoring to that now-vanished id left the
        # browser unable to find any matching element and silently
        # falling back to the top of the page - Christer: "varje gang
        # jag fyllt i en forare pa en specifik resa och trycker pa
        # save, sa hamnar jag hogst upp pa sidan, valdigt irriterande."
        # Anchor to the (always-present) "Specific trips" heading
        # instead - not the exact row, but at least the right section,
        # rather than a dangling fragment that scrolls nowhere.
        return RedirectResponse(
            url="/drivers#specific-trips", status_code=status.HTTP_303_SEE_OTHER
        )

    @app.post("/drivers/bulk-assign")
    async def drivers_bulk_assign(
        from_date: str = Form(...),
        until_date: str = Form(...),
        driver_label: str = Form(""),
        user: User = Depends(require_owner),
    ):
        # Christer: "I want to minimize add driver. How can i tell the
        # system that only i was driving since wife was out of town for
        # 4 days" - a bulk alternative to clicking through the Specific
        # trips table one row at a time. Deliberately scoped to
        # currently-undecided trips only (Christer's own choice via
        # AskUserQuestion): a trip already resolved by a place rule or
        # the increment-1 pattern matcher reflects a more specific
        # signal than "this whole date range was one driver" and isn't
        # touched just because its date falls inside the window. See
        # bulk_assign_undecided_trips()'s docstring for the full
        # reasoning.
        config_dir = default_config_dir()
        knowledge_path = default_driver_knowledge_path(config_dir)
        knowledge = load_knowledge_base(knowledge_path)
        profiles = load_driver_profiles(default_driver_profiles_path(config_dir))

        driver_label = driver_label.strip()
        if knowledge is not None and profiles is not None and driver_label:
            trips, places, trip_overrides = knowledge
            try:
                from_d = date.fromisoformat(from_date)
                until_d = date.fromisoformat(until_date)
            except ValueError:
                from_d = until_d = None
            if from_d is not None and until_d is not None:
                trip_overrides = bulk_assign_undecided_trips(
                    trips, trip_overrides,
                    from_date=from_d, until_date=until_d,
                    driver_label=driver_label,
                )
                resolved = reresolve_trip_drivers(
                    trips, places, profiles, trip_overrides
                )
                save_knowledge_base(
                    knowledge_path, trips=resolved, places=places,
                    trip_overrides=trip_overrides,
                )

        return RedirectResponse(
            url="/drivers#bulk-assign", status_code=status.HTTP_303_SEE_OTHER
        )

    @app.post("/drivers/add-driver")
    async def drivers_add_driver(
        display_name: str = Form(""),
        user: User = Depends(require_owner),
    ):
        # Christer: "How do i add a driver" - until now driver_
        # profiles.json only ever had two drivers (christers_driver_
        # profiles()' seed data) and the only way to add a third was
        # hand-editing that file. This is the minimal version: a name
        # gets a fresh opaque "driverN" label and an empty patterns
        # tuple (add_driver()'s own docstring explains why patterns
        # stay hand-edit-only) - enough to immediately show up in
        # every driver_choices <select> on this page (place rules,
        # bulk-assign, per-trip override).
        config_dir = default_config_dir()
        profiles_path = default_driver_profiles_path(config_dir)
        profiles = load_driver_profiles(profiles_path)

        display_name = display_name.strip()
        if profiles is not None and display_name:
            updated = add_driver(profiles, display_name)
            save_driver_profiles(profiles_path, updated)

        return RedirectResponse(
            url="/drivers#add-driver", status_code=status.HTTP_303_SEE_OTHER
        )

    @app.post("/drivers/rename-driver")
    async def drivers_rename_driver(
        label: str = Form(...),
        display_name: str = Form(""),
        user: User = Depends(require_owner),
    ):
        # Christer: "Jag vill aven byta namn pa 'Fru' till 'Dao'." -
        # add_driver() above only ever appends a new driver; there was
        # no way to fix a display_name already sitting in
        # driver_profiles.json short of hand-editing the file. `label`
        # (the opaque "driverN" key, not the old display_name) is the
        # lookup - see rename_driver()'s own docstring for why. Every
        # already-built TripKnowledge.display_name snapshotted the old
        # name at resolve time (place_knowledge.py's own
        # _resolve_trip_driver()), so - same as drivers_update_place()
        # below it for place-rule edits - a rename also needs a
        # reresolve_trip_drivers() + re-save pass over
        # driver_knowledge.json, not just the profiles file, or every
        # already-decided trip and the driver summary line at the top
        # of the page would keep showing the stale name.
        config_dir = default_config_dir()
        profiles_path = default_driver_profiles_path(config_dir)
        profiles = load_driver_profiles(profiles_path)

        display_name = display_name.strip()
        if profiles is not None and display_name:
            updated_profiles = rename_driver(profiles, label, display_name)
            save_driver_profiles(profiles_path, updated_profiles)

            knowledge_path = default_driver_knowledge_path(config_dir)
            knowledge = load_knowledge_base(knowledge_path)
            if knowledge is not None:
                trips, places, trip_overrides = knowledge
                resolved = reresolve_trip_drivers(
                    trips, places, updated_profiles, trip_overrides
                )
                save_knowledge_base(
                    knowledge_path, trips=resolved, places=places,
                    trip_overrides=trip_overrides,
                )

        return RedirectResponse(
            url="/drivers#add-driver", status_code=status.HTTP_303_SEE_OTHER
        )

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
        autoplay: bool = False,
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
                # Opt-in only (task #1390) - see the <video> tag's own
                # comment in the template for why this defaults to False
                # everywhere except the one place that sets it.
                "autoplay": autoplay,
            },
        )

    @app.post("/archive/{camera_id}/{recording_id}/scene/{direction}/edit")
    async def archive_recording_scene_edit(
        camera_id: str,
        recording_id: str,
        direction: str,
        text: str = Form(...),
        user: User = Depends(require_login),
    ):
        """Saves an edited raw scene.txt/scene-rear.txt straight back to
        disk. Christer: "it would be nice to have an edit option for
        the scene file, next to read aloud." Asked for scope: "Full
        raw scene file" (not just the cleaned description paragraph
        scene_summary shows) and "Overwrite the file on disk" (not a
        page-only edit) - see ArchiveRecording.scene_raw_text()'s own
        docstring for the full exchange.

        `require_login` (not require_owner) matches the other
        write-from-the-detail-page action already on this page -
        archive_recording_frame_calibrate() above, a viewer-writable
        calibration log append - rather than gating this behind the
        owner-only job-trigger routes, since correcting a scene
        description is closer to that kind of in-place annotation than
        to kicking off a new pipeline run.

        Returns JSON (not a redirect) so the client-side fetch() in
        archive_recording_detail.html can report success/failure
        inline next to the edit panel without a full page reload
        losing the user's place on a long page - the reload the panel
        itself triggers on success is deliberate client-side, not
        server-side, so a save failure never loses the in-progress
        edit.
        """

        asset = _SCENE_ASSET_BY_DIRECTION.get(direction.lower())
        if asset is None:
            return JSONResponse(
                {"error": f"Unknown scene direction: {direction!r}"},
                status_code=status.HTTP_404_NOT_FOUND,
            )
        recording = _find_archive_recording(
            app.state.archive_recording_cache,
            app.state.camera_config_cache,
            camera_id,
            recording_id,
        )
        asset_file = recording.recording.file(asset)
        if asset_file is None:
            return JSONResponse(
                {
                    "error": (
                        f"No {direction} scene file exists for this "
                        "recording to edit."
                    )
                },
                status_code=status.HTTP_404_NOT_FOUND,
            )
        try:
            asset_file.path.write_text(text, encoding="utf-8")
        except OSError as exc:
            return JSONResponse(
                {"error": f"Could not save: {exc}"},
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            )
        return JSONResponse({"ok": True})

    @app.get("/api/tts/voices")
    async def tts_voices(user: User = Depends(require_login)):
        """The voice picker for archive_recording_detail.html's "Read
        aloud" feature (see elevenlabs_tts.py's own docstring for the
        full backstory - this replaced a browser-native
        speechSynthesis picker entirely). Returns every voice
        ElevenLabs' account API key can use - premade plus any
        Christer has cloned himself ("select speaker among all
        speakers including my own voices") - with `configured: false`
        and an empty list if no ELEVENLABS_API_KEY is set at all,
        rather than an error: an unconfigured key is an expected,
        normal state for anyone running bv-web without ever having
        set one up, not a failure.

        Cached for TTS_VOICE_CACHE_SECONDS per process - the voice
        list rarely changes and this route fires on every archive
        recording detail page load, not just when the picker is
        actually opened, so an uncached call would mean one ElevenLabs
        API round-trip per page view for data that's realistically
        static for days at a time.
        """

        key = elevenlabs_api_key()
        if key is None:
            return JSONResponse({"configured": False, "voices": []})

        cache = app.state.tts_voice_cache
        now = time.monotonic()
        if cache["voices"] is None or now - cache["fetched_at"] > TTS_VOICE_CACHE_SECONDS:
            try:
                voices = elevenlabs_list_voices(api_key=key)
            except ElevenLabsError as exc:
                return JSONResponse(
                    {"configured": True, "voices": [], "error": str(exc)},
                    status_code=status.HTTP_502_BAD_GATEWAY,
                )
            cache["voices"] = voices
            cache["fetched_at"] = now

        return JSONResponse(
            {
                "configured": True,
                "voices": [
                    {"id": v.voice_id, "name": v.name, "category": v.category}
                    for v in cache["voices"]
                ],
            }
        )

    @app.post("/api/tts/speak")
    async def tts_speak(
        text: str = Form(...),
        voice_id: str = Form(...),
        user: User = Depends(require_login),
    ):
        """Returns JSON (not raw audio bytes, since task #1042) so the
        response can carry ElevenLabs' character-level `alignment`
        alongside the base64-encoded MP3 - one request now serves
        playback, the mp3 download link, and a synced .srt download,
        instead of the SRT needing a second call that could drift out
        of sync with the first (Christer: "could you also give me a
        srt file matching the timestamps in the mp3"). The client
        (archive_recording_detail.html/trip_detail.html's own JS)
        decodes `audio_base64` itself via atob() and builds the SRT
        from `alignment` client-side - see elevenlabs_tts.py's
        synthesize_with_timestamps() docstring for the ElevenLabs API
        details.
        """

        key = elevenlabs_api_key()
        if key is None:
            return JSONResponse(
                {"error": "ELEVENLABS_API_KEY is not configured on this server"},
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            )

        try:
            speech = elevenlabs_synthesize(text, voice_id, api_key=key)
        except ElevenLabsError as exc:
            return JSONResponse(
                {"error": str(exc)}, status_code=status.HTTP_502_BAD_GATEWAY
            )

        response: dict = {"audio_base64": speech.audio_base64}
        if speech.alignment is not None:
            response["alignment"] = {
                "characters": speech.alignment.characters,
                "character_start_times_seconds": (
                    speech.alignment.character_start_times_seconds
                ),
                "character_end_times_seconds": (
                    speech.alignment.character_end_times_seconds
                ),
            }
        return JSONResponse(response)

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
        "/archive/{camera_id}/{recording_id}/scene.srt"
    )
    async def archive_recording_sign_read_srt(
        camera_id: str,
        recording_id: str,
        direction: str = "front",
        user: User = Depends(require_login),
    ):
        """A downloadable .srt built from this recording's zoomed-in
        sign/plate reads - see ArchiveRecording.sign_read_srt()'s own
        docstring for the full backstory (Christer: "Does the scene
        detection ever have the timestamps for the description, then i
        would like a scene.srt file to"). Follows the same
        dynamic-generation-not-a-stored-file pattern as
        /trips/{trip_id}/kml: no new asset is ever written to the
        archive, this just reformats a file bv-scribe/bv-generate
        already wrote, on every request.

        404 (not the KML route's own wording, since the failure modes
        differ) whenever there's nothing to build a cue from - no
        scene text for this direction at all, or a scene text with no
        legible sign reads left in it after "not legible" filtering -
        rather than downloading an empty, cue-less .srt file.
        """

        recording = _find_archive_recording(
            app.state.archive_recording_cache,
            app.state.camera_config_cache,
            camera_id,
            recording_id,
        )
        srt = recording.sign_read_srt(direction)
        if srt is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="no legible sign/plate reads found for this recording/direction",
            )

        return Response(
            content=srt,
            media_type="application/x-subrip",
            headers={
                "Content-Disposition": (
                    f'attachment; filename="{recording_id}.{direction}.scene.srt"'
                )
            },
        )

    @app.get(
        "/archive/{camera_id}/{recording_id}/description.srt"
    )
    async def archive_recording_description_srt(
        camera_id: str,
        recording_id: str,
        direction: str = "front",
        user: User = Depends(require_login),
    ):
        """A downloadable .srt for this recording's main scene
        description, timed against the recording's own real length -
        see ArchiveRecording.description_srt()'s own docstring for the
        full backstory (Christer, right after the sign-read .srt above:
        "Could i also get a srt file that is synced with the video of
        3minutes"). Same dynamic-generation-not-a-stored-file pattern
        as the scene.srt route right above and /trips/{trip_id}/kml -
        nothing new is ever written to the archive.

        404 whenever there's nothing to build from - no '## Description'
        text for this direction, or no usable video duration (no
        front/rear video to probe, or the probe itself failed) - rather
        than downloading an empty or all-cues-at-t=0 .srt file.
        """

        recording = _find_archive_recording(
            app.state.archive_recording_cache,
            app.state.camera_config_cache,
            camera_id,
            recording_id,
        )
        srt = recording.description_srt(direction)
        if srt is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=(
                    "no description text or usable video duration found "
                    "for this recording/direction"
                ),
            )

        return Response(
            content=srt,
            media_type="application/x-subrip",
            headers={
                "Content-Disposition": (
                    f'attachment; filename="{recording_id}.{direction}.description.srt"'
                )
            },
        )

    @app.get(
        "/archive/{camera_id}/{recording_id}/frames/{direction}",
        response_class=HTMLResponse,
    )
    async def archive_recording_frames(
        request: Request,
        camera_id: str,
        recording_id: str,
        direction: str,
        user: User = Depends(require_login),
    ):
        """A frame-by-frame calibration view: the actual video frames
        describe_scene()'s own sampling step approximately looked at
        (see _nominal_frame_timestamps()'s own docstring for why this
        is an approximation, not an exact reconstruction), each next to
        the description/sign-read cue nearest that moment in the
        already lag-corrected description.srt, with a field to type in
        the real timestamp if the shown frame doesn't match.

        Christer, on trying to fine-tune the lag-correction curves
        further by hand from a full video-playback comparison: "do you
        think i can see the describe frames and help matching them" -
        this is a much more direct way to build the next round of
        calibration data than reconstructing a moment from full
        playback and guessing at its second.

        404s the same way the description.srt route does - no video
        for this direction, or no usable duration - since there's
        nothing to extract frames from either way."""

        recording = _find_archive_recording(
            app.state.archive_recording_cache,
            app.state.camera_config_cache,
            camera_id,
            recording_id,
        )
        video_path = recording.video_path(direction)
        if video_path is None or not video_path.is_file():
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="no video found for this recording/direction",
            )

        duration_seconds = load_or_compute_duration(recording.recording)
        if not duration_seconds:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="no usable video duration found for this recording",
            )
        duration_seconds = float(duration_seconds)

        timestamps = _frame_viewer_timestamps(recording, direction, duration_seconds)

        cues = _parse_srt_cues(recording.description_srt(direction) or "")
        frames = []
        for index, nominal_seconds in enumerate(timestamps):
            nearest_text, nearest_gap_seconds = _nearest_cue_text(cues, nominal_seconds)
            frames.append(
                {
                    "index": index,
                    "nominal_seconds": nominal_seconds,
                    "nominal_label": _format_seconds_label(nominal_seconds),
                    "nearest_text": nearest_text,
                    "nearest_gap_seconds": nearest_gap_seconds,
                }
            )

        saved_param = request.query_params.get("saved")
        saved_index = int(saved_param) if saved_param is not None and saved_param.lstrip("-").isdigit() else None

        return templates.TemplateResponse(
            request,
            "archive_recording_frames.html",
            {
                "user": user,
                "camera_id": camera_id,
                "recording": recording,
                "direction": direction,
                "frames": frames,
                "saved": saved_index,
            },
        )

    @app.get("/archive/{camera_id}/{recording_id}/frames/{direction}/{index}.jpg")
    async def archive_recording_frame_image(
        camera_id: str,
        recording_id: str,
        direction: str,
        index: int,
        user: User = Depends(require_login),
    ):
        """Serves (generating and caching on first request) the single
        real video frame at nominal frame `index`'s approximate
        timestamp - see _nominal_frame_timestamps() and the route
        above. Cached under a per-recording/direction subfolder of
        default_config_dir()'s own ".description_frame_cache" (same
        "app-level cache under default_config_dir(), not written into
        the archive itself" convention as .hevc_preview_cache and
        .parking_repair_cache above) - regenerating identical frames on
        every page view would mean one ffmpeg seek+decode per frame per
        visit."""

        recording = _find_archive_recording(
            app.state.archive_recording_cache,
            app.state.camera_config_cache,
            camera_id,
            recording_id,
        )
        video_path = recording.video_path(direction)
        if video_path is None or not video_path.is_file():
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="video not found"
            )

        duration_seconds = load_or_compute_duration(recording.recording)
        if not duration_seconds:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="no usable duration"
            )
        timestamps = _frame_viewer_timestamps(recording, direction, float(duration_seconds))
        if index < 0 or index >= len(timestamps):
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="frame index out of range"
            )

        cache_dir = (
            default_config_dir()
            / ".description_frame_cache"
            / camera_id
            / str(recording_id)
            / direction.lower()
        )
        cache_dir.mkdir(parents=True, exist_ok=True)
        cache_path = cache_dir / f"{index}.jpg"
        if not cache_path.is_file():
            try:
                extract_video_thumbnail(
                    video_path, cache_path, offset_seconds=timestamps[index]
                )
            except MediaToolError as exc:
                raise HTTPException(
                    status_code=status.HTTP_502_BAD_GATEWAY,
                    detail=f"could not extract frame: {exc}",
                ) from exc

        return FileResponse(cache_path)

    @app.post("/archive/{camera_id}/{recording_id}/frames/{direction}/calibrate")
    async def archive_recording_frame_calibrate(
        request: Request,
        camera_id: str,
        recording_id: str,
        direction: str,
        index: int = Form(...),
        nominal_seconds: float = Form(...),
        corrected_seconds: str = Form(""),
        nearest_text: str = Form(""),
        user: User = Depends(require_login),
    ):
        """Records one manually-confirmed frame timestamp to a shared
        calibration log - raw data for the next round of
        _LAG_CORRECTION_CURVE/_SIGN_LAG_CORRECTION_CURVE tuning in
        archive_browser.py, the same way Christer's earlier hand-
        retimed .srt files were used, but captured directly at the
        frame instead of via a separate uploaded file. Deliberately
        NOT auto-applied to either curve - a human (Christer, or
        whoever revisits archive_browser.py next) still reviews and
        picks knots by hand, the same trust-ranking judgment calls
        ("the bus is my most correct point") a raw log can't make for
        itself.

        `corrected_seconds` is optional - a blank submission just means
        "no correction needed, this frame's nominal timestamp already
        looks right," still worth recording as a real (0.0-delta) data
        point.

        One JSON line per submission (JSON Lines, not one big JSON
        array) - appending is then a single write with no
        read-modify-write race between concurrent submissions, the
        same reasoning core/history.py's own HistoryEntry log already
        uses."""

        corrected_value = None
        if corrected_seconds.strip():
            try:
                corrected_value = float(corrected_seconds)
            except ValueError:
                corrected_value = None

        entry = {
            "recorded_at": datetime.now().isoformat(),
            "camera_id": camera_id,
            "recording_id": str(recording_id),
            "direction": direction.lower(),
            "frame_index": index,
            "nominal_seconds": nominal_seconds,
            "corrected_seconds": corrected_value,
            "nearest_cue_text": nearest_text,
        }
        log_path = default_config_dir() / "frame_calibration.jsonl"
        log_path.parent.mkdir(parents=True, exist_ok=True)
        with log_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(entry, ensure_ascii=False) + "\n")

        query = f"?saved={index}"
        return RedirectResponse(
            url=f"/archive/{camera_id}/{recording_id}/frames/{direction}{query}",
            status_code=status.HTTP_303_SEE_OTHER,
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
            {
                "user": user,
                "cameras": _camera_options(),
                # For the optional --snap direction checkboxes (Christer:
                # "i want to be able to get the snapshot for bv-gps in
                # bv-web") - same SNAPSHOT_DIRECTIONS list bv-snap's own
                # CLI uses (core/blackvue_client.py).
                "snapshot_directions": SNAPSHOT_DIRECTIONS,
            },
        )

    @app.post("/jobs/bv-gps")
    async def new_bv_gps_submit(
        request: Request,
        id: str = Form(...),
        timeout: int = Form(5, ge=1),
        no_address: bool = Form(False),
        snap: bool = Form(False),
        directions: list[str] = Form([]),
        user: User = Depends(require_owner),
    ):
        job = app.state.job_runner.start_bv_gps(
            id_=id,
            timeout=timeout,
            no_address=no_address,
            snap=snap,
            # Empty means every direction (F/R/I), not none - same
            # convention bv-snap's own CLI --direction default uses
            # (start_bv_gps()/bv_snap.py's parse_args()) - only
            # meaningful when snap=True.
            directions=directions or None,
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
        gsensor_graph_z: bool = Form(False),
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
        scene_quantize: str = Form("auto"),
        scene_gpu_memory_fraction: str = Form(""),
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
            "gsensor_graph_z": gsensor_graph_z,
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
            "scene_quantize": scene_quantize,
            "scene_gpu_memory_fraction": scene_gpu_memory_fraction,
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
                gsensor_graph_z=gsensor_graph_z,
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
                scene_quantize=_clean(scene_quantize),
                scene_gpu_memory_fraction=_clean(scene_gpu_memory_fraction),
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
            # task #1391 - clear drivers_page()'s has-video cache the
            # instant this download actually succeeds, instead of
            # leaving it to that cache's own TTL backstop. See
            # start_bv_download()'s own docstring for the full
            # reasoning (Christer: "Cant the 5 minute cashed be solved
            # another way, its irritating that after 5 minutes i need
            # to wait 30 seconds").
            on_success=app.state.drivers_page_recording_cache.clear,
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

    @app.get("/jobs/bv-drivers", response_class=HTMLResponse)
    async def new_bv_drivers_form(
        request: Request, user: User = Depends(require_owner)
    ):
        return templates.TemplateResponse(
            request,
            "job_new_bv_drivers.html",
            {"user": user, "cameras": _camera_options(), "error": None},
        )

    @app.post("/jobs/bv-drivers")
    async def new_bv_drivers_submit(
        request: Request,
        id: str = Form(...),
        from_: str = Form(""),
        until: str = Form(""),
        timestamp: str = Form(""),
        max_gap_minutes: str = Form(""),
        gap_tolerance_seconds: str = Form(""),
        min_visits: str = Form(""),
        user: User = Depends(require_owner),
    ):
        # Same "blank means bv-drivers' own default, not zero" numeric
        # handling as bv-ls's own job-trigger route above.
        error = None
        max_gap_minutes_value: int | None = None
        gap_tolerance_seconds_value: int | None = None
        min_visits_value = 2

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

        if error is None and min_visits.strip():
            try:
                min_visits_value = int(min_visits.strip())
            except ValueError:
                error = "Min visits must be a whole number."

        if error is not None:
            return templates.TemplateResponse(
                request,
                "job_new_bv_drivers.html",
                {"user": user, "cameras": _camera_options(), "error": error},
                status_code=status.HTTP_400_BAD_REQUEST,
            )

        archive_path = _find_camera_archive(app.state.camera_config_cache, id)

        job = app.state.job_runner.start_bv_drivers(
            camera_id=id,
            archive_path=archive_path,
            from_=from_.strip() or None,
            until=until.strip() or None,
            timestamp=timestamp.strip() or None,
            max_gap_minutes=max_gap_minutes_value,
            gap_tolerance_seconds=gap_tolerance_seconds_value,
            min_visits=min_visits_value,
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
        llm_model: str = Form("small"),
        user: User = Depends(require_viewer_or_owner),
    ):
        """Quick, synchronous voice-to-text for the bv-search form's
        "Search by voice" button - deliberately NOT a JobRunner
        background Job the way bv-search itself is. Same "on-demand
        action, not a multi-minute run" precedent
        archive_recording_thumbnail()/archive_recording_file()'s HEVC-
        preview branch already set: a few-second spoken query
        transcribes in a few seconds, nowhere near needing the
        Job/polling/history machinery built for bv-generate/bv-export's
        multi-minute runs.

        Transcription was faster-whisper (generate/speech.py's
        transcribe(), model_size="small") until Christer traced a real
        failed search back to it mis-transcribing a Swedish place name
        ("Vårby gård"/"Vårbygård" heard as the unrelated two words "vår
        bygård") - see voice_asr.py's own module docstring for the full
        investigation. Now uses web/voice_asr.py's Qwen3-ASR-1.7B
        integration instead, biased toward place names Christer has
        searched near before (known_places_from_params() below, built
        from his own bv-search history) plus every place name that's
        ever successfully geocoded via a --place lookup
        (known_places_from_learned() - self-maintaining, written by
        cli/bv_search.py's _run() the moment a place resolves to real
        coordinates, no config file to hand-edit; replaced an earlier
        manually-maintained known_places.txt after Christer's own
        objection: "I dont like halfway fixes like known_places, that
        needs to be updated for every single user" - see
        remember_known_place()'s own docstring for the full story).
        Whisper stays completely
        unchanged everywhere else in this project (bv-generate
        --transcribe/--translate, subtitle generation, bv-scribe) -
        Christer's own explicit scope decision when asked: "Replace
        Whisper for voice search only."

        IMPORTANT distinction Christer flagged after that swap landed:
        "i thought audio llm would understand that, its a thinker not
        sound to text only" - Qwen3-ASR-1.7B is speech-to-text ONLY, it
        has no more understanding of what the words mean than Whisper
        did. The part that actually reasons about the transcript is
        web/voice_llm.py, a separate LLM call. Concretely this mattered
        for a real phrasing gap: "VårbyGård in range of 400 m" (place
        BEFORE distance) isn't recognized by voice_query.py's regexes
        at all (they only match distance-before-place: "within 400m of
        X"), so it used to silently fall through to a literal Text
        search - clearly not what was meant.

        So as of this change, **the LLM parser (voice_llm.py) is the
        primary parser** - its result is what auto-fills the form by
        default (`llm_model` now defaults to "small", not "none"). The
        original regex parsers below still run unconditionally and are
        used in two ways: as the actual result whenever `llm_model` is
        "none" (an explicit opt-out - no model load at all, useful if
        Christer wants a fast/free/offline-capable path), and as the
        automatic fallback if the LLM call itself fails (missing torch/
        qwen extras, a broken model download, unparseable model output)
        - matching this project's "never let an experimental/model-
        backed path take down a working one" stance. Both results are
        always in the response (`regex` key always present; the
        top-level text/place/radius_meters/timestamp/from_/until fields
        are whichever one is authoritative - see `parser` for which).

        - parse_spoken_query() (web/voice_query.py): "within/less
          than <distance> <unit> of/from <place>" -> Place/Radius (now
          also the reverse order, "<place> in range of/within
          <distance> <unit>" - see that module's own docstring).
        - parse_spoken_timerange() (web/voice_time.py): relative or
          explicit dates/date-ranges ("yesterday", "last week", "from
          July 15th to July 20th", ...) -> Timestamp or From/Until.

        Both run over the same transcript; the time-range match (if
        any) is stripped first so the place/radius parser doesn't
        have to independently recognize date words. If *either*
        parser recognized something, Text is cleared rather than left
        as whatever wasn't consumed - see voice_query.py's own
        docstring for why: bv-search ANDs Text against every other
        filter, so stray leftover words there would silently zero out
        results that are otherwise correctly filtered by place/radius
        or date. Every field in the response is still just an
        editable suggestion - the frontend fills the form but never
        auto-submits.

        `llm_model` ("none"/"scene"/"small", default "small") selects
        which local-LLM extraction runs (web/voice_llm.py) - "none"
        skips it entirely and uses the regex result directly. Any
        failure loading or running the model (missing extras,
        ImportError, a bad download, a malformed/unparseable model
        response) is caught, reported via `llm_error`, and the response
        degrades to the regex result rather than erroring out.
        """

        suffix = Path(audio.filename or "").suffix or ".webm"
        with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp:
            tmp_path = Path(tmp.name)
            tmp.write(await audio.read())

        # Christer: "i thought the llm would do the transcribe" - it
        # now does. Qwen3-ASR-1.7B replaces Whisper for this route
        # only (see voice_asr.py's own module docstring for the full
        # story: a real failed search traced back to Whisper mis-
        # transcribing a Swedish place name, Qwen2-Audio turned out not
        # to support Swedish at all, Qwen3-ASR-1.7B does and supports
        # native vocabulary biasing). known_places comes from two
        # sources, learned entries first: Christer hit the same
        # mis-transcription again ("Vår Nygård") even with history-
        # based biasing, because that alone only helps once a place has
        # already been searched near successfully once via a full,
        # submitted bv-search job - see known_places_from_learned()'s
        # own docstring for the bootstrap gap this closes (a --place
        # lookup that resolves is remembered the moment it resolves,
        # not only once a whole job with it has run and been recorded).
        # Learned places are deliberately ordered first in the bias
        # string (_build_context() just joins them in order) since
        # they're real, Nominatim-confirmed place names, not just
        # incidental form-field history.
        learned_places = known_places_from_learned(default_config_dir())
        history_places = known_places_from_params(
            [numbered.entry.params for numbered in _recent_web_runs("bv-search")]
        )
        seen_places: set[str] = set()
        known_places: list[str] = []
        for place in [*learned_places, *history_places]:
            key = place.casefold()
            if key in seen_places:
                continue
            seen_places.add(key)
            known_places.append(place)
        try:
            result = transcribe_voice_query(tmp_path, known_places=known_places)
        except MediaToolError as exc:
            return JSONResponse(
                {"error": str(exc)}, status_code=status.HTTP_422_UNPROCESSABLE_ENTITY
            )
        finally:
            tmp_path.unlink(missing_ok=True)

        transcript = result.text.strip()
        today = datetime.now().date()
        time_range = parse_spoken_timerange(transcript, today)
        remainder = time_range.remainder if time_range.matched else transcript
        parsed = parse_spoken_query(remainder)

        matched_something = time_range.matched or parsed.place is not None
        regex_result = {
            "text": "" if matched_something else parsed.text,
            "place": parsed.place,
            "radius_meters": parsed.radius_meters,
            "timestamp": time_range.timestamp,
            "from_": time_range.from_,
            "until": time_range.until,
        }

        # Primary result: the LLM's understanding of the transcript
        # (see docstring above for why this - not the regex parser -
        # is now the default), falling back to the regex result if the
        # LLM is switched off or fails.
        #
        # Real gap Christer hit: "Hitta en resa med bilen som är
        # närmare än tusen meter ifrån vår bygård." - the LLM ran
        # successfully (no exception) but understood the whole thing
        # as free text, missing both the place and the "tusen meter"
        # (written-out "a thousand meters", not digits) radius
        # entirely - a small-model comprehension miss, not a code
        # error. Blindly trusting "the LLM didn't raise" as "the LLM's
        # answer is right" meant a *worse* result (place/radius blank,
        # full sentence dumped into Text) won out over what the regex
        # parser could have found. So: if the LLM's own result found no
        # place/date at all, but the regex parser did, the regex result
        # becomes primary instead - see llm_found_nothing below. This
        # doesn't second-guess the LLM when it DID find something
        # (still primary in that case), only when it found nothing
        # structured, which is the case a plain pattern-match parser
        # can plausibly out-perform a small model on.
        primary = regex_result
        parser_used = "regex"
        llm_error = None
        llm_note = None
        llm_result = None
        if transcript and llm_model in VOICE_LLM_MODEL_CHOICES:
            try:
                llm_parsed = extract_voice_query_llm(
                    transcript, today, model_choice=llm_model
                )
            except (MediaToolError, ValueError) as exc:
                llm_error = str(exc)
            else:
                llm_result = {
                    "text": llm_parsed.text,
                    "place": llm_parsed.place,
                    "radius_meters": llm_parsed.radius_meters,
                    "timestamp": llm_parsed.timestamp,
                    "from_": llm_parsed.from_,
                    "until": llm_parsed.until,
                }
                llm_found_nothing = (
                    llm_result["place"] is None
                    and llm_result["timestamp"] is None
                    and not (llm_result["from_"] and llm_result["until"])
                )
                if llm_found_nothing and matched_something:
                    primary = regex_result
                    parser_used = "regex"
                    llm_note = (
                        "the local LLM understood this as free text only - the "
                        "quick pattern-match parser found a more specific "
                        "place/date match instead"
                    )
                else:
                    primary = llm_result
                    parser_used = "llm"

        return JSONResponse(
            {
                "transcript": transcript,
                "text": primary["text"],
                "place": primary["place"],
                "radius_meters": primary["radius_meters"],
                "timestamp": primary["timestamp"],
                "from_": primary["from_"],
                "until": primary["until"],
                "parser": parser_used,
                "llm_model": llm_model,
                "llm_error": llm_error,
                "llm_note": llm_note,
                "llm": llm_result,
                "regex": regex_result,
            }
        )

    @app.get("/jobs/bv-search/geocode-preview")
    async def geocode_preview_voice_search(
        place: str = Query(""),
        user: User = Depends(require_viewer_or_owner),
    ):
        """Live coordinate preview for the bv-search form's Place field -
        closes a real UX gap Christer hit twice in a row ("Solna showed
        up in place, but no coordinates"), which turned out not to be a
        backend bug at all: "Ok, i never pressed search, i just looked
        at the coordinates". Until this route existed, Place was just
        text - the only place --place ever actually got resolved to
        real lat/lon was inside cli/bv_search.py's _run(), which only
        runs once the whole job is submitted. So a place name could be
        heard/typed correctly and still show nothing, because nothing
        had tried to geocode it yet. This route lets the frontend fire
        a quick, read-only lookup the moment Place is filled (by voice
        or by hand) and show the answer immediately, without requiring
        a full bv-search run.

        Deliberately GET + query param, not POST - this is a read-only
        lookup with no side effect on any job, same shape as
        archive_recording_location()'s own reverse-geocode preview
        below. Reuses load_or_forward_geocode() (export/geocoding.py),
        the exact function cli/bv_search.py's _run() calls for the real
        --place resolution, so the preview and the eventual real search
        always agree.

        Cache dir is default_config_dir() / ".osm_cache" - bv-web's own
        writable scratch space, NOT archive_path / ".osm_cache" the way
        cli/bv_search.py's _run() does it. That distinction matters here
        for the same reason it mattered for the /location route's own
        geocode_cache_dir (see that route's comment above): bv-web's
        docker-compose.yml mounts /data/archive read-only, so writing a
        cache under it 500s with "Read-only file system" on the NAS.

        On a genuine resolve, also calls remember_known_place() - same
        self-learning hook cli/bv_search.py's _run() uses (see that
        function's own docstring), so a place typed/corrected here and
        merely previewed - never actually searched - still gets
        remembered for future ASR bias. Arguably an even better moment
        to learn it than the full-search hook: this fires the instant
        Christer confirms a place resolves, before he's decided whether
        to run the search at all.
        """

        place = place.strip()
        if not place:
            return JSONResponse({"resolved": False, "error": None})

        from ..export.geocoding import load_or_forward_geocode
        from .voice_asr import remember_known_place

        geocode_cache_dir = default_config_dir() / ".osm_cache"
        try:
            result = load_or_forward_geocode(place, geocode_cache_dir)
        except MediaToolError as exc:
            return JSONResponse({"resolved": False, "error": str(exc)})

        if result is None:
            return JSONResponse({"resolved": False, "error": None})

        try:
            remember_known_place(place, default_config_dir())
        except OSError:
            pass

        lat, lon = result.point
        return JSONResponse({"resolved": True, "lat": lat, "lon": lon, "error": None})

    @app.get("/jobs/{job_id}", response_class=HTMLResponse)
    async def job_detail(
        request: Request, job_id: str, user: User = Depends(require_viewer_or_owner)
    ):
        job = _find_job(app.state.job_runner, job_id)
        _authorize_job_view(job, user)
        job_status, output, prompt = job.snapshot()
        # Christer: "i want to see the snapshot pictures on bv-web and
        # then deleted after page refresh" - see
        # _apply_snapshot_deletion_gating()'s own docstring for the
        # full show-once-then-delete design, including the "?auto=1"
        # fix for Christer's follow-up report that the files weren't
        # actually getting deleted.
        is_auto_reload = request.query_params.get("auto") == "1"
        _apply_snapshot_deletion_gating(job, job_status, is_auto_reload)
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
        # See JOB_NEW_LABELS' own comment: job.command always starts
        # with the same command name its "new job" form is registered
        # under, e.g. "bv-search cam1" -> "/jobs/bv-search".
        command_name = job.command.split(maxsplit=1)[0]
        response = templates.TemplateResponse(
            request,
            "job_detail.html",
            {
                "user": user,
                "job": job,
                "back_link_url": f"/jobs/{command_name}",
                "back_link_label": JOB_NEW_LABELS.get(command_name, command_name),
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
                # Whether _job_output_lines.html should try rendering an
                # inline <img> after each "<direction>: saved <path>"
                # line - see SNAP_SAVED_RE's own comment. Stays True
                # even after _delete_job_snapshots() has run (below),
                # since the text lines themselves are never rewritten -
                # the <img> tags just 404-and-hide once their files are
                # gone (see job_snapshot_image()).
                "snapshot_job": job.snapshot_dir is not None,
                "job_id": job.id,
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
            output=displayed_output,
            camera_id=camera_id,
            snapshot_job=job.snapshot_dir is not None,
            job_id=job.id,
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

    @app.get("/jobs/{job_id}/snapshot/{direction}")
    async def job_snapshot_image(
        job_id: str,
        direction: str,
        user: User = Depends(require_viewer_or_owner),
    ):
        """Serve one of a snap-capable job's captured .jpg files, so
        _job_output_lines.html can render it inline (Christer: "Of course
        i want to see the snapshot pictures on bv-web"). 404s once
        _delete_job_snapshots() has removed the file (job_detail()'s
        second finished-state load) - the <img> tag just fails to load at
        that point rather than the page erroring out; same 404 if the job
        never captured that direction at all (e.g. R when only F/I came
        back). Same auth as every other job route - a snapshot is exactly
        as sensitive as the rest of that job's output.
        """

        job = _find_job(app.state.job_runner, job_id)
        _authorize_job_view(job, user)
        path = _job_snapshot_path(job, direction)
        if path is None or not path.is_file():
            raise HTTPException(status_code=404, detail="Snapshot not found")
        return FileResponse(path)

    @app.post("/jobs/{job_id}/snapshot/delete")
    async def job_snapshot_delete(
        job_id: str,
        user: User = Depends(require_viewer_or_owner),
    ):
        """Christer, twice now, after the load-counting design above
        ("?auto=1" exclusion, Cache-Control: no-store, the pageshow/
        persisted bfcache reload) still wasn't enough: "files not
        deleted on page back". The whole show-once-then-delete-on-the-
        next-load design (_apply_snapshot_deletion_gating()) depends on
        a *second real HTTP request* actually reaching this server -
        and a browser's Back button has more ways to avoid that than
        just bfcache (Brave, which Christer uses, doesn't even
        participate in Chromium's 2025 "allow no-store into bfcache"
        rollout the same way Chrome does, so no-store's own cache-
        busting guarantee can't be assumed either). Rather than keep
        chasing every browser's caching behavior, this route sidesteps
        the problem: job_detail.html's own script now fires a
        `navigator.sendBeacon()` here from a `pagehide` handler,
        registered only on a load that actually rendered the finished-
        state images. `pagehide` is the platform's own recommended
        "the user is leaving this page" signal - it fires on a real
        navigation, a Back/Forward, a tab close, *and* a bfcache
        eviction, so deletion no longer depends on whatever the *next*
        load of this URL happens to do. sendBeacon (not fetch) because
        the browser guarantees it's sent even though the page is
        already unloading - a normal fetch() started from a pagehide
        handler can get cancelled mid-flight.

        Deliberately unconditional (beyond auth) rather than re-
        checking Job.snapshot_shown_while_finished - the client only
        ever registers this beacon on a page load that already set
        that flag server-side, and _delete_job_snapshots() is a no-op
        (missing_ok=True) if the files are already gone, so there's no
        real double-delete risk to guard against. 204 with an empty
        body - sendBeacon doesn't inspect the response either way."""

        job = _find_job(app.state.job_runner, job_id)
        _authorize_job_view(job, user)
        _delete_job_snapshots(job)
        return Response(status_code=status.HTTP_204_NO_CONTENT)

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


def _selected_stat_fields(fields: list[str]) -> list[str]:
    """stats_dashboard()'s own --fields query-string validation - drop
    any key that isn't a real STAT_FIELDS entry (a stale bookmark, a
    hand-edited URL), and fall back to bv-stats' own DEFAULT_FIELDS
    when nothing valid is left. The same "silently degrade to
    something sane rather than 500 on a tampered query string" choice
    _archive_filter_flags() below makes for the archive browser's own
    filters. A plain module-level function (not nested inside
    create_app()) so it's directly testable without a TestClient -
    see test_app_reuse.py's own module docstring for why this repo
    tests app.py logic as plain functions rather than through real
    HTTP requests."""
    return [f for f in fields if f in STAT_FIELDS] or list(DEFAULT_FIELDS)


def _selected_graph_fields(fields: list[str]) -> list[str]:
    """stats_dashboard()'s own --graph_fields query-string validation -
    Christer's own follow-up request ("I will also like to have more
    than 1 stats on the y axis") after the dashboard originally only
    ever graphed one field at a time. Keeps only keys that are a real
    STAT_FIELDS entry, falling back to bv-stats' own first
    DEFAULT_FIELDS entry so there's always at least one series to draw
    - the same "degrade to something sane, don't 500 on a tampered or
    stale query string" contract _selected_stat_fields() above already
    follows.

    Deliberately *not* filtered against the report's own --fields
    selection (_selected_stat_fields()) - Christer, looking at a
    5-series chart: "Why 15 fields but only 5 graph fields." The
    "Graph fields" checkbox list used to only ever offer whichever of
    the 15 STAT_FIELDS also happened to be checked under "Fields"
    above it, coupling two independent questions (what the report
    table should show vs. what the chart should plot) for no reason.
    Graphing a field no longer requires it to also be a report column
    - see _fields_for_aggregation() below for how stats_dashboard()
    makes sure a graph-only field still has aggregated data to draw."""
    graph = [f for f in fields if f in STAT_FIELDS]
    return graph or [DEFAULT_FIELDS[0]]


def _fields_for_aggregation(selected_fields: list[str], graph_fields: list[str]) -> list[str]:
    """The full set of STAT_FIELDS keys aggregate_recording_stats()
    needs to actually compute for one /stats request. Now that
    "Graph fields" is independent of "Fields" (see
    _selected_graph_fields()'s own docstring), a field can be graphed
    without being a report table column - so stats_dashboard() has to
    aggregate the union of both lists, not just selected_fields, or a
    graph-only field would have no data in bucket.values and silently
    render as an empty series. Order: selected_fields first (so the
    report table's own column order is unaffected), then any
    graph_fields not already included, deduplicated."""
    fields = list(selected_fields)
    seen = set(fields)
    for field in graph_fields:
        if field not in seen:
            fields.append(field)
            seen.add(field)
    return fields


def _stats_chart_series(buckets: list, graph_fields: list[str]) -> dict[str, object]:
    """The per-series payload stats.html's inline <script> needs to
    draw one or more bars/lines sharing a single x-axis (one entry per
    bucket, in bucket order) and, since Christer also asked "where are
    my legends", enough about each series (its own label/unit) for the
    chart to build a real legend from this data alone rather than the
    template hand-writing one field's label like the single-series
    version did. Each series' own `values` entry is None wherever that
    field has no reading in a bucket (not 0 - same "missing isn't
    zero" convention _stats_chart_data() used before this), aligned by
    index with `keys`/`recording_counts` so the chart script can zip
    them back together without a lookup."""
    return {
        "keys": [bucket.key for bucket in buckets],
        "recording_counts": [len(bucket.recordings) for bucket in buckets],
        "series": [
            {
                "field": field,
                "label": STAT_FIELDS[field].label,
                "unit": STAT_FIELDS[field].unit,
                "values": [bucket.values.get(field) for bucket in buckets],
            }
            for field in graph_fields
        ],
    }


def _slugify(value: object) -> str:
    """stats.html's stat_value/slugify Jinja filters both live here so
    they're testable the same way - a bucket key ("2026-08", "Monday",
    "2026-08-23") becomes a safe HTML element id for the chart's
    click-to-scroll-to-row behavior (see stats.html's own inline
    <script> for what reads this id back out)."""
    return re.sub(r"[^a-z0-9]+", "-", str(value).lower()).strip("-")


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


def _reverse_geocode_or_none(
    point: tuple[float, float] | None, geocode_cache_dir: Path
) -> str | None:
    """load_or_reverse_geocode() for a plain (lat, lon) point (as
    opposed to _describe_gps_fix()'s GpsFix), degrading to None on a
    missing point or a MediaToolError (Nominatim unreachable, etc.) -
    used by drivers_page() to address every common place and undecided
    trip start/stop point without a failed lookup 500ing the whole
    /drivers page."""

    if point is None:
        return None
    try:
        return load_or_reverse_geocode(point[0], point[1], geocode_cache_dir)
    except MediaToolError:
        return None


def _recording_has_video_or_none(
    recording_cache: ArchiveRecordingCache,
    camera_config_cache: CameraConfigCache,
    camera_id: str | None,
    recording_id: str | None,
) -> bool | None:
    """ArchiveRecording.has_video for (camera_id, recording_id), or
    None if either is missing or the recording can't be resolved at
    all (camera/recording gone, bad id, ...) - same "degrade instead
    of 500" shape as _reverse_geocode_or_none() above. Used by
    drivers_page() so its Start/Stop "Video" links can say "No video"
    and point at the archive browser for that day instead of the
    specific (video-less) recording, when the linked recording has no
    video downloaded - Christer: "On specific trips i get a video
    link, even if there is no video, it should be named 'No video' but
    still keep the link" then, on reflection, "the 'No video' link
    should point to browse archive for the same day" (see drivers.html
    for the href swap this drives). A recording can have a resolvable
    id (a GPS/thumbnail sidecar was downloaded) without its own video
    ever having downloaded - see ArchiveRecording.has_video's own
    docstring on why that's a real, common gap, not a bug."""

    if not camera_id or not recording_id:
        return None
    try:
        recording = _find_archive_recording(
            recording_cache, camera_config_cache, camera_id, recording_id
        )
    except HTTPException:
        return None
    return recording.has_video


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


def _parse_srt_cues(srt_text: str) -> list[tuple[float, str]]:
    """Parse an .srt document's own cues into a flat list of
    (start_seconds, text) pairs, in file order.

    Deliberately minimal - this is only used by the frame-viewer
    (archive_recording_frames route) to find "the cue nearest this
    frame's nominal timestamp" for display next to each extracted
    frame, not to re-derive or validate the SRT's own timing. Only
    the cue start time is kept; end times aren't needed for a
    nearest-match lookup. Multi-line cue text is rejoined with a
    single space so it displays on one line next to the frame."""

    cues: list[tuple[float, str]] = []
    blocks = re.split(r"\n\s*\n", srt_text.strip())
    for block in blocks:
        lines = [line for line in block.splitlines() if line.strip()]
        if len(lines) < 2:
            continue
        # lines[0] is the numeric cue index; lines[1] is the
        # "HH:MM:SS,mmm --> HH:MM:SS,mmm" timing line.
        timing_match = re.match(
            r"(\d+):(\d+):(\d+),(\d+)\s*-->", lines[1]
        )
        if timing_match is None:
            continue
        hours, minutes, seconds, millis = (int(value) for value in timing_match.groups())
        start_seconds = hours * 3600 + minutes * 60 + seconds + millis / 1000.0
        text = " ".join(lines[2:]).strip()
        if text:
            cues.append((start_seconds, text))
    return cues


def _nearest_cue_text(
    cues: list[tuple[float, str]], nominal_seconds: float
) -> tuple[str, float | None]:
    """The text of whichever cue in `cues` starts closest (in either
    direction) to `nominal_seconds`, plus how far away it actually is
    in seconds - the description/sign-read line Christer should
    compare this extracted frame against while calibrating, alongside
    the honesty check of knowing whether "nearest" still means
    "close."

    ("", None) if there are no cues at all (e.g. a recording with a
    video but no generated description.srt yet).

    The gap is deliberately surfaced rather than silently hidden or
    thresholded away: Christer, looking at a real frame that visibly
    didn't match its shown cue ("frame 6 ... talks about the bus" /
    "I dont se any red bus"): the *closest* cue by timestamp can still
    be many seconds away from a frame's own (approximate - see
    _nominal_frame_timestamps()'s own docstring) sample time, once a
    clip only has a handful of cues or a cue's own timestamp was
    pulled far from its neighbors by _LAG_CORRECTION_CURVE's
    non-monotonic correction. Silently showing that cue with no
    indication of the gap makes it look like a confident match when
    it may not be one; silently hiding it below some threshold would
    throw away real information Christer might still want to see and
    judge for himself, the same "the user still reviews and picks
    knots by hand" principle the calibration log itself is built on."""

    if not cues:
        return "", None
    nearest = min(cues, key=lambda cue: abs(cue[0] - nominal_seconds))
    return nearest[1], abs(nearest[0] - nominal_seconds)


def _format_seconds_label(seconds: float) -> str:
    """Render a raw seconds value as an "M:SS" (or "H:MM:SS" past the
    hour mark) label for the frame-viewer's per-frame captions -
    matching the "minutes once >=60s" convention already used for
    sign-read display_text (see task #1068)."""

    total_seconds = max(0, int(round(seconds)))
    hours, remainder = divmod(total_seconds, 3600)
    minutes, secs = divmod(remainder, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes}:{secs:02d}"


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


def _job_snapshot_path(job: Job, direction: str) -> Path | None:
    """Resolve a snap-capable job's saved path for one direction, from
    its own "<direction>: saved <path>" output line (see
    SNAP_SAVED_RE) - not from anything client-supplied, since this
    backs job_snapshot_image()'s file-serving route. Returns None if
    the job never captured that direction, or (defensively) if the
    line's path somehow isn't inside job.snapshot_dir - every real
    "saved" line comes from save_snapshots() writing into exactly that
    directory, so this should never actually trigger outside a bug."""

    if job.snapshot_dir is None:
        return None
    snapshot_dir = job.snapshot_dir.resolve()
    _, output, _ = job.snapshot()
    for line in output:
        match = SNAP_SAVED_RE.match(line)
        if match is None or match.group(1) != direction:
            continue
        path = Path(match.group(2)).resolve()
        if not path.is_relative_to(snapshot_dir):
            return None
        return path
    return None


def _delete_job_snapshots(job: Job) -> None:
    """Delete every .jpg file a snap-capable job actually saved
    (Christer: "i want to see the snapshot pictures on bv-web and
    then deleted after page refresh") - called by job_detail() the
    *second* time a finished snap job's page is fully loaded (see
    Job.snapshot_shown_while_finished's own docstring for why not the
    first). Parses the same output lines _job_snapshot_path() does
    and deletes exactly those files, rather than wiping
    job.snapshot_dir wholesale - that directory
    (default_snapshots_dir(id_)) is shared across every run against
    that camera, not just this one job's."""

    if job.snapshot_dir is None:
        return
    snapshot_dir = job.snapshot_dir.resolve()
    _, output, _ = job.snapshot()
    for line in output:
        match = SNAP_SAVED_RE.match(line)
        if match is None:
            continue
        path = Path(match.group(2)).resolve()
        if not path.is_relative_to(snapshot_dir):
            continue
        try:
            path.unlink(missing_ok=True)
        except OSError:
            pass


def _apply_snapshot_deletion_gating(
    job: Job, job_status: JobStatus, is_auto_reload: bool
) -> None:
    """Called by job_detail() on every load of a snap-capable job's
    page - decides whether this particular finished-state load should
    just show the already-captured images, or delete them first
    (Christer: "i want to see the snapshot pictures on bv-web and
    then deleted after page refresh").

    Show-once-then-delete: the *first* time this job's page is ever
    rendered while finished, Job.snapshot_shown_while_finished flips
    True and nothing gets deleted - the images need to actually be
    visible at least once. Every finished-state load after that
    deletes the files first, UNLESS this specific load is
    job_detail.html's own automatic completion reload (marked
    "?auto=1" - see that template's own comment on why, and
    job_detail()'s call site for where is_auto_reload comes from).

    That exclusion is the actual fix for Christer's follow-up report
    that the files weren't getting deleted: job_detail.html polls
    while a job runs and, the instant it notices the job left
    "running", does one automatic reload so the page can show the
    finished-state furniture - and that reload was itself silently
    counting as the "shown once" load. So Christer's own next manual
    refresh was really the *second* finished load and should have
    deleted... except on a fast job (finished before the very first
    page load, so no poll loop and no automatic reload ever ran) or a
    slower job where his manual refresh happened to race the automatic
    one, there was effectively no distinction between "the load that
    shows" and "the load that deletes" - both needed a genuine extra
    manual refresh, which felt like deletion just wasn't happening.
    Excluding the automatic reload from ever counting as "already
    shown, so delete" fixes both: it's always exactly the *next real
    page load* he does himself, whatever that URL happens to be, that
    deletes.

    Never touched while the job is still running, so images already
    visible via live polling (see snapshot_job/job_id in
    job_poll()/_job_output_lines.html) are never yanked out from under
    an in-progress job."""

    if job.snapshot_dir is None or not job_status.is_finished:
        return
    if not job.snapshot_shown_while_finished:
        job.snapshot_shown_while_finished = True
    elif not is_auto_reload:
        _delete_job_snapshots(job)


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
