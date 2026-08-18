"""Heuristic parser for "Search by voice" transcripts.

See web/app.py's ``POST /jobs/bv-search/transcribe`` route and
job_new_bv_search.html's voice-search JS. Whisper transcribes speech
accurately, but a spoken sentence like "Show all videos less than
1000 meters from Vårby gård" is not itself a bv-search query -
bv-search's Text field only does literal/regex substring matching
against transcript/translation/scene-description content and has no
language understanding at all (real report from Christer: dictating
exactly that kind of sentence transcribed correctly but "doesnt
understand do do a radius search" when dumped straight into Text).

This module recognizes one common, high-value shape - "within/less
than <distance> <unit> of/from <place>", in English and Swedish - and
extracts it into bv-search's *existing* Place/Radius fields, which
already do exactly this kind of proximity search
(blackvue.search.search_near() via forward-geocoding, see
cli/bv_search.py). Anything not recognized is left in Text verbatim.
Every field stays editable in the form either way - this is a
best-effort suggestion, never authoritative.

Deliberately NOT a general natural-language-understanding layer: no
LLM call, no new dependency, just a handful of regexes for the one
pattern that prompted this. Extend _PATTERNS with more phrasings as
real gaps show up, rather than trying to anticipate every possible
sentence up front.

Important design choice: when a distance+place pattern matches, the
returned ``text`` is always "" (not "whatever was left over before the
match"). bv-search ANDs Text and Place/Radius together (see
cli/bv_search.py's _run(): a recording must satisfy text_matches
*and* geo_match when both are given) - so leaving leading command
words like "Show all videos that are" in Text would silently zero out
every result even though the place/radius half parsed correctly. An
empty Text field combined with a correct Place/Radius is a strictly
safer default than a populated-but-wrong one.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

__all__ = ["ParsedVoiceQuery", "parse_spoken_query"]


@dataclass(frozen=True)
class ParsedVoiceQuery:
    text: str
    place: str | None
    radius_meters: float | None


# Each pattern must capture, in order: (distance, unit, place-clause).
# Tried in order, case-insensitively; the first match wins.
_PATTERNS = [
    # English: "within/less than/under/closer than 1000 meters of/from X"
    re.compile(
        r"\b(?:within|less than|under|closer than)\s+"
        r"([\d.,]+)\s*"
        r"(kilometers?|kilometres?|km|meters?|metres?|m)\b"
        r"\s*(?:of|from|to)\s+"
        r"(.+)",
        re.IGNORECASE,
    ),
    # Swedish: "mindre än/inom/närmare än 1000 meter från/ifrån X"
    re.compile(
        r"\b(?:mindre än|inom|närmare än)\s+"
        r"([\d.,]+)\s*"
        r"(kilometer|km|meter|m)\b"
        r"\s*(?:ifrån|från)\s+"
        r"(.+)",
        re.IGNORECASE,
    ),
]

# Trimmed off the *end* of an extracted place name - words that
# grammatically trail the place in natural speech but aren't part of
# it (e.g. Christer's own example ends "...vårby gård någon gång" -
# "...Vårby gård at some point"). Matched case-insensitively and
# repeatedly, only at the very end of the captured clause.
_TRAILING_FILLER = [
    "någon gång", "ibland", "en gång till", "en gång",
    "at some point", "at any time", "sometime", "ever",
]

_SENTENCE_END = re.compile(r"[.!?]+\s*$")
_KM_UNITS = {"km", "kilometer", "kilometers", "kilometre", "kilometres"}


def _clean_place(raw: str) -> str:
    place = _SENTENCE_END.sub("", raw.strip()).strip()
    changed = True
    while changed:
        changed = False
        for filler in _TRAILING_FILLER:
            pattern = re.compile(re.escape(filler) + r"\s*$", re.IGNORECASE)
            trimmed = _SENTENCE_END.sub("", pattern.sub("", place)).strip()
            if trimmed != place:
                place = trimmed
                changed = True
    return place.strip(" ,.")


def _parse_distance(raw_number: str, raw_unit: str) -> float:
    # Spoken/dictated numbers sometimes carry a comma as the decimal
    # separator (Swedish convention) rather than a thousands
    # separator - if there's a comma and no dot, treat it as decimal;
    # otherwise drop commas as thousands separators.
    cleaned = raw_number.replace(" ", "").replace("\xa0", "")
    if "," in cleaned and "." not in cleaned:
        cleaned = cleaned.replace(",", ".")
    else:
        cleaned = cleaned.replace(",", "")
    value = float(cleaned)
    if raw_unit.lower() in _KM_UNITS:
        value *= 1000
    return value


def parse_spoken_query(transcript: str) -> ParsedVoiceQuery:
    """Best-effort split of a raw voice transcript into bv-search's
    Text/Place/Radius fields.

    Falls back to ``text=transcript, place=None, radius_meters=None``
    on no match, since the caller treats every field as an editable
    suggestion rather than an authoritative parse.
    """

    transcript = transcript.strip()
    for pattern in _PATTERNS:
        match = pattern.search(transcript)
        if not match:
            continue
        raw_number, raw_unit, raw_place = match.groups()
        try:
            radius_meters = _parse_distance(raw_number, raw_unit)
        except ValueError:
            continue
        place = _clean_place(raw_place)
        if not place:
            continue
        # See module docstring: text is cleared, not set to the
        # leading leftover, to avoid AND-combining stale command words
        # with a correctly parsed place/radius.
        return ParsedVoiceQuery(text="", place=place, radius_meters=radius_meters)

    return ParsedVoiceQuery(text=transcript, place=None, radius_meters=None)
