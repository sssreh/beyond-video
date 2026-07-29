"""
BlackVue client.

Copyright (C) 2026 Christer R. (sssreh)

SPDX-License-Identifier: GPL-3.0-or-later
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request
from urllib.request import urlopen

from ..domain.live_gps_fix import LiveGpsFix
from ..domain.vod_entry import VodEntry
from ..parser.livedata import parse_gps_fix

# blackvue_livedata.cgi never closes its own connection - it's a
# never-ending multipart/x-mixed-replace stream, the same shape as
# blackvue_live.cgi's MJPEG feed but for GPS/g-sensor JSON instead of
# JPEG frames (see WORKING_CONTEXT.md's bv-live entry). live_gps()
# reads it in bounded chunks and returns as soon as one full GPS
# object has been seen, rather than trying to read the response to
# completion - a plain `.read()` here would hang forever. This caps
# how much of the stream we're willing to read before giving up if no
# GPS object ever appears (a livedata.cgi that's serving something
# unexpected, or is g-sensor-only for an implausibly long stretch).
LIVE_GPS_MAX_BYTES = 65536


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

    def snapshot(self) -> tuple[bytes, bytes]:
        """Return front and rear snapshots."""

        front = self._get("/blackvue_live.cgi?direction=F")
        rear = self._get("/blackvue_live.cgi?direction=R")

        return front, rear

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
    