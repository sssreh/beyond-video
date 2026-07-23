"""
Per-trip diagnostic log for bv-export - trip.log, written into every
trip folder, recording what bv-export actually did for that trip and
why: the exact command that produced it, why each recording was
judged to belong to the trip (see trip_builder.TripBuilder's own
`reasons` output), and a timestamped account of each export phase as
it runs (concatenating front/rear/audio, merging GPS/g-sensor/
subtitles, rendering map.mp4, stitching, and so on).

The point is to make a surprising result checkable against the real
reasoning that produced it, instead of guessed at after the fact -
e.g. "why does this trip include a recording from days earlier/later"
should be answerable by reading trip.log, not by re-deriving
TripBuilder's decision by hand.

Written incrementally: every line is flushed to disk immediately, not
buffered until close(). A log that only gets written on a clean exit
would be useless for exactly the runs most worth diagnosing - a hang
or a crash partway through still leaves a partial, honest trip.log
behind, right up to whatever the last completed step was.

Copyright (C) 2026 Christer R. (sssreh)

SPDX-License-Identifier: GPL-3.0-or-later
"""

from __future__ import annotations

import threading
import time
from datetime import datetime
from pathlib import Path

LOG_FILENAME = "trip.log"


class TripLog:
    """Writes `destination/trip.log` for one trip's export_trip() run.

    Also usable as a context manager (`with TripLog.open(...) as log:`)
    so `close()` always runs, even if the export raises partway
    through - the footer then says "did not finish cleanly" instead of
    the log just stopping cold with no explanation, which matters most
    for exactly the runs worth debugging.

    The file has three sections, in order: a header (start time, trip
    label, the full invoking command), trip membership (why each
    recording belongs here - see `membership()`), and export steps
    (see `step()`) - the membership/steps section headers are written
    lazily, the first time `membership()`/`step()` is actually called,
    so a trip log for a run that (say) fails before any steps run
    still reads cleanly rather than showing an empty steps section.
    """

    def __init__(self, path: Path, *, trip_label: str, command: str):
        self._path = path
        self._monotonic_start = time.monotonic()
        self._file = path.open("w", encoding="utf-8")
        self._wrote_membership_header = False
        self._wrote_steps_header = False
        # front/rear/audio concatenation runs in three concurrent
        # threads (see trip_export.export_trip()), each of which may
        # call step()/warning() around the same moment - guards
        # against their lines interleaving mid-write into garbled
        # output.
        self._lock = threading.Lock()

        self._write(f"=== bv-export trip log: {trip_label} ===")
        self._write(
            f"Started: {datetime.now().isoformat(timespec='seconds')}"
        )
        self._write(f"Command: {command}")

    @classmethod
    def open(cls, destination: Path, *, trip_label: str, command: str) -> "TripLog":
        """Open (creating/truncating) trip.log inside `destination`,
        writing the header immediately."""

        return cls(
            destination / LOG_FILENAME, trip_label=trip_label, command=command
        )

    def _write(self, line: str) -> None:
        with self._lock:
            self._file.write(line + "\n")
            self._file.flush()

    def membership(self, recording_id: object, reason: str) -> None:
        """Record why `recording_id` belongs to this trip - normally
        called once per recording, in order, using the same reason
        text TripBuilder.build()'s own `reasons` output already
        computed (see trip_builder.py) - not re-derived here, so the
        log can never disagree with the actual decision that was
        made."""

        if not self._wrote_membership_header:
            self._write("")
            self._write("--- Trip membership ---")
            self._wrote_membership_header = True

        self._write(f"{recording_id}: {reason}")

    def step(self, message: str, *, elapsed_seconds: float | None = None) -> None:
        """Record one export phase as it happens - e.g. "concatenated
        front video (2 recording(s))" or "rendered map.mp4". A
        wall-clock timestamp (HH:MM:SS) is prefixed automatically.
        `elapsed_seconds`, if given, is appended in parentheses - for
        phases worth knowing the duration of (map/stitch rendering in
        particular, which can run to minutes on a real archive)."""

        if not self._wrote_steps_header:
            self._write("")
            self._write("--- Export steps ---")
            self._wrote_steps_header = True

        timestamp = datetime.now().strftime("%H:%M:%S")
        if elapsed_seconds is not None:
            self._write(f"{timestamp}  {message} ({elapsed_seconds:.1f}s)")
        else:
            self._write(f"{timestamp}  {message}")

    def warning(self, message: str) -> None:
        """Record a warning - the same text that also goes into
        ExportResult.warnings, so trip.log has a complete account of
        every "degraded, didn't fail" moment during this trip's export
        too, in context with whatever step it happened during."""

        self.step(f"WARNING: {message}")

    def close(self, *, failed: bool = False) -> None:
        """Write the footer and close the file. `failed=True` (see
        `__exit__`) notes the run didn't finish cleanly instead of
        claiming a normal finish - still records how long it ran for
        up to that point, which is often the most useful single fact
        for diagnosing a hang.
        """

        elapsed = time.monotonic() - self._monotonic_start
        self._write("")
        if failed:
            self._write(
                f"Did not finish cleanly: "
                f"{datetime.now().isoformat(timespec='seconds')} "
                f"(ran for {elapsed:.1f}s before the error)"
            )
        else:
            self._write(
                f"Finished: {datetime.now().isoformat(timespec='seconds')} "
                f"(took {elapsed:.1f}s)"
            )
        self._file.close()

    def __enter__(self) -> "TripLog":
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        self.close(failed=exc_type is not None)
