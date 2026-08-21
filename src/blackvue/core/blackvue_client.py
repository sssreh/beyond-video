"""
BlackVue client.

Copyright (C) 2026 Christer R. (sssreh)

SPDX-License-Identifier: GPL-3.0-or-later
"""

from __future__ import annotations

from collections.abc import Callable
from collections.abc import Sequence
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request
from urllib.request import urlopen

from ..domain.live_gps_fix import LiveGpsFix
from ..domain.vod_entry import VodEntry
from ..parser.livedata import parse_gps_fix

# The camera's own single-letter direction codes, as used directly in
# blackvue_live.cgi's ?direction= query string (Front/Rear/Interior) -
# distinct from the lowercase "front"/"rear"/"interior" convention
# archive_browser.py's own _THUMBNAIL_ASSET_BY_DIRECTION uses for
# already-downloaded footage, since this one has to match the wire
# protocol exactly, not this repo's own naming. "O" (Optional/a 4th
# camera some models have) is a real code too, confirmed alongside F/
# R/I in scan_blackvue_endpoints.py's probing, but left out of the
# default set here - Christer's own request was specifically "camera
# F, R and I".
SNAPSHOT_DIRECTIONS: tuple[str, ...] = ("F", "R", "I")

# blackvue_livedata.cgi never closes its own connection - it's a
# never-ending multipart/x-mixed-replace stream, the same shape as
# blackvue_live.cgi's MJPEG feed but for GPS/g-sensor JSON instead of
# JPEG frames (see WORKING_CONTEXT.md's bv-gps entry). live_gps()
# reads it in bounded chunks and returns as soon as one full GPS
# object has been seen, rather than trying to read the response to
# completion - a plain `.read()` here would hang forever. This caps
# how much of the stream we're willing to read before giving up if no
# GPS object ever appears (a livedata.cgi that's serving something
# unexpected, or is g-sensor-only for an implausibly long stretch).
LIVE_GPS_MAX_BYTES = 65536

# snapshot()'s own equivalent cap for blackvue_live.cgi - see
# _read_one_mjpeg_frame()'s docstring for why this exists at all (the
# short version: blackvue_live.cgi never closes either, same as
# blackvue_livedata.cgi above, so an unbounded read hangs forever - a
# real bug shipped in the first cut of this feature and confirmed by
# Christer: "looks like both commands hang"). Generous headroom over a
# realistic dashcam JPEG frame (typically well under 500KB) while
# still bounding how much of a stream that never ends we'll read
# before giving up on ever seeing one complete frame.
SNAPSHOT_MAX_BYTES = 4 * 1024 * 1024

# How many frames _read_one_mjpeg_frame() throws away before capturing
# the one it actually returns - Christer, after trying the feature:
# "I know sometimes when you switch direction it shows the previous
# direction for a short while." That's a real behavior of
# blackvue_live.cgi itself, not something this repo's HTTP layer
# introduces: bv-live's own live view (live/app.py's stream_camera())
# opens an equally fresh connection per direction change with no
# warm-up of its own, and shows the same brief stale-frame transition
# - so the camera's shared video encoder apparently needs a moment to
# actually reconfigure to the requested lens even on a brand new
# connection, and the very first frame(s) served can still be whatever
# direction was live immediately before this request. Discarding a
# couple of frames before capturing costs a small amount of latency
# per direction (bounded by real frame arrivals, not a fixed sleep)
# but isn't provably enough on every camera/firmware - this can't be
# verified without real hardware, so treat this default as a starting
# point to confirm (and retune if needed) against real hardware, not a
# guaranteed fix.
SNAPSHOT_WARMUP_FRAMES = 2


class NoGpsDataError(RuntimeError):
    """Raised when blackvue_livedata.cgi's response never yielded a
    GPS reading within LIVE_GPS_MAX_BYTES - a protocol-level failure,
    distinct from LiveGpsFix.has_fix being False (a normal reading
    that just says "no fix currently")."""


