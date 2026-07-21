"""
Connection.

Copyright (C) 2026 Christer R. (sssreh)

SPDX-License-Identifier: GPL-3.0-or-later
"""

from __future__ import annotations

from .blackvue_client import BlackVueClient
from .endpoint import Endpoint


class CameraUnreachableError(RuntimeError):
    """Raised when no configured endpoint could be reached."""


def connect(
    endpoints: list[Endpoint],
    timeout: int = 5,
) -> tuple[Endpoint, BlackVueClient]:
    """Try each endpoint in order, return the first that answers.

    Endpoints are tried in the order given - put the preferred
    connection first (e.g. home WiFi, which is cheaper and faster
    than a cellular fallback). The camera does not have to be
    reachable via every endpoint at once; it is normal for most
    endpoints to fail most of the time (e.g. the car is away from
    home, or not on the cellular router).
    """

    errors: list[str] = []

    for endpoint in endpoints:
        client = BlackVueClient(
            f"http://{endpoint.address}",
            timeout=timeout,
        )

        try:
            client.vod()
        except (OSError, RuntimeError) as exc:
            errors.append(f"{endpoint.name} ({endpoint.address}): {exc}")
            continue

        return endpoint, client

    raise CameraUnreachableError(
        "no configured endpoint could be reached: " + "; ".join(errors)
    )
