# bv-export(1)

## NAME

`bv-export` - detect trips in a BlackVue archive and export each one into its own folder

## SYNOPSIS

```
bv-export --target DIR [--prefix PREFIX]
          [--from TIMESTAMP] [--until TIMESTAMP] [--timestamp TIMESTAMP]
          [--max-gap MINUTES] [--movement] [--no-duration] [--duration-heal-archive]
          [--gap-tolerance SECONDS]
          [--max-parking-duration MINUTES]
          [--include-parking] [--parking-speed SPEED]
          [--map] [--map-icon PATH] [--map-zoom [METERS]]
          [--gsensor-video] [--gsensor-graph-video] [--gsensor-graph-x]
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
          [--stitch-graph] [--stitch-graph-side SIDE] [--stitch-graph-size PERCENT]
          [--stitch-subtitles] [--no-subtitles-bg]
          [--overwrite] [--dry-run] [--debug]
          [PATH]
```

## DESCRIPTION

`bv-export` is the last step of the pipeline: it detects **trips** in a local archive (the same time-gap-based detection `bv-ls --trips` previews) and assembles each one into its own folder under `--target` - concatenated front/rear video and audio, a merged GPX track, a merged g-sensor log, and (depending on flags) map overlays, a g-sensor overlay video, and a combined "stitch" video showing both cameras together.

A trip with only one camera falls back to a plain copy of whichever one exists, ignoring every `--stitch-*`/`--map-*` flag.

Each recording's front/rear/audio files are checked individually before being concatenated together - a single unreadable file (most often an incomplete recording whose moov atom never got written, e.g. after the camera lost power mid-write) is left out with a warning rather than failing that entire asset for the whole trip. A trip where every recording's front video is unreadable, say, still gets its rear video and audio, plus a warning per skipped file - it isn't an all-or-nothing failure.

Audio doesn't need a separate `bv-generate --extract-audio` pass first: any recording in the trip that's missing its own `<recording>.aac` gets one extracted on the fly, straight from its video, before the trip's `audio.aac` is assembled - the same self-healing `--duration-heal-archive` and `--stitch-gsensor` already do for `.duration.txt`/`gsensor.mp4`. A recording whose video genuinely has no audio track is skipped silently (not every recording has one); Parking recordings are never extracted from, matching `bv-generate`'s own behavior.

Trip detection is shared with `bv-ls --trips`: `--max-gap`/`--movement`/`--no-duration`/`--gap-tolerance` all mean exactly the same thing here. `--max-parking-duration` and `--duration-heal-archive` are `bv-export`-only for now - `bv-ls --trips`'s preview doesn't apply either, so a trip split `--max-parking-duration` causes, or a `.duration.txt` `--duration-heal-archive` would have written, won't show up there ahead of time. Only recordings with Front video count toward trip detection - a recording with GPS/g-sensor/thumbnail data but no Front video (common if its video was never downloaded) never starts, extends, or belongs to a trip on its own; it's simply not part of any trip's export.

Trip detection is *bounded* to `--timestamp`/`--from`/`--until` rather than scanning the whole archive: it seeds on the recordings actually inside the requested range and grows outward only as far as needed to prove a real gap on both sides - the same trip(s) a full archive scan would also have found, just without reading duration data for recordings nowhere near the request. A run with none of those three flags (export everything) still does a plain full-archive scan, since there's nothing to bound against. Detection itself reads whatever `.duration.txt` files already exist but, by default, doesn't compute a missing one - only the recordings belonging to a trip actually being exported this run get a missing `.duration.txt` computed and written, right before that trip's own overwrite prompt is resolved, so no single trip's prompt ever waits on unrelated recordings elsewhere in the archive. The trade-off: a recording that's never been probed yet and happens to sit right where the bounded search is trying to prove a boundary could still occasionally land in the wrong trip (the old bare-start-timestamp gap calculation) until it's actually been exported once, or `--duration-heal-archive` is used to heal for real within that bounded search instead.

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
| `--max-gap MINUTES` | Largest gap between two recordings still counted as the same trip. Default: derived from the camera's own configured RecordTime (see `bv-download(1)`'s RecordTime snapshot) - the segment length itself, so a single dropped/missing segment doesn't split a trip. Falls back to 5 if the archive has no RecordTime snapshot yet. |
| `--movement` | Bridge a gap over `--max-gap` using GPS/g-sensor movement evidence. **Off by default** - unbounded bridging risk, see `bv-ls(1)`. |
| `--no-duration` | Measure gaps from start timestamps only, ignoring `.duration.txt` entirely - no reading, computing, or writing it. |
| `--duration-heal-archive` | Self-heal a missing `.duration.txt` for real (ffprobe, not just cache reads) for every recording the bounded trip-detection search actually looks at - not the trip(s) being exported this run (the default - see below), but also not the whole archive; only whatever the search touches while proving both of its real boundaries. A possible one-time ffprobe cost before the first overwrite prompt can appear, in exchange for maximum accuracy at exactly the boundary the request cares about. Rejected together with `--no-duration`. |
| `--gap-tolerance SECONDS` | Fixed noise margin added on top of `--max-gap`. Default: 10. |
| `--max-parking-duration MINUTES` | Longest a continuous run of Parking-mode footage can span in real elapsed time (not its played-back length) before a Parking recording is kept out of the trip it would otherwise end and starts the next trip instead - a single Parking recording longer than this on its own is never appended to the drive before it. Two or more chained Parking recordings whose combined real span crosses this can split from each other the same way, not just at the point driving resumes. Requires real duration data, same as `--max-gap`'s own duration-aware gap calculation (`--no-duration` disables this too). Default: 60. |

