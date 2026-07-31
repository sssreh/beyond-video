"""
FastAPI app for bv-live: a one-page live dashboard combining the
camera's own front/rear MJPEG feed (switchable via a button) with two
synthetic MJPEG streams this project renders itself - a scrolling map
(map_stream.py) and a scrolling g-sensor strip (gsensor_stream.py) -
fed by a background telemetry pump (telemetry.py) reading
blackvue_livedata.cgi continuously for as long as the server runs.

Layout: map on the left, camera feed (with a Front/Rear toggle button
under it) to its right, g-sensor strip spanning the full width along
the bottom - "front camera stream ... with gsensor line at the bottom
and a scrolling map to the left", per Christer, both the map and
g-sensor panels sized larger than their export-video defaults since
the live camera feed itself is comparatively small (see
map_stream.py's/gsensor_stream.py's own DEFAULT_WIDTH/DEFAULT_HEIGHT
comments).

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

    @asynccontextmanager
    async def _lifespan(_app: FastAPI):
        pump.start()
        try:
            yield
        finally:
            pump.stop()

    app = FastAPI(title=f"bv-live - {camera_name}", lifespan=_lifespan)

    @app.get("/", response_class=HTMLResponse)
    def index() -> str:
        return _PAGE_HTML.format(camera_name=camera_name)

    @app.get("/stream/camera")
    def stream_camera(direction: str = Query("F", pattern="^[FR]$")):
        # Opened fresh per request rather than kept alive
        # continuously like the telemetry pump - only relayed while a
        # browser is actually displaying it, and closed the moment the
        # viewer switches direction or the tab closes (relay_raw_stream()'s
        # own finally: response.close()), so switching Front/Rear
        # doesn't leave the previous direction's own camera connection
        # dangling open in the background.
        upstream = client.open_stream(f"/blackvue_live.cgi?direction={direction}")
        content_type = upstream.headers.get("Content-Type") or CONTENT_TYPE
        return StreamingResponse(relay_raw_stream(upstream), media_type=content_type)

    @app.get("/stream/map")
    def stream_map():
        render = live_map_frames(state, region, zoom_meters=map_zoom_meters)
        return StreamingResponse(
            rendered_frame_stream(render, MAP_FPS), media_type=CONTENT_TYPE
        )

    @app.get("/stream/gsensor")
    def stream_gsensor():
        render = live_gsensor_frames(state, window_seconds=gsensor_window_seconds)
        return StreamingResponse(
            rendered_frame_stream(render, GSENSOR_FPS), media_type=CONTENT_TYPE
        )

    return app


_PAGE_HTML = """<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<title>bv-live - {camera_name}</title>
<style>
  body {{
    margin: 0; background: #111; color: #eee;
    font-family: system-ui, sans-serif;
  }}
  header {{ padding: 10px 16px; }}
  header h1 {{ font-size: 18px; font-weight: 600; margin: 0; }}
  .dashboard {{ display: flex; flex-wrap: wrap; align-items: flex-start; gap: 12px; padding: 0 12px; }}
  .panel {{ background: #1a1a1a; border: 1px solid #333; border-radius: 6px; padding: 8px; }}
  .panel img {{ display: block; max-width: 100%; border-radius: 4px; }}
  #camera-panel {{ display: flex; flex-direction: column; align-items: center; }}
  #camera-controls {{ margin-top: 8px; display: flex; gap: 8px; }}
  button {{
    background: #222; color: #eee; border: 1px solid #444; border-radius: 4px;
    padding: 6px 16px; cursor: pointer; font-size: 14px;
  }}
  button.active {{ background: #3a5a78; border-color: #5a86ab; }}
  #gsensor-panel {{ width: 100%; box-sizing: border-box; }}
  #gsensor-panel img {{ width: 100%; }}
</style>
</head>
<body>
<header><h1>{camera_name} - live</h1></header>
<div class="dashboard">
  <div class="panel"><img src="/stream/map" alt="live map"></div>
  <div class="panel" id="camera-panel">
    <img id="camera-stream" src="/stream/camera?direction=F" alt="camera feed">
    <div id="camera-controls">
      <button id="btn-front" class="active" onclick="setDirection('F')">Front</button>
      <button id="btn-rear" onclick="setDirection('R')">Rear</button>
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
}}
</script>
</body>
</html>
"""
