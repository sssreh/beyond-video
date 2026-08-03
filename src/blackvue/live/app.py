"""
FastAPI app for bv-live: a one-page live dashboard combining the
camera's own front/rear/interior MJPEG feed (switchable via a button)
with two
synthetic MJPEG streams this project renders itself - a scrolling map
(map_stream.py) and a scrolling g-sensor strip (gsensor_stream.py) -
fed by a background telemetry pump (telemetry.py) reading
blackvue_livedata.cgi continuously for as long as the server runs.

Layout: map on the left, camera feed (with a Front/Rear/Interior toggle button
under it) to its right, g-sensor strip spanning the full width along
the bottom - "front camera stream ... with gsensor line at the bottom
and a scrolling map to the left", per Christer, both the map and
g-sensor panels sized larger than their export-video defaults since
the live camera feed itself is comparatively small (see
map_stream.py's/gsensor_stream.py's own DEFAULT_WIDTH/DEFAULT_HEIGHT
comments).

The camera panel is the one panel that should never be the first
thing to disappear as the browser window shrinks - "the stream should
be there as long as possible since it is the star", per Christer's
own follow-up once he'd actually resized the window and watched the
camera feed vanish before the map/g-sensor did. At normal widths,
`order: -1` is deliberately absent from #camera-panel so the map stays
to its left, per Christer's own layout spec; below a 700px width
breakpoint (see the @media rule in _PAGE_HTML's own CSS) panels stack
into a single column and `order: -1` puts the camera panel first
there instead, so on a small window it's the one panel guaranteed
still on screen without scrolling.

The map and camera images are sized to the same rendered height
("the map should have the same height as the video", per Christer's
own later follow-up) via an explicit `height: 55vh` on both
`#camera-panel img, #map-panel img` (see _PAGE_HTML), each panel's
own width then following from its image's own aspect ratio at that
height - the map's square 1:1 render ends up as wide as it is tall,
the camera feed keeps its native aspect. `min-width: 0` on every
`.panel` still matters below the 700px breakpoint, where the panels
go back to `width: 100%; height: auto` sizing and flex items would
otherwise refuse to shrink below their own content's natural size.

A plain hand-written HTML string, not a Jinja2 template like
blackvue.web's pages - there's exactly one page here, its only dynamic
content is the camera's own display name, and pulling in a templates/
directory (plus the package-data entry it'd need - see bv-web's own
pyproject.toml lesson) isn't worth it for that.

No login, unlike bv-web - this is a personal, run-when-you-want-it
tool in the same spirit as bv-gps/bv-download, not something meant to
sit reachable/always-on with multiple accounts (see cli/bv_live.py's
own --host default).

Copyright (C) 2026 Christer R. (sssreh)

SPDX-License-Identifier: GPL-3.0-or-later
"""

from __future__ import annotations

import threading
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi import Query
from fastapi.responses import HTMLResponse
from fastapi.responses import StreamingResponse

from ..core.blackvue_client import BlackVueClient
from .gsensor_stream import DEFAULT_WINDOW_SECONDS as DEFAULT_GSENSOR_WINDOW_SECONDS
from .gsensor_stream import live_gsensor_frames
from .map_stream import DEFAULT_ZOOM_METERS as DEFAULT_MAP_ZOOM_METERS
from .map_stream import LiveMapRegion
from .map_stream import live_map_frames
from .mjpeg import CONTENT_TYPE
from .mjpeg import relay_raw_stream
from .mjpeg import rendered_frame_stream
from .telemetry import LiveTelemetryPump
from .telemetry import TelemetryState

# Render cadences for our own synthetic streams. The map is the more
# expensive of the two to render (roads/areas projection, even with
# osm_roads.py's bbox filtering) and changes comparatively slowly at
# driving speed within a close, 100m-radius follow-camera view, so it
# doesn't need as high a frame rate as the cheap-to-draw g-sensor
# strip to still read as live.
MAP_FPS = 2.0
GSENSOR_FPS = 5.0


