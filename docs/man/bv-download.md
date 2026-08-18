# bv-download(1)

## NAME

`bv-download` - download recordings from a BlackVue camera

## SYNOPSIS

```
bv-download [--config-dir DIR] [--timeout SECONDS]
            [--mode {A,E,M,N,P,all}[,...]]
            [--from TIMESTAMP] [--until TIMESTAMP] [--timestamp TIMESTAMP]
            [--dry-run] [--files] [--yes] [-v] [--trace]
            (ID | --host HOST --target DIR | --media DIR (ID | --target DIR))
```

## DESCRIPTION

`bv-download` gets recordings into a local archive, building the archive that every other `bv-*` command operates on. Three ways to say where recordings come from and where they go:

- `ID` - a camera set up with `bv-config(1)`, tried over its configured endpoints in order, downloading into that camera's own archive directory.
- `--host HOST --target DIR` - a direct one-off network connection, no config needed: connects straight to `HOST` (e.g. the camera's WiFi IP) and downloads into `DIR`. Useful for a quick download without ever running `bv-config`. It gives up the fallback-endpoint list (a config can list several endpoints tried in order, e.g. home WiFi then a cellular router; `--host` only ever tries the one address given) and the RecordTime snapshot bookkeeping (see below) - both are conveniences tied to the rest of the toolkit's archive conventions, skipped here to keep this path a bare download with nothing extra written beyond the recordings themselves. Every other flag on this page works the same either way.
- `--media DIR` - import recordings directly from a mounted SD card, USB-connected camera, or other removable media at `DIR` instead of connecting over the network at all: no CGI protocol, just a plain filesystem copy of BlackVue's own on-camera files. Combine with `ID` to import into that camera's configured archive (with the same RecordTime snapshot bookkeeping as a normal network download), or with `--target DIR` for a one-off import with no config, same as `--host`. `DIR` is scanned recursively for files matching BlackVue's own filename convention (`YYYYMMDD_HHMMSS_KD.ext`, the same shape `bv-download` itself writes) - anything else on the card is silently ignored, not an error; a card with no recognizable filenames simply reports zero recordings found. Cannot be combined with `--host`. A recording already fully present at the destination (every file, matching size) is dropped from the "Matching recordings" listing/confirmation prompt entirely, not just skipped internally - re-running an import against the same card only ever lists and confirms what's actually new. `--sdcard` is accepted as a deprecated hidden alias for `--media` (same flag, kept working for anyone with the old name in a script) - it's the original, released (v1.0.0) name, from before the flag also covered USB-connected cameras like GoPro.

Exactly one source is required. `--host` can't be combined with `ID` or `--media`. `--media` can be combined with `ID`; otherwise it needs `--target`. `--target` can't be combined with `ID` (it only ever pairs with `--host`, or a bare `--media`).

By default it downloads video for **event** and **manual** recordings, plus the recording immediately before each one (for context leading up to the event). **Metadata** - thumbnails, GPS, g-sensor logs - is always downloaded for every recording regardless of mode, since it's small and useful even for recordings whose video isn't fetched. Use `--mode all` to download video for everything, including routine normal-driving and parking-mode footage.

If `--from`/`--until`/`--timestamp` is given without an explicit `--mode`, the default mode becomes `all` - requesting a specific time range already signals you want everything in it, not just the usual events-plus-context subset.

Every run prints one line up front stating the camera and the folder it's downloading into (or would, under `--dry-run`), and one line per recording as it's actually downloaded (`<id>: downloaded (video+metadata)` or `<id>: downloaded (metadata only)`) - neither gated behind `--verbose`, since both are basic confirmation of what's happening rather than extra diagnostic detail. `--verbose` adds detail on top of this (sidecar-probe results, an "already up to date" line for a recording that needed no download, etc.), it doesn't turn the per-file confirmation on or off.

When a recording's own video file(s) actually transferred, the "downloaded" line is suffixed with how long each one took, by camera direction letter - e.g. `20260818_092350_N: downloaded (video+metadata) [F 4.2s, R 4.0s]`. Once the whole run finishes, a single summary line reports the total across every sidecar (`.gps`/`.3gf`/`.thm`) file downloaded in the run - e.g. `sidecar files: 6 downloaded in 1.8s total` - printed only if at least one sidecar file was actually transferred. A recording (or sidecar) that needed no work because it was already fully present doesn't contribute a measurement to either figure.

Endpoints configured in `bv-config` are tried in order; the first one that responds within `--timeout` is used for the whole run.

`blackvue_vod.cgi`'s own recording listing has, across every camera model confirmed so far, only ever contained video files - the `.gps`/`.3gf`/`.thm` (thumbnail) sidecar files exist and download fine directly at the expected path even though the listing never mentions them. `bv-download` opportunistically checks for all three whenever a recording's listing doesn't already include them, and adds them if the camera actually has them. This costs nothing extra on a camera/firmware combination that already lists everything (no additional requests at all) and just fills the gap on one that doesn't. Checked one recording at a time as each is actually listed or downloaded - not upfront for the whole matching range before the confirmation prompt - so on a camera/firmware combination that does need these extra requests, `--verbose`'s "found ..." lines only ever appear once a download is genuinely underway (or being previewed with `--dry-run`), not before you've been asked to proceed.

If a network error (timeout, dropped connection) interrupts sidecar-checking or downloading for one recording, that recording is skipped - reported on stderr with its id and the underlying error - and the run continues with the rest of the range, rather than aborting the whole batch on one bad recording. The run's exit status reflects whether anything was skipped this way (see EXIT STATUS below).

For `--media`, a recording every one of whose files already matches (by size) what's at the destination is skipped before the range is even printed - `bv-download: N recording(s) already fully downloaded, skipping` - so a repeat import against a card that's already been pulled reports and confirms only genuinely new recordings, not a full re-listing of everything on the card.

Each `ID`-based run also reads the camera's current `config.ini` (over the network for `ID`/`--host` runs, directly off the card for `ID`-backed `--media` runs - tried at `Config/config.ini` and the card's own root) and, if either the configured `RecordTime` (recording segment length) differs from the last value recorded for this archive, or this run's earliest recording isn't already covered by an existing snapshot (e.g. you downloaded an earlier batch after a later one), writes a small snapshot file (`<recording>.record_time.txt`, holding just that one number in seconds) - never a copy of `config.ini` itself, which also carries Wi-Fi/cloud credentials that must never leave the camera. `bv-export` uses these snapshots to derive its own `--max-gap` default from the camera's real segment length instead of a flat constant - see `bv-export(1)`. Snapshots apply forward from the recording they're anchored to, never retroactively, so a recording earlier than every existing snapshot needs its own (even if the value is unchanged) or `bv-export`/`bv-ls` will fall back to a flat 300-second default for it. This is best-effort: any failure reading `config.ini` (older firmware, a transient network error, no `config.ini` found on the imported media) is silently ignored (or reported with `-v`) and never fails the download itself. A bare one-off run - `--host`, or `--media` used without `ID` - skips this step entirely (see above).

