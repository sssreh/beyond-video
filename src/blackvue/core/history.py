"""
Persistent command-history index for bv-* commands.

Companion to core/joblog.py's raw output transcript (see that module's
own docstring) - this is a much smaller, structured, one-line-per-
invocation index (timestamp, full command line, duration, status)
meant to answer "what did I run, and with what options" without
scanning the raw output log at all. The not-yet-built bv-history
command (see WORKING_CONTEXT.md's "Note: bv-history command" entry)
will read this file; this module only writes it.

Covers both invocation paths, per the same "Scope - settled: direct
CLI calls too, not just bv-web jobs" decision core/joblog.py's own
docstring already references:
- Direct CLI: cli/errors.py's run_cli() (the shared wrapper every
  bv-* command's main() already calls) records one entry per
  invocation in a `finally` block, so it fires exactly once whether
  the command succeeded, failed, or was interrupted.
- bv-web: web/jobs.py's JobRunner._spawn() records one entry per job
  once its background thread reaches a terminal status
  (succeeded/failed/cancelled).

Copyright (C) 2026 Christer R. (sssreh)

SPDX-License-Identifier: GPL-3.0-or-later
"""

from __future__ import annotations

import json
import threading
from dataclasses import asdict
from dataclasses import dataclass
from pathlib import Path

from .camera_config import default_logs_dir

_HISTORY_FILENAME = "history.jsonl"

# Guards the append-a-line operation below - the same
# multiple-threads-writing-concurrently concern core/joblog.py's own
# _logger_lock exists for (bv-web's job runner spawns one background
# thread per job).
_lock = threading.Lock()


@dataclass(frozen=True)
class HistoryEntry:
    """One past bv-* invocation.

    `command` is the bare prog name (e.g. "bv-ls"), `command_line` is
    the full invocation as a paste-able string (options included) -
    the same shape web/jobs.py's own Job.replicate_command already
    uses for bv-web jobs, and reconstructed from sys.argv for direct
    CLI runs. `source` is "cli" or "bv-web"; `username` is set only
    for bv-web jobs (a direct terminal invocation has no logged-in
    user to attribute it to). `started_at` is an ISO-8601 UTC
    timestamp; `status` is "succeeded"/"failed"/"cancelled"/
    "interrupted".
    """

    command: str
    command_line: str
    source: str
    username: str | None
    started_at: str
    duration_seconds: float
    status: str


def history_path() -> Path:
    """Return the persistent history file's path - a sibling of
    core/joblog.py's own rotating output logfiles, in the same
    default_logs_dir()."""

    return default_logs_dir() / _HISTORY_FILENAME


def record(entry: HistoryEntry) -> None:
    """Append one entry as a single JSON line (JSON Lines - one
    complete, independently parseable record per line, append-only,
    crash-safe: a process killed mid-write can corrupt at most the
    last, still-in-flight line, never anything already written before
    it). Never raises - a history-write failure (disk full,
    permissions) should never break the command it's recording, the
    same contract core/joblog.py's own log_line() already keeps.
    """

    try:
        path = history_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps(asdict(entry), separators=(",", ":"))
        with _lock:
            with open(path, "a", encoding="utf-8") as fh:
                fh.write(line + "\n")
    except Exception:
        pass


def quote_for_display(value: str) -> str:
    """Minimal, good-enough-for-both-shells quoting for a single argv
    value inside a rebuilt command line - the same approach
    web/jobs.py's own _quote_for_replicate() uses for bv-web's
    Job.replicate_command. Kept as its own copy here rather than a
    cross-import, since core/ shouldn't depend on web/ (jobs.py
    already imports from core/, not the other way around)."""

    if value == "" or any(character.isspace() for character in value):
        return '"' + value.replace('"', '\\"') + '"'
    return value


def command_line_from_argv(prog: str, argv: list[str]) -> str:
    """Rebuild a paste-able command line from a prog name and its
    argv - used by run_cli() (cli/errors.py) to record what a direct
    terminal invocation actually was."""

    return " ".join([prog, *(quote_for_display(a) for a in argv)])
