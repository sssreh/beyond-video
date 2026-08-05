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
        (".gpx", Asset.GPX),
        (".duration.txt", Asset.DURATION),
        # The diarized suffixes must be checked before the plain
        # ones below - ".diarized.transcript.txt" also ends with
        # ".transcript.txt", so the plain check would wrongly match
        # it first if it came before this.
        (".diarized.transcript.txt", Asset.TRANSCRIPT_DIARIZED),
        (".diarized.translation.txt", Asset.TRANSLATION_DIARIZED),
        (".transcript.txt", Asset.TRANSCRIPT),
        (".translation.txt", Asset.TRANSLATION),
        (".srt", Asset.SUBTITLES),
        (".lrc", Asset.LYRICS),
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

        Unlike read(), this doesn't scandir()/stat() every file in the
        archive - only a targeted glob for filenames that could
        possibly belong to this one recording_id (its 17-character
        prefix). This exists for callers that already know exactly
        which recording they want and would otherwise pay read()'s
        full-archive cost on every single lookup - e.g. bv-web's
        archive browser, which resolves one recording per thumbnail
        image request and per video-player range request. On a large
        archive, doing that via read() would make an N-thumbnail page
        load O(N^2), and a video player making dozens of range
        requests while seeking would re-scan the whole archive on
        every one of them.
        """

        recording: Recording | None = None

        try:
            candidates = self._path.glob(f"{recording_id.value}*")
        except OSError:
            return None

        for path in candidates:
            if not path.is_file():
                continue

            # The glob pattern is already an exact prefix match, but
            # confirm the full parsed id too - a filename that merely
            # starts with this prefix (rather than being exactly it,
            # e.g. a differently-shaped name that happens to share the
            # digits) shouldn't be silently folded into this
            # recording.
            if RecordingId.parse(path.name) != recording_id:
                continue

            asset = self._detect_asset(path.name)
            if asset is None:
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
    