# bv-search(1)

## NAME

`bv-search` - search an archive's transcripts/translations/scene descriptions by text and/or GPS proximity

## SYNOPSIS

```
bv-search [--from TIMESTAMP] [--until TIMESTAMP] [--timestamp TIMESTAMP]
          [--text PATTERN] [--asset {all,transcript,translation,scene}]
          [--regex] [--case-sensitive]
          [--near LAT,LON | --place NAME] [--radius METERS]
          [PATH]
```

## DESCRIPTION

`bv-search` searches recordings in a local archive by text content, GPS proximity, or both combined. Selects recordings the same way every other `bv-*` command does - by timestamp/`--from`/`--until`/`--timestamp` - then narrows to whichever of those recordings match every criterion given.

At least one of `--text`, `--near`, or `--place` must be given. When more than one is given, a recording only matches if it satisfies all of them (logical AND) - e.g. `--text pothole --near 59.33,18.07` finds recordings that both mention "pothole" *and* have a GPS fix near that point, not either one alone.

**Text search** (`--text`) looks across a recording's transcript, translation, and scene-description text files - whichever of those it actually has (see `bv-generate(1)`/`bv-scribe(1)` for how they're produced). Diarized transcript/translation and the rear-camera scene description are included by default alongside their plain/front counterparts. Restrict to one category with `--asset`.

**GPS proximity search** (`--near`/`--place`) checks a recording's `.gps` track for any valid fix within `--radius` meters of a point, reporting the closest one. `--near` takes a raw coordinate; `--place` geocodes a free-text place name to a coordinate first via OpenStreetMap Nominatim (needs network access the first time a given name is looked up; results are cached to disk under `<archive>/.osm_cache` afterward, the same cache directory/pattern `bv-export`'s reverse geocoding already uses).

When `--place` resolves to a road or an area (rather than a point-like address/POI), Nominatim's own reply includes the match's actual line/boundary geometry, not just one representative point - `bv-search` uses that geometry automatically, measuring distance to the nearest point *along the whole road* (or area boundary) instead of to a single coordinate. This matters for long roads specifically: a single point somewhere along a multi-kilometer road would make `--radius` only cover a small stretch near that one point, missing recordings near the rest of the road entirely. A confirmation line reports whether this happened (`"<name>" -> lat,lon (road/area geometry, N segment(s) - ...)`).

## ARGUMENTS

| Argument | Description |
|---|---|
| `PATH` | Archive directory. Default: current directory. |

## OPTIONS

### Selection

| Option | Description |
|---|---|
| `--from TIMESTAMP` | Only consider recordings from this timestamp. |
| `--until TIMESTAMP` | Only consider recordings up to this timestamp. |
| `--timestamp TIMESTAMP` | Only consider recordings matching this timestamp or prefix. |

### Text search

| Option | Description |
|---|---|
| `--text PATTERN` | Search for `PATTERN` in transcript/translation/scene-description text. Case-insensitive substring match by default. |
| `--asset {all,transcript,translation,scene}` | Restrict `--text` to one category of text asset. `all` (default) covers transcript, translation, and scene description, including diarized/rear variants. |
| `--regex` | Treat `PATTERN` as a regular expression instead of a plain substring. |
| `--case-sensitive` | Make `--text` case-sensitive. Default: case-insensitive. |

### GPS proximity search

| Option | Description |
|---|---|
| `--near LAT,LON` | Only consider recordings with a GPS fix within `--radius` of this coordinate. Mutually exclusive with `--place`. |
| `--place NAME` | Same as `--near`, but geocodes a free-text place name to a coordinate first via Nominatim. Mutually exclusive with `--near`. |
| `--radius METERS` | Search radius for `--near`/`--place`. Default: `200`. |

### General

| Option | Description |
|---|---|
| `-h`, `--help` | Show help and exit. |

## EXIT STATUS

| Code | Meaning |
|---|---|
| 0 | OK (including "no matches" - that's a normal, successful search result, not an error). |
| 1 | Argument error (e.g. no search criterion given, bad `--timestamp`, unparsable `--near`). |
| 2 | Completed, but one or more recordings had errors (e.g. an unreadable text file), or `--place` failed to resolve. |

## EXAMPLES

Find every mention of "roundabout" across a day's transcripts/translations/scene descriptions:

```
bv-search --timestamp 20260715 --text roundabout
```

Search only scene descriptions, case-sensitively, for an exact license-plate-looking string:

```
bv-search --text "ABC123" --asset scene --case-sensitive
```

Find recordings that passed within 150m of a coordinate:

```
bv-search --near 59.3293,18.0686 --radius 150
```

Find recordings near a named place instead of a raw coordinate:

```
bv-search --place "Slussen, Stockholm" --radius 300
```

Combine both: recordings that mention "construction" *and* were near a specific intersection:

```
bv-search --text construction --near 59.3293,18.0686 --radius 200
```

Regex search across a whole month:

```
bv-search --from 202607 --until 202607 --text "speed(ing)?" --regex
```

## SEE ALSO

`bv-ls(1)` for listing an archive's recordings and their assets. `bv-generate(1)`/`bv-scribe(1)` for producing the transcript/translation/scene-description text this command searches.