### Parking footage

`--max-parking-duration` above decides which recordings end up *inside* a trip in the first place; `--include-parking` decides what happens to a Parking-mode recording that already is one, once that trip is actually assembled.

| Option | Description |
|---|---|
| `--include-parking` | Include every Parking-mode recording as-is in `front.mp4`/`rear.mp4`/`audio.aac`. **Off by default**: a Parking recording is left out entirely instead - wherever it falls in the trip (leading, trailing, or mid-trip) - with nothing substituted in its place. |
| `--parking-speed SPEED` | Play back every included Parking-mode recording at `SPEED` times its own natural pace (0.10-5.0 - e.g. `2` plays it twice as fast, `0.5` half as fast). Parking footage is motion-triggered and sparse, so a long real-world span can compress into a slow, uneventful stretch of the final export; this speeds it up (or slows it down) without touching the pace of the rest of the trip. Has no effect without `--include-parking`, and is a no-op at its default of `1` (no change). Every downstream consumer - `map.mp4`, `gsensor.mp4`/`gsensor_graph.mp4`, `--stitch-subtitles`, and `audio.aac`'s silence padding - is repositioned to stay in sync automatically, the same way they already are for prebuffer trimming and front/rear alignment. |

A Parking recording's own raw video also gets an automatic, transparent repair before being probed or concatenated: BlackVue's own Parking-mode container has a known quirk (an empty, unused audio track that still trips ffmpeg's strict validation) that otherwise makes `ffprobe` fail outright on every Parking recording - `--include-parking` would then leave every single one of them out again, unrepaired, defeating the flag. The repair only ever drops that one confirmed-broken, empty audio track from a copy of the file's `moov` box; the real video content is never touched. See `generate/mp4_repair.py`'s own docstring for the full technical detail - this is the same fix already used to make Parking recordings playable in bv-web's archive browser.

An earlier version of this feature replaced a mid-trip Parking recording with a short synthetic "PARKING FOOTAGE SKIPPED" transition clip (optionally one of several bundled or custom clips/images), leaving a leading/trailing Parking recording untouched either way. This was removed after a real export from a 4K HEVC dashcam showed the splice corrupting `front.mp4`/`rear.mp4` from that point onward: MP4's `hvc1`/`avc1` sample-entry tagging declares a track's SPS/PPS/VPS parameter sets once, at the container level, and two files from separate encoder sessions (the dashcam's own hardware encoder vs. anything `bv-export` rendered itself) generally don't share compatible parameter sets - so `ffmpeg`'s concat demuxer (a stream copy, not a re-encode) muxed them together with no error at export time, but no real decoder could parse the result past the splice point. A full decode-and-re-encode would avoid this, but at real trip-length/4K cost for one skipped recording's worth of benefit, so a Parking recording is now simply left out - matching the treatment leading/trailing Parking recordings already had.

### Pre-record buffer trimming

An Event(E) or Manual(M) mode recording's camera continuously buffers footage and flushes it into the file on trigger, so its first several seconds can duplicate the tail of the recording immediately before it - a real "pre-record buffer" of roughly 5-7 seconds on Christer's own camera, confirmed by cross-correlating the two recordings' g-sensor tracks. `bv-export` detects this automatically for every Event/Manual recording that has a recording before it in the same trip, and trims the duplicated head off its `front.mp4`/`rear.mp4`/`audio.aac` source and `trip.3gf` g-sensor data before that recording is concatenated in - no flag, always on. An Event/Manual recording that starts a trip (nothing before it to compare against) is never trimmed.

Detection compares a fixed-length window of the two recordings' `.3gf` g-sensor tracks (per-axis, z-scored, concatenated into one vector) via a sliding dot-product cross-correlation, sweeping candidate overlap offsets up to 12 seconds back into the preceding recording. It only trims when the best match clears a confidence threshold tuned well above the observed noise floor (real overlaps scored ~0.98 in testing; unrelated tracks sat around ±0.15-0.2) - on an ambiguous or low-confidence pair, it does nothing and leaves the recording untouched rather than guessing. A recording missing g-sensor data on either side is skipped the same way.

The trim itself is a stream-copy head cut (`ffmpeg -ss` before `-i`), which can only land on a real keyframe - it snaps to the nearest keyframe *before* the detected offset, so a small sliver of duplicated footage can survive rather than risk cutting into real content. This can occasionally leave a barely-visible jump at the trim point instead of a perfectly seamless cut; a splice-then-reencode approach would avoid that but at real per-recording transcode cost, so it isn't done. Runs before front/rear duration alignment below, so alignment's own end-of-recording trim always sees the corrected (prebuffer-trimmed) duration rather than the original one.

A trimmed recording's video also no longer starts at its own ID timestamp - frame 0 has been moved forward in wall-clock terms by however much got cut off the front - so `map.mp4`/`map_zoom_METERSm.mp4`/`--stitch-map`'s position and the `--stitch-subtitles` burn-in are all repositioned to account for it automatically; without this, a trimmed recording's overlay would visibly lag its own footage by close to the trimmed amount.

### Front/rear duration alignment

Every recording's front and rear video are expected to run the same length. `bv-export` probes both for every recording and, whenever they differ at all (beyond a hundredth of a second, which is treated as ffprobe's own floating-point rounding rather than a real gap), automatically trims the *longer* side down to match the shorter one for that recording - every recording, every export, not just the dramatic cases. A message is added to `trip.log` for every trim, naming the recording, the size of the gap, and which side was trimmed - informational only, not a warning, since this is routine and expected rather than a sign anything went wrong. The shorter side is left untouched. Only the longer side is ever trimmed - never is the shorter side padded or extended, since that would require splicing in synthetically generated content, the same class of corruption described above for the removed Parking-placeholder feature. This check runs regardless of `--include-parking`; a Parking recording that will be dropped anyway is skipped without probing either side.

An earlier version of this only trimmed a recording once its own front/rear gap passed a 5-second tolerance, on the theory that anything smaller was ordinary per-camera jitter not worth acting on. Dropped after a real export came back with `front.mp4`/`rear.mp4`/`stitch.mp4` all 8 seconds out of sync overall, despite no single recording differing by anywhere near 5 seconds and `trip.log` showing no trim had fired anywhere - small per-recording gaps, each individually below the tolerance, had simply accumulated across the whole trip. Trimming every recording's pair exactly, every time, closes that gap: there's no accumulation window left for drift to hide in.

### Map

| Option | Description |
|---|---|
| `--map` | Render `map.mp4`: a static whole-trip route/position/speed overlay on an OpenStreetMap basemap. **Off by default** - first fetch of an area's roads needs network (cached under `--target/.osm_cache` afterward), and rendering adds real time. |
| `--map-icon PATH` | Use a custom image (ideally a transparent PNG pointing "up") as the position marker. Applies to `--map` and `--map-zoom` alike. Default: a bundled red car icon - pass the literal value `none` to use a plain rotating arrow instead, or a path to use your own image. Whatever image is used (bundled or custom) is rendered at half its own source resolution, so the marker reads as a small position indicator rather than dominating the frame. |
| `--map-zoom [METERS]` | Render `map_zoom_METERSm.mp4`: a scrolling "follow camera" view, real-world half-width `METERS` (default 120 if given with no value). Independent of `--map` - works with or without it. |

`map.mp4` is always square. `map_zoom_METERSm.mp4` isn't: it matches the trip's front (or rear, if no front) video's own frame - full height for a north-south trip, full width for an east-west one, using the same geometry check `--stitch-map`'s embedded panel and `--stitch-layout auto` both use - rather than rendering as a square and getting letterboxed or cropped down the line.

Both `map.mp4` and `map_zoom_METERSm.mp4` show a small satellite badge in the top-right corner whenever the current frame's position comes from a real GPS fix. It's off during a leading or trailing gap in GPS coverage (e.g. no signal yet at the very start of a trip), when the position marker is frozen at the nearest known fix instead of tracking a live one - and off during a signal-loss gap of more than 3 seconds *between* two real fixes mid-trip (e.g. a tunnel), when the marker is sliding in a straight line between the last fix before the gap and the first fix after it, extrapolating a constant speed/course across the silence rather than tracking an actual reading. Not behind a flag - automatic whenever either map is rendered.

The position marker itself is hidden entirely before the trip's very first real GPS fix (nothing to show a position for yet), but stays visible - frozen at the nearest known fix, same as always - through a trailing gap or a mid-trip signal-loss gap once GPS has locked on at least once; only the badge goes dark for those two cases.

### G-sensor overlay video

| Option | Description |
|---|---|
| `--gsensor-video` | Render `gsensor.mp4`: a dot moving on a gauge tracking lateral (left/right, the raw Y field) vs. longitudinal (forward/back - acceleration/braking, the raw Z field) g-forces, with a fading trail, on chroma-key green - meant for compositing later, or via `--stitch-gsensor`. This Y/Z axis mapping matches what two separate real test recordings have shown (an interim override to raw X/Y was tried and then reverted once a second recording, with labeled acceleration and turning events, reconfirmed the original Y/Z finding); see `gsensor_render.py`'s module docstring for the full story. |
| `--gsensor-graph-video` | Render `gsensor_graph.mp4`: a second, alternate g-sensor visualization - a static whole-trip strip chart of Y/Z (and X, see `--gsensor-graph-x`) readings as colored line traces, with a playhead marking the current position, on the same chroma-key green background. Independent of `--gsensor-video` - either, both, or neither can be given. Independent of `--stitch-graph` too, which renders its own copy fresh at the panel's exact size rather than reusing this file. |
| `--gsensor-graph-x` | Also plot X (up/down) on the g-sensor graph - both `--gsensor-graph-video`'s own file and `--stitch-graph`'s panel share this one switch. X is hidden by default: the one situation where it genuinely matters (a real bump/pothole) is already captured by the footage itself, so it's opt-in rather than on by default. Meaningless without `--gsensor-graph-video` and/or `--stitch-graph` also given. |

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
| `--stitch-bitrate RATE` | Target video bitrate (e.g. `256k`, `2M`), passed to ffmpeg's `-b:v`/`-maxrate`/`-bufsize`. Capped to twice the original front/rear footage's own combined bitrate (front alone for `rearview_mirror`) - a request well above what the source ever had can't recover detail that isn't there. Without this flag, `stitch.mp4` defaults to matching that same source bitrate directly (front+rear summed for `side_by_side`/`top_down`, front alone for `rearview_mirror`) rather than a flat quality target independent of the source - falls back to a fixed high-quality CQ/CRF 19 target only if the source bitrate can't be determined. `map.mp4`/`gsensor.mp4` are unaffected either way - always CQ/CRF 19. |
| `--stitch-scale PERCENT` | Scale the natural resolution down by this percentage (1-100), always preserving aspect ratio - preferred over guessing a `--stitch-resolution`. |
| `--stitch-max-width PIXELS` | Cap the natural width, scaling down (never up) just enough to fit, preserving aspect ratio. |
| `--stitch-max-height PIXELS` | Cap the natural height - see `--stitch-max-width`. |

`--stitch-scale`/`--stitch-max-width`/`--stitch-max-height` combine - whichever shrinks the output most wins.

### Stitch map panel

| Option | Description |
|---|---|
| `--stitch-map [{map,zoom}]` | Compose a map panel alongside the cameras, rendered fresh at the composite's own size (not a copy of `--map`'s file). Bare flag = static overview; `zoom` = follow-camera view (needs `--map-zoom METERS` too, reused as the radius). |
| `--stitch-map-side {left,right,top,down}` | Panel side. Default: left for `top_down`, down for `side_by_side`. For `rearview_mirror` (a single full-frame camera, not a stack, so there's no camera shape to nest the panel against) the default instead follows the trip's own real-world shape: left for a mainly north-south trip, down for a mainly east-west one (or when there's no GPS data to judge by). |
| `--stitch-map-size PERCENT` | Panel width/height as a percent of the matching composite dimension (5-80). Default: sized automatically from the trip's own aspect ratio. |

### Stitch g-sensor overlay

| Option | Description |
|---|---|
| `--stitch-gsensor` | Composite `gsensor.mp4` (must already exist - this run's own `--gsensor-video`, or an earlier run's) as a transparent overlay on the camera footage. |
| `--stitch-gsensor-size PERCENT` | Overlay size as a percent of the composite's width (5-40). Default: 15. |
| `--stitch-gsensor-pos POSITION` | Named position (`left`/`right`/`top`/`down`/`center` combinations, e.g. `top-right`). Default: `top-right`. Mutually exclusive with `--stitch-gsensor-xy`. |
| `--stitch-gsensor-xy X,Y` | Explicit X,Y percent position of the footage region's top-left corner - can overlap `--stitch-map`'s panel. Mutually exclusive with `--stitch-gsensor-pos`. |

### Stitch g-sensor graph panel

A second, alternate g-sensor visualization alongside `--stitch-gsensor`'s dot-gauge overlay: a strip chart of this trip's X/Y/Z g-sensor readings with a moving playhead, composed as its own panel - selectable by side like `--stitch-map`, rather than overlaid on top of the footage. Unlike `--stitch-gsensor`, this is rendered fresh at the exact panel size and grows the composite, the same way `--stitch-map` does. A `left`/`right` panel renders vertically (a tall, narrow strip with upright tick labels and time running top to bottom); a `top`/`down` panel renders horizontally (time running left to right, like the standalone `gsensor_graph.mp4`). Composed after any `--stitch-map` panel, so the two can be used together - e.g. a map on the bottom (`--stitch-map`) and this graph as a vertical side panel.

| Option | Description |
|---|---|
| `--stitch-graph` | Compose a g-sensor strip-chart panel alongside the cameras. Plots Y/Z by default; add `--gsensor-graph-x` to also plot X. |
| `--stitch-graph-side {left,right,top,down}` | Panel side. Default: whichever side `--stitch-map`'s own panel *didn't* use (a map on the left defaults the graph to the bottom, and vice versa), so the two grow the frame on perpendicular axes and stay closer to a 16:9 shape overall; defaults to the bottom if there's no map panel actually present at all. |
| `--stitch-graph-size PERCENT` | Panel width/height as a percent of the matching composite dimension (5-80). Default: a fixed 50%, matching the map panel's own size ceiling - there's no `--stitch-map`-style automatic sizing here, a synthetic chart has no real-world shape to derive one from. |

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

Each trip becomes a folder named `[PREFIX_]trip_STARTTIMESTAMP_ENDTIMESTAMP` under `--target`, containing (depending on flags). `STARTTIMESTAMP`/`ENDTIMESTAMP` (and trip_info.txt's own "Started"/"Ended" lines, below) match whatever actually ends up in `front.mp4`/`rear.mp4`: without `--include-parking`, a leading or trailing Parking recording is skipped for this purpose too, not just left out of the video itself - otherwise the folder name/trip_info.txt would claim a wider time range than the exported video actually covers.

| File | Written by |
|---|---|
| `front.mp4`, `rear.mp4` | always (whichever cameras exist) - a Parking-mode recording is left out entirely unless `--include-parking` is given; a recording whose own front/rear durations differ at all has its longer side auto-trimmed to match (see "Front/rear duration alignment" above) |
| `trip.gpx` | always, if GPS data exists |
| `trip.3gf` | always, if g-sensor data exists |
| `trip.srt`, `trip.lrc` | always, if transcript data exists |
| `trip.log` | always - the exact command line used, trip membership reasoning, and (with `--debug`) phase timings |
| `trip_info.txt` | always - start/end time, duration, total on-disk size, whether Parking-mode footage is included, and (if GPS data exists) distance, average/max speed, moving/idle time, and a reverse-geocoded start/end address |
| `map.mp4` | `--map` |
| `map_zoom_METERSm.mp4` | `--map-zoom` |
| `gsensor.mp4` | `--gsensor-video` |
| `gsensor_graph.mp4` | `--gsensor-graph-video` |
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

A map panel on the bottom with the g-sensor strip chart as a vertical panel beside it:

```
bv-export /path/to/archive --target /path/to/trips \
    --stitch --stitch-layout side_by_side \
    --stitch-map --stitch-map-side down \
    --stitch-graph
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
