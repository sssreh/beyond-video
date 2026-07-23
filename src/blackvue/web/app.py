"""
FastAPI app factory for bv-web.

Server-rendered (Jinja2), not a JSON API + separate frontend build -
there's no client-side app to build/deploy, and the pages here
(a trip list, a trip detail/player page, a login form) don't need
anything richer. bv-web's CLI (cli/bv_web.py) is the only thing that
imports this module, and only inside `bv-web serve` - see
web/__init__.py's docstring for why that matters.

This first increment is browse/watch only: login (owner/viewer
roles), the trip list, and a trip detail page with video playback
(range-request support comes for free from Starlette's own
FileResponse) plus GPX/SRT/LRC download links. Triggering
download/generate/export from the browser is intentionally not part
of this increment - see WORKING_CONTEXT.md.

Copyright (C) 2026 Christer R. (sssreh)

SPDX-License-Identifier: GPL-3.0-or-later
"""

from __future__ import annotations

from pathlib import Path

from fastapi import Depends
from fastapi import FastAPI
from fastapi import Form
from fastapi import HTTPException
from fastapi import Request
from fastapi import status
from fastapi.exception_handlers import http_exception_handler
from fastapi.responses import FileResponse
from fastapi.responses import HTMLResponse
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates

from .auth import SESSION_COOKIE_NAME
from .auth import SessionStore
from .auth import require_login
from .trips import TripAssets
from .trips import scan_trip
from .trips import scan_trips
from .users import User
from .users import UsersConfig

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
                "forbidden.html",
                {"request": request, "detail": exc.detail},
                status_code=status.HTTP_403_FORBIDDEN,
            )
        # Anything else (404s in particular) keeps FastAPI's normal
        # JSON error body - only 401/403 need browser-friendly
        # handling here.
        return await http_exception_handler(request, exc)

    @app.get("/login", response_class=HTMLResponse)
    async def login_form(request: Request, next: str = "/"):
        return templates.TemplateResponse(
            "login.html", {"request": request, "next": next, "error": None}
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
                "login.html",
                {
                    "request": request,
                    "next": next,
                    "error": "Wrong username or password.",
                },
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
            "trip_list.html", {"request": request, "user": user, "trips": trips}
        )

    @app.get("/trips/{trip_id}", response_class=HTMLResponse)
    async def trip_detail(
        request: Request, trip_id: str, user: User = Depends(require_login)
    ):
        trip = _find_trip(target, trip_id)
        return templates.TemplateResponse(
            "trip_detail.html", {"request": request, "user": user, "trip": trip}
        )

    @app.get("/trips/{trip_id}/files/{filename}")
    async def trip_file(
        trip_id: str, filename: str, user: User = Depends(require_login)
    ):
        trip = _find_trip(target, trip_id)
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

    return app


def _find_trip(target: Path, trip_id: str) -> TripAssets:
    """Resolve a trip id (its folder name) to a TripAssets inside
    `target`, 404ing if it doesn't exist or isn't actually a trip
    folder. `trip_id` comes straight from the URL path and is
    therefore untrusted - reject anything that could walk outside
    `target` (a component like ".." or a path separator) before ever
    touching the filesystem with it."""

    if (
        "/" in trip_id
        or "\\" in trip_id
        or trip_id in (".", "..")
    ):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="trip not found"
        )

    trip = scan_trip(target / trip_id)
    if trip is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="trip not found"
        )
    return trip
