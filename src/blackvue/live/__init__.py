"""
bv-live: a live, browser-based dashboard for a single BlackVue camera -
its own front/rear MJPEG feed (switchable), a scrolling map following
its current position, and a scrolling g-sensor strip, all fed live
from the camera's own endpoints for as long as `bv-live` runs.

Deliberately its own top-level package, separate from `blackvue.web`
(bv-web, the archive browser - trips already exported by bv-export,
with login/roles). The two solve different problems: bv-web reads
finished trip folders from disk and has no live camera connection at
all; bv-live is nothing *but* a live camera connection, with no
archive/login concept of its own - see blackvue.web.app's own
docstring for its explicit "browse/watch only" scope, which bv-live
sits entirely outside of. Sharing one package for both would tangle
two genuinely different concerns together for no real benefit.

Not imported at module level from anywhere except `bv-live` itself
(cli/bv_live.py) - this package pulls in fastapi, the same "only
import it once the command that needs it actually runs" convention
`blackvue.web` already follows (see its own __init__.py), so every
other `bv-*` command keeps working on a plain `pip install .` with no
web extras installed.

Copyright (C) 2026 Christer R. (sssreh)

SPDX-License-Identifier: GPL-3.0-or-later
"""
