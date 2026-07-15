"""
BlackVue VOD parser.

Reads and parses the output from:

    http://<camera>/blackvue_vod.cgi

The parser will eventually support multiple firmware versions.

Copyright (C) 2026 Christer R. (sssreh)

SPDX-License-Identifier: GPL-3.0-or-later
"""

from dataclasses import dataclass


@dataclass(slots=True)
class Recording:
    """Represents one recording group."""

    basename: str

    event: bool

    manual: bool


def parse(text: str) -> list[Recording]:
    """
    Parse the output from blackvue_vod.cgi.

    Currently only returns an empty list.
    """

    return []
