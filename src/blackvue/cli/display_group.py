from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

from blackvue.archive import Archive, Asset, Recording


def _source_name(recording: Recording, root: Path) -> str:
    """Return the recording's real, on-disk FRONT filename - as a path
    relative to `root` when possible (so two same-named files in
    different subfolders, e.g. a GoPro archive's 100GOPRO/GH010001.MP4
    and 101GOPRO/GH010001.MP4, stay distinguishable), else just the
    bare filename. Empty string if the recording has no FRONT asset at
    all."""

    asset_file = recording.file(Asset.FRONT)
    if asset_file is None:
        return ""

    try:
        return str(asset_file.path.relative_to(root))
    except ValueError:
        return asset_file.path.name


@dataclass(frozen=True)
class DisplayGroup:
    """A group of recordings displayed as a single row."""

    recordings: tuple[Recording, ...]

    def __post_init__(self) -> None:
        if not self.recordings:
            raise ValueError("DisplayGroup must contain at least one recording.")

    @property
    def first(self) -> Recording:
        return self.recordings[0]

    @property
    def last(self) -> Recording:
        return self.recordings[-1]

    @property
    def label(self) -> str:
        if len(self.recordings) == 1:
            return str(self.first.id)

        return f"{self.first.id}..{self.last.id}"

    @property
    def size(self) -> int:
        return sum(recording.size for recording in self.recordings)

    def has(self, asset: Asset) -> bool:
        return all(recording.has(asset) for recording in self.recordings)

    def source_label(self, root: Path) -> str:
        """The real on-disk filename(s) behind this row - see
        _source_name()'s own docstring. For a BlackVue archive this is
        always id-derived (e.g. "20260715_133255_NF.mp4"), so bv-ls
        skips showing this column entirely rather than clutter every
        row with a near-duplicate of the Recording column (see
        _source_column_needed() in bv_ls.py, which decides that once
        for the whole table). For a recursive-
        scan adapter (folder/gopro) the on-camera filename carries no
        timestamp at all (e.g. "GH010001.MP4"), which is exactly the
        real report this column exists for: Christer's GoPro archive
        can synthesize the same recording id for two genuinely
        different physical files if the ffprobe/GPMF timestamp sources
        both come up empty and it falls back to file mtime (mtime
        reflecting when a file was copied/downloaded, not recorded) -
        seeing the real filename is how you'd actually notice and
        untangle that."""

        first = _source_name(self.first, root)
        if len(self.recordings) == 1:
            return first

        last = _source_name(self.last, root)
        if first == last:
            return first

        return f"{first} .. {last}"

    @classmethod
    def group(
        cls,
        archive: Archive,
        recordings: Iterable[Recording],
    ) -> list["DisplayGroup"]:
        """
        Group consecutive recordings.

        Current policy:
        - identical asset set
        - identical recording mode
        - identical RecordTime
        """

        recordings = tuple(recordings)

        if not recordings:
            return []

        groups: list[DisplayGroup] = []
        current: list[Recording] = [recordings[0]]

        for recording in recordings[1:]:
            previous = current[-1]

            same_assets = (
                set(recording.assets)
                == set(previous.assets)
            )

            same_mode = (
                recording.id.value[-1]
                == previous.id.value[-1]
            )

            same_record_time = (
                archive.configuration(recording).record_time
                == archive.configuration(previous).record_time
            )

            if same_assets and same_mode and same_record_time:
                current.append(recording)
            else:
                groups.append(cls(tuple(current)))
                current = [recording]

        groups.append(cls(tuple(current)))

        return groups
    