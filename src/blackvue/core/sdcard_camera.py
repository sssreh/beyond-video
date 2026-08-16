"""
SD-card camera.

Copyright (C) 2026 Christer R. (sssreh)

SPDX-License-Identifier: GPL-3.0-or-later
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from pathlib import PurePosixPath

from ..adapters.manifest import AdapterManifest
from ..domain.recording import Recording
from ..domain.vod_entry import VodEntry
from ..parser.vod import parse_timestamp

# The recording-kind letters BlackVue's own filenames use - kept as a
# local constant rather than importing bv_download.py's own ALL_KINDS
# (cli/ is a layer above core/; this module must not depend on it).
# Duplicated content, not duplicated meaning: this is the same fixed
# vocabulary domain/recording.py's is_normal/is_event/is_manual/
# is_parking/is_a properties already encode.
_KIND_LETTERS = "NEMPA"
_DIRECTION_LETTERS = "FRI"

# BlackVue's own on-camera filename convention - the same shape
# bv-download itself writes into an archive (see
# BlackVueClient.download()'s own `destination / entry.path.name`) and
# docs/CAMERA_ADAPTERS.md's "SD-card mirrors what bv-download already
# produces" note. Video and thumbnail files always carry a direction
# letter (front/rear/interior); .gps/.3gf sidecars never do - see
# BlackVueCamera.probe_missing_sidecars()'s own path construction for
# both shapes. Matched strictly (not just VodEntry.recording's own
# lenient stripping) specifically so a folder of arbitrarily-named
# files - like Christer's own emulated test card, see this module's
# own SdCardCamera docstring - is reliably recognized as "nothing
# here" rather than accidentally matching something it shouldn't.
_FILENAME_RE = re.compile(
    r"^\d{8}_\d{6}_(?P<kind>[" + _KIND_LETTERS + r"])"
    r"(?P<direction>[" + _DIRECTION_LETTERS + r"])?"
    r"\.(?P<ext>mp4|gps|3gf|thm)$",
    re.IGNORECASE,
)

_DIRECTIONAL_EXTENSIONS = frozenset({"mp4", "thm"})


def _matches_blackvue_filename(name: str) -> bool:
    """Return True if `name` looks like a real BlackVue-written
    filename - a 15-character timestamp, a recording-kind letter, an
    extension-appropriate direction letter (required for video/
    thumbnail, forbidden for .gps/.3gf), and a recognized extension.
    """

    match = _FILENAME_RE.match(name)

    if match is None:
        return False

    has_direction = match.group("direction") is not None
    needs_direction = match.group("ext").lower() in _DIRECTIONAL_EXTENSIONS

    return has_direction == needs_direction


def _matches_generic_video(name: str, extensions: frozenset[str]) -> bool:
    """Return True if `name` is a video file this adapter's manifest
    recognizes - used instead of _matches_blackvue_filename() when a
    manifest is given (see _scan()'s own docstring): any camera whose
    own on-camera filenames carry no BlackVue-style timestamp+kind
    convention at all (GoPro's GH010123.MP4 chapter+file-number scheme
    being the first real example - see manifest's own unsupported_notes
    on why there's no filename_pattern to match against instead).

    Extension-only, case-insensitive - `.MP4` and `.mp4` are the same
    file to a camera's own firmware. Hidden/AppleDouble files (leading
    `.`, e.g. macOS's own `._GH010123.MP4` shadow copies left behind by
    a Finder-mediated card copy) are excluded even though their
    extension would otherwise match - these were never written by the
    camera itself.
    """

    if name.startswith("."):
        return False

    return Path(name).suffix.lower() in extensions


@dataclass
class SdCardScanResult:
    """The result of scanning a mounted SD card / removable media
    folder for BlackVue-named files - see SdCardCamera's own
    docstring. `total_files_seen` and `recognized_file_count` exist
    purely for _run()'s own "N files scanned, 0 recognized" summary
    message (bv_download.py), not used by the download path itself.
    """

    recordings: list[Recording]
    local_paths: dict[str, Path]
    total_files_seen: int
    recognized_file_count: int


def _scan(root: Path, manifest: AdapterManifest | None = None) -> SdCardScanResult:
    """Recursively scan `root` for recognized files and group them into
    Recording/VodEntry objects - the same domain model
    BlackVueCamera.recordings() (the network path) already returns, so
    the rest of bv-download's own _run() works unmodified regardless
    of which source it's reading from.

    Recursive (not a flat, single-directory scan) since the real
    on-disk layout of a mounted SD card hasn't been confirmed for
    every camera (see docs/CAMERA_ADAPTERS.md) - this works whether the
    camera's own files sit directly at `root` or inside a subfolder
    (BlackVue's `Record`, GoPro's `DCIM/100GOPRO`, ...), without
    hard-coding a guess either way.

    `manifest is None` (the default): BlackVue's own strict filename
    convention (`_matches_blackvue_filename()`), timestamp parsed
    straight out of the filename - unchanged from before this
    parameter existed, so every existing BlackVue-only caller
    (including SdCardCamera's own default construction) behaves
    identically.

    `manifest` given: a generic, adapter-driven recognizer instead -
    `_matches_generic_video()` against `manifest.video_extensions`
    (any video file this adapter's own recursive-folder scan would
    also pick up - see adapters/_recursive_scan.py), timestamp from the
    file's own mtime rather than its name (GoPro's own on-camera
    filenames, e.g. `GH010001.MP4`, carry a chapter+file counter, not a
    timestamp - see gopro/manifest.json's own unsupported_notes). Each
    matched file becomes its own recording (`VodEntry.recording` is
    just the file's stem when there's no trailing BlackVue-style F/R/I
    letter to strip) - chaptered multi-file recordings aren't stitched
    back together here, the same limitation FolderAdapter/GoProAdapter
    already have for any multi-part video (see gopro/manifest.json).

    A file that doesn't match is silently skipped, not an error - see
    SdCardCamera's own docstring for why that's an expected outcome,
    not a bug, for a card whose files don't follow the active
    recognizer's convention. A genuine same-name collision across two
    subfolders (on-camera filenames are meant to be unique per card)
    keeps whichever sorts first and drops the rest, rather than
    failing the whole scan over what would be unexpected input either
    way.
    """

    total_files_seen = 0
    local_paths: dict[str, Path] = {}
    matched_names: list[str] = []
    matched_paths: dict[str, Path] = {}

    extensions = frozenset(manifest.video_extensions) if manifest is not None else None

    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue

        total_files_seen += 1
        name = path.name

        if extensions is None:
            matches = _matches_blackvue_filename(name)
        else:
            matches = _matches_generic_video(name, extensions)

        if not matches:
            continue

        if name in local_paths:
            continue

        local_paths[name] = path
        matched_names.append(name)
        matched_paths[name] = path

    entries_by_recording: dict[str, list[VodEntry]] = {}

    for name in matched_names:
        if extensions is None:
            timestamp = parse_timestamp(name)
        else:
            timestamp = datetime.fromtimestamp(matched_paths[name].stat().st_mtime)

        entry = VodEntry(
            timestamp=timestamp,
            path=PurePosixPath(name),
            fields={},
        )
        entries_by_recording.setdefault(entry.recording, []).append(entry)

    recordings = [
        Recording(id=recording_id, entries=entries)
        for recording_id, entries in sorted(entries_by_recording.items())
    ]

    return SdCardScanResult(
        recordings=recordings,
        local_paths=local_paths,
        total_files_seen=total_files_seen,
        recognized_file_count=len(matched_names),
    )


class SdCardCamera:
    """A local, filesystem-based counterpart to BlackVueCamera, used by
    bv-download's --sdcard mode (see bv_download.py's own module and
    docs/CAMERA_ADAPTERS.md's "Add SD-card import to bv-download" step).

    Scans `root` once, eagerly, at construction time for files matching
    BlackVue's own on-camera filename convention (`YYYYMMDD_HHMMSS_KD.
    ext` - the same shape bv-download itself writes into an archive).
    Not a generic recursive video importer like
    adapters/folder/adapter.py's FolderAdapter, which is for footage
    with no naming convention at all - this class only recognizes
    BlackVue's own naming. A file that doesn't match is silently
    skipped, not an error: Christer's own emulated test card
    (`X:\\SD_card`, 2026-08-16) is loaded with sample clips that don't
    follow the real convention, so "zero recognized recordings" is an
    expected, valid outcome for that data - see scan_summary(), which
    _run() uses to report that clearly instead of just doing nothing.

    Deliberately does NOT share scanning code with FolderAdapter/
    GoProAdapter despite the superficial similarity (both walk a folder
    tree) - the two solve different problems for two different
    consumers. This class parses source files into the domain/Recording
    model bv-download's own network path (BlackVueCamera) already
    returns, so the rest of _run() (mode selection, dry-run listing,
    the download loop, RecordTime capture) works completely unmodified.
    FolderAdapter/GoProAdapter instead synthesize brand new ids for
    footage already sitting in an archive, into the unrelated
    archive/Recording model the read-only adapters (bv-ls/bv-web) use
    for browsing - not for importing new files off a card.

    `manifest`, when given, switches recognition from BlackVue's own
    strict filename convention to a generic, extension-based one keyed
    off `manifest.video_extensions` - see _scan()'s own docstring for
    the full contract. bv-download's own _run() picks whichever to use
    from the target camera config's own `adapter` field (BlackVue's own
    id stays on the strict/default path; anything else loads that
    adapter's manifest and passes it through here) - see this class's
    own callers in cli/bv_download.py.
    """

    def __init__(self, root: Path, manifest: AdapterManifest | None = None) -> None:
        self._root = root
        self._scan_result = _scan(root, manifest)

    def recordings(self) -> list[Recording]:
        """Return the recordings found on the card."""

        return self._scan_result.recordings

    def scan_summary(self) -> SdCardScanResult:
        """Return the full scan result, including files seen but not
        recognized as BlackVue recordings - see this class's own
        docstring and _run()'s "N files scanned, 0 recognized"
        message.
        """

        return self._scan_result

    def probe_missing_sidecars(self, recording: Recording) -> list[VodEntry]:
        """No-op: every file physically present on the card was
        already found and attached to its recording during the
        initial scan. Unlike BlackVueCamera's own version (see its
        docstring), there's no "camera's own listing vs. what it
        actually serves" discrepancy to work around here - a
        filesystem scan doesn't have a listing that can omit a file
        that's genuinely there.
        """

        return []

    def download(
        self,
        recording: Recording,
        destination: Path,
        *,
        select: Callable[[VodEntry], bool] | None = None,
        on_bytes: Callable[[int], None] | None = None,
    ) -> bool:
        """Copy a recording's files from the card into `destination`.

        Mirrors BlackVueCamera.download()'s own contract (select/
        on_bytes/return value) so bv-download's shared download loop
        in _run() works unmodified regardless of which camera
        implementation is driving it. A file already present at the
        destination with the same size is skipped, not re-copied - the
        local equivalent of BlackVueClient.download()'s own resume
        support, except a local copy has no partial-transfer state
        worth resuming into: either the whole file already made it
        across, or it didn't.
        """

        destination.mkdir(parents=True, exist_ok=True)

        changed = False

        for entry in recording.entries:
            if select is not None and not select(entry):
                continue

            source = self._scan_result.local_paths[entry.path.name]
            target = destination / entry.path.name

            if target.exists() and target.stat().st_size == source.stat().st_size:
                continue

            with source.open("rb") as src, target.open("wb") as dst:
                while chunk := src.read(64 * 1024):
                    dst.write(chunk)

                    if on_bytes is not None:
                        on_bytes(len(chunk))

            changed = True

        return changed

    def is_fully_downloaded(self, recording: Recording, destination: Path) -> bool:
        """True if every one of `recording`'s entries is already present
        at `destination` with a matching size - i.e. download() would
        have nothing left to copy for it.

        Reuses the exact same same-size check download() itself does
        per-entry, just without actually opening/copying anything. Lets
        bv-download's _run() filter such a recording out of the
        "Matching recordings" listing/confirmation prompt entirely
        (Christer: "ignore files already fully downloaded") rather than
        listing it and then silently no-op'ing through it inside
        download() as before. BlackVueCamera has no equivalent method -
        a network listing has no local "already there" concept the way
        a filesystem scan does - so bv_download.py only calls this via
        hasattr(), leaving the network path untouched.
        """

        for entry in recording.entries:
            source = self._scan_result.local_paths[entry.path.name]
            target = destination / entry.path.name

            if not target.exists() or target.stat().st_size != source.stat().st_size:
                return False

        return True

    def read_config_text(self) -> str | None:
        """Best-effort read of the card's own config.ini, for
        RecordTime capture (see bv_download.py's own
        _capture_record_time_from_sdcard()).

        Tries the same relative path the camera serves it at over HTTP
        (`/Config/config.ini` - see BlackVueClient.config()) first,
        then the card root directly, since the real on-disk layout of
        a mounted BlackVue SD card hasn't been confirmed yet (see
        docs/CAMERA_ADAPTERS.md). Returns None, not an error, if
        neither exists or can't be read - matching
        _capture_record_time()'s own best-effort contract for the
        network path, where a missing/unreadable config.ini only ever
        means bv-export's own --max-gap default goes unset, never a
        reason to fail a download run.
        """

        for candidate in (
            self._root / "Config" / "config.ini",
            self._root / "config.ini",
        ):
            if not candidate.is_file():
                continue

            try:
                return candidate.read_text(encoding="utf-8")
            except OSError:
                return None

        return None
