# Command line interface

This page documents the one thing shared across every `bv-*` command that reads an archive: **recording selection**. For each command's full option reference, see `docs/man/`. For the order commands are normally run in, see `docs/PIPELINE.md`.

> This page previously described a larger, aspirational selection syntax (`--type`, `--match`, `--latest`, `--last-hours`/`--last-minutes`/`--last-days`, and commands named `bv-find`/`bv-transcribe`) that was never actually built. None of those exist in the current CLI - this page now documents only what's real, cross-checked against each command's own `--help` output.

## Camera system ID

`bv-config` and `bv-download` take a camera system ID as their first argument:

```text
bv-config Kirby
bv-download Kirby
```

The ID identifies the camera's configuration and, through it, the local archive directory downloads are saved into (see `docs/man/bv-config.md`). It's an ASCII string suitable for filenames and command lines - a separate, free-form display name (which may contain UTF-8/emoji) is set alongside it in the config wizard.

`bv-ls`, `bv-generate`, and `bv-export` don't take a camera ID - they operate directly on an archive directory (the same directory `bv-download` wrote into), given as a plain path argument.

## Recording selection by timestamp

`bv-download`, `bv-ls`, `bv-generate`, and `bv-export` all accept the same three timestamp options, narrowing which recordings a run considers:

```text
--from TIMESTAMP
--until TIMESTAMP
--timestamp TIMESTAMP
```

`--timestamp` matches a single timestamp or prefix and can't be combined with `--from`/`--until`. `--from`/`--until` can be used together or independently to bound a range; `--until` is inclusive.

Accepted timestamp formats, from least to most precise:

```text
YYYY
YYYYMM
YYYYMMDD
YYYYMMDD_HH
YYYYMMDD_HHMM
YYYYMMDD_HHMMSS
```

The precision given determines the implied range - a short prefix like `--from 202607` means "anything in July 2026 onward," not "exactly July 2026 00:00:00 onward down to the second." Concretely:

| Option | Value | Expands to |
|---|---|---|
| `--from` | `202607` | `2026-07-01 00:00:00` |
| `--until` | `202607` | `2026-07-31 23:59:59` |
| `--from` | `20260715` | `2026-07-15 00:00:00` |
| `--until` | `20260715` | `2026-07-15 23:59:59` |
| `--from` | `20260715_14` | `2026-07-15 14:00:00` |
| `--until` | `20260715_14` | `2026-07-15 14:59:59` |

Examples:

```text
bv-download Kirby --from 20260715
bv-ls /path/to/archive --timestamp 20260715_14
bv-generate /path/to/archive --from 202607 --until 202608
bv-export /path/to/archive --target /path/to/trips --timestamp 20260715
```

## What's genuinely per-command

Beyond the shared timestamp selection above, each command has its own distinct options - `bv-download --mode` picks which recording kinds get video downloaded, `bv-ls --trips`/`--all` change the output shape entirely, `bv-export` has its own trip-grouping (`--max-gap`, `--movement`) and dozens of `--stitch-*`/`--map-*` rendering flags. These aren't shared across commands and are documented per-command in `docs/man/`, not here.
