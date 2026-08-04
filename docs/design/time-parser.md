# Time Parser Design

> This doc previously described a design that was never actually built (calendar validation, explicit wildcard rejection, a `parse_from`/`parse_until` API, and commands named `bv-copy`/`bv-delete`/`bv-trip` that don't exist). It's been rewritten to match the real implementation in `src/blackvue/lexicaltimeparser.py`. For the user-facing version of this, see `docs/CLI.md`.

## Purpose

`LexicalTimeParser` provides a common implementation of `--from`, `--until`,
and `--timestamp` for all `bv-*` commands that select recordings by time.

It converts a user-supplied timestamp prefix into a `TimeInterval` - a pair
of complete, fixed-width timestamps that recordings can be tested against
with plain string comparison.

The implementation is shared by:

- `bv-download`
- `bv-ls`
- `bv-generate`
- `bv-export`

Future commands that select recordings by time should reuse this
implementation instead of writing their own.

---

## Canonical Timestamp Format

All timestamps use the native BlackVue format.

```
YYYYMMDD_HHMMSS
```

Examples:

```
20250101_000000
20250614_123456
20251231_235959
```

Because every field is fixed width, timestamps can be compared directly as
strings.

```
first <= timestamp <= last
```

No `datetime` objects are required or used anywhere in the parser.

---

## Accepted Prefixes

The parser is purely lexical: it does not know what a month or an hour is,
only how to pad a digit string. A prefix is any run of 1-14 digits, with an
optional single `_` allowed only right after the 8th digit. Anything that
fits that shape is accepted, not just the "round" lengths below:

```
2025
202506
20250614
20250614_08
20250614_0830
20250614_083015
```

A prefix never needs a trailing wildcard.

Because there's no length restriction beyond "at most 14 digits" and no
calendar check, odd prefixes like `20250` or `2025061412` or `20250614_25`
(hour 25) are also accepted and expanded the same mechanical way - they just
aren't documented as the intended usage in `docs/CLI.md`, since they don't
reliably mean what you'd expect.

---

## Prefix Expansion

Expansion pads the digit string out to 14 digits, then splits it into
`YYYYMMDD_HHMMSS`. `--from` pads with `"0"`, `--until` pads with `"9"`.

### --from

| Input | Result |
|-------|--------|
| `2025` | `20250101_000000` |
| `202506` | `20250601_000000` |
| `20250614` | `20250614_000000` |
| `20250614_08` | `20250614_080000` |
| `20250614_0830` | `20250614_083000` |
| `20250614_083015` | `20250614_083015` |

### --until

| Input | Result |
|-------|--------|
| `2025` | `20259999_999999` |
| `202506` | `20250699_999999` |
| `20250614` | `20250614_999999` |
| `20250614_08` | `20250614_089999` |
| `20250614_0830` | `20250614_083099` |
| `20250614_083015` | `20250614_083015` |

Note that padding with `9` doesn't produce a real calendar date or time
(`20259999_999999` isn't a valid month/day) - it doesn't need to. Any real
recording timestamp within the intended range (e.g. anything in 2025) is
still lexically `<=` a string of trailing 9s, and anything outside the
range compares strictly greater, so the interval membership test in
`TimeInterval.__contains__` still gives the right answer. This is what lets
the parser skip calendar math (leap years, 30 vs. 31 day months, etc.)
entirely.

---

## No Validation

The parser does not validate year, month, day, hour, minute, or second
values. `202513` (month 13), `20250230` (Feb 30th), and `20250614_25` (hour
25) are all accepted and expanded exactly like valid ones - they just don't
correspond to anything a real recording's timestamp would ever lexically
fall within, so in practice they tend to produce an empty or unexpected
range rather than raising an error.

The only things that *do* raise `ValueError`: more than one `_`, an `_` not
immediately after the 8th digit, more than 14 digits, non-digit characters,
or an empty string.

---

## Wildcards

There's no wildcard-specific handling in the parser at all - `*` (or any
other non-digit character) simply fails the `isdigit()` check in
`_expand()` and raises `ValueError("timestamp must contain digits only")`.
Inputs like `2025*` or `2025*14` are rejected as a side effect of that
check, not because the parser recognizes and special-cases `*` as a
wildcard token.

Genuine wildcard/pattern matching (matching multiple disjoint timestamps
rather than bounding a single interval) isn't implemented and would need to
be a separate feature with different semantics from `--from`/`--until`.

---

## API

```python
from blackvue.lexicaltimeparser import LexicalTimeParser, TimeInterval

interval: TimeInterval = LexicalTimeParser(
    timestamp=None,   # mutually exclusive with from_/until
    from_="20250614",
    until="20250615",
).parse()

interval.first  # "20250614_000000"
interval.last   # "20250615_999999"
```

`TimeInterval` is a frozen dataclass with `first`/`last` string fields and a
`__contains__` method, so `timestamp_str in interval` works directly - it
strips a trailing recording-kind suffix (`_E`, `_P`, etc.) off the
candidate timestamp before comparing.

---

## Usage

Typical usage is:

```python
interval = LexicalTimeParser(
    timestamp=args.timestamp,
    from_=args.from_,
    until=args.until,
).parse()

if recording.id.timestamp in interval:
    ...
```

All `bv-*` commands that select recordings by time should build a
`LexicalTimeParser` from their own `--timestamp`/`--from`/`--until` args
rather than implementing their own parsing.

---

## Implementation

Implementation file:

```
src/blackvue/lexicaltimeparser.py
```

Documentation:

```
docs/design/time-parser.md
```
