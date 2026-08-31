# bv-drivers(1)

## NAME

`bv-drivers` - build or refresh the driver-knowledge base (who was driving on each trip)

## SYNOPSIS

```
bv-drivers [PATH] [--config-dir DIR] [--from TIMESTAMP | --until TIMESTAMP | --timestamp TIMESTAMP]
           [--max-gap MINUTES] [--gap-tolerance SECONDS] [--min-visits N] [--trace] [--debug]
```

## DESCRIPTION

Scans an archive's detected trips (the same `TripBuilder` logic `bv-ls --trips`/`bv-export` use), and for each one works out how long the vehicle was away from home, whether the stop ended in a downloaded Parking-mode (P) recording (categorized as "parked") or not ("no parking file"), and the trip's weekday/time in the camera's own raw clock (no DST correction - see `trip/place_knowledge.py`'s `local_weekday_and_time()` docstring for why). Home itself - Hammarby Sjöstad/Heliosgatan, where the vehicle has underground parking - is never counted as a "stop."

Every destination visited more than once is clustered into a **common place**: one entry with a single driver rule Christer sets via bv-web's `/drivers` page. Once set, every trip to that place - past or future - inherits it automatically; a place both drivers visit (no single rule fits) is best left unset and handled per-trip instead. A destination that only shows up once falls through to `driver_detect.py`'s increment-1 named-pattern matching, and failing that, sits in `/drivers`' specific-trips list for a one-off manual override - which always wins over a place's own rule, so it also covers a shared place one visit at a time.

A place whose own trips are already resolved (via those per-trip overrides) to more than one driver - Christer's real "Globen Parking," for instance - is reported as **Mixed**, not as still needing a rule. There's no single rule that could ever be right for a place both drivers actually use, so it's excluded from both the "without a driver rule" count and `/drivers`' &#9888; warning marker; it gets its own "Mixed" tag there instead.

The result is written to `driver_knowledge.json` under `--config-dir`, which `/drivers` reads and edits. Re-running this command is meant to be routine - every place's label and driver, and every per-trip manual override, survive a rebuild untouched; only visit counts, weekday/time, and the trip list itself are refreshed from the archive's current state. A run scoped with `--from`/`--until`/`--timestamp` only rescans that window - any trip/place outside it is carried forward untouched, never dropped.

Every save also updates a second, separate file - `common_places.json`, alongside `driver_knowledge.json` under the same `--config-dir` - as redundant insurance against exactly the same kind of loss the carry-forward behavior above already guards against, working even if a bug ever broke that primary path. It's a "living mirror": a place already in it gets its label/driver refreshed to match `driver_knowledge.json`'s current state on every save, but a place that's in it and *missing* from the current build (dropped by a scoped rebuild, or filtered out upstream) is left untouched rather than removed - so a place, once known, can never be silently lost from this file specifically.

This command is scoped to the live archive it's pointed at - there's no cross-year aggregation, and no attempt to reconcile place labels across separate archives. Christer's own framing: addresses and routines will drift over time, so this is a hand-reviewed, continuously-refreshed registry, not something meant to stay accurate forever without revisiting.

## OPTIONS

| Option | Description |
|---|---|
| `PATH` | Archive directory, or a configured camera system id (see `bv-config`) - resolved to that camera's archive target. Defaults to `.`. |
| `--config-dir DIR` | Directory camera configs, `driver_profiles.json`, and `driver_knowledge.json` live in (default: the usual `default_config_dir()`). |
| `--from TIMESTAMP` | Only consider recordings from this timestamp onward. |
| `--until TIMESTAMP` | Only consider recordings up to this timestamp. |
| `--timestamp TIMESTAMP` | Only consider recordings matching this timestamp or prefix. Can't be combined with `--from`/`--until`. |
| `--max-gap MINUTES` | Largest gap between two recordings that still counts as the same trip (same meaning as `bv-export`'s own flag). |
| `--gap-tolerance SECONDS` | Small fixed margin added on top of `--max-gap`. |
| `--min-visits N` | How many visits make a place "common" enough to flag as without a driver rule (default: 2) - purely a reporting threshold for the summary counts and `/drivers`' warning marker; every place with 2+ visits is still built and saved regardless. |
| `--trace` | Print a `.` every 10 trips resolved, so a long run over a large archive shows it's still active. |
| `--debug` | Print elapsed time for each phase (archive scan, trip detection, known-place geocoding, per-trip GPS fix resolution, driver/place resolution). |

## EXAMPLES

Build (or refresh) the knowledge base for the whole Kirby archive:

```
bv-drivers Kirby
bv-drivers: 214 trip(s), 18 place(s)
bv-drivers: 176/214 trip(s) resolved to a driver
bv-drivers: 5 common place(s) (>= 2 visits) without a driver rule
bv-drivers: 1 common place(s) with drivers split across trips (mixed - already handled per-trip)
bv-drivers: 38 trip(s) still undecided
bv-drivers: wrote /home/christer/beyond-video-data/.config/driver_knowledge.json
```

Then open bv-web's `/drivers` page to fill in each place's driver and any one-off trip overrides.

Refresh just this year so far, watching progress on a slow archive:

```
bv-drivers Kirby --timestamp 2026 --trace --debug
```

## SEE ALSO

`bv-web`'s `/drivers` page, where the common-places registry and specific-trip overrides this command builds are actually edited. `trip/driver_detect.py` for the increment-1 named-pattern matcher this builds on. `bv-config(1)` for setting up a camera system id.
