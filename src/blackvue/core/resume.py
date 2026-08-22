"""
Per-archive "where did the last --resume run get to" cursor for
bv-generate.

Christer, running bv-generate daily against an archive that only grows:
"i would like a --lazy (not the right name) for bv-generate that
autostart at first none generated asset, i am planning to run it daily
and dont want to scan through all previous assets."

This module is that cursor's data model - deliberately the same shape
as core/lock.py's own manifest (one JSON sidecar per archive, entries
keyed by an asset-name set), because it answers a structurally similar
question: "for exactly this combination of action flags, how far
through this archive have I already gotten?" Where bv-lock's manifest
is a range Christer builds up on purpose (bv-lock is its own command,
run deliberately), this one is written automatically by bv-generate
itself at the end of every --resume run, with no separate command.

Deliberately a single high-water mark per asset-name combination, not
an attempt to track "is recording X actually fully generated" per
recording: some recordings (silent audio, no audio stream at all, a
photo/Parking-mode recording skipped for an audio action) will *never*
produce every requested asset no matter how many times bv-generate
runs against them, so a cursor built from "does the output file exist"
would get stuck at the first such recording forever - re-scanning
everything after it, on every future run, defeating the entire point.
Advancing to the newest recording *attempted* this run, regardless of
whether every individual action produced a file for it (or hit an
error - see bv_generate.py's own --resume wiring for that tradeoff),
sidesteps that: the next run always resumes exactly where this one
stopped looking, cheaply re-checking (not re-generating - existing
files are still skipped the normal way) at most one recording twice.

Cursor advancement is intentionally per exact asset-name set, not
"covers a superset" the way bv-lock's assets_fully_locked() is: running
bv-generate daily with the exact same flags (the expected use) always
matches its own prior cursor; changing the flag set (e.g. adding
--describe-scene to a routine that used to be --extract-audio
--transcribe only) starts that new combination's own cursor from
scratch rather than risking silently skipping a range never actually
generated for the new action - a conservative default, not a bug.

Copyright (C) 2026 Christer R. (sssreh)

SPDX-License-Identifier: GPL-3.0-or-later
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from dataclasses import dataclass
from dataclasses import field
from datetime import datetime
from datetime import timezone
from pathlib import Path

_RESUME_FILENAME = ".bv-generate-resume.json"


class ResumeError(Exception):
    """Raised for a malformed resume-cursor file."""


@dataclass(frozen=True)
class ResumeEntry:
    """One asset-name combination's cursor. `last_seen` is a
    zero-padded "YYYYMMDD_HHMMSS" string - the same shape
    lexicaltimeparser.TimeInterval's own first/last use - so it can be
    dropped straight into a TimeInterval(first=...) with a plain
    string comparison against the requested range, no date parsing
    needed here or at the call site."""

    assets: frozenset[str]
    last_seen: str
    updated_at: str


@dataclass
class ResumeState:
    """An archive's full set of resume cursors, one per distinct
    asset-name combination that's ever been run with --resume."""

    entries: list[ResumeEntry] = field(default_factory=list)


def resume_state_path(archive_path: Path) -> Path:
    """Where archive_path's resume cursor lives - next to the
    recordings themselves, so it travels with the archive the same way
    .bv-lock.json does (see core/lock.py)."""

    return archive_path / _RESUME_FILENAME


def load_resume_state(archive_path: Path) -> ResumeState:
    """Load archive_path's resume cursor, or an empty one if it
    doesn't exist yet - a fresh/never-resumed archive is not an error,
    same convention as core/lock.py's load_lock_manifest()."""

    path = resume_state_path(archive_path)
    if not path.exists():
        return ResumeState()

    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        raise ResumeError(f"could not read {path}: {exc}") from exc

    try:
        entries = [
            ResumeEntry(
                assets=frozenset(raw["assets"]),
                last_seen=raw["last_seen"],
                updated_at=raw["updated_at"],
            )
            for raw in data.get("entries", [])
        ]
    except (KeyError, TypeError) as exc:
        raise ResumeError(f"malformed resume cursor {path}: {exc}") from exc

    return ResumeState(entries=entries)


def save_resume_state(archive_path: Path, state: ResumeState) -> None:
    """Write archive_path's resume cursor, creating the archive
    directory first if it doesn't exist yet (mirrors load's own
    tolerance of a not-yet-existing archive)."""

    path = resume_state_path(archive_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    data = {
        "entries": [
            {
                "assets": sorted(entry.assets),
                "last_seen": entry.last_seen,
                "updated_at": entry.updated_at,
            }
            for entry in state.entries
        ]
    }
    path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")


def resume_point(state: ResumeState, assets: Iterable[str]) -> str | None:
    """Return the cursor's `last_seen` for the entry whose asset set
    exactly matches `assets`, or None if --resume has never been run
    for this exact combination before (a fresh cursor, not an error -
    the caller should just fall back to scanning the whole requested
    range, same as running without --resume at all)."""

    requested = frozenset(assets)
    for entry in state.entries:
        if entry.assets == requested:
            return entry.last_seen
    return None


def advance_resume_point(
    state: ResumeState,
    assets: Iterable[str],
    last_seen: str,
    *,
    updated_at: str | None = None,
) -> ResumeState:
    """Return a new state with the cursor for exactly `assets` set to
    `last_seen` (replacing any existing entry for that exact
    combination, same "exact match, not merge" precedent as
    core/lock.py's add_lock_assets())."""

    requested = frozenset(assets)
    updated_at = updated_at or datetime.now(timezone.utc).isoformat()

    entries = list(state.entries)
    for i, entry in enumerate(entries):
        if entry.assets == requested:
            entries[i] = ResumeEntry(
                assets=requested, last_seen=last_seen, updated_at=updated_at
            )
            return ResumeState(entries=entries)

    entries.append(
        ResumeEntry(assets=requested, last_seen=last_seen, updated_at=updated_at)
    )
    return ResumeState(entries=entries)
