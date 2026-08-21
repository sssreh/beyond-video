"""
Save captured camera snapshots to disk - the library module shared by
cli/bv_snap.py (standalone) and cli/bv_gps.py's own --snap mode, plus
bv-web's job wiring for both.

Christer: "I would like to have a snap function that takes 1 snapshot
for camera F, R and I." The actual HTTP capture lives on
BlackVueClient.snapshot() (core/blackvue_client.py) since it's a pure
camera-protocol concern; this module is the pure-filesystem half -
turning whatever bytes came back into files a user can open, with one
shared naming convention no matter which CLI entry point triggered
the capture.

Copyright (C) 2026 Christer R. (sssreh)

SPDX-License-Identifier: GPL-3.0-or-later
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path


def save_snapshots(
    snapshots: dict[str, bytes],
    output_dir: Path,
    *,
    timestamp: str | None = None,
) -> dict[str, Path]:
    """Write each captured direction's JPEG bytes to
    `output_dir/snap_<timestamp>_<direction>.jpg`, creating
    `output_dir` if it doesn't exist yet.

    One shared timestamp across every file from the same snap event
    (so `snap_20260821_180512_F.jpg` / `..._R.jpg` / `..._I.jpg`
    visibly belong together), generated fresh via `datetime.now()`
    unless a caller supplies one - tests pass a fixed string so
    assertions aren't racing the real clock.

    Returns the path each direction was written to, in `snapshots`'
    own key order - callers that need to report "F: saved <path>"
    per direction (bv-snap, bv-gps --snap) can iterate this directly
    rather than re-deriving filenames themselves.
    """

    if timestamp is None:
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")

    output_dir.mkdir(parents=True, exist_ok=True)

    paths: dict[str, Path] = {}

    for direction, data in snapshots.items():
        path = output_dir / f"snap_{timestamp}_{direction}.jpg"
        path.write_bytes(data)
        paths[direction] = path

    return paths
