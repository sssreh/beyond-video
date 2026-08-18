"""
Per-archive asset-generation lock manifest for bv-generate.

After finishing a full generate pass over years of archive (2019-2025
in Christer's case), he never wants bv-generate to touch those years
again by accident - not re-walk them, not re-prompt about existing
files, nothing - unless a genuinely new asset type gets added to
bv-generate later. His own framing: "I never ever want to run
bv-generate on them again. Unless we have a new asset to add. I think
would lock out those years, maybe with som kind of lock file in thoose
archives."

This module is that lock file's data model. The bv-lock CLI
(cli/bv_lock.py) is the only thing that writes it (`--lock-assets`/
`--unlock-assets`); bv-generate (cli/bv_generate.py) only reads it, at
the very start of a run, to decide whether it can skip the whole
selected range without walking a single recording. A locked range that
gets a *new* asset flag later (one not yet in the locked set) is not
blocked - only flags already covered by a lock get skipped - so
"unless we have a new asset to add" falls out of the design rather
than needing special-casing.

Deliberately simple: one manifest file per archive
(.bv-lock.json, a sibling of the recordings themselves so it travels
with the archive), each entry a single lexical time range (the same
`first`/`last` YYYYMMDD_HHMMSS strings lexicaltimeparser.TimeInterval
already produces) plus the set of asset names locked for exactly that
range. No merging of overlapping-but-different ranges, no per-language
granularity for --translate (Christer, asked directly: "locked for all
translations too, if you need a new translation (unlikely) you have to
add an option to override the lock for that command, probably 1
recording" - see bv-generate's own --ignore-lock for that override) -
just enough to answer "is this exact request already covered" cheaply.

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

from ..lexicaltimeparser import TimeInterval

_LOCK_FILENAME = ".bv-lock.json"

# Every asset bv-generate can produce, and therefore every name
# --lock-assets/--unlock-assets accepts. Kept here rather than
# imported from cli/bv_generate.py (core/ modules never depend on
# cli/) since this is the one place both bv-generate and bv-lock need
# to agree on the vocabulary. "translate" covers every target
# language, not one lock per LANG - see module docstring.
LOCKABLE_ASSETS = frozenset({
    "extract-audio",
    "get-duration",
    "thumbnail",
    "transcribe",
    "translate",
    "srt",
    "describe-scene",
    "diarize",
})


class LockError(Exception):
    """Raised for a malformed lock manifest file or an unknown asset name."""


@dataclass(frozen=True)
class LockEntry:
    """One locked lexical time range and the asset names locked for
    it. `first`/`last` are TimeInterval's own zero/nine-padded
    YYYYMMDD_HHMMSS strings, so a lock entry can be compared against a
    parsed run interval with plain string comparison - no date parsing
    needed anywhere in this module."""

    first: str
    last: str
    assets: frozenset[str]
    locked_at: str


@dataclass
class LockManifest:
    """A camera archive's full set of lock entries."""

    entries: list[LockEntry] = field(default_factory=list)


def lock_manifest_path(archive_path: Path) -> Path:
    """Where archive_path's lock manifest lives - next to the
    recordings themselves, so it travels with the archive if it's ever
    moved or copied (the same reasoning <recording>.duration.txt etc.
    live next to their source recording, just archive-scoped instead
    of per-recording)."""

    return archive_path / _LOCK_FILENAME


def load_lock_manifest(archive_path: Path) -> LockManifest:
    """Load archive_path's lock manifest, or an empty one if it
    doesn't exist yet - a fresh/never-locked archive is not an error,
    same convention as core/history.py's read_entries()."""

    path = lock_manifest_path(archive_path)
    if not path.exists():
        return LockManifest()

    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        raise LockError(f"could not read {path}: {exc}") from exc

    try:
        entries = [
            LockEntry(
                first=raw["first"],
                last=raw["last"],
                assets=frozenset(raw["assets"]),
                locked_at=raw["locked_at"],
            )
            for raw in data.get("entries", [])
        ]
    except (KeyError, TypeError) as exc:
        raise LockError(f"malformed lock manifest {path}: {exc}") from exc

    return LockManifest(entries=entries)


