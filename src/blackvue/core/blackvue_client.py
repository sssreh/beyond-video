"""
BlackVue client.

Copyright (C) 2026 Christer R. (sssreh)

SPDX-License-Identifier: GPL-3.0-or-later
"""

from __future__ import annotations

from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request
from urllib.request import urlopen

from ..domain.vod_entry import VodEntry


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
    ) -> bool:
        """Download one file.

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

        return True
    