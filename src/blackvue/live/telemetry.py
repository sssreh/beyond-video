"""
Live telemetry pump for bv-live: a background thread that continuously
reads blackvue_livedata.cgi and keeps a small, time-bounded in-memory
buffer of recent GPS/g-sensor readings for the live map and g-sensor
renderers (map_stream.py/gsensor_stream.py) to draw from.

blackvue_livedata.cgi never closes its own connection and interleaves
GPS and g-sensor ("3G") JSON objects with no way to ask for just one
kind (see parser/livedata.py's own module docstring). BlackVueClient
.live_gps() already knows how to read a single bounded slice of that
stream and return the first GPS reading it finds, but bv-live needs
something different: every reading, of both kinds, for as long as the
server runs - not just the first one. LiveTelemetryPump is that
continuous version, built on the same regex-based parsing (see
parser/livedata.py's find_next_gps()/find_next_gsensor_reading(),
added alongside this for exactly this use) but looping forever instead
of stopping at the first match.

Started once at server startup (see live/app.py) and run for the
whole process's lifetime, not lazily per-viewer - livedata.cgi's own
connection is cheap to hold open (small JSON objects, no video), so
there's no real cost to keeping it going even while no browser tab
happens to be open, and it means the map/g-sensor panels have real
history to draw from the instant a tab opens rather than starting from
an empty buffer.

Copyright (C) 2026 Christer R. (sssreh)

SPDX-License-Identifier: GPL-3.0-or-later
"""

from __future__ import annotations

import statistics
import threading
import time
from collections import deque
from dataclasses import dataclass

from ..core.blackvue_client import BlackVueClient
from ..parser.livedata import find_next_gps
from ..parser.livedata import find_next_gsensor_reading

# How far back TelemetryState keeps readings, regardless of what any
# particular caller (map route trail, g-sensor window) asks for -
# generous headroom beyond bv-live's own default --gsensor-window
# (60s) and typical --map-zoom route-trail needs, so a long live
# session doesn't need retuning this just to keep working, while still
# bounding memory for a session left running a long time.
MAX_HISTORY_SECONDS = 600.0

# Individual .read() call size while draining blackvue_livedata.cgi -
# same as BlackVueClient.live_gps()'s own LIVE_GPS_MAX_BYTES chunking,
# just repeated forever here instead of stopping at the first match.
READ_CHUNK_BYTES = 4096

# If neither pattern has matched anything in this many buffered
# characters, drop the buffer and keep reading rather than growing it
# forever - the same safety-cap spirit as BlackVueClient.live_gps()'s
# own LIVE_GPS_MAX_BYTES, but self-healing (drop-and-continue) rather
# than raising, since a continuous background pump has no caller to
# report a one-off failure to.
MAX_BUFFER_CHARS = 65536

# How long to wait before retrying after the stream drops (a WiFi
# blip, the camera briefly unreachable) - short enough to recover
# quickly, long enough not to hammer a camera that's genuinely gone
# for a while.
RECONNECT_DELAY_SECONDS = 2.0

# How long TelemetryState collects raw g-sensor readings for before
# freezing them into a zero-offset baseline - Christer, parked, seeing
# the live g-sensor strip sit visibly offset from its own zero line:
# "I think we need some type of ero[zero] calibration". The camera's
# own FrontRear/LeftRight/UpperLower readings aren't necessarily
# centered on zero at rest (a dashcam mounted at even a slight angle,
# or a plain sensor bias - the same reasoning export/gsensor_render.py
# 's own baseline_for_samples() already documents for the *offline*
# gauge, which centers on a finished trip's own median reading rather
# than raw (0, 0) for exactly this). bv-live has no finished trip to
# take a median of upfront, so it calibrates from the first
# CALIBRATION_SECONDS of live readings instead, once, the first time
# the pump starts producing g-sensor data - see _finish_calibration().
# 3 seconds is a first guess (assumed roughly stationary right after
# bv-live starts, matching how Christer's own test just now was run
# with the car parked) - not measured against anything, easy to widen
# if a quick startup jolt (a door closing, engine starting) ever ends
# up baked into the baseline in practice.
GSENSOR_CALIBRATION_SECONDS = 3.0


@dataclass(frozen=True)
class GpsSample:
    """One live GPS reading, timestamped on receipt (time.monotonic())
    - blackvue_livedata.cgi's own GPS object carries no timestamp of
    its own, unlike the offline .gps files' NMEA-derived ones."""

    at: float
    latitude: float
    longitude: float


@dataclass(frozen=True)
class GSensorSample:
    """One live g-sensor reading, timestamped on receipt - see
    GpsSample's own docstring for why "on receipt" rather than a
    reading embedded in the data itself."""

    at: float
    front_rear: float
    left_right: float
    upper_lower: float


