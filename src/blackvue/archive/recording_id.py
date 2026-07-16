"""
BlackVue recording identifier.
"""

from dataclasses import dataclass


@dataclass(frozen=True, order=True)
class RecordingId:
    """A BlackVue recording identifier."""

    value: str

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

    def __str__(self) -> str:
        return self.value

    def __format__(self, format_spec: str) -> str:
        return format(self.value, format_spec)

    def __repr__(self) -> str:
        return self.value
