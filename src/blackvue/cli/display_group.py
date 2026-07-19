from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

from blackvue.archive import Archive, Asset, Recording


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
    