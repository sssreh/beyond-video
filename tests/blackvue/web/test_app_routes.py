"""
Regression tests for the "/trips/{trip_id:path}..." route-registration-
order bug (2026-08-27, see WORKING_CONTEXT.md and the comment above
trip_location() in web/app.py).

Christer moved his real trips onto the NAS deployment and found every
click into a trip's video files, GPS location page, or KML download
404ing with {"detail":"trip not found"} - while the plain trip detail
page itself always loaded fine. Root cause: trip_detail()'s route
pattern is a bare "/trips/{trip_id:path}", which compiles to the
regex ``^/trips/(?P<trip_id>.*)$`` - no trailing literal, so it
matches ANY "/trips/..." URL. Starlette resolves routes by trying them
in registration order and using the first full match. trip_detail()
used to be registered *first* among the four "/trips/{trip_id:path}..."
routes, so it silently swallowed every request meant for
trip_location(), trip_kml(), and trip_file() before those more
specific routes ever got a chance - trip_id inside trip_detail() then
held the whole URL tail (e.g. "kirby_2019/trip_xyz/files/front.mp4"),
which _find_trip()'s own segment-count guard correctly rejected as
malformed, producing the exact 404 Christer saw.

The fix is registration order, not route-pattern content, so the only
way to actually prove it works is to exercise Starlette's real
route-matching against the app's real route table - inspecting
`app.routes` order alone would not catch a case where the order looks
right but something else (e.g. a second stray registration) still
shadows a route. This mirrors exactly what Router.app() does
internally: walk `app.routes` in order, call `route.matches(scope)`,
and take the first FULL match.

Deliberately its own file, not folded into test_app_reuse.py: same
reasoning as that file's own docstring - web/app.py imports fastapi,
so this module can only be collected in an environment with the `web`
extra installed (CI has it; this repo's day-to-day dev sandbox often
doesn't).

Copyright (C) 2026 Christer R. (sssreh)

SPDX-License-Identifier: GPL-3.0-or-later
"""

from __future__ import annotations

from pathlib import Path

import pytest
from starlette.routing import Match

from blackvue.web.app import create_app
from blackvue.web.users import UsersConfig


def _build_app(tmp_path: Path):
    users_config = UsersConfig(path=tmp_path / "web-users.cfg")
    return create_app(target=tmp_path, users_config=users_config)


def _resolve(app, path: str) -> str:
    """Mimic Starlette's own Router.app() dispatch: walk app.routes in
    registration order, return the endpoint function name of the
    first FULL match. Raises if nothing matches."""

    scope = {"type": "http", "method": "GET", "path": path}
    for route in app.routes:
        match, _child_scope = route.matches(scope)
        if match == Match.FULL:
            return route.endpoint.__name__
    raise AssertionError(f"no route matched {path!r}")


# ---------------------------------------------------------------------------
# The four routes, in the order they must be registered (see the comment
# above trip_location() in web/app.py): the three specific "sub-paths"
# first, the bare catch-all trip_detail() last.
# ---------------------------------------------------------------------------


def test_trip_detail_is_registered_last_among_the_trip_id_path_routes(tmp_path):
    app = _build_app(tmp_path)

    trip_route_names = [
        route.endpoint.__name__
        for route in app.routes
        if getattr(route, "path", "").startswith("/trips/{trip_id:path}")
    ]

    assert trip_route_names == [
        "trip_location",
        "trip_kml",
        "trip_file",
        "trip_detail",
    ], trip_route_names


@pytest.mark.parametrize(
    "path, expected_endpoint",
    [
        # Flat (single-segment) trip id, no camera prefix.
        ("/trips/cirkel2_trip_20260802_103513_20260802_103846", "trip_detail"),
        (
            "/trips/cirkel2_trip_20260802_103513_20260802_103846/location",
            "trip_location",
        ),
        ("/trips/cirkel2_trip_20260802_103513_20260802_103846/kml", "trip_kml"),
        (
            "/trips/cirkel2_trip_20260802_103513_20260802_103846/files/stitch.mp4",
            "trip_file",
        ),
        # Camera-prefixed (two-segment) trip id - exactly Christer's real,
        # previously-broken case from his NAS logs.
        (
            "/trips/kirby_2019/cirkel2_trip_20260802_103513_20260802_103846",
            "trip_detail",
        ),
        (
            "/trips/kirby_2019/cirkel2_trip_20260802_103513_20260802_103846/location",
            "trip_location",
        ),
        (
            "/trips/kirby_2019/cirkel2_trip_20260802_103513_20260802_103846/kml",
            "trip_kml",
        ),
        (
            "/trips/kirby_2019/cirkel2_trip_20260802_103513_20260802_103846"
            "/files/map_zoom_120m_tu.mp4",
            "trip_file",
        ),
        # A non-ASCII trip name (Christer's real "Malmö" trips) shouldn't
        # change which route wins.
        (
            "/trips/Malm%C3%B6_trip_20230728_115941_20230728_120337/files/front.mp4",
            "trip_file",
        ),
    ],
)
def test_trip_routes_resolve_to_the_expected_endpoint(tmp_path, path, expected_endpoint):
    app = _build_app(tmp_path)

    assert _resolve(app, path) == expected_endpoint
