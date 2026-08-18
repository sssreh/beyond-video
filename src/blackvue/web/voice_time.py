"""Heuristic parser for spoken timestamp ranges in "Search by voice"
transcripts.

Companion to voice_query.py (distance+place parsing) - see that
module's docstring for the shared design philosophy. This one
recognizes spoken date/date-range references and routes them into
bv-search's existing Timestamp (single day) or From/Until (range)
fields, in English and Swedish:

- Relative single days: "today"/"idag", "yesterday"/"igar", "tomorrow"
  /"imorgon", "last <weekday>" ("last tuesday"), Swedish "i
  <weekday>s" idiom ("i tisdags" = "last Tuesday").
- Relative spans: "this week"/"last week", "this month"/"last month"
  (and Swedish equivalents) - a full Monday-Sunday week or
  calendar-month range.
- Explicit dates: "15 July 2026" / "July 15th, 2026" / "15 juli 2026"
  / ISO "2026-07-15" (year optional, defaults to the current year).
- Explicit ranges: "from X to Y" / "between X and Y" (English),
  "fran X till Y" / "mellan X och Y" (Swedish), where X and Y are any
  of the single-day forms above.

Deliberately bounded, like voice_query.py: no LLM, no general date
library dependency, just the phrasings that came up in practice.
Extend as real gaps show up rather than trying to cover every
possible utterance up front. Every field this produces is still just
an editable suggestion in the bv-search form - never authoritative.
"""

from __future__ import annotations

import calendar
import re
from dataclasses import dataclass
from datetime import date
from datetime import timedelta

__all__ = ["ParsedTimeRange", "parse_spoken_timerange"]


@dataclass(frozen=True)
class ParsedTimeRange:
    matched: bool
    timestamp: str | None  # YYYYMMDD - single-day match
    from_: str | None  # YYYYMMDD - range start
    until: str | None  # YYYYMMDD - range end
    remainder: str  # transcript with the matched phrase removed


_MONTHS = {
    # English, full names and common abbreviations.
    "january": 1, "jan": 1,
    "february": 2, "feb": 2,
    "march": 3, "mar": 3,
    "april": 4, "apr": 4,
    "may": 5,
    "june": 6, "jun": 6,
    "july": 7, "jul": 7,
    "august": 8, "aug": 8,
    "september": 9, "sep": 9, "sept": 9,
    "october": 10, "oct": 10,
    "november": 11, "nov": 11,
    "december": 12, "dec": 12,
    # Swedish, full names only - abbreviations aren't idiomatic.
    "januari": 1, "februari": 2, "mars": 3, "maj": 5,
    "juni": 6, "juli": 7, "augusti": 8,
    "oktober": 10,
    # "april"/"september"/"november"/"december" are spelled the same
    # in Swedish as English and already covered above.
}

_EN_WEEKDAYS = {
    "monday": 0, "tuesday": 1, "wednesday": 2, "thursday": 3,
    "friday": 4, "saturday": 5, "sunday": 6,
}

# The Swedish "i <weekday>s" idiom inflects the weekday name itself
# (mandag -> mandags) rather than adding a separate word like "last".
_SV_WEEKDAY_IDIOM = {
    "mandags": 0, "måndags": 0,
    "tisdags": 1,
    "onsdags": 2,
    "torsdags": 3,
    "fredags": 4,
    "lordags": 5, "lördags": 5,
    "sondags": 6, "söndags": 6,
}

_MONTH_ALTERNATION = "|".join(
    sorted((re.escape(name) for name in _MONTHS), key=len, reverse=True)
)

# One composite pattern for "a single date reference", reused both as
# a top-level search over the whole transcript and (via .search())
# against the short bounded clauses _CONNECTOR_PATTERNS capture below.
_DATE_TOKEN = re.compile(
    r"(?P<today>today|idag)"
    r"|(?P<yesterday>yesterday|ig[aå]r)"
    r"|(?P<tomorrow>tomorrow|imorgon)"
    r"|\blast\s+(?P<last_weekday_en>monday|tuesday|wednesday|thursday|friday|saturday|sunday)\b"
    r"|\bi\s+(?P<last_weekday_sv>m[aå]ndags|tisdags|onsdags|torsdags|fredags|l[oö]rdags|s[oö]ndags)\b"
    r"|\b(?P<dmy_day>\d{1,2})(?:st|nd|rd|th)?\s+(?:of\s+)?(?P<dmy_month>" + _MONTH_ALTERNATION + r")\.?\s*(?P<dmy_year>\d{4})?"
    r"|\b(?P<mdy_month>" + _MONTH_ALTERNATION + r")\.?\s+(?P<mdy_day>\d{1,2})(?:st|nd|rd|th)?,?\s*(?P<mdy_year>\d{4})?"
    r"|\b(?P<iso_year>\d{4})-(?P<iso_month>\d{1,2})-(?P<iso_day>\d{1,2})",
    re.IGNORECASE,
)

