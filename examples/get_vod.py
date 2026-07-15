#!/usr/bin/env python3
"""
Download and print the contents of blackvue_vod.cgi.

Usage:
    python examples/get_vod.py <ip-address>

Example:
    python examples/get_vod.py 192.168.8.1

Copyright (C) 2026 Christer R. (sssreh)

SPDX-License-Identifier: GPL-3.0-or-later
"""

from __future__ import annotations

import sys
from pathlib import Path

# Allow imports from the project root when running from examples/
PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from src.core.camera import get_vod


def main() -> int:
    if len(sys.argv) != 2:
        print(f"Usage: {sys.argv[0]} <ip-address>")
        return 1

    ip = sys.argv[1]

    try:
        print(get_vod(ip))
    except Exception as exc:
        print(f"Error: {exc}")
        return 2

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
