# bv-live(1)

## NAME

`bv-live` - serve a live browser dashboard for a BlackVue camera

## SYNOPSIS

```
bv-live [--config-dir DIR] [--timeout SECONDS] [--host HOST] [--port PORT]
        [--map-zoom METERS] [--gsensor-window SECONDS] [--no-browser]
        ID
```

## DESCRIPTION

`bv-live` connects to a BlackVue camera (over its configured endpoints - see `bv-config(1)`) and serves a one-page live dashboard in your browser: the camera's own front/rear video feed (switchable with a button), a map that scrolls to follow its current position, and a strip chart of its live g-sensor readings - all fed live from the camera's own endpoints for as long as this command keeps running.

A browser window opens automatically a moment after the server starts (pass `--no-browser` to skip this and just print the URL) - on Windows, in whichever browser is actually set as your OS-level default, detected from the same registry key Windows itself uses to decide which browser handles a link; a fixed Edge/Chrome/Firefox search is used as a fallback if that can't be determined (a non-Windows OS, or a default browser this doesn't recognize a "new window" flag for). The camera feed is the star of the dashboard: as the browser window is resized smaller, the map and g-sensor panels give up space (and eventually stack below it) before the camera feed does.

The dashboard has three panels:

- **Map** (left) - follows the camera's current GPS position at a fixed real-world radius (`--map-zoom`), the same "follow camera" framing `bv-export --map-zoom` uses for a finished trip, but scrolling live instead. Road/water/park geometry is fetched from OpenStreetMap's Overpass API and cached to disk (under the camera's own archive directory, alongside `bv-export`'s own map cache) the first time it's needed for a given area. If Overpass is briefly unreachable or overloaded (a 504 Gateway Timeout, a network blip), the map keeps rendering with whatever geometry it already has cached rather than the stream going black - it waits about 30 seconds before trying Overpass again rather than retrying on every frame. Displayed at the same height as the camera feed next to it (not the camera's own resolution - just matched screen height), so its own square render ends up as wide as it is tall. The current position is marked with the same bundled red-car icon `bv-export --map` defaults to, rotated to match the direction of travel once it can be computed from a couple of live fixes.
- **Camera** (top right) - the camera's own live MJPEG feed, proxied unchanged - not resized, cropped, or re-encoded, so its quality is exactly whatever the camera's own live-view stream provides. A Front/Rear button switches which direction is shown; only the direction currently on screen is actually being streamed from the camera.
- **G-sensor** (bottom, full width) - a scrolling strip of the last `--gsensor-window` seconds of live g-sensor readings, plotted against raw zero (no startup calibration delay). The strip's own vertical scale only ever grows to fit the biggest deviation seen so far in the session, never shrinks back down as an old peak scrolls out of view. May be below the fold on a shorter window - scroll down to see it.

Endpoints configured in `bv-config` are tried in order; the first one that responds within `--timeout` is used for the whole session. This only works while the camera is actually reachable, the same as `bv-gps`/`bv-download`.

## ARGUMENTS

| Argument | Description |
|---|---|
| `ID` | Camera system id (see `bv-config(1)`). |

## OPTIONS

| Option | Description |
|---|---|
| `--config-dir DIR` | Directory camera configs live in. Default: the platform's standard config directory. |
| `--timeout SECONDS` | Per-endpoint connection timeout. Default: 5. |
| `--host HOST` | Address to listen on. Default: 127.0.0.1 - this is a personal, run-when-you-want-it tool, not meant to sit reachable by anyone else on the network. |
| `--port PORT` | Port to listen on. Default: 8100 (different from `bv-web`'s own default 8000, so both can run at once). |
| `--map-zoom METERS` | Live map follow-camera radius in meters. Default: 100. |
| `--gsensor-window SECONDS` | How many seconds of live g-sensor history the scrolling strip shows at once. Default: 60. |
| `--no-browser` | Don't automatically open a browser window once the server starts - just print the URL. |
| `-h`, `--help` | Show help and exit. |

## EXIT STATUS

| Code | Meaning |
|---|---|
| 0 | OK (only reached after the server is stopped, e.g. Ctrl-C). |
| 1 | Config error (missing/invalid camera config, or no endpoints configured). |
| 2 | Camera unreachable on every configured endpoint. |
| 3 | fastapi/uvicorn aren't installed (`pip install beyond-video[web]`). |

## EXAMPLES

```
bv-live Kirby
```

```
bv-live: serving Kirby (via home) at http://127.0.0.1:8100/ - press Ctrl-C to stop
```

Open `http://127.0.0.1:8100/` in a browser to see the dashboard. A wider map, following more closely:

```
bv-live Kirby --map-zoom 60
```

## SEE ALSO

`bv-config(1)` to set up the camera this connects to, `bv-gps(1)` for a one-shot live GPS reading instead of a persistent dashboard, `bv-export(1)`'s `--map`/`--map-zoom` for the equivalent rendered-once-per-trip map on already-downloaded recordings.
