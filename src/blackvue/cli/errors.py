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
from collections.abc import Callable

EXIT_INTERRUPTED = 130
EXIT_OS_ERROR = 1


def run_cli(prog: str, main: Callable[[], int]) -> int:
    """Run a CLI main() function, turning KeyboardInterrupt and
    OSError (covers FileNotFoundError, NotADirectoryError,
    PermissionError, and friends - whatever path a command was
    pointed at) into a short stderr message and a normal exit code,
    instead of letting either turn into a raw traceback.

    Any other exception is left to propagate as-is - this only
    covers failure modes common enough, and unambiguous enough, to
    be worth a blanket catch across every command.
    """

    try:
        return main()
    except KeyboardInterrupt:
        print(f"\n{prog}: interrupted", file=sys.stderr)
        return EXIT_INTERRUPTED
    except OSError as exc:
        detail = (
            f"{exc.strerror}: {exc.filename}"
            if exc.strerror and exc.filename
            else str(exc)
        )
        print(f"{prog}: {detail}", file=sys.stderr)
        return EXIT_OS_ERROR
