# bv-export(1)

## NAME

`bv-export` - detect trips in a BlackVue archive and export each one into its own folder

## SYNOPSIS

```
bv-export --target DIR [--prefix PREFIX]
          [--from TIMESTAMP] [--until TIMESTAMP] [--timestamp TIMESTAMP]
          [--max-gap MINUTES] [--movement] [--no-duration] [--gap-tolerance SECONDS]
          [--map] [--map-icon PATH] [--map-zoom [METERS]]
          [--gsensor-video]
          [--stitch] [--stitch-layout LAYOUT]
          [--stitch-mirror-size PERCENT] [--stitch-mirror-radius PERCENT]
          [--stitch-mirror-zoom PERCENT]
          [--stitch-mirror-pan-x PERCENT] [--stitch-mirror-pan-y PERCENT]
          [--stitch-mirror-icon PATH]
          [--stitch-resolution WIDTHxHEIGHT] [--stitch-bitrate RATE]
          [--stitch-scale PERCENT] [--stitch-max-width PIXELS] [--stitch-max-height PIXELS]
          [--stitch-map [{map,zoom}]] [--stitch-map-side SIDE] [--stitch-map-size PERCENT]
          [--stitch-gsensor] [--stitch-gsensor-size PERCENT]
          [--stitch-gsensor-pos POSITION | --stitch-gsensor-xy X,Y]
          [--stitch-subtitles] [--no-subtitles-bg]
          [--overwrite] [--dry-run] [--debug]
          [PATH]
```

## DESCRIPTION

`bv-export` is the last step of the pipeline: it detects **trips** in a local archive (the same time-gap-based detection `bv-ls --trips` previews) and assembles each one into its own folder under `--target` - concatenated front/rear video and audio, a merged GPX track, a merged g-sensor log, and (depending on flags) map overlays, a g-sensor overlay video, and a combined "stitch" video showing both cameras together.

A trip with only one camera falls back to a plain copy of whichever one exists, ignoring every `--stitch-*`/`--map-*` flag.

Trip detection is shared with `bv-ls --trips`: `--max-gap`/`--movement`/`--no-duration`/`--gap-tolerance` all mean exactly the same thing here. Only recordings with Front video count toward trip detection - a recording with GPS/g-sensor/thumbnail data but no Front video (common if its video was never downloaded) never starts, extends, or belongs to a trip on its own; it's simply not part of any trip's export.

