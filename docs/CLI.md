# Command line interface

This page documents the one thing shared across every `bv-*` command that reads an archive: **recording selection**. For each command's full option reference, see `docs/man/`. For the order commands are normally run in, see `docs/PIPELINE.md`.

> This page previously described a larger, aspirational selection syntax (`--type`, `--match`, `--latest`, `--last-hours`/`--last-minutes`/`--last-days`, and commands named `bv-find`/`bv-transcribe`) that was never actually built. None of those exist in the current CLI - this page now documents only what's real, cross-checked against each command's own `--help` output.

One command doesn't fit the recording-selection model below at all: `bv-history` browses the persistent record of every `bv-*` command run (direct-CLI and bv-web alike), the way pwsh/bash's own `history` browses shell history - see `docs/man/bv-history.md`.

## Camera system ID

`bv-config`, `bv-download`, `bv-gps`, `bv-live`, and `bv-snap` take a camera system ID as their first argument:

```text
bv-config Kirby
bv-download Kirby
bv-gps Kirby
bv-live Kirby
bv-snap Kirby --output ~/snaps
```

The ID identifies the camera's configuration and, through it, the local archive directory downloads are saved into (see `docs/man/bv-config.md`). It's an ASCII string suitable for filenames and command lines - a separate, free-form display name (which may contain UTF-8/emoji) is set alongside it in the config wizard.

`bv-download` alone also accepts `--host HOST --target DIR` instead of an ID - a direct one-off connection with no `bv-config` setup at all, for a quick download without committing to the rest of the toolkit. See `docs/man/bv-download.md`. `bv-gps`, `bv-snap`, and `bv-live` have the same quick-connect escape hatch via their own `--host`/`--camera-host` flag (mutually exclusive with the ID, no `--target` involved since none of the three touch the archive) - `bv-live` specifically uses `--camera-host` rather than `--host`, since it already has its own unrelated `--host` for the address its own web server listens on. See `docs/man/bv-gps.md`/`docs/man/bv-snap.md`/`docs/man/bv-live.md`.

`bv-gps`, `bv-live`, and `bv-snap` are the odd ones out among these five: none touches the archive at all, only a live connection to the camera itself (see `docs/man/bv-gps.md`/`docs/man/bv-live.md`/`docs/man/bv-snap.md` - one prints a single GPS reading and exits, one serves a persistent live dashboard, and one grabs a live F/R/I snapshot and exits) - they're listed here purely because they share the same camera-ID-as-first-argument shape as `bv-config`/`bv-download`, not because they take part in recording selection below. (`bv-gps` also has its own `--snap` one-shot mode doing exactly what `bv-snap` does, for anyone who'd rather not reach for a second command - see `docs/man/bv-gps.md`.)

`bv-ls`, `bv-generate`, `bv-export`, `bv-scribe`, `bv-search`, `bv-lock`, and `bv-stats` accept a camera system ID in the same `path` position a literal archive directory would go - `bv-ls Kirby` resolves `Kirby` to that camera's configured `archive` directory, same as `bv-download`/`bv-config` already do. Resolution only kicks in for a bare id: anything that looks like a real path (`.`/`..`, a `./`/`../` prefix, an absolute path, or anything containing a path separator) is always used literally, so a directory that happens to share a name with a configured camera still works via `./Kirby` or `.\Kirby` - the same escape hatch `git checkout ./file` uses for a same-named branch. A bare name that isn't a configured camera id falls back to being treated as a literal path too, so nothing about existing scripts using plain directory names changes. Each of these seven commands also takes its own `--config-dir` to point at a non-default camera config directory, matching `bv-config`'s own flag.

`bv-export` additionally reads an optional `Target` directory from the resolved camera's config (see `docs/man/bv-config.md`) and uses it as its own `--target` flag's default when `--target` isn't given explicitly on the command line - an explicit `--target` always wins. If `path` resolves to a camera with no `Target` set (or doesn't resolve to a camera at all), `--target` is still required.

## Recording selection by timestamp

