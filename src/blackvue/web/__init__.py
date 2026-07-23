"""
blackvue.web - the Beyond Video web app (bv-web).

Browses and plays back the folders bv-export writes (front/rear/
stitch video, map/gsensor overlays, GPX/SRT/LRC) from a browser,
behind a simple two-role login: "owner" (Christer - can also trigger
download/generate/export once that's built; not yet in this
increment) and "viewer" (family members - browse/watch only). A third
"manager" role is anticipated but deliberately not built yet - see
WORKING_CONTEXT.md.

This subpackage is optional at import time: none of blackvue's other
commands (bv-download, bv-generate, bv-export, ...) import anything
from here, and this package's own modules only import fastapi/
uvicorn/jinja2 inside the functions that need them (or, for app.py,
at module level since it's only ever imported once bv-web itself is
run) - so installing beyond-video without the web extras still works
for every other command.

Copyright (C) 2026 Christer R. (sssreh)

SPDX-License-Identifier: GPL-3.0-or-later
"""
