# bv-lock(1)

## NAME

`bv-lock` - mark a time range in a BlackVue archive as already generated, so `bv-generate` skips it on future runs

## SYNOPSIS

```
bv-lock [PATH] [--config-dir DIR] [--from TIMESTAMP | --until TIMESTAMP | --timestamp TIMESTAMP] --lock-assets ASSET[,ASSET...]
bv-lock [PATH] [--config-dir DIR] [--from TIMESTAMP | --until TIMESTAMP | --timestamp TIMESTAMP] --unlock-assets ASSET[,ASSET...]
bv-lock [PATH] [--config-dir DIR] --list
```

## DESCRIPTION

Once a stretch of an archive has been through `bv-generate` for good - every asset type wanted, checked over, done - there's no reason to ever walk those recordings again. `bv-lock` records that decision as a small `.bv-lock.json` manifest file sitting next to the recordings themselves, and `bv-generate` checks it at the start of every run: if the selected range is already locked for every action flag given on that run, the whole range is skipped with a single summary line - no per-recording file checks, no `--overwrite` prompts, not even the cost of walking the archive's file list.

A lock is scoped to two things at once: an exact time range (the same `--from`/`--until`/`--timestamp` selection every other `bv-*` command uses - see `docs/CLI.md`) and a set of asset names (`extract-audio`, `get-duration`, `transcribe`, `translate`, `srt`, `describe-scene`, `diarize` - the same vocabulary `bv-generate`'s own action flags map to). A range locked for `get-duration` only doesn't block a later `bv-generate --transcribe` run over the same range - only a flag combination that's a subset of what's already locked gets skipped. Locking is deliberately exact-range matching, not range-merging: a sub-range wholly inside an already-locked range (say, a single day inside a locked year) is treated as covered, but two overlapping-and-different ranges are never merged into one - lock the same selection you generated with, typically a whole year at a time.

`translate` is a single blanket name covering every target language, not one lock per language - locking `translate` after generating Swedish translations also blocks a later `--translate es` run over the same range. This is intentional (needing a second language for already-finished recordings is rare); the rare case is handled by narrowing the selection to just the recording(s) that need it and passing `bv-generate`'s own `--ignore-lock` flag, rather than by unlocking the whole range.

Nothing about `bv-lock` deletes or checks the generated files themselves - it's purely a skip-ahead manifest for `bv-generate`. Other commands (`bv-ls`, `bv-export`, `bv-scribe`, `bv-search`) don't read it at all.

## OPTIONS

| Option | Description |
|---|---|
| `PATH` | Archive directory, or a configured camera system id (see `bv-config`) - resolved to that camera's archive directory. A path containing a separator is always used literally, never as an id. Defaults to `.`. |
| `--config-dir DIR` | Directory camera configs live in, for resolving `PATH` as a camera id. |
| `--from TIMESTAMP` | Only consider recordings from this timestamp onward (inclusive). |
| `--until TIMESTAMP` | Only consider recordings up to this timestamp (inclusive). |
| `--timestamp TIMESTAMP` | Only consider recordings matching this timestamp or prefix. Can't be combined with `--from`/`--until`. |
| `--lock-assets ASSET[,ASSET...]` | Mark these asset types as done for the selected range. Comma-separated, from: `extract-audio`, `get-duration`, `transcribe`, `translate`, `srt`, `describe-scene`, `diarize`. |
| `--unlock-assets ASSET[,ASSET...]` | Remove these asset types from the lock for the selected range. A name that was never locked for that exact range is silently ignored, not an error. |
| `--list` | List this archive's current locks and exit. Ignores `--from`/`--until`/`--timestamp` - always shows every lock entry, since a lock's own range is exactly what's being listed. |

Exactly one of `--lock-assets`, `--unlock-assets`, or `--list` is required.

## EXAMPLES

Lock all of 2019 once `bv-generate` has finished it for good:

```
bv-lock Kirby --timestamp 2019 --lock-assets get-duration,describe-scene
```

Future `bv-generate` runs over the same range skip immediately:

```
bv-generate Kirby --timestamp 2019 --get-duration --describe-scene
bv-generate: /path/to/archive - 20190000_000000..20199999_999999 already locked for [describe-scene, get-duration], skipping (see bv-lock --list, or --ignore-lock to run anyway)
```

See what's currently locked:

```
bv-lock Kirby --list
```

Need a fresh Swedish translation for one already-locked recording - narrow the selection and override the lock rather than unlocking the whole year:

```
bv-generate Kirby --timestamp 20190715_140000 --translate sv --ignore-lock
```

Unlock a range again (e.g. a bug meant a whole year needs reprocessing):

```
bv-lock Kirby --timestamp 2019 --unlock-assets get-duration,describe-scene
```

## SEE ALSO

`bv-generate(1)`, specifically its `--ignore-lock` flag and the lock-skip message it prints. `bv-config(1)` for setting up a camera system id.
