"""
bv-history - browse the persistent command-history index every other
bv-* command writes to (see core/history.py's own module docstring),
the way pwsh/bash's own `history` browses your shell history.

Two invocations:
- `bv-history [filters...]` (the default) lists entries oldest-first,
  tail-style (only the most recent DEFAULT_TAIL_COUNT by default - see
  blackvue.history's own docstring for why).
- `bv-history show <id>` dumps one past run's full logged output
  (best-effort, from core/joblog.py - see blackvue.history's
  matching_log_lines() for the real limitations).

No argparse subparsers here - "show" is handled as a special reserved
first token instead, checked before the main parser ever runs. This
keeps the common case (`bv-history`, `bv-history --command bv-ls`)
free of any subcommand keyword, matching how bash's own bare `history`
works - argparse subparsers would either force typing a "list" keyword
for the common case, or fight with a bare positional --last/count
argument at the top level. Two small, separately testable parsers is
simpler than reconciling that.

Copyright (C) 2026 Christer R. (sssreh)

SPDX-License-Identifier: GPL-3.0-or-later
"""

from __future__ import annotations

import argparse
import sys

from .errors import run_cli
from ..core.history import HistoryEntry
from ..history import DEFAULT_TAIL_COUNT
from ..history import HistoryFilter
from ..history import NumberedEntry
from ..history import all_entries
from ..history import filtered_entries
from ..history import matching_log_lines
from ..history import tail

EXIT_OK = 0
EXIT_ARGS_ERROR = 1
EXIT_NOT_FOUND = 2


def _default_warn(message: str) -> None:
    print(message, file=sys.stderr)


def _format_duration(seconds: float) -> str:
    if seconds < 60:
        return f"{seconds:.1f}s"
    minutes, secs = divmod(int(round(seconds)), 60)
    if minutes < 60:
        return f"{minutes}m{secs:02d}s"
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h{minutes:02d}m"


def _format_who(entry: HistoryEntry) -> str:
    if entry.source == "bv-web":
        return entry.username or "bv-web"
    return "cli"


def _format_row(numbered: NumberedEntry) -> str:
    entry = numbered.entry
    # started_at is stored UTC (see core/history.py's own
    # HistoryEntry docstring) - shown in local time here, since a
    # terminal listing is for a human reading it right now, not a
    # machine-parseable log.
    from datetime import datetime

    started_local = datetime.fromisoformat(entry.started_at).astimezone()
    return (
        f"{numbered.number:>5}  "
        f"{started_local:%Y-%m-%d %H:%M:%S}  "
        f"{_format_duration(entry.duration_seconds):>7}  "
        f"{entry.status:<11} "
        f"{_format_who(entry):<9} "
        f"{entry.command_line}"
    )


def parse_list_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="bv-history",
        description=(
            "Browse the persistent command-history index every bv-* "
            "command writes to (direct-CLI runs and bv-web jobs "
            "alike) - the same idea as pwsh/bash's own `history`. "
            "Shows the most recent entries by default, oldest first; "
            "use --all to see everything. `bv-history show <id>` "
            "dumps one past run's full logged output."
        ),
        allow_abbrev=False,
    )

    parser.add_argument(
        "--last",
        type=int,
        metavar="N",
        default=DEFAULT_TAIL_COUNT,
        help=(
            "Show only the N most recent matching entries (default: "
            f"{DEFAULT_TAIL_COUNT}) - see --all to disable this limit."
        ),
    )
    parser.add_argument(
        "--all",
        action="store_true",
        help="Show every matching entry, ignoring --last's default limit.",
    )
    parser.add_argument(
        "--command",
        metavar="NAME",
        help="Only show entries for this command (e.g. bv-ls).",
    )
    parser.add_argument(
        "--camera",
        metavar="TEXT",
        help=(
            "Only show entries whose command line mentions this text "
            "(a camera id or archive path, typically) - a plain "
            "substring match, since command lines aren't parsed."
        ),
    )
    parser.add_argument(
        "--from",
        dest="from_",
        metavar="TIMESTAMP",
        help="Only show entries started at or after this timestamp.",
    )
    parser.add_argument(
        "--until",
        metavar="TIMESTAMP",
        help="Only show entries started at or before this timestamp.",
    )
    parser.add_argument(
        "--timestamp",
        metavar="TIMESTAMP",
        help="Only show entries matching this timestamp or prefix.",
    )
    parser.add_argument(
        "--failed-only",
        action="store_true",
        help="Only show entries that failed or were interrupted.",
    )
    parser.add_argument(
        "--search",
        metavar="TEXT",
        help="Only show entries whose full command line contains TEXT.",
    )
    parser.add_argument(
        "--source",
        choices=["cli", "bv-web"],
        help="Only show entries from this source.",
    )

    return parser.parse_args(argv)


def parse_show_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="bv-history show",
        description=(
            "Dump one past run's full logged output, best-effort "
            "reconstructed from the persistent output log (see "
            "blackvue.history.matching_log_lines() for the real "
            "limitations - most notably: nothing to show if the "
            "run's own month has already rotated/pruned away)."
        ),
        allow_abbrev=False,
    )
    parser.add_argument(
        "id",
        type=int,
        metavar="ID",
        help="Entry number, from the main bv-history listing.",
    )
    return parser.parse_args(argv)


def _run_list(args: argparse.Namespace, *, say=print, warn=_default_warn) -> int:
    try:
        filt = HistoryFilter(
            command=args.command,
            camera=args.camera,
            since=args.from_,
            until=args.until,
            timestamp=args.timestamp,
            failed_only=args.failed_only,
            search=args.search,
            source=args.source,
        )
        matches = filtered_entries(filt, entries=all_entries())
    except ValueError as exc:
        warn(f"bv-history: {exc}")
        return EXIT_ARGS_ERROR

    if not matches:
        say("bv-history: no matching entries.")
        return EXIT_OK

    shown = matches if args.all else tail(matches, args.last)

    if not args.all and len(shown) < len(matches):
        say(
            f"bv-history: showing the {len(shown)} most recent of "
            f"{len(matches)} matching entries (--all for everything)."
        )

    for numbered in shown:
        say(_format_row(numbered))

    return EXIT_OK


def _run_show(args: argparse.Namespace, *, say=print, warn=_default_warn) -> int:
    entries = all_entries()
    match = next((e for e in entries if e.number == args.id), None)

    if match is None:
        warn(f"bv-history: no entry numbered {args.id}.")
        return EXIT_NOT_FOUND

    say(_format_row(match))
    say("")

    lines = matching_log_lines(match.entry)
    if not lines:
        say(
            "bv-history: no logged output found for this entry "
            "(it may predate logging, or its month's logfile has "
            "already rotated/been pruned)."
        )
        return EXIT_OK

    for line in lines:
        say(f"{line.timestamp:%H:%M:%S}  {line.message}")

    return EXIT_OK


def main(argv: list[str] | None = None) -> int:
    """Run bv-history. Dispatches on a leading "show" token before
    argparse ever runs - see this module's own docstring for why."""

    raw_argv = sys.argv[1:] if argv is None else argv

    if raw_argv and raw_argv[0] == "show":
        show_args = parse_show_args(raw_argv[1:])
        return run_cli(
            "bv-history", lambda: _run_show(show_args), argv=raw_argv
        )

    list_args = parse_list_args(raw_argv)
    return run_cli(
        "bv-history", lambda: _run_list(list_args), argv=raw_argv
    )


if __name__ == "__main__":
    raise SystemExit(main())
