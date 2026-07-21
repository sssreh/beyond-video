# Time Parser Design

## Purpose

The time pilter provides a common implementation of `--from` and `--until`
for all `bv-*` commands.

It converts a user supplied timestamp prefix into a complete Beyond Video
timestamp that can be compared lexicographically.

The implementation is shared by:

- `bv-ls`
- `bv-copy`
- `bv-export`
- `bv-delete`
- `bv-trip`

Future commands should reuse this implementation instead of implementing
their own timestamp parsing.

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
timestamp >= from_timestamp
timestamp <= until_timestamp
```

No `datetime` objects are required.

---

## Accepted Prefixes

Users may enter any leading prefix of the canonical timestamp.

Examples:

```
2025
202506
20250614
20250614_08
20250614_0830
20250614_083015
```

A prefix never needs a trailing wildcard.

---

## Prefix Expansion

### --from

Missing fields are expanded to their minimum value.

| Input | Result |
|-------|--------|
| `2025` | `20250101_000000` |
| `202506` | `20250601_000000` |
| `20250614` | `20250614_000000` |
| `20250614_08` | `20250614_080000` |
| `20250614_0830` | `20250614_083000` |
| `20250614_083015` | `20250614_083015` |

---

### --until

Missing fields are expanded to their maximum value.

| Input | Result |
|-------|--------|
| `2025` | `20251231_235959` |
| `202506` | `20250630_235959` |
| `20250614` | `20250614_235959` |
| `20250614_08` | `20250614_085959` |
| `20250614_0830` | `20250614_083059` |
| `20250614_083015` | `20250614_083015` |

The parser must calculate the correct last day of the month.

---

## Validation

The parser validates:

- year
- month
- day
- hour
- minute
- second

Invalid values are rejected.

Examples:

```
202513
20250230
20250614_25
20250614_1260
20250614_123460
```

are invalid.

---

## Wildcards

Wildcards are **not** accepted by `--from` or `--until`.

Examples:

```
2025*
2025*14
2025*_08
```

must all be rejected.

Wildcards represent multiple independent timestamps and therefore cannot
define a single continuous interval.

Wildcard matching may be introduced later as a separate feature with
different semantics.

---

## API

The parser exposes two public functions.

```python
parse_from(prefix: str) -> str

parse_until(prefix: str) -> str
```

Both functions return a complete canonical timestamp.

---

## Usage

Typical usage is:

```python
from_ts = parse_from(args.from_)
until_ts = parse_until(args.until)

if from_ts <= recording.timestamp <= until_ts:
    ...
```

All `bv-*` commands should use these functions instead of implementing
their own timestamp parsing.

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