Every trip also gets a `trip_info.txt` summary - start/end time, duration, total size, and whether Parking-mode footage is included always, and (whenever the trip has GPS data) distance, average/max speed, moving/idle time, and a reverse-geocoded address for the first and last GPS position. This isn't behind a flag: it's automatic, the same way `--map`'s road data is automatically fetched once requested. The address lookup uses OpenStreetMap's Nominatim service (one request per trip's start, one for its end, cached under `--target/.osm_cache` afterward like road/area data) - a network failure there only drops the address lines with a warning, never the rest of the export.

## ARGUMENTS

| Argument | Description |
|---|---|
| `PATH` | Archive directory. Default: current directory. |

## OPTIONS

### Required

| Option | Description |
|---|---|
| `--target DIR` | Directory to create trip subfolders in. |

### Naming and selection

| Option | Description |
|---|---|
| `--prefix PREFIX` | Prepend `PREFIX_` to each trip's folder name. |
| `--from TIMESTAMP` | Export every trip with at least one recording from this timestamp onward, in full. |
| `--until TIMESTAMP` | Export every trip with at least one recording up to this timestamp, in full. |
| `--timestamp TIMESTAMP` | Export every trip with at least one recording matching this timestamp or prefix, in full. |

### Trip detection

| Option | Description |
|---|---|
| `--max-gap MINUTES` | Largest gap between two recordings still counted as the same trip. Default: 5. |
| `--movement` | Bridge a gap over `--max-gap` using GPS/g-sensor movement evidence. **Off by default** - unbounded bridging risk, see `bv-ls(1)`. |
| `--no-duration` | Measure gaps from start timestamps only, ignoring `.duration.txt`. |
| `--gap-tolerance SECONDS` | Fixed noise margin added on top of `--max-gap`. Default: 10. |

### Map

| Option | Description |
|---|---|
| `--map` | Render `map.mp4`: a static whole-trip route/position/speed overlay on an OpenStreetMap basemap. **Off by default** - first fetch of an area's roads needs network (cached under `--target/.osm_cache` afterward), and rendering adds real time. |
| `--map-icon PATH` | Use a custom image (ideally a transparent PNG pointing "up") as the position marker. Applies to `--map` and `--map-zoom` alike. Default: a bundled red car icon - pass the literal value `none` to use a plain rotating arrow instead, or a path to use your own image. |
| `--map-zoom [METERS]` | Render `map_zoom_METERSm.mp4`: a scrolling "follow camera" view, real-world half-width `METERS` (default 120 if given with no value). Independent of `--map` - works with or without it. |

### G-sensor overlay video

| Option | Description |
|---|---|
| `--gsensor-video` | Render `gsensor.mp4`: a dot moving on a gauge tracking g-sensor (x, y) readings with a fading trail, on chroma-key green - meant for compositing later, or via `--stitch-gsensor`. |

### Stitch (combined camera video)

| Option | Description |
|---|---|
| `--stitch` | Render `stitch.mp4`: front + rear composed into one video. Everything below is only meaningful together with `--stitch`. |
| `--stitch-layout {side_by_side,top_down,rearview_mirror,auto}` | Camera arrangement. `auto` (default) picks side-by-side or top-down from the trip's own GPS extent; `rearview_mirror` is never auto-picked, name it explicitly. |

### Mirror inset (`--stitch-layout rearview_mirror`)

| Option | Description |
|---|---|
| `--stitch-mirror-size PERCENT` | Inset size as a percentage of the composite's width (10-50). Default: 40. |
| `--stitch-mirror-radius PERCENT` | Round the inset's four corners (0-100, percent of min(width,height)/2). Default: 0 (square). Ignored if `--stitch-mirror-icon` is given. |
| `--stitch-mirror-zoom PERCENT` | Crop this percent off each edge of the rear source, toward its center, before scaling into the inset (0-95). Default: 40. |
| `--stitch-mirror-pan-x PERCENT` | Pan the crop window left(-)/right(+) within the margin `--stitch-mirror-zoom` cropped away (-100 to 100). Default: 0 (centered). No effect at `--stitch-mirror-zoom 0`. |
| `--stitch-mirror-pan-y PERCENT` | Same as `--stitch-mirror-pan-x`, up(-)/down(+). Default: -30 (panned up). |
| `--stitch-mirror-icon PATH` | Composite the inset into a photo of a real physical mirror instead of the plain rounded rectangle - rear footage is clipped into the photo's own glass area, automatically segmented from a plain product-style photo (darker frame/mount around a lighter glass area, on a light background). Falls back to the procedural inset with a warning if the photo can't be read/segmented. Default: a bundled reference mirror photo - pass the literal value `none` to use the plain procedural inset instead, or a path to use your own photo. |

### Stitch output sizing

| Option | Description |
|---|---|
| `--stitch-resolution WIDTHxHEIGHT` | Scale to an exact resolution (e.g. `320x240`) - handy for a fast test render. Can distort aspect ratio if chosen carelessly. |
| `--stitch-bitrate RATE` | Target video bitrate (e.g. `256k`, `2M`), passed to ffmpeg's `-b:v`/`-maxrate`/`-bufsize`. |
| `--stitch-scale PERCENT` | Scale the natural resolution down by this percentage (1-100), always preserving aspect ratio - preferred over guessing a `--stitch-resolution`. |
| `--stitch-max-width PIXELS` | Cap the natural width, scaling down (never up) just enough to fit, preserving aspect ratio. |
| `--stitch-max-height PIXELS` | Cap the natural height - see `--stitch-max-width`. |

`--stitch-scale`/`--stitch-max-width`/`--stitch-max-height` combine - whichever shrinks the output most wins.

### Stitch map panel

| Option | Description |
|---|---|
| `--stitch-map [{map,zoom}]` | Compose a map panel alongside the cameras, rendered fresh at the composite's own size (not a copy of `--map`'s file). Bare flag = static overview; `zoom` = follow-camera view (needs `--map-zoom METERS` too, reused as the radius). |
| `--stitch-map-side {left,right,top,down}` | Panel side. Default: left for `top_down`, down for `side_by_side`/`rearview_mirror`. |
| `--stitch-map-size PERCENT` | Panel width/height as a percent of the matching composite dimension (5-80). Default: sized automatically from the trip's own aspect ratio. |