`bv-download`, `bv-ls`, `bv-generate`, `bv-export`, `bv-scribe`, `bv-search`, `bv-lock` (with `--lock-assets`/`--unlock-assets` - `--list` ignores them and always shows every lock), and `bv-stats` all accept the same three timestamp options, narrowing which recordings/range a run considers (`bv-scribe --raw` is the one exception - it processes raw video files with no archive/recording-id structure at all, so these don't apply there; see `docs/man/bv-scribe.md`):

```text
--from TIMESTAMP
--until TIMESTAMP
--timestamp TIMESTAMP
```

`--timestamp` matches a single timestamp or prefix and can't be combined with `--from`/`--until`. `--from`/`--until` can be used together or independently to bound a range; `--until` is inclusive.

The parser is purely lexical, not a calendar parser: a timestamp is any run of 1-14 digits, optionally with a single `_` right after the 8th digit (`YYYYMMDD_HHMMSS`, with `HHMMSS` itself allowed to be any length from 1 to 6 digits). The six shapes below are the natural/expected ones - matching the real `YYYY`/`YYYYMM`/.../`YYYYMMDD_HHMMSS` boundaries of the underlying timestamp - and the ones worth actually using:

```text
YYYY
YYYYMM
YYYYMMDD
YYYYMMDD_HH
YYYYMMDD_HHMM
YYYYMMDD_HHMMSS
```

The parser never computes an actual calendar date - it just pads the digit string on the right (`0` for `--from`, `9` for `--until`) out to 14 digits and splits it. For a round prefix that padding lands exactly on a field boundary, so the raw result reads like a real timestamp even though it's still just string padding underneath:

| Option | Value | Raw expansion | Effectively means |
|---|---|---|---|
| `--from` | `202607` | `20260700_000000` | 2026-07-01 00:00:00 |
| `--until` | `202607` | `20260799_999999` | anything through 2026-07-31 23:59:59 |
| `--from` | `20260715` | `20260715_000000` | 2026-07-15 00:00:00 |
| `--until` | `20260715` | `20260715_999999` | anything through 2026-07-15 23:59:59 |
| `--from` | `20260715_14` | `20260715_140000` | 2026-07-15 14:00:00 |
| `--until` | `20260715_14` | `20260715_149999` | anything through 2026-07-15 14:59:59 |

An odd-length ("half") prefix is accepted exactly the same way, but the padding now lands in the *middle* of a field instead of at its edge - so the raw expansion stops looking like a real timestamp at all, and what it effectively selects gets harder to predict at a glance:

| Option | Value | Raw expansion | Effectively means |
|---|---|---|---|
| `--from` | `2026071` (7 digits) | `20260710_000000` | day `1` becomes day `10` (the `0`-pad lands in the day's *ones* digit) - not "any day starting with 1" |
| `--until` | `2026071` (7 digits) | `20260719_999999` | same field, `9`-padded - day becomes `19` |
| `--from` | `20260` (5 digits) | `20260000_000000` | padding spills into both month and day - month `00`, day `00` |
| `--until` | `20260` (5 digits) | `20260999_999999` | month `09`, day `99` - not a real calendar value, but still a correct lexical upper bound for "anything in 2026" |

None of the raw expansions above need to be valid calendar dates for the parser to work correctly - `TimeInterval` only ever compares these strings lexically against other zero-padded timestamps, never interprets them as real dates. But because a half-length prefix's raw expansion is unpredictable without doing this digit-by-digit math yourself, stick to the six round shapes listed above unless you have a specific reason not to.

The precision given determines the implied range - a short prefix like `--from 202607` means "anything in July 2026 onward," not "exactly July 2026 00:00:00 onward down to the second."

Examples:

```text
bv-download Kirby --from 20260715
bv-ls /path/to/archive --timestamp 20260715_14
bv-generate /path/to/archive --from 202607 --until 202608
bv-export /path/to/archive --target /path/to/trips --timestamp 20260715
bv-search /path/to/archive --timestamp 20260715 --text roundabout
bv-lock /path/to/archive --timestamp 2019 --lock-assets get-duration,describe-scene
bv-stats /path/to/archive --timestamp 2026 --group month
```

## What's genuinely per-command

Beyond the shared timestamp selection above, each command has its own distinct options - `bv-download --mode` picks which recording kinds get video downloaded, `bv-ls --trips`/`--all` change the output shape entirely, `bv-export` has its own trip-grouping (`--max-gap`, `--movement`) and dozens of `--stitch-*`/`--map-*` rendering flags. These aren't shared across commands and are documented per-command in `docs/man/`, not here.
