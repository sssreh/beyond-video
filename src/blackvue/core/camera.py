"""
BlackVue camera communication.

Copyright (C) 2026 Christer R. (sssreh)

SPDX-License-Identifier: GPL-3.0-or-later
"""

from urllib.request import urlopen


def get_vod(host: str, port: int = 80) -> str:
    """Download blackvue_vod.cgi from the camera."""

    url = f"http://{host}:{port}/blackvue_vod.cgi"

    with urlopen(url, timeout=10) as response:
        return response.read().decode("utf-8", errors="replace")
        return response.read().decode("utf-8", errors="replace")
