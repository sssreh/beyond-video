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
from collections.abc import Callable
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

    Optional `say` (task: "could bv-export be more talkative, not like
    trip.txt, but tell what its doing, now i dont get anything") - when
    given, every `step()`/`warning()` call also gets echoed live
    through it, in addition to being written to trip.log. Before this,
    export_trip() had no live output at all: the existing `--debug`
    print()s went straight to raw `sys.stderr`, which is process-wide,
    not per-thread - the exact same class of bug as `bv_export.py`'s
    `_interactive()` fix (see WORKING_CONTEXT.md), just hitting
    progress output instead of an `input()` prompt. In bv-web's
    background job thread, those prints went to the server's own
    unwatched console, never to the job's own output box - "now i dont
    get anything" even with `--debug` already on. Reusing `step()`'s
    own call sites (already comprehensive - concatenation, map render
    start/end with timing, gsensor, stitch, and so on) rather than
    threading a separate callable through each of `trip_export.py`'s
    individual phases means every phase already worth recording to
    trip.log is, for free, also worth saying live - deliberately not
    gated behind `--debug` either, unlike the print()s it doesn't
    touch: this is coarser than trip.log's own per-recording
    concatenation detail (Christer's own "not like trip.txt"), so it
    doesn't need debug's extra opt-in to stay readable.

    `trip_label` is announced live exactly once, as its own line
    ("bv-export: trip_20260814_..."), right when the trip log opens -
    not repeated on every subsequent step() line. The first version of
    this prefixed trip_label onto every single live-echoed line
    instead; Christer: "i asked for the trip name in bv-export, but
    not for every output, just one time to show where we are working."
    A single bv-export run processes its trips one at a time, but
    Christer's own earlier "print trip name too, in case there are
    more than 1 trips" request still applies: without the one-time
    banner, a multi-trip run's live output would be a single
    undifferentiated stream of step lines with no way to tell which
    trip is currently being reported on.
    """

    def __init__(
        self,
        path: Path,
        *,
        trip_label: str,
        command: str,
        say: Callable[[str], None] | None = None,
    ):
        self._path = path
        self._monotonic_start = time.monotonic()
        self._file = path.open("w", encoding="utf-8")
        self._say = say
        self._trip_label = trip_label
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

        # Announce which trip we're working on exactly once, live, when
        # the trip starts - Christer: "i asked for the trip name in
        # bv-export, but not for every output, just one time to show
        # where we are working." The first version (see this class's
        # own docstring) prefixed trip_label onto every single step()
        # line instead, which over-delivered on "print trip name too,
        # in case there are more than 1 trips" into needless repetition
        # on every phase line. step() below no longer repeats it.
        if self._say is not None:
            self._say(f"bv-export: {trip_label}")

    @classmethod
    def open(
        cls,
        destination: Path,
        *,
        trip_label: str,
        command: str,
        say: Callable[[str], None] | None = None,
    ) -> "TripLog":
        """Open (creating/truncating) trip.log inside `destination`,
        writing the header immediately. `say`, if given, is forwarded
        straight to `__init__` - see this class's own docstring for
        what it does."""

        return cls(
            destination / LOG_FILENAME,
            trip_label=trip_label,
            command=command,
            say=say,
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

    def step(
        self,
        message: str,
        *,
        elapsed_seconds: float | None = None,
        live: bool = True,
    ) -> None:
        """Record one export phase as it happens - e.g. "concatenated
        front video (2 recording(s))" or "rendered map.mp4". A
        wall-clock timestamp (HH:MM:SS) is prefixed automatically.
        `elapsed_seconds`, if given, is appended in parentheses - for
        phases worth knowing the duration of (map/stitch rendering in
        particular, which can run to minutes on a real archive).

        Also echoed live through `self._say`, if one was given to
        `__init__`/`open()` - see this class's own docstring - unless
        `live=False`. Christer, re: the per-recording front/rear
        alignment trim lines specifically: "That als goes for the
        trimming part, just same thing close to 'Trimming videos so
        front and rear matches', of course the trip.log can keep that
        output." Same shape as the trip-name-per-line fix right above:
        a phase that can legitimately log many lines (one per
        recording trimmed) still belongs in trip.log in full, but the
        live stream should show one summary line for the phase, not
        one line per recording. Callers wanting that split call
        `step()` once normally for the summary, then `step(...,
        live=False)` for each detail line - trip.log gets every line
        either way, since `live` only controls the `self._say` echo."""

        if not self._wrote_steps_header:
            self._write("")
            self._write("--- Export steps ---")
            self._wrote_steps_header = True

        if elapsed_seconds is not None:
            full_message = f"{message} ({elapsed_seconds:.1f}s)"
        else:
            full_message = message

        timestamp = datetime.now().strftime("%H:%M:%S")
        self._write(f"{timestamp}  {full_message}")
        if live and self._say is not None:
            self._say(f"bv-export: {full_message}")

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
