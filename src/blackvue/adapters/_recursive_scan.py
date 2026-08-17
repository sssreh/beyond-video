"""
Shared recursive-folder scanning logic.

`FolderAdapter` (generic video folder, no embedded telemetry) and
`GoProAdapter` (GPMF telemetry embedded in the video stream itself)
both scan a recursive folder tree of ordinary video files the exact
same way: resolve each video's timestamp via ffprobe's embedded
`creation_time` tag first, file mtime second; synthesize a
`RecordingId` in BlackVue's own "YYYYMMDD_HHMMSS_K" shape from that
timestamp (see `assign_recording_ids()`'s own docstring for why);
store the single video under `Asset.FRONT`; pick up generated-asset
files (transcript, subtitles, ...) per the calling adapter's own
`manifest.asset_suffix_table`, checked both same-stem next to the
video and id-named at the archive root (see `generated_assets_for()`'s
own docstring for why both). The two adapters differ
only in their manifest (video extensions, kind code, generated-asset
suffix table - both currently identical, but not guaranteed to stay
that way) and in whether `read_gps()`/`read_gsensor()` return real
telemetry or `AdapterCapabilityError` - this module holds the part
that's identical between them.

Factored out here per Christer's steer on the open design question
from the GoPro adapter's own design pass: "Share them as long as it
is possible, in worst case you make a branch later." If a third
adapter of this shape (e.g. a DJI drone's per-clip `.srt` telemetry)
ever needs a genuinely different scan strategy, that's the branch -
nothing about this module assumes there will only ever be two callers.

Leading underscore: this is shared internal machinery, not an adapter
in its own right - it has no `manifest.json`/registry entry of its
own, and nothing outside `adapters/` should import it directly.
"""

from __future__ import annotations

import json
import subprocess
from collections.abc import Callable
from datetime import datetime
from datetime import timedelta
from pathlib import Path

from ..archive.archive import Archive
from ..archive.asset import Asset
from ..archive.asset_file import AssetFile
from ..archive.configuration import Configuration
from ..archive.recording import Recording
from ..archive.recording_id import RecordingId
from .manifest import AdapterManifest


def _probe_creation_time(path: Path) -> datetime | None:
    """Return a video's embedded `format.tags.creation_time` (ffprobe),
    or None if ffprobe is missing, fails, the tag isn't present, or the
    tag's value doesn't parse as an ISO-8601 timestamp - any of which
    just means "fall back to file mtime" to this module's caller, not
    an error worth surfacing on its own (a folder of ordinary videos
    routinely has some files with no creation_time metadata at all,
    e.g. after being re-encoded or copied through a tool that drops
    it)."""

    try:
        result = subprocess.run(
            [
                "ffprobe", "-v", "error",
                "-show_entries", "format_tags=creation_time",
                "-of", "json",
                str(path),
            ],
            capture_output=True,
            text=True,
            check=True,
        )
    except (FileNotFoundError, subprocess.CalledProcessError):
        return None

    try:
        tags = json.loads(result.stdout)["format"]["tags"]
        raw = tags["creation_time"]
    except (KeyError, ValueError, json.JSONDecodeError):
        return None

    # ffprobe reports creation_time as UTC ISO-8601, typically with a
    # trailing "Z" - datetime.fromisoformat() (3.11+) accepts that
    # directly, but this project's own minimum is broader, so normalize
    # "Z" to the explicit "+00:00" offset fromisoformat() has always
    # accepted.
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00")).replace(tzinfo=None)
    except ValueError:
        return None


def _resolve_timestamp(
    path: Path,
    *,
    telemetry_timestamp: Callable[[Path], datetime | None] | None = None,
) -> datetime:
    """Resolve a video's recording timestamp: ffprobe's own
    creation_time metadata first, then `telemetry_timestamp(path)` (if
    the caller passed one) as a second, still-real-capture-time
    fallback, then the file's mtime as the last resort - see
    _probe_creation_time()'s docstring for why the first step so
    often falls through on a real, mixed-source folder of videos.

    `telemetry_timestamp` is an adapter-supplied hook (GoProAdapter
    passes gpmf.first_creation_time; FolderAdapter has no embedded
    telemetry to offer, so passes None and this always falls straight
    through to mtime) rather than something this shared module knows
    how to compute itself - see module docstring on why the scanning
    logic is shared but each adapter's own telemetry format isn't.
    mtime reflects when a file was last written, which for a copied-
    off-the-card or downloaded clip is when that copy happened, not
    when the video was actually recorded - a real report from
    Christer: some of his GoPro archive's synthetic recording ids
    landed on download time instead of capture time, which risks two
    genuinely different clips colliding into the same id if the
    ffprobe metadata a file would otherwise resolve from is missing
    (e.g. after a re-encode). A telemetry-derived timestamp is real
    device-clock data recorded at the moment of capture, so it's tried
    before falling all the way back to mtime."""

    creation_time = _probe_creation_time(path)
    if creation_time is not None:
        return creation_time

    if telemetry_timestamp is not None:
        telemetry_time = telemetry_timestamp(path)
        if telemetry_time is not None:
            return telemetry_time

    return datetime.fromtimestamp(path.stat().st_mtime)


