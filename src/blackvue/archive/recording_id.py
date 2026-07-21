"""
BlackVue recording identifier.
"""

from dataclasses import dataclass
from datetime import datetime
from typing import ClassVar


@dataclass(frozen=True, order=True)
class RecordingId:
    """A BlackVue recording identifier."""

    value: str

    MIN: ClassVar["RecordingId"]
    MAX: ClassVar["RecordingId"]

    @classmethod
    def parse(cls, filename: str) -> "RecordingId | None":
        """
        Parse a filename into a RecordingId.

        Examples:
            20260715_133255_NF.mp4
            20260715_133255_NR.mp4
            20260715_133255_N.gps
        """

        if len(filename) < 17:
            return None

        if filename[8] != "_":
            return None

        if filename[15] != "_":
            return None

        return cls(filename[:17])

    @property
    def timestamp(self) -> datetime:
        """Return the recording's start timestamp."""

        return datetime.strptime(self.value[:15], "%Y%m%d_%H%M%S")

    @property
    def kind(self) -> str:
        """Return the recording kind (N, E, M, or P)."""

        return self.value[16]

    @property
    def is_normal(self) -> bool:
        return self.kind == "N"

    @property
    def is_event(self) -> bool:
        return self.kind == "E"

    @property
    def is_manual(self) -> bool:
        return self.kind == "M"

    @property
    def is_parking(self) -> bool:
        return self.kind == "P"

    def __str__(self) -> str:
        return self.value

    def __format__(self, format_spec: str) -> str:
        return format(self.value, format_spec)

    def __repr__(self) -> str:
        return self.value


RecordingId.MIN = RecordingId("00010101_000000")
RecordingId.MAX = RecordingId("99991231_235959")
