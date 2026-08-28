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

import os
import re
import subprocess
import sys
from datetime import datetime
from pathlib import Path

# Characters allowed straight through into a filename. Everything else
# (colons from a bare --host's optional :PORT, slashes from a
# malformed one, etc.) gets collapsed to "_" by _sanitize_label() -
# camera ids are already documented as "an ASCII string suitable for
# filenames" (see docs/CLI.md), but --host is arbitrary user input and
# has to be made filesystem-safe the same way regardless, since
# save_snapshots() doesn't know or care which one it was given.
_LABEL_SAFE_RE = re.compile(r"[^A-Za-z0-9._-]+")


def _sanitize_label(label: str) -> str:
    """Make an arbitrary id/host string safe to embed in a filename."""

    return _LABEL_SAFE_RE.sub("_", label).strip("_")


def save_snapshots(
    snapshots: dict[str, bytes],
    output_dir: Path,
    *,
    timestamp: str | None = None,
    label: str | None = None,
) -> dict[str, Path]:
    """Write each captured direction's JPEG bytes to
    `output_dir/snap_<label>_<timestamp>_<direction>.jpg` (or
    `output_dir/snap_<timestamp>_<direction>.jpg` when no `label` is
    given), creating `output_dir` if it doesn't exist yet.

    `label` is the camera id or --host string the snapshot was taken
    from - Christer: "I would also like the id or host be in the
    output name of the files," so a shared --output directory used
    across more than one camera still produces files that say which
    camera they came from at a glance, not just when they were taken.
    Sanitized via _sanitize_label() before use, since --host in
    particular is arbitrary user input (e.g. "192.168.1.42:80" - the
    ":" alone isn't filename-safe on Windows).

    One shared timestamp across every file from the same snap event
    (so `snap_Kirby_20260821_180512_F.jpg` / `..._R.jpg` / `..._I.jpg`
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

    prefix = "snap"
    if label:
        prefix = f"{prefix}_{_sanitize_label(label)}"

    paths: dict[str, Path] = {}

    for direction, data in snapshots.items():
        path = output_dir / f"{prefix}_{timestamp}_{direction}.jpg"
        path.write_bytes(data)
        paths[direction] = path

    return paths


def open_with_default_app(path: Path) -> bool:
    """Open `path` in the OS's default application for its file type
    (a JPEG viewer, for a snapshot).

    Standalone `bv-snap` just writes files and prints "saved <path>" -
    fine for scripting, but next to useless for "let me see what the
    camera sees right now" (Christer: "so its almost useless" once he
    realized nothing pops the image up for him - bv-web's own snapshot
    UI already shows the picture inline in the browser, but a plain
    terminal invocation of bv-snap has no equivalent surface). This is
    what `bv-snap --open` (see cli/bv_snap.py) calls per saved file to
    close that gap for terminal use.

    Cross-platform: `os.startfile()` on Windows (a direct OS call, not
    a subprocess - the one platform that exposes this), `open` on
    macOS, `xdg-open` on Linux/BSD - the same three-way split
    bv_live.py's own browser-launch code documents doing for URLs,
    applied here to a single file instead of a URL.

    Returns whether the OS handed the file off successfully. Never
    raises - a missing `xdg-open` on a headless server, or no default
    app registered for `.jpg`, isn't a bv-snap failure in itself (the
    file is still saved and still usable); it just means bv-snap
    couldn't also pop a viewer open for it.
    """

    try:
        if sys.platform == "win32":
            os.startfile(str(path))  # type: ignore[attr-defined]
        elif sys.platform == "darwin":
            subprocess.run(["open", str(path)], check=True)
        else:
            subprocess.run(["xdg-open", str(path)], check=True)
        return True
    except (OSError, subprocess.CalledProcessError):
        return False
