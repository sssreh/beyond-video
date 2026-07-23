# bv-download(1)

## NAME

`bv-download` - download recordings from a BlackVue camera

## SYNOPSIS

```
bv-download [--config-dir DIR] [--timeout SECONDS]
            [--mode {E,M,N,P,all}[,...]]
            [--from TIMESTAMP] [--until TIMESTAMP] [--timestamp TIMESTAMP]
            [--dry-run] [--yes] [-v] [--trace]
            ID
```

## DESCRIPTION

`bv-download` connects to a BlackVue camera (over its configured endpoints - see `bv-config(1)`) and downloads recordings into the camera's own target directory, building the local archive that every other `bv-*` command operates on.

By default it downloads video for **event** and **manual** recordings, plus the recording immediately before each one (for context leading up to the event). **Metadata** - thumbnails, GPS, g-sensor logs - is always downloaded for every recording regardless of mode, since it's small and useful even for recordings whose video isn't fetched. Use `--mode all` to download video for everything, including routine normal-driving and parking-mode footage.

If `--from`/`--until`/`--timestamp` is given without an explicit `--mode`, the default mode becomes `all` - requesting a specific time range already signals you want everything in it, not just the usual events-plus-context subset.

Endpoints configured in `bv-config` are tried in order; the first one that responds within `--timeout` is used for the whole run.

## ARGUMENTS

| Argument | Description |
|---|---|
| `ID` | Camera system id (see `bv-config(1)`). |

## OPTIONS

| Option | Description |
|---|---|
| `--config-dir DIR` | Directory camera configs live in. Default: the platform's standard config directory. |
| `--timeout SECONDS` | Per-endpoint connection timeout. Default: 5. |
| `--mode {E,M,N,P,all}[,...]` | Recording kinds to download video for (comma-separated, case-insensitive), or `all`. `E`=event, `M`=manual, `N`=normal, `P`=parking. Default: event/manual recordings plus the recording before each. |
| `--from TIMESTAMP` | Only consider recordings from this timestamp onward. |
| `--until TIMESTAMP` | Only consider recordings up to this timestamp. |
| `--timestamp TIMESTAMP` | Only consider recordings matching this timestamp or prefix. |
| `--dry-run` | List what would be downloaded without downloading it. |
| `--yes` | Skip the interactive range confirmation. |
| `-v`, `--verbose` | Print each file as it is downloaded. |
| `--trace` | Print a `.` for every 10MB downloaded - a simple progress indicator across the whole run, independent of `-v`. |
| `-h`, `--help` | Show help and exit. |

## TIMESTAMP FORMAT

`--from`/`--until`/`--timestamp` accept `YYYY`, `YYYYMM`, `YYYYMMDD`, `YYYYMMDD_HH`, `YYYYMMDD_HHMM`, or `YYYYMMDD_HHMMSS` - precision determines the implied range (e.g. `--from 202607` means the whole month of July 2026 onward).

## EXIT STATUS

| Code | Meaning |
|---|---|
| 0 | OK. |
| 1 | Config error (missing/invalid camera config). |
| 2 | Camera unreachable on every configured endpoint. |
| 3 | Aborted (e.g. declined the range confirmation). |

## EXAMPLES

Download the default set (events/manual + context) since a given time:

```
bv-download Kirby --from 20260715_1400
```

Download everything - including routine driving and parking footage - for a specific day:

```
bv-download Kirby --timestamp 20260715 --mode all
```

Preview what a run would fetch without downloading anything:

```
bv-download Kirby --dry-run
```

Unattended run (e.g. from a scheduled task), skipping the confirmation prompt and showing progress:

```
bv-download Kirby --yes --trace
```

## SEE ALSO

`bv-config(1)` to set up the camera this downloads from, `bv-ls(1)` to inspect the resulting archive.
