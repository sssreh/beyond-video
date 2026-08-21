# bv-snap(1)

## NAME

`bv-snap` - grab one live snapshot per camera direction (Front/Rear/Interior)

## SYNOPSIS

```
bv-snap [--config-dir DIR] [--timeout SECONDS] --output DIR
        [--direction {F,R,I}]... (ID | --host HOST[:PORT])
```

## DESCRIPTION

`bv-snap` connects to a BlackVue camera - either one already set up via `bv-config` (`ID`, tried endpoint by endpoint) or a bare `--host` for a camera that hasn't been configured at all - and grabs one live JPEG frame per camera direction over `blackvue_live.cgi`, the same MJPEG feed `bv-live`/`bv-web`'s live view reads from, rather than anything already downloaded into the archive.

By default it requests every direction (Front, Rear, Interior). Each direction is independent: a request that errors, or that a camera model doesn't actually support, is silently dropped from the result rather than failing the whole run - `bv-snap` reports which directions it saved and warns (but still exits successfully) about any that came back empty. This matters in particular for Interior: some BlackVue firmware answers `direction=I` with a "Valid" HTTP response without ever serving a real image, so treating a missing Interior frame as a hard error would make `bv-snap` unusable on hardware where only Front/Rear actually work.

The camera's own live-view feed can keep serving whatever direction was live a moment ago for a short while after a switch, so each direction quietly discards a handful of frames before capturing the one that's actually saved - this costs a small amount of latency per direction but avoids the previous direction's image ending up in a differently-named file. Interior discards twice as many frames as Front/Rear, since it needs more settle time in practice. This is a tuned-by-feedback mitigation, not a guaranteed fix for every camera/firmware: the discard count started at 2, got raised to 8 after still seeing near-duplicates, then doubled again for Interior specifically - if you still see this, it may need raising further still. Note also that on a camera without a real Interior lens (e.g. a 2-camera Front/Rear-only setup), `direction=I` can come back as a genuine, non-empty frame that's simply whatever the encoder is actually showing at that moment (usually Rear) rather than erroring or returning nothing - so an `I` file that looks like a slightly time-shifted copy of `R` most likely means the hardware just doesn't have a separate Interior stream to switch to, not a bug in the capture itself.

Snapshots are saved as `snap_<id-or-host>_<timestamp>_<direction>.jpg` (one shared timestamp per run, `id-or-host` sanitized for filesystem safety - e.g. a `--host`'s `:PORT` becomes `_PORT`) into `--output`, a directory you choose - deliberately not the camera's own recording archive, since a snap is a one-off grab, not part of a recording. Including the id/host in the filename means a single `--output` directory shared across more than one camera still produces files that say which camera they came from.

`ID` and `--host` are mutually exclusive - exactly one is required, same as `bv-gps`. See `bv-gps(1)` for a `--snap` mode that does the same capture from within an existing `bv-gps` invocation, if you'd rather not have a second command.

## ARGUMENTS

| Argument | Description |
|---|---|
| `ID` | Camera system id (see `bv-config(1)`). Mutually exclusive with `--host`. |

## OPTIONS

| Option | Description |
|---|---|
| `--host HOST[:PORT]` | Connect directly to this address instead of a `bv-config`'d camera id - no `--config-dir` lookup, no endpoint fallback list. Mutually exclusive with `ID`. |
| `--config-dir DIR` | Directory camera configs live in. Default: the platform's standard config directory. Ignored when `--host` is given. |
| `--timeout SECONDS` | Per-endpoint connection timeout. Default: 5. |
| `--output DIR`, `-o DIR` | Directory to save the snapshot `.jpg` files into (created if it doesn't exist yet). Required. |
| `--direction {F,R,I}` | Only snap this direction - repeatable (e.g. `--direction F --direction R`). Default: every direction. |
| `-h`, `--help` | Show help and exit. |

## EXIT STATUS

| Code | Meaning |
|---|---|
| 0 | OK - at least one direction was saved (see warnings for any that were dropped). |
| 1 | Config error (missing/invalid camera config, or no endpoints configured). |
| 2 | Camera unreachable on every configured endpoint. |
| 3 | Camera reachable, but no snapshot was received for any requested direction. |

## EXAMPLES

```
bv-snap Kirby --output ~/snaps
```

```
F: saved /home/christer/snaps/snap_Kirby_20260821_180512_F.jpg
R: saved /home/christer/snaps/snap_Kirby_20260821_180512_R.jpg
bv-snap: home: no snapshot received for direction I
```

Only the front camera:

```
bv-snap Kirby --output ~/snaps --direction F
```

Probe a raw IP directly, no `bv-config` setup needed:

```
bv-snap --host 192.168.1.42 --output ~/snaps
```

## SEE ALSO

`bv-config(1)` to set up the camera this connects to, `bv-gps(1)` for its own `--snap` mode doing the same capture.
