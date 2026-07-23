"""
Session-based login for bv-web: FastAPI dependencies that routes use
to require a logged-in user (require_login) or specifically the owner
role (require_owner - see WORKING_CONTEXT.md: only the owner,
Christer, can trigger download/generate/export once that's built;
everyone else is browse/watch only).

Sessions are an in-memory session-id -> username map, not a signed
cookie (JWT/itsdangerous) - the cookie itself only carries an opaque
random token (see SessionStore.create()), and the server looks it up.
Trade-off, deliberately accepted for now: sessions don't survive a
server restart, so everyone has to log in again after a redeploy.
That's a minor inconvenience for what is currently a single-owner-
plus-a-few-family-members deployment behind a NAS reverse proxy, and
it avoids adding a signing/JWT dependency just for this. Revisit
(e.g. persist sessions to the users file's directory) if restarts
become frequent enough to be annoying in practice.

Copyright (C) 2026 Christer R. (sssreh)

SPDX-License-Identifier: GPL-3.0-or-later
"""

from __future__ import annotations

import secrets

from fastapi import Depends
from fastapi import HTTPException
from fastapi import Request
from fastapi import status

from .users import User
from .users import UsersConfig

SESSION_COOKIE_NAME = "bv_session"


class SessionStore:
    """In-memory session-id -> username map. One instance lives on
    `app.state.session_store` for the life of the process (see
    app.py's create_app())."""

    def __init__(self) -> None:
        self._usernames_by_session: dict[str, str] = {}

    def create(self, username: str) -> str:
        """Start a new session for `username`, returning the opaque
        token to set as the session cookie's value."""

        session_id = secrets.token_urlsafe(32)
        self._usernames_by_session[session_id] = username
        return session_id

    def username_for(self, session_id: str | None) -> str | None:
        if session_id is None:
            return None
        return self._usernames_by_session.get(session_id)

    def destroy(self, session_id: str | None) -> None:
        if session_id is not None:
            self._usernames_by_session.pop(session_id, None)


def get_current_user(request: Request) -> User | None:
    """FastAPI dependency: the logged-in User, or None if there isn't
    one - for routes (like the login page itself) that behave
    differently depending on login state but don't reject outright.
    Routes that should simply refuse without a login should depend on
    require_login/require_owner below instead."""

    session_store: SessionStore = request.app.state.session_store
    users_config: UsersConfig = request.app.state.users_config

    session_id = request.cookies.get(SESSION_COOKIE_NAME)
    username = session_store.username_for(session_id)
    if username is None:
        return None

    # The session can outlive the account it points at (e.g. an
    # account removed by hand-editing the users file while its owner
    # is still logged in) - treated the same as "not logged in"
    # rather than crashing on a None User.
    return users_config.get(username)


def require_login(user: User | None = Depends(get_current_user)) -> User:
    """FastAPI dependency: 401s if not logged in. app.py's exception
    handler turns that 401 into a redirect to /login for real
    browser requests."""

    if user is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="login required"
        )
    return user


def require_owner(user: User = Depends(require_login)) -> User:
    """FastAPI dependency: 403s if logged in but not the owner role."""

    if user.role != "owner":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="owner role required"
        )
    return user
