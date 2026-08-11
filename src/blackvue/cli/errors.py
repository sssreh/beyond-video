"""
Shared CLI error handling.

Every bv-* console-script entry point runs its body through
run_cli(), so two failure modes every command can hit the same way -
Ctrl-C mid-run, and a bad path (missing, not a directory, not
readable) passed as an archive/config/target location - print one
clean line on stderr instead of a raw Python traceback.

This has to live inside main() itself, not behind an
`if __name__ == "__main__":` guard: the installed console-script
entry points (see pyproject.toml) call `blackvue.cli.bv_ls:main`
directly, so that guard never runs for a real install - only when a
module is executed as a script directly.

Copyright (C) 2026 Christer R. (sssreh)

SPDX-License-Identifier: GPL-3.0-or-later
"""

from __future__ import annotations

import sys
import time
from collections.abc import Callable
from datetime import datetime
from datetime import timezone

from ..core import history

EXIT_INTERRUPTED = 130
EXIT_OS_ERROR = 1


def run_cli(
    prog: str, main: Callable[[], int], *, argv: list[str] | None = None
) -> int:
    """Run a CLI main() function, turning KeyboardInterrupt and
    OSError (covers FileNotFoundError, NotADirectoryError,
    PermissionError, and friends - whatever path a command was
    pointed at) into a short stderr message and a normal exit code,
    instead of letting either turn into a raw traceback.

    Any other exception is left to propagate as-is - this only
    covers failure modes common enough, and unambiguous enough, to
    be worth a blanket catch across every command.

    Also the single shared hook point every bv-* command's own
    main() already passes through, so this is where the persistent
    command-history entry (core/history.py) gets recorded for the
    direct-CLI half of "I would also want a logfile of all the
    output" (core/joblog.py's own docstring) - a `finally` block
    means exactly one entry is recorded per invocation regardless of
    which of the branches below actually returns, or whether an
    uncaught exception propagates straight through instead. `argv` is
    each command's own already-parsed argv (None means "used
    sys.argv[1:]", matching argparse's own convention) - passed
    through here rather than read from sys.argv unconditionally, so a
    caller that invoked main() with an explicit argv (tests, embedding)
    gets an accurate command-history entry instead of whatever
    happened to be on the real process's sys.argv at the time.
    """

    started_at = datetime.now(timezone.utc)
    clock_start = time.monotonic()
    status = "failed"
    code = EXIT_OS_ERROR
    try:
        code = main()
        status = "succeeded" if code == 0 else "failed"
        return code
    except KeyboardInterrupt:
        print(f"\n{prog}: interrupted", file=sys.stderr)
        status = "interrupted"
        code = EXIT_INTERRUPTED
        return code
    except OSError as exc:
        detail = (
            f"{exc.strerror}: {exc.filename}"
            if exc.strerror and exc.filename
            else str(exc)
        )
        print(f"{prog}: {detail}", file=sys.stderr)
        status = "failed"
        code = EXIT_OS_ERROR
        return code
    finally:
        history.record(
            history.HistoryEntry(
                command=prog,
                command_line=history.command_line_from_argv(
                    prog, argv if argv is not None else sys.argv[1:]
                ),
                source="cli",
                username=None,
                started_at=started_at.isoformat(),
                duration_seconds=time.monotonic() - clock_start,
                status=status,
            )
        )