### Stitch g-sensor overlay

| Option | Description |
|---|---|
| `--stitch-gsensor` | Composite `gsensor.mp4` (must already exist - this run's own `--gsensor-video`, or an earlier run's) as a transparent overlay on the camera footage. |
| `--stitch-gsensor-size PERCENT` | Overlay size as a percent of the composite's width (5-40). Default: 15. |
| `--stitch-gsensor-pos POSITION` | Named position (`left`/`right`/`top`/`down`/`center` combinations, e.g. `top-right`). Default: `top-right`. Mutually exclusive with `--stitch-gsensor-xy`. |
| `--stitch-gsensor-xy X,Y` | Explicit X,Y percent position of the footage region's top-left corner - can overlap `--stitch-map`'s panel. Mutually exclusive with `--stitch-gsensor-pos`. |

### Stitch subtitles

| Option | Description |
|---|---|
| `--stitch-subtitles` | Burn the trip's `trip.srt` into the final frame, centered near the bottom. Skipped with a warning if the trip has no transcript data. |
| `--no-subtitles-bg` | Disable the dark semi-transparent bar behind subtitle text (on by default). |

### General

| Option | Description |
|---|---|
| `--overwrite` | Wipe and rebuild each trip folder from scratch, without asking. |
| `--dry-run` | Show which trip folders would be created/refreshed without writing anything. |
| `--debug` | Print wall-clock timing per trip phase (concatenation/map/stitch), plus which decode method (`nvdec`/`cpu`) `--stitch` used. |
| `-h`, `--help` | Show help and exit. |

Without `--overwrite`: an interactive run asks once whether to wipe or keep existing trip folders (the answer applies to every trip folder touched that run); a non-interactive run always keeps them, only overwriting the files it actually regenerates.

## OUTPUT

Each trip becomes a folder named `[PREFIX_]trip_STARTTIMESTAMP_ENDTIMESTAMP` under `--target`, containing (depending on flags):

| File | Written by |
|---|---|
| `front.mp4`, `rear.mp4` | always (whichever cameras exist) |
| `trip.gpx` | always, if GPS data exists |
| `trip.3gf` | always, if g-sensor data exists |
| `trip.srt`, `trip.lrc` | always, if transcript data exists |
| `trip.log` | always - the exact command line used, trip membership reasoning, and (with `--debug`) phase timings |
| `trip_info.txt` | always - start/end time, duration, total on-disk size, whether Parking-mode footage is included, and (if GPS data exists) distance, average/max speed, moving/idle time, and a reverse-geocoded start/end address |
| `map.mp4` | `--map` |
| `map_zoom_METERSm.mp4` | `--map-zoom` |
| `gsensor.mp4` | `--gsensor-video` |
| `stitch.mp4` | `--stitch` |

## EXAMPLES

Export every trip since a given time, plain (front/rear/audio/GPX/g-sensor log only):

```
bv-export /path/to/archive --target /path/to/trips --from 20260715
```

Add a static route map and a rearview-mirror-style combined video:

```
bv-export /path/to/archive --target /path/to/trips \
    --map --stitch --stitch-layout rearview_mirror
```

A realistic full combo - mirror inset composited into a real mirror photo, zoomed/panned/rounded, with a follow-camera map panel, g-sensor overlay, burned-in subtitles, and a smaller/faster render:

```
bv-export /path/to/archive --target /path/to/trips --prefix Holiday \
    --map --map-zoom 60 \
    --gsensor-video \
    --stitch --stitch-layout rearview_mirror \
    --stitch-mirror-size 20 --stitch-mirror-zoom 70 \
    --stitch-mirror-pan-x -20 --stitch-mirror-icon mirror.png \
    --stitch-map zoom --stitch-gsensor --stitch-subtitles \
    --stitch-scale 25 --debug
```

Fast small test render before committing to a full-size one:

```
bv-export /path/to/archive --target /path/to/trips --stitch --stitch-resolution 640x360
```

Preview what would be created, without writing anything:

```
bv-export /path/to/archive --target /path/to/trips --dry-run
```

## SEE ALSO

`bv-download(1)` and `bv-generate(1)` populate the archive this reads, `bv-ls(1) --trips` previews the same trip detection this uses.
