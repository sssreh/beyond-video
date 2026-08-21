# bv-gps(1)

## NAME

`bv-gps` - fetch a BlackVue camera's current GPS reading live

## SYNOPSIS

```
bv-gps [--config-dir DIR] [--timeout SECONDS] [--no-address]
       [--snap --output DIR [--direction {F,R,I}]...]
       (ID | --host HOST[:PORT])
```

## DESCRIPTION

`bv-gps` connects to a BlackVue camera - either one already set up via `bv-config` (`ID`, tried endpoint by endpoint) or a bare `--host` for a camera that hasn't been configured at all - and reads one GPS fix live from `blackvue_livedata.cgi`, the camera's own real-time telemetry feed - the same endpoint the official BlackVue app polls for live GPS/g-sensor data, not anything read from a recording.

It prints the fix as a coordinate pair pasteable straight into Google Maps' own search box, a clickable Google Maps link, and (unless `--no-address` is given) a reverse-geocoded street address, looked up via the same Nominatim service `bv-export`'s `trip_info.txt` uses.

`ID` and `--host` are mutually exclusive - exactly one is required. Endpoints configured in `bv-config` are tried in order; `--host` skips `bv-config` entirely and tries just that one address, no `--config-dir` lookup involved - handy for probing a list of candidate IPs (e.g. from `scan_blackvue_endpoints.py`) one at a time without setting each one up as a full camera first. Either way, the connection only works while the camera is actually reachable - typically while connected to its own WiFi AP - not after the fact and not remotely, since `blackvue_livedata.cgi` only reports the camera's *current* position, not a history.

If the camera currently has no GPS fix, it reports `(0.0, 0.0)` on this feed rather than anything meaningful - `bv-gps` treats that reading as "no fix" and says so, rather than printing a false location out in the Atlantic.

`--snap` adds a live camera snapshot per direction (Front/Rear/Interior by default), saved to `--output`, alongside the normal GPS reading above rather than instead of it: the coordinates/Maps-link/address lines print first, then the snapshot report. The GPS side is best-effort here - if the camera has no fix yet, or the GPS feed errors, that's just a warning, not a failure; the snapshot still runs and `--snap`'s own exit code is what the command reports. This is the exact same capture `bv-snap(1)`'s own standalone command does (which has no GPS reading to report at all); `--snap` here just saves reaching for a second command when you're already invoking `bv-gps`. See `bv-snap(1)` for the snapshot behavior itself (per-direction drop-on-failure, filename format, etc.) - it's identical either way.

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
| `--no-address` | Skip the reverse-geocoding lookup and print only the coordinates and Google Maps link. |
| `--snap` | One-shot mode: grab a camera snapshot instead of a GPS reading (requires `--output`). See `bv-snap(1)`. |
| `--output DIR`, `-o DIR` | Directory to save `--snap`'s `.jpg` files into. Required with `--snap`, invalid without it. |
| `--direction {F,R,I}` | With `--snap`, only snap this direction - repeatable. Default: every direction. Invalid without `--snap`. |
| `-h`, `--help` | Show help and exit. |

## EXIT STATUS

| Code | Meaning |
|---|---|
| 0 | OK. |
| 1 | Config error (missing/invalid camera config, or no endpoints configured). |
| 2 | Camera unreachable on every configured endpoint. |
| 3 | Camera reachable, but currently has no GPS fix. |
| 4 | Camera reachable, but `blackvue_livedata.cgi` never returned a readable GPS object. |
| 5 | `--snap` mode: camera reachable, but no snapshot was received for any requested direction. |

## EXAMPLES

```
bv-gps Kirby
```

```
Coordinates: 59.334591,18.06324
Google Maps: https://www.google.com/maps?q=59.334591,18.06324
Address: Drottninggatan 1, Stockholm, Sweden
```

Skip the address lookup:

```
bv-gps Kirby --no-address
```

Probe a raw IP directly, no `bv-config` setup needed:

```
bv-gps --host 192.168.1.42
```

Check a whole list of candidate IPs, one per line in `ips.txt`:

```
while read -r ip; do bv-gps --host "$ip" --no-address; done < ips.txt
```

Grab F/R/I snapshots alongside the normal GPS reading:

```
bv-gps Kirby --snap --output ~/snaps
```

## SEE ALSO

`bv-config(1)` to set up the camera this connects to, `bv-download(1)` for downloading recordings from the same endpoints, `bv-snap(1)` for the standalone version of `--snap`'s snapshot capture.
