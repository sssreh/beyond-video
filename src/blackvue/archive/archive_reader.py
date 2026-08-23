"""
BlackVue archive reader.
"""

from os import scandir
from pathlib import Path

from .asset import Asset
from .asset_file import AssetFile
from .recording import Recording
from .recording_id import RecordingId


class ArchiveReader:
    """Read a BlackVue archive."""

    ASSETS = (
        ("F.mp4", Asset.FRONT),
        ("R.mp4", Asset.REAR),
        # Interior camera suffix/extension inferred from the F/R
        # pattern, not yet confirmed against a real interior-equipped
        # camera - see WORKING_CONTEXT.md.
        ("I.mp4", Asset.INTERIOR),
        (".gps", Asset.GPS),
        (".3gf", Asset.GSENSOR),
        ("F.thm", Asset.FRONT_THUMBNAIL),
        ("R.thm", Asset.REAR_THUMBNAIL),
        ("I.thm", Asset.INTERIOR_THUMBNAIL),
        (".aac", Asset.AUDIO),
        (".duration.txt", Asset.DURATION),
        (".thumb.jpg", Asset.THUMBNAIL),
        (".stats.json", Asset.RECORDING_STATS),
        # The diarized suffixes must be checked before the plain
        # ones below - ".diarized.transcript.txt" also ends with
        # ".transcript.txt", so the plain check would wrongly match
        # it first if it came before this.
        (".diarized.transcript.txt", Asset.TRANSCRIPT_DIARIZED),
        (".diarized.translation.txt", Asset.TRANSLATION_DIARIZED),
        (".transcript.txt", Asset.TRANSCRIPT),
        (".translation.txt", Asset.TRANSLATION),
        (".srt", Asset.SUBTITLES),
        # Checked before the plain ".scene.txt" below, same reasoning
        # as the diarized-before-plain transcript/translation ordering
        # above - ".rear.scene.txt" also ends with ".scene.txt".
        (".rear.scene.txt", Asset.SCENE_DESCRIPTION_REAR),
        (".scene.txt", Asset.SCENE_DESCRIPTION),
    )

    def __init__(self, path: Path):
        self._path = Path(path)

    def read(self) -> list[Recording]:
        """Read the archive."""

        recordings: dict[RecordingId, Recording] = {}

        with scandir(self._path) as entries:
            for entry in entries:

                if not entry.is_file():
                    continue

                recording_id = RecordingId.parse(entry.name)
                if recording_id is None:
                    continue

                asset = self._detect_asset(entry.name)
                if asset is None:
                    continue

                recording = recordings.setdefault(
                    recording_id,
                    Recording(recording_id),
                )

                recording.size += entry.stat().st_size

                recording.assets[asset] = AssetFile(
                    asset=asset,
                    path=Path(entry.path),
                )

        return sorted(recordings.values(), key=lambda r: r.id)

    def read_recording(self, recording_id: RecordingId) -> Recording | None:
        """Read a single recording by id, or None if it doesn't exist
        in this archive.

        Unlike read(), this never lists the archive directory at all -
        it only stat()s the small, fixed set of filenames a recording
        could possibly have, one per ASSETS entry, built by appending
        each known suffix directly to recording_id.value. This exists
        for callers that already know exactly which recording they
        want and would otherwise pay read()'s full-archive cost on
        every single lookup - e.g. bv-web's archive browser, which
        resolves one recording per thumbnail image request and per
        video-player range request. On a large archive, doing that via
        read() would make an N-thumbnail page load O(N^2), and a video
        player making dozens of range requests while seeking would
        re-scan the whole archive on every one of them.

        This used to be implemented as
        `self._path.glob(f"{recording_id.value}*")`, which reads as
        "targeted" but isn't: pathlib's glob() still has to list every
        entry in the directory (an os.scandir() over the parent,
        filtered by pattern) to find matches - it just skips stat()ing
        the entries that don't match. On a small archive that's fine,
        but on a large one it's no cheaper than read()'s own directory
        listing. This bit for real: on Christer's archive, which has
        years of unpruned recordings sitting in one flat directory, a
        single thumbnail request was still slow even in isolation with
        nothing else competing for the server - the earlier per-
        recording caches added to bv-web (see WORKING_CONTEXT.md)
        couldn't help, because the very first lookup of any given
        recording already paid this cost, and a thumbnail grid asks
        for a different recording each time. Probing each of the
        dozen-ish known exact filenames directly turns this into a
        fixed number of stat()s regardless of archive size - a
        filesystem can resolve one specific name via its own directory
        index without reading the whole directory, the same way
        find_recording() always claimed to work.
        """

        recording: Recording | None = None

        for suffix, asset in self.ASSETS:
            path = self._path / f"{recording_id.value}{suffix}"

            if not path.is_file():
                continue

            if recording is None:
                recording = Recording(recording_id)

            try:
                recording.size += path.stat().st_size
            except OSError:
                pass

            recording.assets[asset] = AssetFile(asset=asset, path=path)

        return recording

    @classmethod
    def _detect_asset(cls, filename: str) -> Asset | None:
        """Return the asset represented by the filename."""

        for suffix, asset in cls.ASSETS:
            if filename.endswith(suffix):
                return asset

        return None
    