def create_live_app(
    client: BlackVueClient,
    *,
    camera_name: str,
    osm_cache_dir: Path,
    map_zoom_meters: float = DEFAULT_MAP_ZOOM_METERS,
    gsensor_window_seconds: float = DEFAULT_GSENSOR_WINDOW_SECONDS,
) -> FastAPI:
    """Build the bv-live FastAPI app for one already-connected
    `client` (see core/connection.py's connect(), same as bv-gps
    uses)."""

    state = TelemetryState()
    pump = LiveTelemetryPump(client, state)
    region = LiveMapRegion(osm_cache_dir)

    # Set once, in _lifespan's own shutdown phase below, and checked by
    # every one of this app's three streaming routes (via mjpeg.py's
    # own stop_event param) - see mjpeg.py's rendered_frame_stream()/
    # relay_raw_stream() docstrings for why this exists at all: with no
    # stop signal, those generators run forever regardless of server
    # shutdown, which is what left `bv-live` hanging at uvicorn's own
    # "Waiting for connections to close" on Ctrl-C (Christer: "The only
    # way to take it down is to use task manager") - closing the
    # browser tab alone doesn't reliably unblock them either, since the
    # generator itself has no way to notice a closed connection while
    # it's mid-render or mid-read.
    shutdown_event = threading.Event()

    @asynccontextmanager
    async def _lifespan(_app: FastAPI):
        pump.start()
        try:
            yield
        finally:
            pump.stop()
            shutdown_event.set()

    app = FastAPI(title=f"bv-live - {camera_name}", lifespan=_lifespan)

    @app.get("/", response_class=HTMLResponse)
    def index() -> str:
        return _PAGE_HTML.format(camera_name=camera_name)

    @app.get("/stream/camera")
    def stream_camera(direction: str = Query("F", pattern="^[FRI]$")):
        # Opened fresh per request rather than kept alive
        # continuously like the telemetry pump - only relayed while a
        # browser is actually displaying it, and closed the moment the
        # viewer switches direction or the tab closes (relay_raw_stream()'s
        # own finally: response.close()), so switching Front/Rear/Interior
        # doesn't leave the previous direction's own camera connection
        # dangling open in the background. 'I' matches this project's
        # own interior-camera letter elsewhere (Recording.interior,
        # is_interior - see domain/recording.py) rather than "Internal"
        # or "Inside" - no independent term introduced for the same
        # thing. Note: on a camera that doesn't actually have a third
        # channel, blackvue_live.cgi may still answer with *something*
        # for direction=I rather than a clean error (see
        # scripts/scan_blackvue_endpoints.py's own caveat on this) -
        # the button doesn't hide itself for that case, since there's
        # no reliable way from here to tell "no interior channel" apart
        # from "interior channel, just nothing to see right now."
        upstream = client.open_stream(f"/blackvue_live.cgi?direction={direction}")
        content_type = upstream.headers.get("Content-Type") or CONTENT_TYPE
        return StreamingResponse(
            relay_raw_stream(upstream, stop_event=shutdown_event),
            media_type=content_type,
        )

    @app.get("/stream/map")
    def stream_map():
        render = live_map_frames(state, region, zoom_meters=map_zoom_meters)
        return StreamingResponse(
            rendered_frame_stream(render, MAP_FPS, stop_event=shutdown_event),
            media_type=CONTENT_TYPE,
        )

    @app.get("/stream/gsensor")
    def stream_gsensor():
        render = live_gsensor_frames(state, window_seconds=gsensor_window_seconds)
        return StreamingResponse(
            rendered_frame_stream(render, GSENSOR_FPS, stop_event=shutdown_event),
            media_type=CONTENT_TYPE,
        )

    return app