def scan_video_files(root: Path, extensions: frozenset[str]) -> list[Path]:
    """Recursively collect every file under `root` whose extension is
    one of `extensions` - sorted for deterministic, reproducible scan
    order (os.walk()'s own order is filesystem-dependent and not
    something callers, or assign_recording_ids()'s own collision-
    disambiguation, should rely on)."""

    return sorted(
        path
        for path in root.rglob("*")
        if path.is_file() and path.suffix.lower() in extensions
    )


def assign_recording_ids(
    videos: list[tuple[Path, datetime]], kind_code: str
) -> list[tuple[Path, RecordingId]]:
    """Turn (video path, resolved timestamp) pairs into (video path,
    RecordingId) pairs, one second apart at minimum.

    RecordingId's "YYYYMMDD_HHMMSS_K" shape only has one-second
    resolution, so two videos resolving to the same wall-clock second
    (two phones filming the same moment, clips copied from multiple
    sources into one folder) would otherwise collide and silently
    overwrite one another in the recordings dict. Ids are assigned in
    (already sorted) timestamp order, bumping any id that would repeat
    forward by one second until it's unique - an ordering
    approximation for the rare collision case, not a claim that the
    bumped second is when that clip actually happened.

    `kind_code` is the caller's single kind_vocabulary/
    direction_vocabulary code (BlackVue-shape ids need exactly one
    letter there) - "V" for both FolderAdapter and GoProAdapter today,
    but not hardcoded here so a future caller with its own single code
    doesn't need a copy of this function just to change one letter.
    """

    assigned: list[tuple[Path, RecordingId]] = []
    seen: set[str] = set()

    for path, timestamp in sorted(videos, key=lambda item: (item[1], item[0])):
        candidate = timestamp
        value = f"{candidate:%Y%m%d_%H%M%S}_{kind_code}"
        while value in seen:
            candidate = candidate + timedelta(seconds=1)
            value = f"{candidate:%Y%m%d_%H%M%S}_{kind_code}"

        seen.add(value)
        assigned.append((path, RecordingId(value)))

    return assigned


def generated_assets_for(
    video_path: Path,
    manifest: AdapterManifest,
    *,
    root: Path,
    recording_id: RecordingId,
) -> dict[Asset, AssetFile]:
    """Return every generated-asset sibling file that actually exists
    for this recording - checked in two places, same-stem next to
    `video_path` first, then `root/<recording_id>.<suffix>`, per
    `manifest.asset_suffix_table`.

    Same-stem was this function's only check for a long time (see
    module docstring's own "same-stem rather than recording-id-keyed"
    framing), which quietly assumed bv-generate would write its output
    that way too. It doesn't: every bv-generate write site builds its
    destination as `archive_path / f"{recording.id}.<suffix>"` - a
    flat path at the *archive root*, keyed by the synthetic recording
    id, matching how BlackVue's own already-flat, already-id-named
    archives work. For a recursive-scan adapter (folder/gopro), the
    video itself usually lives several directories deep (e.g.
    `DCIM/100GOPRO/GH010123.MP4`), so a real bv-generate output landed
    at the archive root was invisible here - confirmed by a real
    report from Christer: `bv-generate` assets existed on disk for his
    GoPro archive, but `bv-ls` showed none of them. Checking the root
    location too (without removing the same-stem check some future
    non-bv-generate tool might still rely on) fixes that without
    needing any change on bv-generate's side."""

    assets: dict[Asset, AssetFile] = {}
    stem_path = video_path.with_suffix("")

    for entry in manifest.asset_suffix_table:
        same_stem_candidate = Path(str(stem_path) + entry.suffix)
        root_candidate = root / f"{recording_id}{entry.suffix}"

        if same_stem_candidate.is_file():
            candidate = same_stem_candidate
        elif root_candidate.is_file():
            candidate = root_candidate
        else:
            continue

        asset = Asset[entry.asset]
        assets[asset] = AssetFile(asset=asset, path=candidate)

    return assets


