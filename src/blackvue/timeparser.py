from __future__ import annotations

from dataclasses import dataclass
from enum import Enum, auto
from typing import Self


class Bound(Enum):
    LOWER = auto()
    UPPER = auto()


@dataclass
class Cursor:
    """Parser cursor."""

    text: str
    pos: int = 0

    @property
    def finished(self) -> bool:
        return self.pos >= len(self.text)

    def take(self, count: int = 1) -> str:
        result = self.text[self.pos:self.pos + count]
        self.pos += len(result)
        return result

    def expect_end(self) -> None:
        if not self.finished:
            raise ValueError(
                f"unexpected character: {self.text[self.pos]!r}"
            )


@dataclass(frozen=True)
class Field:
    """
    One partially specified numeric field.

    Missing trailing digits expand lexically to either the lowest or highest
    possible value.
    """

    width: int
    minimum: str
    maximum: str
    digits: str = ""

    @property
    def empty(self) -> bool:
        return self.digits == ""

    @property
    def complete(self) -> bool:
        return len(self.digits) == self.width

    @property
    def valid(self) -> bool:
        if len(self.digits) > self.width:
            return False

        if not self.digits:
            return True

        value = int(self.digits)

        return (
            int(self.minimum[:len(self.digits)])
            <= value
            <= int(self.maximum[:len(self.digits)])
        )

    def validate(self, name: str) -> None:
        if not self.valid:
            raise ValueError(f"invalid {name}: {self.digits!r}")

    def expand(self, bound: Bound) -> str:
        pad = "0" if bound is Bound.LOWER else "9"
        return self.digits.ljust(self.width, pad)


@dataclass(frozen=True)
class Timestamp:
    """A complete or partial timestamp."""

    year: Field
    month: Field
    day: Field
    hour: Field
    minute: Field
    second: Field

    @classmethod
    def parse(cls, text: str) -> Self:
        """Parse a timestamp."""

        cursor = Cursor(text)

        year = Field(4, "0000", "9999", cursor.take(4))

        month = Field(2, "01", "12")
        day = Field(2, "01", "31")
        hour = Field(2, "00", "23")
        minute = Field(2, "00", "59")
        second = Field(2, "00", "59")

        if not cursor.finished:
            month = Field(2, "01", "12", cursor.take(2))

        if not cursor.finished:
            day = Field(2, "01", "31", cursor.take(2))

        if not cursor.finished:
            if cursor.take() != "_":
                raise ValueError(
                    "expected '_' before the time component "
                    "(format: YYYYMMDD_HHMISS)"
)

            if not cursor.finished:
                hour = Field(2, "00", "23", cursor.take(2))

            if not cursor.finished:
                minute = Field(2, "00", "59", cursor.take(2))

            if not cursor.finished:
                second = Field(2, "00", "59", cursor.take(2))

        cursor.expect_end()

        year.validate("year")
        month.validate("month")
        day.validate("day")
        hour.validate("hour")
        minute.validate("minute")
        second.validate("second")

        return cls(
            year=year,
            month=month,
            day=day,
            hour=hour,
            minute=minute,
            second=second,
        )

    def expand(self, bound: Bound) -> str:
        """Expand the timestamp to the requested bound."""

        result = self.year.expand(bound)

        if not self.month.empty:
            result += self.month.expand(bound)

        if not self.day.empty:
            result += self.day.expand(bound)

        if (
            not self.hour.empty
            or not self.minute.empty
            or not self.second.empty
        ):
            result += "_"
            result += self.hour.expand(bound)
            result += self.minute.expand(bound)
            result += self.second.expand(bound)
        elif not self.day.empty:
            result += "_"
            if bound is Bound.LOWER:
                result += "000000"
            else:
                result += "235959"

        return result

    @property
    def first(self) -> str:
        return self.expand(Bound.LOWER)

    @property
    def last(self) -> str:
        return self.expand(Bound.UPPER)

    def __str__(self) -> str:
        result = self.year.digits

        if not self.month.empty:
            result += self.month.digits

        if not self.day.empty:
            result += self.day.digits

        if (
            not self.hour.empty
            or not self.minute.empty
            or not self.second.empty
        ):
            result += "_"
            result += self.hour.digits
            result += self.minute.digits
            result += self.second.digits

        return result


@dataclass(frozen=True)
class TimeInterval:
    """An inclusive interval of timestamps."""

    first: str
    last: str

    @classmethod
    def exactly(cls, timestamp: Timestamp) -> Self:
        return cls(
            first=timestamp.first,
            last=timestamp.last,
        )

    @classmethod
    def between(
        cls,
        first: Timestamp | None,
        last: Timestamp | None,
    ) -> Self:
        return cls(
            first=(
                "00000101_000000"
                if first is None
                else first.first
            ),
            last=(
                "99991231_235959"
                if last is None
                else last.last
            ),
        )

    def __contains__(self, timestamp: str) -> bool:
        timestamp = timestamp.rsplit("_", 1)[0]
        return self.first <= timestamp <= self.last


@dataclass(frozen=True)
class TimeParser:
    """Parse command-line timestamp arguments."""

    timestamp: str | None = None
    from_: str | None = None
    until: str | None = None

    def parse(self) -> TimeInterval:
        if self.timestamp is not None:
            if self.from_ is not None or self.until is not None:
                raise ValueError(
                    "--timestamp cannot be combined with "
                    "--from or --until"
                )

            return TimeInterval.exactly(
                Timestamp.parse(self.timestamp)
            )

        first = (
            None
            if self.from_ is None
            else Timestamp.parse(self.from_)
        )

        last = (
            None
            if self.until is None
            else Timestamp.parse(self.until)
        )

        return TimeInterval.between(
            first=first,
            last=last,
        )
    