# Bounded (<=3 token) clauses either side of a range connector, so a
# stray "to"/"and" elsewhere in the sentence (e.g. "...from Slussen
# going to work") can't swallow unrelated words - see module docstring
# and _resolve_connector()'s own comment for why this is safe even
# when it over-matches: the clause still has to resolve via
# _DATE_TOKEN to count as a real range.
_CLAUSE = r"(?:\S+\s+){0,2}\S+"
_CONNECTOR_PATTERNS = [
    re.compile(rf"\bfrom\s+(?P<start>{_CLAUSE}?)\s+to\s+(?P<end>{_CLAUSE})", re.IGNORECASE),
    re.compile(rf"\bbetween\s+(?P<start>{_CLAUSE}?)\s+and\s+(?P<end>{_CLAUSE})", re.IGNORECASE),
    re.compile(rf"\bfr[aå]n\s+(?P<start>{_CLAUSE}?)\s+till\s+(?P<end>{_CLAUSE})", re.IGNORECASE),
    re.compile(rf"\bmellan\s+(?P<start>{_CLAUSE}?)\s+och\s+(?P<end>{_CLAUSE})", re.IGNORECASE),
]

# Relative *span* phrases (a Monday-Sunday week or a calendar month),
# as opposed to _DATE_TOKEN's single-day phrases. Checked as plain
# case-insensitive substrings, not part of _DATE_TOKEN, since each one
# needs a different (start, end) computation - see the four _span_*
# helpers below.
_SPAN_KEYWORDS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"\bthis\s+week\b", re.IGNORECASE), "this_week"),
    (re.compile(r"\bden(?:na|\s+h[aä]r)\s+veckan?\b", re.IGNORECASE), "this_week"),
    (re.compile(r"\blast\s+week\b", re.IGNORECASE), "last_week"),
    (re.compile(r"\bf[oö]rra\s+veckan\b", re.IGNORECASE), "last_week"),
    (re.compile(r"\bthis\s+month\b", re.IGNORECASE), "this_month"),
    (re.compile(r"\bden(?:na|\s+h[aä]r)\s+m[aå]nad(?:en)?\b", re.IGNORECASE), "this_month"),
    (re.compile(r"\blast\s+month\b", re.IGNORECASE), "last_month"),
    (re.compile(r"\bf[oö]rra\s+m[aå]naden\b", re.IGNORECASE), "last_month"),
]


def _last_weekday(today: date, target_weekday: int) -> date:
    # "Last <weekday>" always means a day strictly before today, even
    # if today happens to be that same weekday - go back a full week
    # in that case rather than returning today.
    delta = (today.weekday() - target_weekday) % 7
    if delta == 0:
        delta = 7
    return today - timedelta(days=delta)


def _resolve_date_token(match: re.Match[str], today: date) -> tuple[date | None, bool]:
    """Returns (resolved_date, year_was_explicit).

    year_was_explicit distinguishes "the year was actually said" from
    "defaulted to today's year because none was given" - see
    _resolve_connector()'s cross-clause year inference, which needs
    that distinction to handle a spoken range like "from July 15th to
    July 20th 2025", where the year is normally only said once and
    implicitly applies to both ends.
    """

    if match.group("today"):
        return today, True
    if match.group("yesterday"):
        return today - timedelta(days=1), True
    if match.group("tomorrow"):
        return today + timedelta(days=1), True
    if match.group("last_weekday_en"):
        return _last_weekday(today, _EN_WEEKDAYS[match.group("last_weekday_en").lower()]), True
    if match.group("last_weekday_sv"):
        return _last_weekday(today, _SV_WEEKDAY_IDIOM[match.group("last_weekday_sv").lower()]), True
    if match.group("dmy_month"):
        month = _MONTHS[match.group("dmy_month").lower()]
        day = int(match.group("dmy_day"))
        year_str = match.group("dmy_year")
        year = int(year_str) if year_str else today.year
        return _safe_date(year, month, day), bool(year_str)
    if match.group("mdy_month"):
        month = _MONTHS[match.group("mdy_month").lower()]
        day = int(match.group("mdy_day"))
        year_str = match.group("mdy_year")
        year = int(year_str) if year_str else today.year
        return _safe_date(year, month, day), bool(year_str)
    if match.group("iso_year"):
        return (
            _safe_date(
                int(match.group("iso_year")),
                int(match.group("iso_month")),
                int(match.group("iso_day")),
            ),
            True,
        )
    return None, False


