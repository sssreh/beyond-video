"""
Query the persistent command-history index (core/history.py) - the
library module behind bv-history (cli/bv_history.py) and bv-web's own
/history page.

Entries are numbered by absolute position in the full, unfiltered,
oldest-first history - the same convention bash/pwsh's own `history`
uses (`history | grep foo` still shows each match's real original
number, not a 1..N renumbering of the filtered subset). That numbering
is what `bv-history show <id>` and bv-web's own output-lookup both key
off of.

Copyright (C) 2026 Christer R. (sssreh)

SPDX-License-Identifier: GPL-3.0-or-later
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from datetime import timedelta

from .core import history
from .core import joblog
from .core.history import HistoryEntry
from .lexicaltimeparser import LexicalTimeParser

# Default tail-style limit - "I am more like a tail guy" (Christer,
# see WORKING_CONTEXT.md's bv-history design notes). Chosen to be
# generous enough to be useful without pagination, small enough not to
# dump hundreds of lines into a terminal by default.
DEFAULT_TAIL_COUNT = 15

# Small buffer added on both sides of a HistoryEntry's own
# [started_at, started_at + duration_seconds] window when correlating
# it against core/joblog.py's timestamped lines (see
# matching_log_lines() below) - the two timestamps aren't written by
# the exact same clock call, so a line landing a fraction of a second
# outside the nominal window is still almost certainly this run's own
# output, not a neighboring one's.
_LOG_MATCH_BUFFER = timedelta(seconds=2)


@dataclass(frozen=True)
class NumberedEntry:
    """One HistoryEntry plus its stable display number (its absolute
    1-based position in the full oldest-first history - see this
    module's own docstring)."""

    number: int
    entry: HistoryEntry


@dataclass(frozen=True)
class HistoryFilter:
    """Narrows a history listing. Every field is optional - an unset
    field imposes no restriction. `search`/`camera` are both plain
    case-insensitive substring matches against the full
    `command_line` (there's no separate structured "camera" field in
    HistoryEntry - a recording archive path or camera id typically
    just appears somewhere in the command line itself, e.g. `bv-ls
    Kirby --all`, so a substring match is good enough without adding
    a new field that every write-side call site would need to fill
    in). `since`/`until` reuse the same lexical YYYYMMDD[_HHMMSS]
    syntax every other bv-* command's own --from/--until already
    uses, compared against each entry's own `started_at` after
    converting it to that same shape.
    """

    command: str | None = None
    camera: str | None = None
    since: str | None = None
    until: str | None = None
    timestamp: str | None = None
    failed_only: bool = False
    search: str | None = None
    source: str | None = None


def _lexical_timestamp(started_at: str) -> str:
    """Convert a HistoryEntry.started_at ISO-8601 string into the
    YYYYMMDD_HHMMSS shape LexicalTimeParser's own interval boundaries
    use, so --since/--until can be compared directly without going
    through TimeInterval.__contains__ (which strips a trailing
    "_suffix" for recording-id tags like _E/_P - wrong here, since
    our own single "_" already separates date from time, not a tag)."""

    return datetime.fromisoformat(started_at).strftime("%Y%m%d_%H%M%S")


def all_entries() -> list[NumberedEntry]:
    """Every recorded entry, oldest first, numbered by absolute
    position - the full history before any filtering."""

    return [
        NumberedEntry(number=index, entry=entry)
        for index, entry in enumerate(history.read_entries(), start=1)
    ]


def filtered_entries(
    filt: HistoryFilter, *, entries: list[NumberedEntry] | None = None
) -> list[NumberedEntry]:
    """Apply `filt` to `entries` (default: all_entries()), preserving
    each match's real original number. Raises ValueError if
    since/until/timestamp are lexically invalid (same contract every
    other bv-* command's own LexicalTimeParser().parse() call has -
    callers already know how to turn that into a clean CLI/web error
    message)."""

    if entries is None:
        entries = all_entries()

    interval = None
    if filt.since or filt.until or filt.timestamp:
        interval = LexicalTimeParser(
            timestamp=filt.timestamp, from_=filt.since, until=filt.until
        ).parse()

    command_lower = filt.command.lower() if filt.command else None
    camera_lower = filt.camera.lower() if filt.camera else None
    search_lower = filt.search.lower() if filt.search else None

    results = []
    for numbered in entries:
        entry = numbered.entry

        if command_lower is not None and entry.command.lower() != command_lower:
            continue
        if filt.source is not None and entry.source != filt.source:
            continue
        if filt.failed_only and entry.status not in ("failed", "interrupted"):
            continue
        if (
            camera_lower is not None
            and camera_lower not in entry.command_line.lower()
        ):
            continue
        if (
            search_lower is not None
            and search_lower not in entry.command_line.lower()
        ):
            continue
        if interval is not None:
            ts = _lexical_timestamp(entry.started_at)
            if not (interval.first <= ts <= interval.last):
                continue

        results.append(numbered)

    return results


def tail(
    entries: list[NumberedEntry], count: int | None = DEFAULT_TAIL_COUNT
) -> list[NumberedEntry]:
    """Return the last `count` entries, still oldest-first within that
    slice (matches `history 20`/`tail`'s own ordering, not most-recent-
    first). `count=None` returns everything, unlimited."""

    if count is None or count >= len(entries):
        return entries
    return entries[-count:]


def matching_log_lines(entry: HistoryEntry) -> list[joblog.LogLine]:
    """Best-effort reconstruction of one past run's full logged
    output, for `bv-history show <id>` - every core/joblog.py line
    tagged with this entry's own prog name (`entry.command`) whose
    timestamp falls within the run's own [started, started+duration]
    window (plus a small buffer - see _LOG_MATCH_BUFFER).

    Two real limitations, both acceptable trade-offs rather than bugs:
    - core/joblog.py's lines are naive local time; HistoryEntry's own
      `started_at` is UTC-aware (see run_cli()/JobRunner's own
      history.record() calls) - converted here before comparing.
    - if the *same* command ran more than once concurrently (two
      bv-scribe jobs at once, say), their output lines share the same
      source tag and could both fall in an overlapping window - this
      can't distinguish them from each other. Rare in practice (bv-web
      jobs of the same type aren't commonly run in parallel), not
      worth a bigger correlation key (e.g. thread id) for.
    - already-pruned/rotated-away months (see joblog.py's own
      DEFAULT_BACKUP_MONTHS) mean very old entries may have no output
      left to show at all - an empty result, not an error.
    """

    started_utc = datetime.fromisoformat(entry.started_at)
    started_local = started_utc.astimezone().replace(tzinfo=None)
    ended_local = started_local + timedelta(seconds=entry.duration_seconds)

    return joblog.read_lines(
        since=started_local - _LOG_MATCH_BUFFER,
        until=ended_local + _LOG_MATCH_BUFFER,
        source=entry.command,
    )