## ARGUMENTS

| Argument | Description |
|---|---|
| `ID` | Camera system id (see `bv-config(1)`). Omit this and use `--host`/`--target` (or `--media`/`--target`) instead for a config-free connection. Required unless `--host` or `--media` is given; can't be combined with `--host`, and can't be combined with `--target`. |

## OPTIONS

| Option | Description |
|---|---|
| `--host HOST` | Connect directly to this camera address (e.g. its WiFi IP) instead of looking up a configured id. Requires `--target`; can't be combined with `ID` or `--media`. |
| `--media DIR` | Import recordings from a mounted SD card, USB-connected camera, or other removable media at `DIR` instead of connecting over the network. Combine with `ID` to import into that camera's configured archive, or with `--target` for a one-off import with no config. Can't be combined with `--host`. `--sdcard` still works as a deprecated hidden alias for this flag. |
| `--target DIR` | Directory to download into. Requires `--host` or a bare `--media` (no `ID`). |
| `--config-dir DIR` | Directory camera configs live in. Default: the platform's standard config directory. |
| `--timeout SECONDS` | Per-endpoint connection timeout, and the timeout for every other camera request in the run (sidecar probes, downloads). Default: 30. The bv-web job form uses its own separate 5s default. |
| `--mode {A,E,M,N,P,all}[,...]` | Recording kinds to download video for (comma-separated, case-insensitive), or `all`. `E`=event, `M`=manual, `N`=normal, `P`=parking, `A`=unknown meaning (observed on real hardware but not yet identified - see `WORKING_CONTEXT.md`). Default: event/manual recordings plus the recording before each. |
| `--from TIMESTAMP` | Only consider recordings from this timestamp onward. |
| `--until TIMESTAMP` | Only consider recordings up to this timestamp. |
| `--timestamp TIMESTAMP` | Only consider recordings matching this timestamp or prefix. |
| `--dry-run` | List what would be downloaded without downloading it. |
| `--files` | With `--dry-run`, list every individual file (video, thumbnail, GPS, gsensor, etc.) for each matching recording, and whether it would be downloaded, instead of one summary line per recording id. Requires `--dry-run`. |
| `--yes` | Skip the interactive range confirmation. |
| `-v`, `--verbose` | Extra diagnostic detail: sidecar-probe results, RecordTime-backfill notes, and "already up to date" lines for recordings that needed no download. Per-recording download confirmation itself (`<id>: downloaded (...)`) always prints, with or without this flag - see below. |
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
| 4 | One or more recordings failed partway through (network error) and were skipped; everything else in the range was still attempted. |

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

Preview the same run at the individual-file level, instead of one line per recording:

```
bv-download Kirby --dry-run --files
```

```
20260715_140212_E:
  20260715_140212_EF.mp4: download
  20260715_140212_ER.mp4: download
  20260715_140212_EF.thm: download
  20260715_140212_E.gps: download
20260715_140312_N:
  20260715_140312_NF.mp4: skip
  20260715_140312_NR.mp4: skip
  20260715_140312_NF.thm: download
  20260715_140312_N.gps: download
```

Unattended run (e.g. from a scheduled task), skipping the confirmation prompt and showing progress:

```
bv-download Kirby --yes --trace
```

Download straight from a camera's WiFi IP, no `bv-config` setup at all:

```
bv-download --host 10.99.88.1 --target ~/blackvue-archive
```

Import from a mounted SD card into a configured camera's own archive:

```
bv-download Kirby --media /Volumes/BLACKVUE
```

Import from a mounted SD card with no config at all, straight into a folder:

```
bv-download --media /Volumes/BLACKVUE --target ~/blackvue-archive
```

## SEE ALSO

`bv-config(1)` to set up a camera for repeat use (not needed for a one-off `--host`/`--target` download), `bv-ls(1)` to inspect the resulting archive.