class RecursiveFolderArchive:
    """Archive-shaped container for a recursive-folder scan - see
    base.py's CameraAdapter.open_archive() docstring on duck-type
    compatibility: bv-ls/bv-web only ever touch `.recordings` and
    `.configuration(recording)`, both provided here without going
    through the BlackVue-specific Archive/ArchiveReader classes at
    all.

    `.configuration()` always returns Configuration.fallback() rather
    than looking for a camera-native config snapshot - every adapter
    this module serves declares config_snapshot support False (there's
    no config.ini equivalent for a folder of videos, GoPro or
    otherwise), so default_trip_gap_seconds is always the right answer
    here, not a degraded condition worth Archive's usual one-time
    warning about.
    """

    def __init__(self, recordings: list[Recording]):
        self._recordings = recordings
        self._fallback = Configuration.fallback()

    @property
    def recordings(self) -> list[Recording]:
        return self._recordings

    @property
    def configurations(self) -> list[Configuration]:
        return []

    def configuration(self, recording: Recording) -> Configuration:
        return self._fallback

    def __iter__(self):
        return iter(self._recordings)

    def __len__(self):
        return len(self._recordings)

    def __getitem__(self, index):
        return self._recordings[index]


def _scan(
    path: Path,
    manifest: AdapterManifest,
    kind_code: str,
    *,
    telemetry_timestamp: Callable[[Path], datetime | None] | None = None,
) -> list[Recording]:
    """Full recursive scan of `path`, returning Recording objects
    sorted by id - shared by scan_recursive_archive() and
    find_recording_in_recursive_archive() (which just filters this
    down to the one id it wants; see base.py's own docstring on why
    that's an accepted O(archive size) cost for this kind of
    adapter). `telemetry_timestamp` is passed straight through to
    _resolve_timestamp() - see its own docstring."""

    extensions = frozenset(manifest.video_extensions)
    video_paths = scan_video_files(path, extensions)
    videos_with_timestamps = [
        (p, _resolve_timestamp(p, telemetry_timestamp=telemetry_timestamp))
        for p in video_paths
    ]

    recordings = []
    for video_path, recording_id in assign_recording_ids(
        videos_with_timestamps, kind_code
    ):
        assets: dict[Asset, AssetFile] = {
            Asset.FRONT: AssetFile(asset=Asset.FRONT, path=video_path),
        }
        assets.update(
            generated_assets_for(
                video_path, manifest, root=path, recording_id=recording_id
            )
        )

        size = 0
        for asset_file in assets.values():
            try:
                size += asset_file.path.stat().st_size
            except OSError:
                pass

        recordings.append(Recording(id=recording_id, assets=assets, size=size))

    return sorted(recordings, key=lambda r: r.id)


def scan_recursive_archive(
    path: Path,
    manifest: AdapterManifest,
    kind_code: str,
    *,
    telemetry_timestamp: Callable[[Path], datetime | None] | None = None,
) -> RecursiveFolderArchive:
    """Full scan of `path` per `manifest` - the shared body of
    FolderAdapter.open_archive()/GoProAdapter.open_archive().
    `telemetry_timestamp` is passed straight through to _scan() - see
    _resolve_timestamp()'s own docstring."""

    return RecursiveFolderArchive(
        _scan(path, manifest, kind_code, telemetry_timestamp=telemetry_timestamp)
    )


def find_recording_in_recursive_archive(
    path: Path,
    recording_id: RecordingId,
    manifest: AdapterManifest,
    kind_code: str,
    *,
    telemetry_timestamp: Callable[[Path], datetime | None] | None = None,
) -> Recording | None:
    """Full rescan filtered by id - the shared body of
    FolderAdapter.find_recording()/GoProAdapter.find_recording(). No
    equivalent to ArchiveReader's targeted, fixed-stat-count lookup: a
    recursive-scan adapter's ids are computed at scan time from
    resolved timestamps, not derivable from a filename alone the way
    BlackVue's own id-embedding filename convention is.
    `telemetry_timestamp` is passed straight through to _scan() - see
    _resolve_timestamp()'s own docstring."""

    for recording in _scan(
        path, manifest, kind_code, telemetry_timestamp=telemetry_timestamp
    ):
        if recording.id == recording_id:
            return recording
    return None
