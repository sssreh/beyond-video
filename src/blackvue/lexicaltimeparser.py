from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class TimeInterval:
    """An inclusive lexical interval of timestamps."""

    first: str
    last: str

    def __contains__(self, timestamp: str) -> bool:
        # Ignore recording suffix (_E, _P, etc.)
        timestamp = timestamp.rsplit("_", 1)[0]
        return self.first <= timestamp <= self.last


@dataclass(frozen=True)
class LexicalTimeParser:
    """Parse command-line timestamp arguments lexically."""

    timestamp: str | None = None
    from_: str | None = None
    until: str | None = None

    @staticmethod
    def _expand(prefix: str, pad: str) -> str:
        """Expand a lexical timestamp prefix."""

        if prefix.count("_") > 1:
            raise ValueError("timestamp may contain at most one '_'")

        if "_" in prefix:
            if prefix.index("_") != 8:
                raise ValueError(
                    "expected '_' after YYYYMMDD "
                    "(format: YYYYMMDD_HHMMSS)"
                )

        digits = prefix.replace("_", "")

        if not digits:
            raise ValueError("empty timestamp")

        if len(digits) > 14:
            raise ValueError("timestamp is too long")

        if not digits.isdigit():
            raise ValueError("timestamp must contain digits only")

        digits = digits.ljust(14, pad)

        return f"{digits[:8]}_{digits[8:]}"

    @classmethod
    def _first(cls, prefix: str) -> str:
        return cls._expand(prefix, "0")

    @classmethod
    def _last(cls, prefix: str) -> str:
        return cls._expand(prefix, "9")

    def parse(self) -> TimeInterval:

        if self.timestamp is not None:
            if self.from_ is not None or self.until is not None:
                raise ValueError(
                    "--timestamp cannot be combined with "
                    "--from or --until"
                )

            return TimeInterval(
                first=self._first(self.timestamp),
                last=self._last(self.timestamp),
            )

        return TimeInterval(
            first=(
                "00000000_000000"
                if self.from_ is None
                else self._first(self.from_)
            ),
            last=(
                "99999999_999999"
                if self.until is None
                else self._last(self.until)
            ),
        )
