# bv-history(1)

## NAME

`bv-history` - browse the persistent command-history index every `bv-*` command writes to

## SYNOPSIS

```
bv-history [--last N | --all] [--command NAME] [--camera TEXT]
           [--from TIMESTAMP] [--until TIMESTAMP] [--timestamp TIMESTAMP]
           [--failed-only] [--search TEXT] [--source {cli,bv-web}]

bv-history show ID
```

## DESCRIPTION

`bv-history` browses the persistent record of every `bv-*` invocation - typed straight into a terminal or triggered from bv-web - the same idea as pwsh/bash's own `history`. Every command already records one entry per run to `history.jsonl` (see `docs/WEB_ARCHITECTURE.md`'s Job runner section); `bv-history` is the command that reads it back.

Entries are numbered by absolute position in the full history, oldest first - the same convention bash/pwsh's own `history` uses. A number stays stable as new entries are appended, and a filtered view still shows each match's real original number, not a 1..N renumbering of the subset shown.

The default listing is tail-style: only the most recent 15 matching entries, still shown oldest-to-newest within that slice (the same shape plain `tail`/`history 20` take) - use `--all` to see every match instead.

`bv-history show ID` dumps one past run's full logged output, best-effort reconstructed from the persistent output log (`core/joblog.py`) by matching lines tagged with that run's own command name inside its `[started, started+duration]` time window. Two real limitations: a month that's already rotated/pruned away has nothing left to show, and two runs of the *same* command overlapping in time can't be told apart from each other (their output shares the same tag).

## ARGUMENTS

| Argument | Description |
|---|---|
| `ID` (`show` only) | Entry number, from the main listing. |

## OPTIONS

| Option | Description |
|---|---|
| `--last N` | Show only the N most recent matching entries. Default: 15. |
| `--all` | Show every matching entry, ignoring `--last`'s default limit. |
| `--command NAME` | Only show entries for this command (e.g. `bv-ls`). |
| `--camera TEXT` | Only show entries whose full command line mentions this text (a camera id or archive path, typically) - a plain substring match. |
| `--from TIMESTAMP` | Only show entries started at or after this timestamp. |
| `--until TIMESTAMP` | Only show entries started at or before this timestamp. |
| `--timestamp TIMESTAMP` | Only show entries matching this timestamp or prefix. |
| `--failed-only` | Only show entries that failed or were interrupted. |
| `--search TEXT` | Only show entries whose full command line contains TEXT - same idea as `--camera`, offered separately for convenience. |
| `--source {cli,bv-web}` | Only show entries from this source. |

## EXAMPLES

Show the most recent commands run:

```
bv-history
```

Find every `bv-ls` run and the options used, going back through the whole history:

```
bv-history --command bv-ls --all
```

Show only failed/interrupted runs:

```
bv-history --failed-only
```

Dump the full output of a past run:

```
bv-history show 42
```

## SEE ALSO

`docs/WEB_ARCHITECTURE.md`'s Job runner section (the persistent command-history/output-log design); every other `bv-*(1)` man page (each one's own invocation is what populates this history).