class TelemetryState:
    """Thread-safe rolling buffer of recent GpsSample/GSensorSample
    readings, written by LiveTelemetryPump's background thread and
    read by the map/g-sensor frame renderers (running on whatever
    thread FastAPI/Starlette happens to serve their request on) - a
    plain threading.Lock guards every access since both sides run
    concurrently.

    Samples older than `history_seconds` are dropped as new ones
    arrive (see _trim()) - this never grows without bound, regardless
    of how long bv-live keeps running for.
    """

    def __init__(self, history_seconds: float = MAX_HISTORY_SECONDS) -> None:
        self._history_seconds = history_seconds
        self._lock = threading.Lock()
        self._gps: deque[GpsSample] = deque()
        self._gsensor: deque[GSensorSample] = deque()

        # Zero-offset calibration + a monotonically-growing scale
        # watermark (see GSENSOR_CALIBRATION_SECONDS's own comment) -
        # kept here rather than in gsensor_stream.py's renderer because
        # it has to persist for the whole live session, not just
        # whatever's still inside the display's own rolling
        # `window_seconds` (old peaks would otherwise "forget"
        # themselves the moment they scroll out of view, letting the
        # scale shrink back down - the opposite of what Christer asked
        # for: "when newer data comes in and are greater than the
        # previous max value, we scale down the lines to match the new
        # max value" i.e. the scale should only ever grow).
        self._gsensor_calibration_start: float | None = None
        self._gsensor_calibration_buffer: list[GSensorSample] = []
        self._gsensor_baseline: tuple[float, float, float] | None = None
        self._gsensor_max_deviation: float = 0.0

    def add_gps(self, latitude: float, longitude: float) -> None:
        now = time.monotonic()
        with self._lock:
            self._gps.append(GpsSample(now, latitude, longitude))
            self._trim(self._gps, now)

    def add_gsensor(
        self, front_rear: float, left_right: float, upper_lower: float
    ) -> None:
        now = time.monotonic()
        sample = GSensorSample(now, front_rear, left_right, upper_lower)
        with self._lock:
            self._gsensor.append(sample)
            self._trim(self._gsensor, now)
            self._update_gsensor_calibration(sample, now)

    def _update_gsensor_calibration(self, sample: GSensorSample, now: float) -> None:
        """Feed one just-arrived sample into calibration bookkeeping -
        called under `self._lock` from add_gsensor(), not on its own.

        Before a baseline exists: buffer the sample, and once
        GSENSOR_CALIBRATION_SECONDS have elapsed since the very first
        g-sensor reading ever seen, freeze the *median* (not mean) of
        the buffered readings per axis as the baseline - median rather
        than mean for the same reason export/gsensor_render.py's own
        baseline_for_samples() uses it: robust to a single jostle
        (a door closing, someone bumping the car) during the
        calibration window pulling an average off to one side.

        After a baseline exists: this sample no longer affects the
        baseline itself (a one-time calibration, not a continuously
        drifting one - actual driving would otherwise slowly drag the
        "zero" line to wherever the car has spent the most time), only
        the scale watermark, which only ever grows.
        """

        if self._gsensor_baseline is None:
            if self._gsensor_calibration_start is None:
                self._gsensor_calibration_start = now
            self._gsensor_calibration_buffer.append(sample)

            elapsed = now - self._gsensor_calibration_start
            if elapsed >= GSENSOR_CALIBRATION_SECONDS:
                buffered = self._gsensor_calibration_buffer
                self._gsensor_baseline = (
                    statistics.median(s.front_rear for s in buffered),
                    statistics.median(s.left_right for s in buffered),
                    statistics.median(s.upper_lower for s in buffered),
                )
                self._gsensor_calibration_buffer = []
            return

        baseline_fr, baseline_lr, baseline_ul = self._gsensor_baseline
        self._gsensor_max_deviation = max(
            self._gsensor_max_deviation,
            abs(sample.front_rear - baseline_fr),
            abs(sample.left_right - baseline_lr),
            abs(sample.upper_lower - baseline_ul),
        )

    def gsensor_baseline(self) -> tuple[float, float, float] | None:
        """The (front_rear, left_right, upper_lower) reading treated as
        "zero" - None until GSENSOR_CALIBRATION_SECONDS of readings
        have been collected. gsensor_stream.py's renderer holds off on
        drawing a trace at all until this stops being None, the same
        way it already does for "fewer than two samples yet"."""

        with self._lock:
            return self._gsensor_baseline

    def gsensor_max_deviation(self) -> float:
        """The largest |reading - baseline| seen, on any axis, since
        calibration finished - monotonically non-decreasing for the
        rest of the session (see _update_gsensor_calibration()'s own
        docstring), 0.0 before calibration finishes. gsensor_stream.py
        applies its own padding/minimum-scale floor on top of this -
        this is the raw watermark only."""

        with self._lock:
            return self._gsensor_max_deviation

    def _trim(self, samples: deque, now: float) -> None:
        cutoff = now - self._history_seconds
        while samples and samples[0].at < cutoff:
            samples.popleft()

    def latest_gps(self) -> GpsSample | None:
        """The most recent GPS reading, or None if none has arrived
        yet (e.g. the camera has no fix at all right now, or bv-live
        only just started)."""

        with self._lock:
            return self._gps[-1] if self._gps else None

    def gps_history(self, seconds: float | None = None) -> tuple[GpsSample, ...]:
        """Readings from the last `seconds` (or every buffered
        reading, up to `history_seconds`, if None) - map_stream.py
        uses this to draw the live route trail behind the current
        position."""

        with self._lock:
            if seconds is None:
                return tuple(self._gps)
            cutoff = time.monotonic() - seconds
            return tuple(sample for sample in self._gps if sample.at >= cutoff)

    def gsensor_history(self, seconds: float) -> tuple[GSensorSample, ...]:
        """Readings from the last `seconds` - gsensor_stream.py's own
        rolling-window strip chart draws exactly this slice, redrawn
        fresh on every frame."""

        with self._lock:
            cutoff = time.monotonic() - seconds
            return tuple(sample for sample in self._gsensor if sample.at >= cutoff)


