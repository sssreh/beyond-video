# bv-gps(1)

## NAME

`bv-gps` - fetch a BlackVue camera's current GPS reading live

## SYNOPSIS

```
bv-gps [--config-dir DIR] [--timeout SECONDS] [--no-address]
       (ID | --host HOST[:PORT])
```

## DESCRIPTION

`bv-gps` connects to a BlackVue camera - either one already set up via `bv-config` (`ID`, tried endpoint by endpoint) or a bare `--host` for a camera that hasn't been configured at all - and reads one GPS fix live from `blackvue_livedata.cgi`, the camera's own real-time telemetry feed - the same endpoint the official BlackVue app polls for live GPS/g-sensor data, not anything read from a recording.

It prints the fix as a coordinate pair pasteable straight into Google Maps' own search box, a clickable Google Maps link, and (unless `--no-address` is given) a reverse-geocoded street address, looked up via the same Nominatim service `bv-export`'s `trip_info.txt` uses.

`ID` and `--host` are mutually exclusive - exactly one is required. Endpoints configured in `bv-config` are tried in order; `--host` skips `bv-config` entirely and tries just that one address, no `--config-dir` lookup involved - handy for probing a list of candidate IPs (e.g. from `scan_blackvue_endpoints.py`) one at a time without setting each one up as a full camera first. Either way, the connection only works while the camera is actually reachable - typically while connected to its own WiFi AP - not after the fact and not remotely, since `blackvue_livedata.cgi` only reports the camera's *current* position, not a history.

If the camera currently has no GPS fix, it reports `(0.0, 0.0)` on this feed rather than anything meaningful - `bv-gps` treats that reading as "no fix" and says so, rather than printing a false location out in the Atlantic.

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
| `-h`, `--help` | Show help and exit. |

## EXIT STATUS

| Code | Meaning |
|---|---|
| 0 | OK. |
| 1 | Config error (missing/invalid camera config, or no endpoints configured). |
| 2 | Camera unreachable on every configured endpoint. |
| 3 | Camera reachable, but currently has no GPS fix. |
| 4 | Camera reachable, but `blackvue_livedata.cgi` never returned a readable GPS object. |

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

## SEE ALSO

`bv-config(1)` to set up the camera this connects to, `bv-download(1)` for downloading recordings from the same endpoints.