def _safe_date(year: int, month: int, day: int) -> date | None:
    try:
        return date(year, month, day)
    except ValueError:
        return None


def _span_this_week(today: date) -> tuple[date, date]:
    monday = today - timedelta(days=today.weekday())
    return monday, monday + timedelta(days=6)


def _span_last_week(today: date) -> tuple[date, date]:
    this_monday, _ = _span_this_week(today)
    last_monday = this_monday - timedelta(days=7)
    return last_monday, last_monday + timedelta(days=6)


def _span_this_month(today: date) -> tuple[date, date]:
    start = today.replace(day=1)
    _, last_day = calendar.monthrange(today.year, today.month)
    return start, today.replace(day=last_day)


def _span_last_month(today: date) -> tuple[date, date]:
    first_of_this_month = today.replace(day=1)
    last_of_prev_month = first_of_this_month - timedelta(days=1)
    start = last_of_prev_month.replace(day=1)
    return start, last_of_prev_month


_SPAN_RESOLVERS = {
    "this_week": _span_this_week,
    "last_week": _span_last_week,
    "this_month": _span_this_month,
    "last_month": _span_last_month,
}


def _fmt(d: date) -> str:
    return d.strftime("%Y%m%d")


def _remove_span(text: str, start: int, end: int) -> str:
    return (text[:start] + " " + text[end:]).strip()


def _resolve_connector(transcript: str, today: date) -> ParsedTimeRange | None:
    for pattern in _CONNECTOR_PATTERNS:
        match = pattern.search(transcript)
        if not match:
            continue
        start_date = end_date = None
        start_year_explicit = end_year_explicit = False
        start_token = _DATE_TOKEN.search(match.group("start"))
        if start_token:
            start_date, start_year_explicit = _resolve_date_token(start_token, today)
        end_token = _DATE_TOKEN.search(match.group("end"))
        if end_token:
            end_date, end_year_explicit = _resolve_date_token(end_token, today)
        if start_date is None or end_date is None:
            # Bounded-clause over-match (e.g. "...from Slussen going
            # to work") that isn't actually a date range - try the
            # next connector pattern rather than removing anything.
            continue

        # A spoken range usually states the year once, e.g. "from
        # July 15th to July 20th 2025" - without this, the year-less
        # side would silently default to *today's* year instead of
        # inheriting the one actually said (see _resolve_date_token's
        # own docstring). Only cross-apply when exactly one side is
        # explicit; if both or neither are, each date stands as-is.
        if start_year_explicit and not end_year_explicit:
            end_date = _safe_date(start_date.year, end_date.month, end_date.day) or end_date
        elif end_year_explicit and not start_year_explicit:
            start_date = _safe_date(end_date.year, start_date.month, start_date.day) or start_date

        first, last = sorted((start_date, end_date))
        return ParsedTimeRange(
            matched=True,
            timestamp=None,
            from_=_fmt(first),
            until=_fmt(last),
            remainder=_remove_span(transcript, match.start(), match.end()),
        )
    return None


def _resolve_span_keyword(transcript: str, today: date) -> ParsedTimeRange | None:
    for pattern, kind in _SPAN_KEYWORDS:
        match = pattern.search(transcript)
        if not match:
            continue
        first, last = _SPAN_RESOLVERS[kind](today)
        return ParsedTimeRange(
            matched=True,
            timestamp=None,
            from_=_fmt(first),
            until=_fmt(last),
            remainder=_remove_span(transcript, match.start(), match.end()),
        )
    return None


def _resolve_single_date(transcript: str, today: date) -> ParsedTimeRange | None:
    match = _DATE_TOKEN.search(transcript)
    if not match:
        return None
    resolved, _year_explicit = _resolve_date_token(match, today)
    if resolved is None:
        return None
    return ParsedTimeRange(
        matched=True,
        timestamp=_fmt(resolved),
        from_=None,
        until=None,
        remainder=_remove_span(transcript, match.start(), match.end()),
    )


def parse_spoken_timerange(transcript: str, today: date) -> ParsedTimeRange:
    """Best-effort extraction of a spoken date/date-range reference.

    Tries, in order: an explicit "from X to Y"/"between X and Y"
    range (English or Swedish connectors), a relative span keyword
    ("this/last week"/"this/last month"), then a single-day reference
    (today/yesterday/tomorrow/last <weekday>/an explicit date). First
    match wins. Falls back to ``matched=False, remainder=transcript``
    unchanged if nothing is recognized.
    """

    transcript = transcript.strip()
    for resolver in (_resolve_connector, _resolve_span_keyword, _resolve_single_date):
        result = resolver(transcript, today)
        if result is not None:
            return result

    return ParsedTimeRange(
        matched=False, timestamp=None, from_=None, until=None, remainder=transcript
    )