def save_lock_manifest(archive_path: Path, manifest: LockManifest) -> None:
    """Write archive_path's lock manifest, creating the archive
    directory first if it doesn't exist yet (mirrors load's own
    tolerance of a not-yet-existing archive - bv-lock can run before
    bv-generate ever has, e.g. to pre-lock a range you know is empty)."""

    path = lock_manifest_path(archive_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    data = {
        "entries": [
            {
                "first": entry.first,
                "last": entry.last,
                "assets": sorted(entry.assets),
                "locked_at": entry.locked_at,
            }
            for entry in manifest.entries
        ]
    }
    path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")


def _validate_assets(assets: Iterable[str]) -> frozenset[str]:
    validated = frozenset(assets)
    unknown = validated - LOCKABLE_ASSETS
    if unknown:
        raise LockError(
            f"unknown asset name(s): {', '.join(sorted(unknown))} - "
            f"valid names: {', '.join(sorted(LOCKABLE_ASSETS))}"
        )
    return validated


def add_lock_assets(
    manifest: LockManifest,
    interval: TimeInterval,
    assets: Iterable[str],
    *,
    locked_at: str | None = None,
) -> LockManifest:
    """Return a new manifest with `assets` locked for exactly
    `interval`'s range. If an entry already exists for that exact
    (first, last) range, its asset set is extended (union) and its
    locked_at timestamp refreshed; otherwise a new entry is appended.

    Deliberately exact-range matching rather than merging overlapping-
    but-different ranges into one - bv-lock is meant to be run with
    the same selection you already used to generate (a whole year, a
    whole archive, ...), not built up piecemeal from arbitrary
    fragments. Raises LockError for any name not in LOCKABLE_ASSETS."""

    validated = _validate_assets(assets)
    locked_at = locked_at or datetime.now(timezone.utc).isoformat()

    entries = list(manifest.entries)
    for i, entry in enumerate(entries):
        if entry.first == interval.first and entry.last == interval.last:
            entries[i] = LockEntry(
                first=entry.first,
                last=entry.last,
                assets=entry.assets | validated,
                locked_at=locked_at,
            )
            return LockManifest(entries=entries)

    entries.append(
        LockEntry(
            first=interval.first,
            last=interval.last,
            assets=validated,
            locked_at=locked_at,
        )
    )
    return LockManifest(entries=entries)


def remove_lock_assets(
    manifest: LockManifest, interval: TimeInterval, assets: Iterable[str]
) -> LockManifest:
    """Return a new manifest with `assets` unlocked for exactly
    `interval`'s range - the same exact-range matching add_lock_assets()
    uses. An entry left with no assets after removal is dropped
    entirely rather than kept as an empty placeholder. A range with no
    matching entry at all is left untouched (not an error) - unlocking
    something that was never locked is a no-op, not a mistake worth
    stopping the command over."""

    validated = _validate_assets(assets)

    entries = []
    for entry in manifest.entries:
        if entry.first == interval.first and entry.last == interval.last:
            remaining = entry.assets - validated
            if remaining:
                entries.append(
                    LockEntry(
                        first=entry.first,
                        last=entry.last,
                        assets=remaining,
                        locked_at=entry.locked_at,
                    )
                )
        else:
            entries.append(entry)

    return LockManifest(entries=entries)


def assets_fully_locked(
    manifest: LockManifest, interval: TimeInterval, assets: Iterable[str]
) -> LockEntry | None:
    """Return the single LockEntry whose own range fully contains
    `interval` (its `first` is <= interval.first and its `last` is >=
    interval.last) and which already has every name in `assets`
    locked, or None if no single entry covers the request. This is
    the whole check bv-generate needs to decide "can this run be
    skipped entirely, before walking a single recording".

    Containment is against one entry at a time, not a union of several
    overlapping entries - matches how bv-lock is meant to be used
    (lock a year, then later query that same year or a sub-range
    within it) and keeps this a plain string comparison, no
    interval-merging math. An empty `assets` (nothing was actually
    requested) always returns None rather than trivially "locked" -
    bv-generate itself never calls this with no action flags, argparse
    already refuses that combination, but a caller shouldn't get a
    surprising True out of an empty request either way."""

    requested = frozenset(assets)
    if not requested:
        return None

    for entry in manifest.entries:
        if (
            entry.first <= interval.first
            and interval.last <= entry.last
            and requested <= entry.assets
        ):
            return entry

    return None
