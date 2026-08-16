"""
Camera adapter interface.

See docs/CAMERA_ADAPTERS.md for the full design. Short version: exactly
one adapter is active per camera config at a time (a plugin model, not a
multi-adapter fan-out - see the doc's "exactly one active adapter at a
time" section). Each adapter pairs a declarative manifest.json (this
package's manifest.py) with a small class implementing the handful of
things a manifest genuinely can't express - sidecar parsing, the camera's
own network protocol, config-snapshot parsing. CameraAdapter below is
that class's shape.

STATUS: interface only, as of this commit. adapters/blackvue/adapter.py
is the first (and so far only) implementation, built as a pure
delegation wrapper around the existing core/parser/telemetry/archive
code - see that module's own docstring. Nothing yet calls
registry.get_adapter() from a real bv-* command; CameraConfig.adapter
exists and round-trips (core/camera_config.py) but isn't read by
anything yet either. Both are queued next per docs/CAMERA_ADAPTERS.md's
"Suggested next steps".

Not every code_hooks_required entry in a manifest gets its own method
here. `camera_client`/`config_snapshot_parser`/`sidecar_prober` are
bv-download's concern - a later build step per docs/CAMERA_ADAPTERS.md,
not exercised by any wired-up command yet - but connect()/
config_snapshot() are declared now so BlackVueAdapter's delegation
wrapper has an honest home for the equivalent existing code (core/
connection.py's connect(), archive/configuration.py's
parse_record_time_seconds()) today, rather than needing a second
interface change later once bv-download actually gets wired through
this.
"""

from __future__ import annotations

from pathlib import Path
from typing import Protocol
from typing import runtime_checkable

from ..archive.archive import Archive
from ..archive.recording import Recording
from ..archive.recording_id import RecordingId
from ..core.endpoint import Endpoint
from ..telemetry.gps_reader import GpsFix
from ..telemetry.gsensor_reader import GSensorSample
from .manifest import AdapterManifest


class AdapterCapabilityError(NotImplementedError):
    """Raised when a CameraAdapter method is called for a capability its
    own manifest declares unsupported (e.g. connect() on an adapter whose
    manifest.capabilities["network_connect"] is False) - a clear, named
    error instead of an AttributeError/silent no-op. Callers that offer
    an optional feature should check `adapter.manifest.supports(...)`
    themselves and skip the call entirely rather than relying on this
    exception for control flow; it exists for the case where that check
    was missed.
    """


@runtime_checkable
class CameraAdapter(Protocol):
    """Structural interface every camera adapter implements.

    A CameraAdapter instance is always paired 1:1 with the
    AdapterManifest it was built from (`self.manifest`) - callers should
    check `adapter.manifest.supports("gps")` (etc.) before calling a
    capability-gated method rather than catching AdapterCapabilityError,
    the same way docs/CAMERA_ADAPTERS.md's manifest section describes
    capability flags being used.
    """

    manifest: AdapterManifest

    def open_archive(self, path: Path) -> Archive:
        """Scan `path` (per `self.manifest.archive_layout` - flat or
        recursive) and return an Archive-shaped object: a `.recordings`
        list[Recording] and a `.configuration(recording) ->
        Configuration` lookup. Returns the project's real `Archive`
        class today (BlackVueAdapter delegates to it directly); a
        future adapter with a genuinely different scan strategy (e.g.
        FolderAdapter's recursive walk) only needs to return something
        duck-type-compatible with those same two members, not this
        exact class - see docs/CAMERA_ADAPTERS.md's FolderAdapter
        discussion.
        """
        ...

    def find_recording(self, path: Path, recording_id: RecordingId) -> Recording | None:
        """Resolve a single recording by id within the archive at
        `path`, or None if it doesn't exist - a targeted lookup for
        callers that already know the id they want (bv-web's archive
        browser: one call per thumbnail request and per HTTP range
        request while a video plays) and would otherwise pay
        open_archive()'s full-archive scan cost on every single one.

        BlackVueAdapter delegates to ArchiveReader.read_recording(),
        which resolves a recording via a fixed number of direct
        stat()s (see that method's own docstring for why that matters
        at scale) rather than open_archive()'s full directory scan.
        An adapter without an equally targeted lookup (e.g.
        FolderAdapter, whose ids aren't derivable from a filename
        alone) may fall back to open_archive(path) filtered by id -
        correct, just O(archive size) per call, matching the
        recursive_scanner code hook already declared for that kind of
        adapter.
        """
        ...

    def read_gps(self, path: Path) -> tuple[GpsFix, ...]:
        """Parse a GPS sidecar file at `path` into fixes, oldest first.

        Only meaningful when `self.manifest.supports("gps")` -
        AdapterCapabilityError otherwise.
        """
        ...

    def read_gsensor(self, path: Path) -> tuple[GSensorSample, ...]:
        """Parse a g-sensor sidecar file at `path` into samples, oldest
        first.

        Only meaningful when `self.manifest.supports("gsensor")` -
        AdapterCapabilityError otherwise.
        """
        ...

    def connect(
        self, endpoints: list[Endpoint], timeout: int = 5
    ) -> tuple[Endpoint, object]:
        """Reach the camera over the network - try each endpoint in
        order, return `(endpoint, client)` for the first that answers.

        Only meaningful when `self.manifest.supports("network_connect")`
        - AdapterCapabilityError otherwise (e.g. every folder-style
        adapter, which has nothing to connect to).
        """
        ...

    def config_snapshot_seconds(self, config_text: str) -> int:
        """Parse this adapter's camera-config text (BlackVue's
        config.ini) into a nominal per-recording segment length in
        seconds, the way archive/configuration.py's
        parse_record_time_seconds() does today.

        Only meaningful when `self.manifest.supports("config_snapshot")`
        - AdapterCapabilityError otherwise (adapters without this
        capability rely on `self.manifest.default_trip_gap_seconds`
        instead - see docs/CAMERA_ADAPTERS.md).
        """
        ...
