# bv-stats(1)

## NAME

`bv-stats` - aggregate an archive's per-recording Stats assets into a summary report, grouped by calendar period

## SYNOPSIS

```
bv-stats [--from TIMESTAMP] [--until TIMESTAMP] [--timestamp TIMESTAMP]
         [--group {all,year,month,date,week,weekday,monthday}]
         [--fields FIELD1,FIELD2,...] [--list-fields]
         [--json] [--config-dir DIR] [--trace]
         [PATH]
```

## DESCRIPTION

`bv-stats` reads every selected recording's `Stats` asset (the `<id>.stats.json` file `bv-generate --stats` writes - see `bv-generate(1)`), groups the recordings by a calendar period, and prints an aggregated summary per group: total distance, average/max speed, elevation gain, and so on. Selects recordings the same way every other `bv-*` command does - by timestamp/`--from`/`--until`/`--timestamp`.

A recording with no `Stats` asset yet is skipped (not an error) - run `bv-generate --stats` over the archive first to produce them. `bv-stats` reports how many recordings in range were skipped this way.

This command is deliberately split into a library half (`blackvue.stats_report`, the grouping/aggregation logic) and a thin CLI wrapper around it, the same way `bv-search`'s own `blackvue.search` module is - so a future bv-web stats tab (summary, graph, clickable points linking back to the underlying recordings) can call the aggregation directly instead of parsing this command's text output.

### Grouping

`--group` controls how recordings are bucketed:

| Grouping | Bucket | Meaning |
|---|---|---|
| `all` (default) | one bucket | Everything in range, summarized together. |
| `year` | `2026` | One bucket per calendar year. |
| `month` | `2026-08` | One bucket per calendar year+month. |
| `date` | `2026-08-23` | One bucket per exact calendar date. |
| `week` | `2026-W34` | One bucket per ISO 8601 week (Monday-Sunday). |
| `weekday` | `Monday` .. `Sunday` | One bucket per day-of-week name, **recurring** across every date in the selection - a genuinely different question from the other five (which all partition the selection into disjoint time spans; this one re-cuts the whole selection by a repeating pattern, e.g. "which day of the week do I drive most on"). |
| `monthday` | `01` .. `31` | One bucket per day-of-month number, **recurring** the same way `weekday` does (not to be confused with `date` above, which partitions into one bucket per exact date) - e.g. "which day of the month do I drive most on," and surfaces a single low-mileage day inside an otherwise normal month that a whole-month total would average away. Some months have fewer than 31 days, so buckets `29`-`31` will always have fewer contributing recordings than the rest - expected, not a bug. |

### Fields

`--fields` selects which stats to report, as a comma-separated list of field keys (or `all` for every field). Run `bv-stats --list-fields` to print every available key with its unit and how it's combined across recordings in a bucket. Default: `duration_seconds,distance_km,avg_speed_kmh,max_speed_kmh,elevation_gain_m`.

Each field combines multiple recordings' own readings one of four ways: `sum` (total across the bucket - distance, time, elevation gain), `avg` (mean of each recording's own average - speed, g-force), `max`/`min` (the single largest/smallest reading in the bucket - peak speed, peak g-force, lowest altitude). A recording missing a field entirely (no GPS, no g-sensor, or an older `stats.json` from before a later field was added) simply doesn't contribute to that field's aggregate rather than counting as zero. A bucket where *no* recording in it has a reading for a field reports it as `-` (unknown), not `0`.

`distance_km`, both speed fields, `moving_seconds`/`idle_seconds`, and all three altitude fields need a recording to have had at least two positioned GPS fixes at all (its `Stats` asset's own `has_gps` flag) - a recording can have a perfectly real, present `Stats` asset and still contribute nothing to any of these if it never got a usable GPS fix (cold-start acquisition delay, a tunnel, a Parking-mode clip recorded somewhere with no signal). Whenever `--fields` includes at least one of these, `bv-stats` reports "N of M recording(s) with Stats data have no GPS fix" alongside the usual "no Stats asset yet" line below - the two are different gaps (no `Stats` asset at all, vs. a real `Stats` asset with nothing GPS-derived in it) and a total can look short from either one. `duration_seconds` and every g-force field are unaffected by `has_gps` (duration has its own video/`.3gf` fallback chain, g-force comes from the g-sensor sidecar) and never trigger this message.

### Summary

A grouped breakdown (`--group` anything but `all`) can make the grand total hard to eyeball - e.g. seven `--group weekday` lines, or thirty-odd `--group date` lines, none of which is itself "the whole range." `--summary` adds one more section computed the same way `--group all` would (every selected recording, one bucket), printed ahead of the per-group breakdown in text mode, and as a `"summary"` key alongside `"buckets"` in `--json` mode (the plain list shape from before is unchanged when `--summary` isn't given, for anything already parsing this command's JSON output).

If the summary's own totals look lower than expected, check both coverage lines printed just above it: "N of M recording(s) in range have no Stats asset yet, skipped" (run `bv-generate --stats` over the archive first, or over more of it) and, for GPS-dependent fields, "N of M recording(s) with Stats data have no GPS fix" (see "Fields" above) - a total can look short from either gap, and they're not the same thing: the first means `bv-generate --stats` hasn't touched those recordings at all, the second means it has, but the recording itself never got a usable GPS fix.

### Estimating missing distance

`--estimate-gaps` fills in an *estimated* `distance_km` for every recording that has no real one (no GPS fix, or the rarer case of too few fixes to compute anything) but does have a `duration_seconds` reading - by multiplying that recording's own duration by an average speed basis (real distance divided by real duration, from whichever recordings in the same bucket do have both readings, falling back to the whole selection's basis if the bucket itself has none). Parking-mode recordings are never estimated - they're stationary by definition, so extrapolating a moving average speed onto one would invent distance that was never driven, not fill a real gap.

