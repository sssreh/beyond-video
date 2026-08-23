# bv-stats(1)

## NAME

`bv-stats` - aggregate an archive's per-recording Stats assets into a summary report, grouped by calendar period

## SYNOPSIS

```
bv-stats [--from TIMESTAMP] [--until TIMESTAMP] [--timestamp TIMESTAMP]
         [--group {all,year,month,monthday,week,weekday}]
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
| `monthday` | `2026-08-23` | One bucket per exact calendar date. |
| `week` | `2026-W34` | One bucket per ISO 8601 week (Monday-Sunday). |
| `weekday` | `Monday` .. `Sunday` | One bucket per day-of-week name, **recurring** across every date in the selection - a genuinely different question from the other five (which all partition the selection into disjoint time spans; this one re-cuts the whole selection by a repeating pattern, e.g. "which day of the week do I drive most on"). |

### Fields

`--fields` selects which stats to report, as a comma-separated list of field keys (or `all` for every field). Run `bv-stats --list-fields` to print every available key with its unit and how it's combined across recordings in a bucket. Default: `duration_seconds,distance_km,avg_speed_kmh,max_speed_kmh,elevation_gain_m`.

Each field combines multiple recordings' own readings one of four ways: `sum` (total across the bucket - distance, time, elevation gain), `avg` (mean of each recording's own average - speed, g-force), `max`/`min` (the single largest/smallest reading in the bucket - peak speed, peak g-force, lowest altitude). A recording missing a field entirely (no GPS, no g-sensor, or an older `stats.json` from before a later field was added) simply doesn't contribute to that field's aggregate rather than counting as zero. A bucket where *no* recording in it has a reading for a field reports it as `-` (unknown), not `0`.

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
| `--group {all,year,month,monthday,week,weekday}` | Calendar period to group recordings by. Default: `all`. |
| `--fields FIELD1,FIELD2,...` | Comma-separated stats fields to report, or `all`. Default: `duration_seconds,distance_km,avg_speed_kmh,max_speed_kmh,elevation_gain_m`. |
| `--list-fields` | Print every field `--fields` accepts, with its unit and aggregation kind, then exit. Needs no archive. |
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

Which day of the week has the most driving:

```
bv-stats --group weekday --fields distance_km,duration_seconds
```

Every field, as JSON, for a future bv-web stats tab or other scripting:

```
bv-stats --group monthday --fields all --json
```

See every field `--fields` understands:

```
bv-stats --list-fields
```

## SEE ALSO

`bv-generate(1)` for producing the `Stats` asset this command reads. `bv-ls(1)` for listing an archive's recordings and their assets.
