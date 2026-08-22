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
import threading
import time
import traceback
from collections.abc import Callable
from datetime import datetime
from datetime import timezone

from ..core import history
from ..core.camera_config import default_config_dir
from ..core.notify import NotifyConfigError
from ..core.notify import load_notify_config
from ..core.notify import send_crash_notification

EXIT_INTERRUPTED = 130
EXIT_OS_ERROR = 1


def _unattended() -> bool:
    """True when there's no real terminal attached to notice a crash
    on - a cron job, a scheduled task, an SSH session whose window got
    closed, or anything else running detached. Same check (and same
    reasoning) as bv_generate.py's own _interactive(), inverted: stdin/
    stdout are process-wide, not per-thread, so a background thread
    inside an otherwise-interactive process (bv-web's job runner) is
    still correctly unattended from *this* call's point of view even
    though the parent process has a real console somewhere."""

    return not (
        sys.stdin.isatty()
        and sys.stdout.isatty()
        and threading.current_thread() is threading.main_thread()
    )


def _notify_of_crash(prog: str, argv: list[str], exc: BaseException) -> None:
    """Best-effort email notification for an unattended crash - see
    core/notify.py's own module docstring for the full "why global,
    why voluntary" reasoning. Always uses default_config_dir() (not
    whatever --config-dir a command happened to be given), since
    notify.toml is a global setting, not scoped to one camera or
    archive - the vast majority of runs never override --config-dir
    anyway. Never raises: a malformed notify.toml, an unreachable SMTP
    relay, or any other failure here must never change the outcome of
    the command that actually crashed - it already has its own real
    error on stderr/the logfile regardless of whether this succeeds.
    """

    try:
        notify_config = load_notify_config(default_config_dir())
    except NotifyConfigError:
        return

    if notify_config is None:
        return

    command_line = " ".join([prog, *argv])
    body = (
        f"{prog} crashed while running unattended (no terminal attached).\n\n"
        f"Command: {command_line}\n\n"
        f"{''.join(traceback.format_exception(type(exc), exc, exc.__traceback__))}"
    )

    send_crash_notification(
        notify_config,
        subject=f"{prog} crashed: {exc}",
        body=body,
    )


def run_cli(
    prog: str, main: Callable[[], int], *, argv: list[str] | None = None
) -> int:
    """Run a CLI main() function, turning KeyboardInterrupt and
    OSError (covers FileNotFoundError, NotADirectoryError,
    PermissionError, and friends - whatever path a command was
    pointed at) into a short stderr message and a normal exit code,
    instead of letting either turn into a raw traceback.

    Any other exception is still left to propagate as-is - this only
    covers failure modes common enough, and unambiguous enough, to
    be worth a blanket catch across every command - but is now also
    noted first (see `crash_exc` below) so an unattended run still
    gets a chance to notify before the exception continues upward.

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

    That same `finally` is also where an optional crash-notification
    email fires (core/notify.py) - Christer: "mailing when cli
    commands without any tty crashes, since noone knows that if they
    dont read logs all the time". Deliberately not for every failure:
    a plain non-zero exit from an otherwise-clean run (e.g.
    bv-generate warning-and-skipping one corrupted recording) is
    expected, already-surfaced behavior, not a crash - only
    KeyboardInterrupt-free exceptions (the OSError branch below, or
    anything else propagating straight through) count, and only when
    _unattended() is true; an attended terminal run already shows the
    error directly, no extra signal needed.
    """

    started_at = datetime.now(timezone.utc)
    clock_start = time.monotonic()
    status = "failed"
    code = EXIT_OS_ERROR
    crash_exc: BaseException | None = None
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
        crash_exc = exc
        return code
    except Exception as exc:
        crash_exc = exc
        raise
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
        if crash_exc is not None and _unattended():
            _notify_of_crash(
                prog, argv if argv is not None else sys.argv[1:], crash_exc
            )
