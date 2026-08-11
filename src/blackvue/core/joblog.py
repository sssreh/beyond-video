"""
Persistent output log for bv-* commands.

Christer, after restarting bv-web to clear stuck GPU memory and losing
all record of what had been running: "I would also want a logfile of
all the output." This module is that logfile's writing side - a
single append-only transcript covering both direct-CLI invocations
(bv-scribe typed straight into pwsh) and bv-web-triggered jobs, per
the "Scope - settled: direct CLI calls too, not just bv-web jobs"
decision in WORKING_CONTEXT.md. The companion command-history feature
(bv-history, WORKING_CONTEXT.md's own "Note: bv-history command"
entry) is a separate, not-yet-built piece that will read a structured
index alongside this raw transcript - this module only writes.

Copyright (C) 2026 Christer R. (sssreh)

SPDX-License-Identifier: GPL-3.0-or-later
"""

from __future__ import annotations

import gzip
import logging
import shutil
import threading
from datetime import datetime
from pathlib import Path

from .camera_config import default_logs_dir

_LOG_FILENAME_PREFIX = "beyond-video"

# How many already-rotated (gzipped) months to keep around before the
# oldest ones start getting deleted. 12 months is a full year of
# history at essentially no disk cost (this is plain text, gzipped -
# even a very heavy bv-scribe day's output is a few hundred KB) - see
# WORKING_CONTEXT.md's "cadence" note for why monthly rotation itself
# was chosen. None disables deletion entirely (keep everything
# forever) - not the default, since an unbounded number of month files
# defeats part of the point of rotating in the first place.
DEFAULT_BACKUP_MONTHS = 12


class MonthlyRotatingFileHandler(logging.Handler):
    """Writes to one active logfile per calendar month
    (<prefix>-YYYY-MM.log), gzip-compressing the previous month's file
    the first time a record is emitted after the month has changed.

    stdlib's own logging.handlers.TimedRotatingFileHandler doesn't
    have a monthly unit built in (its largest "when" is weekly,
    "W0"-"W6") and its rollover math is all fixed-interval-in-seconds
    based - awkward to bend into "roll over on the 1st of next month"
    since months aren't a fixed number of seconds. Simpler to just
    compare the current year-month against the currently-open file's
    year-month on every write and swap files when it differs, rather
    than fighting that class's own rollover scheduling. See
    WORKING_CONTEXT.md's "who owns rotation" / "cadence" notes for why
    this needed to be monthly and in-process (not external
    cron/logrotate, not weekly).

    Not thread-safe on its own - callers (see get_logger() below) are
    expected to share one process-wide instance behind a lock, since
    bv-web's job runner writes from multiple background threads at
    once.
    """

    def __init__(
        self,
        directory: Path,
        *,
        prefix: str = _LOG_FILENAME_PREFIX,
        backup_months: int | None = DEFAULT_BACKUP_MONTHS,
    ) -> None:
        super().__init__()
        self._directory = directory
        self._prefix = prefix
        self._backup_months = backup_months
        self._current_month: str | None = None
        self._stream = None

    def _month_key(self, when: datetime) -> str:
        return when.strftime("%Y-%m")

    def _path_for(self, month_key: str) -> Path:
        return self._directory / f"{self._prefix}-{month_key}.log"

    def _open_for(self, month_key: str) -> None:
        self._directory.mkdir(parents=True, exist_ok=True)
        self._stream = open(self._path_for(month_key), "a", encoding="utf-8")
        self._current_month = month_key

    def _gzip_and_remove(self, path: Path) -> None:
        if not path.exists():
            return
        try:
            with open(path, "rb") as src, gzip.open(f"{path}.gz", "wb") as dst:
                shutil.copyfileobj(src, dst)
            path.unlink()
        except OSError:
            # Best-effort - a failed compression (disk full, odd
            # permissions) shouldn't take down logging itself, and the
            # uncompressed file is still there either way.
            pass

    def _prune_old_backups(self) -> None:
        if self._backup_months is None:
            return
        gz_files = sorted(self._directory.glob(f"{self._prefix}-*.log.gz"))
        excess = len(gz_files) - self._backup_months
        for old_file in gz_files[:excess]:
            try:
                old_file.unlink()
            except OSError:
                pass

    def _rotate_if_needed(self) -> None:
        month_key = self._month_key(datetime.now())
        if month_key == self._current_month:
            return
        previous_month = self._current_month
        if self._stream is not None:
            self._stream.close()
            self._stream = None
        if previous_month is not None:
            self._gzip_and_remove(self._path_for(previous_month))
            self._prune_old_backups()
        self._open_for(month_key)

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self._rotate_if_needed()
            self._stream.write(self.format(record) + "\n")
            self._stream.flush()
        except Exception:
            self.handleError(record)

    def close(self) -> None:
        if self._stream is not None:
            self._stream.close()
            self._stream = None
        super().close()


_logger: logging.Logger | None = None
_logger_lock = threading.Lock()


def get_logger() -> logging.Logger:
    """Return the process-wide persistent-output logger, creating it
    (and its MonthlyRotatingFileHandler) on first use. Safe to call
    from any thread - bv-web's job runner spawns one background thread
    per job, all of which call log_line() below concurrently."""

    global _logger
    with _logger_lock:
        if _logger is None:
            logger = logging.getLogger("beyond_video.joblog")
            logger.setLevel(logging.INFO)
            logger.propagate = False
            handler = MonthlyRotatingFileHandler(default_logs_dir())
            handler.setFormatter(
                logging.Formatter("%(asctime)s [%(name_)s] %(message)s")
            )
            logger.addHandler(handler)
            _logger = logger
        return _logger


def log_line(source: str, message: str) -> None:
    """Append one line to the persistent output log, tagged with
    `source` (a command name like "bv-scribe", or a bv-web job id) -
    the same shared write path for both direct-CLI runs (see
    cli/errors.py's run_cli()) and bv-web jobs (see web/jobs.py's
    Job.append_output()). Never raises - a logging failure (disk full,
    permissions) should never take down the actual command it's
    logging.
    """

    try:
        get_logger().info(message, extra={"name_": source})
    except Exception:
        pass


def wrap_say(prog: str, say=print):
    """Wrap a CLI's own `say` callable so every line it prints also
    gets persisted - used by each bv-* command's own main() (the
    direct-CLI entry point, which unlike bv-web's JobRunner doesn't
    otherwise pass through any single shared point per line of
    output). `say` still gets called exactly as before, so this is
    purely additive - printing behavior/formatting is untouched."""

    def wrapped(message: str = "") -> None:
        say(message)
        log_line(prog, message)

    return wrapped


def wrap_warn(prog: str, warn):
    """Same as wrap_say() above, for a CLI's `warn` callable."""

    def wrapped(message: str) -> None:
        warn(message)
        log_line(prog, message)

    return wrapped