class BlackVueClient:
    """Client for communicating with a BlackVue camera."""

    def __init__(
        self,
        base_url: str,
        timeout: int = 5,
    ) -> None:
        """Initialize a BlackVue client."""

        self._base_url = base_url.rstrip("/")
        self._timeout = timeout

    def _get(self, path: str) -> bytes:
        """Fetch raw data from the camera."""

        url = f"{self._base_url}{path}"

        try:
            with urlopen(url, timeout=self._timeout) as response:
                return response.read()

        except HTTPError as exc:
            raise RuntimeError(
                f"Unable to fetch {path}"
            ) from exc

    def vod(self) -> str:
        """Return the raw VOD response."""

        return self._get("/blackvue_vod.cgi").decode("utf-8")

    def config(self) -> str:
        """Return the raw configuration."""

        return self._get("/Config/config.ini").decode("utf-8")

    def _read_one_mjpeg_frame(self, path: str, *, discard: int = 0) -> bytes:
        """Read one JPEG frame out of blackvue_live.cgi's own
        multipart/x-mixed-replace MJPEG stream and return its raw
        bytes - throwing away `discard` complete frames first before
        capturing the one that's actually returned.

        blackvue_live.cgi never closes its own connection - it's a
        never-ending multipart stream, the same shape as
        blackvue_livedata.cgi's own GPS feed (see live_gps()'s own
        docstring and LIVE_GPS_MAX_BYTES above). snapshot()'s first
        cut called `_get()` here, which does a plain whole-response
        `.read()` - that never returns on a stream that never closes,
        which is exactly what made both bv-snap and bv-gps --snap
        hang (Christer: "looks like both commands hang"). This reads
        in bounded chunks instead, looks for the per-part
        `Content-Length: N` header multipart/x-mixed-replace puts
        before each frame's body, and returns as soon as that many
        bytes of image data have arrived for the frame it's keeping -
        the rest of the stream is never read. The response is always
        closed before returning, same as `_get()`'s own `with
        urlopen(...)` behavior.

        `discard` exists for SNAPSHOT_WARMUP_FRAMES - see that
        constant's own comment for why: the first frame(s) served
        right after a direction change can still be showing whatever
        direction was live before this request.
        """

        url = f"{self._base_url}{path}"
        frames_needed = discard + 1
        frames_seen = 0

        try:
            with urlopen(url, timeout=self._timeout) as response:
                buffer = b""
                total_read = 0

                while total_read < SNAPSHOT_MAX_BYTES:
                    chunk = response.read(4096)
                    if not chunk:
                        break
                    buffer += chunk
                    total_read += len(chunk)

                    # Drain every complete frame already sitting in
                    # the buffer before reading more off the network -
                    # a single chunk is often bigger than one frame
                    # (frames are typically well under 4096 bytes), so
                    # with discard > 0 more than one complete frame
                    # can already be present here. Without this inner
                    # loop, a discarded frame's leftover bytes just
                    # sit unparsed until response.read() happens to
                    # return more data - and once the camera has
                    # already sent everything it's going to send in
                    # this chunk, that next read() returns empty and
                    # looks like the stream ended, even though the
                    # frame being kept was already fully buffered.
                    while True:
                        header_end = buffer.find(b"\r\n\r\n")
                        if header_end == -1:
                            break

                        length = None
                        header_text = buffer[:header_end].decode(
                            "ascii", errors="replace"
                        )
                        for line in header_text.splitlines():
                            if line.lower().startswith("content-length:"):
                                length = int(line.split(":", 1)[1].strip())
                                break

                        if length is None:
                            # No Content-Length seen yet in what's
                            # arrived so far - wait for more bytes
                            # rather than treating a still-incomplete
                            # header block as fatal.
                            break

                        body_start = header_end + 4
                        if len(buffer) < body_start + length:
                            break

                        frame = buffer[body_start : body_start + length]
                        buffer = buffer[body_start + length :]
                        frames_seen += 1

                        if frames_seen >= frames_needed:
                            return frame

                        # Still within the warm-up window - this
                        # frame's already been dropped from buffer
                        # above; loop back around to look for the
                        # next complete frame already buffered.

        except HTTPError as exc:
            raise RuntimeError(f"Unable to fetch {path}") from exc

        raise RuntimeError(
            f"no complete frame received from {path} within "
            f"{SNAPSHOT_MAX_BYTES} bytes"
        )

    def snapshot(
        self, directions: Sequence[str] = SNAPSHOT_DIRECTIONS
    ) -> dict[str, bytes]:
        """Grab one live JPEG frame per camera direction (F/R/I by
        default) via a single bounded read per direction - Christer:
        "I would like to have a snap function that takes 1 snapshot
        for camera F, R and I."

        Each direction is independent: a request that errors, or
        that comes back with an empty body (this repo's own firmware-
        endpoint scan - see scan_blackvue_endpoints.py and
        WORKING_CONTEXT.md - confirmed a "Valid" HTTP response for
        direction=I on Christer's hardware, but never actually
        displayed a real image for it) is silently dropped from the
        result rather than failing the whole call, so a camera model
        without a working Interior lens still returns Front/Rear.
        Callers can tell exactly which directions failed by comparing
        `directions` against this dict's keys - nothing is lost by
        not raising here.

        Each direction also discards SNAPSHOT_WARMUP_FRAMES frames
        before capturing - see that constant's own comment: the first
        frame(s) after a direction change can still be showing the
        previous direction for a short while (Christer, confirmed
        from using the feature).
        """

        results: dict[str, bytes] = {}

        for direction in directions:
            try:
                data = self._read_one_mjpeg_frame(
                    f"/blackvue_live.cgi?direction={direction}",
                    discard=SNAPSHOT_WARMUP_FRAMES,
                )
            except RuntimeError:
                continue

            if data:
                results[direction] = data

        return results

    def live_gps(self) -> LiveGpsFix:
        """Return the camera's current GPS reading, read live from
        blackvue_livedata.cgi.

        Raises NoGpsDataError if no GPS object appears within
        LIVE_GPS_MAX_BYTES of the stream. A GPS object that does
        appear but reads (0.0, 0.0) is returned normally as a
        LiveGpsFix with has_fix False, not treated as an error - see
        LiveGpsFix.has_fix's own docstring for why.
        """

        url = f"{self._base_url}/blackvue_livedata.cgi"

        with urlopen(url, timeout=self._timeout) as response:
            buffer = b""

            while len(buffer) < LIVE_GPS_MAX_BYTES:
                chunk = response.read(4096)

                if not chunk:
                    break

                buffer += chunk
                fix = parse_gps_fix(buffer.decode("utf-8", errors="replace"))

                if fix is not None:
                    latitude, longitude = fix
                    return LiveGpsFix(latitude=latitude, longitude=longitude)

        raise NoGpsDataError(
            "no GPS reading found in blackvue_livedata.cgi's response "
            f"within {LIVE_GPS_MAX_BYTES} bytes"
        )

    def open_stream(self, path: str):
        """Open a raw, long-lived streaming connection to `path` on
        the camera (e.g. blackvue_live.cgi's MJPEG feed, or
        blackvue_livedata.cgi's telemetry feed) and return the raw
        urlopen() response for the caller to .read() from in chunks
        and close() when done.

        Neither of these camera endpoints ever closes its own
        connection (see live_gps()'s own docstring). Unlike
        live_gps(), which reads a single bounded slice and returns one
        parsed reading, this is the lower-level primitive bv-live's
        continuous camera-passthrough relay and telemetry pump (see
        blackvue.live.mjpeg/blackvue.live.telemetry) use to keep a
        stream open for as long as they want it, reading and parsing
        indefinitely rather than stopping after the first result.
        """

        url = f"{self._base_url}{path}"

        return urlopen(url, timeout=self._timeout)

    def probe(self, path: str) -> bool:
        """Return True if a plain GET to `path` succeeds, False if the
        camera responds with an HTTP error (typically 404 - meaning
        the file doesn't exist there).

        Used to opportunistically check for files a camera's own
        blackvue_vod.cgi listing doesn't mention but still serves
        directly at a predictable path - see
        BlackVueCamera.probe_missing_sidecars()'s own docstring for
        why that's needed at all (confirmed necessary on a real
        BlackVue Elite 10 - see WORKING_CONTEXT.md). Deliberately a
        real GET, not a HEAD: HEAD support hasn't been confirmed
        against every camera's embedded web server, where a plain GET
        already has. The files this is used for are small metadata,
        not video, so reading (and discarding) the body here costs
        nothing.

        Network-level failures (unreachable, timeout) are not caught -
        they propagate, since those mean something is actually wrong,
        not just "this file doesn't exist here".
        """

        try:
            self._get(path)
        except RuntimeError:
            return False

        return True

    def size(self, entry: VodEntry) -> int:
        """Return the size of a remote file."""

        request = Request(
            f"{self._base_url}{entry.path.as_posix()}",
            method="HEAD",
        )

        with urlopen(request, timeout=self._timeout) as response:
            return int(response.headers["Content-Length"])

    def download(
        self,
        entry: VodEntry,
        destination: Path,
        *,
        on_bytes: Callable[[int], None] | None = None,
    ) -> bool:
        """Download one file.

        If on_bytes is given, it's called with the number of bytes
        written for each chunk actually downloaded (video files
        download in 64KB chunks; metadata files download in one
        shot) - used by bv-download --trace for a simple progress
        indicator on long downloads.

        Returns True if bytes were downloaded.
        """

        destination.parent.mkdir(
            parents=True,
            exist_ok=True,
        )

        #
        # Metadata files are never resumed.
        #
        if not entry.is_video:
            if destination.exists():
                return False

            data = self._get(entry.path.as_posix())
            destination.write_bytes(data)

            if on_bytes is not None:
                on_bytes(len(data))

            return True

        #
        # Video files support resume.
        #
        remote_size = self.size(entry)

        if destination.exists():
            local_size = destination.stat().st_size

            if local_size == remote_size:
                return False

            if local_size > remote_size:
                destination.unlink()
                local_size = 0
        else:
            local_size = 0

        request = Request(
            f"{self._base_url}{entry.path.as_posix()}",
        )

        mode = "wb"

        if local_size:
            request.add_header(
                "Range",
                f"bytes={local_size}-",
            )
            mode = "ab"

        with (
            urlopen(request, timeout=self._timeout) as response,
            destination.open(mode) as file,
        ):
            while chunk := response.read(64 * 1024):
                file.write(chunk)

                if on_bytes is not None:
                    on_bytes(len(chunk))

        return True
    