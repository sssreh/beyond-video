from __future__ import annotations

from dataclasses import dataclass

from .lexicaltimeparser import TimeInterval


@dataclass(frozen=True)
class HumanTimeFormatter:
    """Convert lexical timestamps into human-friendly timestamps."""

    interval: TimeInterval

    @staticmethod
    def _clamp(value: int, low: int, high: int) -> int:
        return max(low, min(value, high))

    @classmethod
    def _format(cls, timestamp: str) -> str:
        date, time = timestamp.split("_")

        year = date[:4]
        month = cls._clamp(int(date[4:6]), 1, 12)
        day = cls._clamp(int(date[6:8]), 1, 31)

        hour = cls._clamp(int(time[:2]), 0, 23)
        minute = cls._clamp(int(time[2:4]), 0, 59)
        second = cls._clamp(int(time[4:6]), 0, 59)

        return (
            f"{year}"
            f"{month:02}"
            f"{day:02}_"
            f"{hour:02}"
            f"{minute:02}"
            f"{second:02}"
        )

    @property
    def first(self) -> str:
        return self._format(self.interval.first)

    @property
    def last(self) -> str:
        return self._format(self.interval.last)

    def format(self) -> TimeInterval:
        return TimeInterval(
            first=self.first,
            last=self.last,
        )