The estimated portion is never silently blended into the real number with no trace: a bucket's `distance_km` total includes it, but the text report appends "(includes ~X.XX km estimated from N recording(s) with no GPS fix)" on the same line, and `--json` output carries it as separate `estimated_distance_km`/`estimated_recording_count` keys on that bucket. This is still an estimate, not a measurement - useful for a sanity-check total (confirmed against Christer's own real Arlanda round trip: the raw GPS-only total undercounted his own known 107.7 km by about 15%, and `--estimate-gaps` closed roughly a third of that gap by filling in the recording right at trip start and the one right at trip end, both losing their GPS fix during acquisition/loss rather than being genuinely stationary) - but it can't recover distance from a recording that has neither a GPS fix nor a duration at all, and a bucket with no real GPS data anywhere in the whole selection has nothing to build a speed basis from in the first place.

## ARGUMENTS

| Argument | Description |
|---|---|
| `PATH` | Archive directory, or a camera system id (see `bv-config(1)`) - resolved to that camera's configured target directory. Default: current directory. |

## OPTIONS

### Selection

| Option | Description |
|---|---|
| `--config-dir DIR` | Directory camera configs live in, for resolving `PATH` as a camera id. Default: the platform's standard config directory. |
| `--from TIMESTAMP` | Only consider recordings from this timestamp. |
| `--until TIMESTAMP` | Only consider recordings up to this timestamp. |
| `--timestamp TIMESTAMP` | Only consider recordings matching this timestamp or prefix. |

### Report

| Option | Description |
|---|---|
| `--group {all,year,month,date,week,weekday,monthday}` | Calendar period to group recordings by. Default: `all`. |
| `--fields FIELD1,FIELD2,...` | Comma-separated stats fields to report, or `all`. Default: `duration_seconds,distance_km,avg_speed_kmh,max_speed_kmh,elevation_gain_m`. |
| `--list-fields` | Print every field `--fields` accepts, with its unit and aggregation kind, then exit. Needs no archive. |
| `--summary` | Also report an overall summary (totals across the whole selection, same as `--group all`) alongside the per-group breakdown. No effect when `--group` is already `all`. See "Summary" below. |
| `--estimate-gaps` | Fill in an estimated `distance_km` for recordings with no GPS fix but a real duration, extrapolated from average speed. See "Estimating missing distance" below. |
| `--json` | Print the aggregated report as JSON instead of a human-readable table. |

### General

| Option | Description |
|---|---|
| `--trace` | Print a `.` to stdout every 25 recordings scanned, so a long run shows it's still active. |
| `-h`, `--help` | Show help and exit. |

## EXIT STATUS

| Code | Meaning |
|---|---|
| 0 | OK (including "no recordings in range" and "none have a Stats asset yet" - both normal, successful results, not errors). |
| 1 | Argument error (e.g. bad `--timestamp`, unknown `--fields` value). |

## EXAMPLES

Archive-wide summary with the default fields:

```
bv-stats
```

Month-by-month distance and elevation gain for 2026:

```
bv-stats --timestamp 2026 --group month --fields distance_km,elevation_gain_m
```

Which day of the week has the most driving, with a grand total alongside the per-day breakdown:

```
bv-stats --group weekday --fields distance_km,duration_seconds --summary
```

Distance for a single day, with an estimate for any GPS-less recordings called out separately:

```
bv-stats --timestamp 20260823 --fields distance_km --estimate-gaps
```

Every field, as JSON, for a future bv-web stats tab or other scripting:

```
bv-stats --group date --fields all --json
```

See every field `--fields` understands:

```
bv-stats --list-fields
```

## SEE ALSO

`bv-generate(1)` for producing the `Stats` asset this command reads. `bv-ls(1)` for listing an archive's recordings and their assets.