_PAGE_HTML = """<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<title>bv-live - {camera_name}</title>
<style>
  html, body {{
    margin: 0; background: #111; color: #eee;
    font-family: system-ui, sans-serif;
  }}
  header {{ padding: 10px 16px; }}
  header h1 {{ font-size: 18px; font-weight: 600; margin: 0; }}
  .dashboard {{ display: flex; flex-wrap: wrap; align-items: flex-start; gap: 12px; padding: 0 12px; }}
  .panel {{
    background: #1a1a1a; border: 1px solid #333; border-radius: 6px; padding: 8px;
    box-sizing: border-box;
    /* Flex items refuse to shrink below their own content's natural
       size by default (min-width: auto) - without this, a camera
       feed sent at a larger native resolution than the map/gsensor
       panels' own fixed render size won't shrink to fit a narrower
       window at all, forcing the row to overflow instead. */
    min-width: 0;
  }}
  .panel img {{ display: block; width: 100%; height: auto; border-radius: 4px; }}
  /* The camera feed is the star - it should be the last panel to
     give up space as the window narrows, not the first. Deliberately
     no `order` here at normal widths: Christer wants the map to the
     left of the camera feed (the markup's own natural order), and an
     earlier version's `order: -1` wrongly applied everywhere,
     dragging the camera panel in front of the map even at full width
     instead of only once panels stack vertically - see the
     max-width: 700px rule below, which is the only place `order`
     belongs. */
  #camera-panel {{
    display: flex; flex-direction: column; align-items: center;
    flex: 0 0 auto;
  }}
  /* Map sized to match the camera feed's own height (Christer: "The
     map should have the same height as the video") rather than a
     flex-grow share of the row's width - the height-matching img
     rule below is what actually drives both panels' sizing now.
     flex: 0 0 auto here just means "take exactly the width your
     content needs," not a fixed share of the row. */
  #map-panel {{ flex: 0 0 auto; }}
  /* Force the camera and map images to the same rendered height so
     the map visually matches the video's height - each image's own
     width then just follows whatever its aspect ratio implies at
     that height (the map's own square 1:1 render ends up as wide as
     it is tall; the camera feed keeps its native aspect, unchanged).
     No server-side resizing or cropping involved - see
     stream_camera()'s and live_map_frames()'s own docstrings above.
     If the row can't fit both panels side by side at this height,
     .dashboard's existing flex-wrap: wrap (above) drops the map
     panel to its own line rather than overflowing or distorting
     either image's aspect ratio. 55vh is a starting point, not
     measured against Christer's own screen - nudge it if it's too
     tall/short in practice. */
  #camera-panel img, #map-panel img {{
    width: auto;
    height: 55vh;
    max-width: 100%;
  }}
  #camera-controls {{ margin-top: 8px; display: flex; gap: 8px; }}
  button {{
    background: #222; color: #eee; border: 1px solid #444; border-radius: 4px;
    padding: 6px 16px; cursor: pointer; font-size: 14px;
  }}
  button.active {{ background: #3a5a78; border-color: #5a86ab; }}
  #gsensor-panel {{ width: 100%; box-sizing: border-box; }}
  /* Narrow window: stack everything full-width instead of shrinking
     the map/camera side by side into illegibility. This is the only
     place the camera panel gets order: -1, so it's first (top) once
     stacked, without disturbing the map-left/camera-right order at
     normal widths above. */
  @media (max-width: 700px) {{
    .dashboard {{ flex-direction: column; }}
    #camera-panel {{ order: -1; flex-basis: auto; width: 100%; }}
    #map-panel {{ flex-basis: auto; width: 100%; }}
    /* The height-matching rule above doesn't make sense once panels
       are stacked full-width instead of sitting side by side - go
       back to width-driven sizing here, same as before this change. */
    #camera-panel img, #map-panel img {{ width: 100%; height: auto; }}
  }}
</style>
</head>
<body>
<header><h1>{camera_name} - live</h1></header>
<div class="dashboard">
  <div class="panel" id="map-panel"><img src="/stream/map" alt="live map"></div>
  <div class="panel" id="camera-panel">
    <img id="camera-stream" src="/stream/camera?direction=F" alt="camera feed">
    <div id="camera-controls">
      <button id="btn-front" class="active" onclick="setDirection('F')">Front</button>
      <button id="btn-rear" onclick="setDirection('R')">Rear</button>
      <button id="btn-interior" onclick="setDirection('I')">Interior</button>
    </div>
  </div>
</div>
<div class="dashboard">
  <div class="panel" id="gsensor-panel"><img src="/stream/gsensor" alt="live g-sensor"></div>
</div>
<script>
function setDirection(direction) {{
  var img = document.getElementById('camera-stream');
  img.src = '/stream/camera?direction=' + direction + '&t=' + Date.now();
  document.getElementById('btn-front').classList.toggle('active', direction === 'F');
  document.getElementById('btn-rear').classList.toggle('active', direction === 'R');
  document.getElementById('btn-interior').classList.toggle('active', direction === 'I');
}}
</script>
</body>
</html>
"""
