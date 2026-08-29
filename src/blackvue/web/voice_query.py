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

This module recognizes distance+place phrasing in either word order -
"within/less than <distance> <unit> of/from <place>" (distance first)
and "<place> in range of/within <distance> <unit>" (place first), in
English and Swedish - and extracts it into bv-search's *existing*
Place/Radius fields, which already do exactly this kind of proximity
search (blackvue.search.search_near() via forward-geocoding, see
cli/bv_search.py). Anything not recognized is left in Text verbatim.
Every field stays editable in the form either way - this is a
best-effort suggestion, never authoritative.

The place-first patterns exist because Christer said it out loud
naturally as "VårbyGård in range of 400 m" - place before distance -
and the original distance-first-only patterns didn't match that at
all, silently falling through to a literal Text search of the whole
sentence. Speech-to-text alone can't fix this (it doesn't understand
word order any more than these regexes originally did) - see
web/voice_llm.py, which is now this project's primary voice-search
parser precisely because it reasons about phrasing instead of matching
fixed patterns; this module's patterns remain the fast, free fallback
for when the LLM path is off or unavailable.

Deliberately NOT a general natural-language-understanding layer: no
LLM call, no new dependency, just a handful of regexes for the one
pattern that prompted this. Extend _PATTERNS with more phrasings as
real gaps show up, rather than trying to anticipate every possible
sentence up front.

_NUM also recognizes a small set of written-out round numbers
("tusen"/"hundra", "thousand"/"hundred") alongside digits - another
real Christer report: "...närmare än tusen meter ifrån..." ("...closer
than a thousand meters from...") wasn't just an ASR mis-hearing this
time, the *transcript itself* was correct, but [\d.,]+ alone never
matches a spelled-out number. See _WORD_NUMBERS below.

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


# Every pattern uses the same three named groups (number/unit/place)
# regardless of which order they appear in the sentence - distance-
# first patterns write the place group last in the pattern text,
# place-first patterns write it first. Tried in order, case-
# insensitively; the first match wins. Distance-first patterns are
# tried before place-first ones since they're the original, more
# specific/less prone-to-overcapture shape (place-first has to grab an
# unbounded run of leading words as the place candidate - see
# _LEADING_FILLER below).
_UNIT = r"(?P<unit>kilometers?|kilometres?|km|meters?|metres?|m)"
_UNIT_SV = r"(?P<unit>kilometer|km|meter|m)"

# Real gap: Christer's own report, "Hitta en resa med bilen som är
# närmare än tusen meter ifrån vår bygård" - "tusen meter" is spoken
# Swedish for "a thousand meters", not digits, so [\d.,]+ alone never
# matched it and the whole distance+place pattern silently failed to
# recognize the sentence at all. Deliberately narrow (just the two
# most common round-number words in each language, not a full
# number-word grammar) - same "handle the shape actually hit" approach
# this module's own docstring already describes for _PATTERNS.
_WORD_NUMBERS = {
    "hundra": 100.0,
    "tusen": 1000.0,
    "hundred": 100.0,
    "thousand": 1000.0,
}
_NUM = r"(?P<number>[\d.,]+|hundra|tusen|hundred|thousand)"

_PATTERNS = [
    # English, distance-first: "within/less than/under/closer than
    # 1000 meters of/from X"
    re.compile(
        rf"\b(?:within|less than|under|closer than)\s+{_NUM}\s*{_UNIT}\b"
        rf"\s*(?:of|from|to)\s+(?P<place>.+)",
        re.IGNORECASE,
    ),
    # Swedish, distance-first: "mindre än/inom/närmare än 1000 meter
    # från/ifrån X"
    re.compile(
        rf"\b(?:mindre än|inom|närmare än)\s+{_NUM}\s*{_UNIT_SV}\b"
        rf"\s*(?:ifrån|från)\s+(?P<place>.+)",
        re.IGNORECASE,
    ),
    # English, place-first: "X in range of/within/less than/under/
    # closer than 1000 meters" - Christer's own real phrasing
    # ("VårbyGård in range of 400 m") that started this: speech-to-
    # text alone doesn't understand word order, so both orders need
    # their own pattern rather than hoping one regex covers both.
    re.compile(
        r"(?P<place>.+?)\s+(?:is\s+)?"
        rf"(?:in range of|within|less than|under|closer than)\s+{_NUM}\s*{_UNIT}\b",
        re.IGNORECASE,
    ),
    # Swedish, place-first: "X inom 1000 meter"
    re.compile(
        rf"(?P<place>.+?)\s+inom\s+{_NUM}\s*{_UNIT_SV}\b",
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

# Trimmed off the *start* of a place-first match's captured place -
# only relevant there, since distance-first patterns anchor their
# place group right after "of/from" and never pick up a command
# prefix. A place-first pattern's place group is "everything before
# the distance phrase", so a full sentence like "show me videos near
# Vårbygård in range of 400 m" would otherwise capture "show me videos
# near Vårbygård" as the place. Deliberately short/conservative - an
# unrecognized prefix is safer left in (a slightly-wrong place name
# that still resolves is a minor annoyance; every field stays
# editable) than aggressively stripped and accidentally eating part of
# a real place name.
_LEADING_FILLER = [
    "show me", "show all videos", "show videos", "find", "search for",
    "look for", "videos of", "videos near", "recordings near",
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
    for filler in _LEADING_FILLER:
        pattern = re.compile(r"^\s*" + re.escape(filler) + r"\s+", re.IGNORECASE)
        trimmed = pattern.sub("", place).strip()
        if trimmed != place:
            place = trimmed
    return place.strip(" ,.")


def _parse_distance(raw_number: str, raw_unit: str) -> float:
    word_value = _WORD_NUMBERS.get(raw_number.strip().lower())
    if word_value is not None:
        value = word_value
    else:
        # Spoken/dictated numbers sometimes carry a comma as the
        # decimal separator (Swedish convention) rather than a
        # thousands separator - if there's a comma and no dot, treat
        # it as decimal; otherwise drop commas as thousands
        # separators.
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
        try:
            radius_meters = _parse_distance(match.group("number"), match.group("unit"))
        except ValueError:
            continue
        place = _clean_place(match.group("place"))
        if not place:
            continue
        # See module docstring: text is cleared, not set to the
        # leading leftover, to avoid AND-combining stale command words
        # with a correctly parsed place/radius.
        return ParsedVoiceQuery(text="", place=place, radius_meters=radius_meters)

    return ParsedVoiceQuery(text=transcript, place=None, radius_meters=None)