def _drain_livedata_buffer(buffer: str, state: TelemetryState) -> str:
    """Consume every complete GPS/g-sensor object currently sitting in
    `buffer`, feeding each into `state`, and return whatever's left
    over (an incomplete trailing object, or multipart framing noise)
    for the next read to extend.

    A pure function of (buffer, state) rather than a method on
    LiveTelemetryPump - it needs no thread/socket state of its own,
    which makes it trivial to unit test without any real network
    connection or background thread involved.

    Each pass picks whichever of the two objects (GPS or g-sensor)
    starts *earliest* in the buffer, not just whichever pattern
    happens to be tried first - truncating to `buffer[end:]` discards
    everything before that match too (framing noise ahead of it, which
    is fine to drop), so consuming the later of two matches first
    would silently discard the earlier, not-yet-processed one instead
    of leaving it for the next pass. See parser/livedata.py's
    find_next_gps()/find_next_gsensor_reading() for why they return a
    start index at all.
    """

    while True:
        gps = find_next_gps(buffer)
        gsensor = find_next_gsensor_reading(buffer)

        if gps is None and gsensor is None:
            break

        gps_starts_first = gsensor is None or (
            gps is not None and gps[1] <= gsensor[1]
        )

        if gps_starts_first:
            (latitude, longitude), _start, end = gps
            state.add_gps(latitude, longitude)
        else:
            (front_rear, left_right, upper_lower), _start, end = gsensor
            state.add_gsensor(front_rear, left_right, upper_lower)

        buffer = buffer[end:]

    if len(buffer) > MAX_BUFFER_CHARS:
        buffer = ""

    return buffer


class LiveTelemetryPump:
    """Background thread continuously reading blackvue_livedata.cgi
    and feeding a TelemetryState - see this module's own docstring for
    why it runs for the whole process's lifetime rather than per
    -viewer.

    Reconnects automatically (after RECONNECT_DELAY_SECONDS) if the
    stream drops - a real camera's WiFi link can blip mid-session, and
    this should keep trying rather than silently going stale forever
    with no way to recover short of restarting bv-live itself.
    """

    def __init__(self, client: BlackVueClient, state: TelemetryState) -> None:
        self._client = client
        self._state = state
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        self._thread = threading.Thread(
            target=self._run, name="bv-live-telemetry", daemon=True
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                self._read_once()
            except OSError:
                # Connection refused/reset/timed out, DNS hiccup, etc.
                # - the camera going briefly unreachable is a normal,
                # expected condition (WiFi range, a reboot), not a bug
                # to crash the pump over. Falls through to the
                # reconnect delay below and tries again.
                pass

            if not self._stop.is_set():
                self._stop.wait(RECONNECT_DELAY_SECONDS)

    def _read_once(self) -> None:
        response = self._client.open_stream("/blackvue_livedata.cgi")
        try:
            buffer = ""
            while not self._stop.is_set():
                chunk = response.read(READ_CHUNK_BYTES)
                if not chunk:
                    break
                buffer += chunk.decode("utf-8", errors="replace")
                buffer = _drain_livedata_buffer(buffer, self._state)
        finally:
            response.close()
