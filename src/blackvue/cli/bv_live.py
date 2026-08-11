"""
bv-live.

Copyright (C) 2026 Christer R. (sssreh)

SPDX-License-Identifier: GPL-3.0-or-later
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
import threading
import webbrowser
from pathlib import Path

from .errors import run_cli
from ..core.camera_config import CameraConfigError
from ..core.camera_config import config_path
from ..core.camera_config import default_config_dir
from ..core.camera_config import load_camera_config
from ..core.connection import CameraUnreachableError
from ..core.connection import connect
from ..live.gsensor_stream import DEFAULT_WINDOW_SECONDS
from ..live.map_stream import DEFAULT_ZOOM_METERS

EXIT_OK = 0
EXIT_CONFIG_ERROR = 1
EXIT_UNREACHABLE = 2
EXIT_MISSING_DEPENDENCY = 3


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Parse command line arguments."""

    parser = argparse.ArgumentParser(
        prog="bv-live",
        description=(
            "Serve a live, one-page browser dashboard for a BlackVue "
            "camera: its own front/rear video feed (switchable), a "
            "scrolling map following its current position, and a "
            "scrolling g-sensor strip - all fed live from the "
            "camera's own endpoints (see bv-config(1)) for as long as "
            "this command keeps running."
        ),
        # See bv_export.py's own ArgumentParser for why: argparse's
        # default prefix-abbreviation matching silently breaks the
        # moment a sibling flag sharing a prefix gets added later.
        allow_abbrev=False,
    )

    parser.add_argument(
        "id",
        help="Camera system id (see bv-config).",
    )

    parser.add_argument(
        "--config-dir",
        type=Path,
        default=default_config_dir(),
        help="Directory camera configs live in (default: %(default)s).",
    )

    parser.add_argument(
        "--timeout",
        type=int,
        default=5,
        help="Per-endpoint connection timeout in seconds (default: %(default)s).",
    )

    parser.add_argument(
        "--host",
        default="127.0.0.1",
        help=(
            "Address to listen on (default: 127.0.0.1 - this is a "
            "personal, run-when-you-want-it tool, not something meant "
            "to sit reachable by anyone else on the network)."
        ),
    )

    parser.add_argument(
        "--port",
        type=int,
        default=8100,
        help=(
            "Port to listen on (default: %(default)s - deliberately "
            "different from bv-web's own default 19373, so both can "
            "run at once)."
        ),
    )

    parser.add_argument(
        "--map-zoom",
        type=float,
        default=DEFAULT_ZOOM_METERS,
        metavar="METERS",
        help=(
            "Live map follow-camera radius in meters (default: "
            "%(default)s)."
        ),
    )

    parser.add_argument(
        "--gsensor-window",
        type=float,
        default=DEFAULT_WINDOW_SECONDS,
        metavar="SECONDS",
        help=(
            "How many seconds of live g-sensor history the scrolling "
            "strip shows at once (default: %(default)s)."
        ),
    )

    parser.add_argument(
        "--no-browser",
        action="store_true",
        help="Don't automatically open a browser window once the server starts.",
    )

    parser.add_argument(
        "--browser",
        choices=("default", "chrome", "edge", "firefox", "brave"),
        default="default",
        help=(
            "Which browser to open (default: %(default)s - auto-detect "
            "the OS-level default browser, falling back to a fixed "
            "Edge/Chrome/Firefox search if that can't be determined). "
            "Set this if the OS-level default keeps resolving to a "
            "browser you don't actually want (Windows silently resets "
            "it back to Edge from time to time)."
        ),
    )

    return parser.parse_args(argv)


# How long to wait before opening the browser - long enough for
# uvicorn.run() (called right after this is scheduled) to actually
# finish binding the port first, short enough that it doesn't feel
# like a delay. If the browser somehow still beats the bind (a slow
# machine), it just shows a normal "can't connect" page the user can
# refresh - not worth the complexity of polling the socket instead.
BROWSER_OPEN_DELAY_SECONDS = 1.0

# Fixed fallback candidates for browsers that support an explicit
# "open in a new window" command-line flag - used when the user's own
# OS-level default browser can't be detected at all, or turns out to
# be something this module doesn't know a new-window flag for (see
# _default_browser_launch(), tried first in _open_new_window()).
# Absolute paths are checked directly (typical Windows/macOS install
# locations); bare names are looked up on PATH (typical on Linux, and
# for anyone who's added a browser to PATH themselves on any OS).
# Edge/Chrome are tried before Firefox simply because Edge ships by
# default on every Windows install, Christer's own platform, and is
# Chromium-based like Chrome - not a judgment about which browser is
# "better".
_EDGE_PATHS = (
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
)
_EDGE_COMMANDS = ("microsoft-edge",)
_CHROME_PATHS = (
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
)
_CHROME_COMMANDS = ("google-chrome", "chromium-browser", "chromium")
_CHROMIUM_PATHS = _EDGE_PATHS + _CHROME_PATHS
_CHROMIUM_COMMANDS = _EDGE_COMMANDS + _CHROME_COMMANDS
_FIREFOX_PATHS = (
    r"C:\Program Files\Mozilla Firefox\firefox.exe",
    r"C:\Program Files (x86)\Mozilla Firefox\firefox.exe",
    "/Applications/Firefox.app/Contents/MacOS/firefox",
)
_FIREFOX_COMMANDS = ("firefox",)
# Brave isn't part of the generic _CHROMIUM_PATHS/_CHROMIUM_COMMANDS
# fallback search above (unlike Edge/Chrome, it's never a Windows/macOS
# default install), so it's only reachable via an explicit
# --browser brave, not the OS-default-detection-failed fallback chain.
_BRAVE_PATHS = (
    r"C:\Program Files\BraveSoftware\Brave-Browser\Application\brave.exe",
    r"C:\Program Files (x86)\BraveSoftware\Brave-Browser\Application\brave.exe",
    "/Applications/Brave Browser.app/Contents/MacOS/Brave Browser",
)
_BRAVE_COMMANDS = ("brave-browser", "brave")

# --browser CHOICE -> (paths, commands, new-window flag), used by
# _open_new_window() to bypass OS-default detection entirely when the
# user has asked for a specific browser (added after Windows kept
# resetting Christer's OS-level default back to Edge regardless of
# what he'd picked in Settings - see WORKING_CONTEXT.md).
_BROWSER_OVERRIDES = {
    "chrome": (_CHROME_PATHS, _CHROME_COMMANDS, "--new-window"),
    "edge": (_EDGE_PATHS, _EDGE_COMMANDS, "--new-window"),
    "firefox": (_FIREFOX_PATHS, _FIREFOX_COMMANDS, "-new-window"),
    "brave": (_BRAVE_PATHS, _BRAVE_COMMANDS, "--new-window"),
}


def _find_browser(paths: tuple[str, ...], commands: tuple[str, ...]) -> str | None:
    for path in paths:
        if Path(path).exists():
            return path
    for command in commands:
        found = shutil.which(command)
        if found:
            return found
    return None


# The same per-user registry key Windows itself reads to decide which
# browser handles an http:// link - see _windows_default_browser_command().
_DEFAULT_BROWSER_REGISTRY_KEY = (
    r"Software\Microsoft\Windows\Shell\Associations\UrlAssociations\http\UserChoice"
)

# Executable stems (lowercase, no extension) _default_browser_launch()
# recognizes a new-window flag for. All Chromium-family browsers here
# accept the same --new-window flag Edge/Chrome do in _open_new_window()
# below; Firefox uses a different one (-new-window, single dash).
# Anything else (Internet Explorer, Safari-on-Windows, some obscure
# browser) isn't recognized - _default_browser_launch() returns None
# for it rather than guessing at a flag it might not actually support.
_CHROMIUM_EXE_STEMS = {"chrome", "msedge", "brave", "opera", "vivaldi", "chromium"}
_FIREFOX_EXE_STEMS = {"firefox"}


def _windows_default_browser_command() -> str | None:
    """The raw "open" command Windows would run for an http:// URL,
    read straight from the registry's own per-user browser choice, or
    None if it can't be read for any reason (not on Windows at all -
    winreg doesn't exist elsewhere - or the expected keys are simply
    missing).

    A separate function from _default_browser_launch() below purely so
    tests on this project's own Linux dev/CI environment can
    monkeypatch this one function directly instead of needing a real
    winreg module, which doesn't exist outside Windows at all and
    can't be imported unconditionally at module level without breaking
    bv-live everywhere else.
    """

    try:
        import winreg
    except ImportError:
        return None

    try:
        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER, _DEFAULT_BROWSER_REGISTRY_KEY
        ) as key:
            prog_id, _ = winreg.QueryValueEx(key, "ProgId")

        with winreg.OpenKey(
            winreg.HKEY_CLASSES_ROOT, rf"{prog_id}\shell\open\command"
        ) as key:
            command, _ = winreg.QueryValueEx(key, None)
    except OSError:
        return None

    return command


def _exe_from_command(command: str) -> str | None:
    """Pull the executable path out of a registry "open" command
    string - typically `"C:\\path\\to\\browser.exe" -- "%1"` (quoted,
    the common case) or occasionally an unquoted
    `C:\\path\\to\\browser.exe %1`. Returns None for an empty or
    otherwise unparseable command rather than raising."""

    command = command.strip()
    if not command:
        return None

    if command.startswith('"'):
        end = command.find('"', 1)
        if end == -1:
            return None
        return command[1:end]

    return command.split(" ", 1)[0]


def _default_browser_launch() -> tuple[str, str] | None:
    """Return (exe_path, new_window_flag) for the user's own OS-level
    default browser, or None if it can't be determined, or turns out
    to be a browser this module doesn't know a new-window flag for.

    Christer: "I want it to detect my OS-level default browser and use
    that, unless there is something that breaks the function." "The
    function" is _open_new_window()'s own job - getting a genuinely
    new *window*, not a tab (see its own docstring) - so every failure
    mode here (not on Windows, the registry key is missing, the
    command string can't be parsed, the resolved browser isn't a
    Chromium/Firefox exe this module recognizes a flag for, or the
    resolved exe doesn't actually exist on disk) returns None rather
    than raising or guessing. _open_new_window() falls back to the
    fixed Edge/Chrome/Firefox priority list either way, exactly as it
    did before this existed.
    """

    if sys.platform != "win32":
        return None

    command = _windows_default_browser_command()
    if command is None:
        return None

    exe_path = _exe_from_command(command)
    if exe_path is None or not Path(exe_path).exists():
        return None

    stem = Path(exe_path).stem.lower()
    if stem in _CHROMIUM_EXE_STEMS:
        return exe_path, "--new-window"
    if stem in _FIREFOX_EXE_STEMS:
        return exe_path, "-new-window"
    return None


def _open_new_window(url: str, browser: str = "default") -> None:
    """Open `url` in a genuinely new browser *window*, not just a new
    tab in whatever window is already open.

    webbrowser.open_new() asks for a new window too, but on Windows it
    goes through os.startfile(), which just hands the URL to the OS's
    default-app association - the browser's own already-running
    instance almost always reuses itself and opens a tab instead,
    regardless of what was actually asked for (confirmed by Christer:
    "It doesnt get its own window, it became a tab"). The only
    reliable way around that is launching a browser's own executable
    directly with a flag it actually respects for this.

    If `browser` is anything other than "default" (see --browser),
    that specific browser is tried first, bypassing OS-default
    detection entirely - added because Windows' own UserChoice
    registry kept reporting Edge as Christer's default even after he
    changed it in Settings and rebooted (see WORKING_CONTEXT.md). If
    the requested browser isn't found on disk/PATH, or fails to
    launch, this falls through to the normal chain below exactly as if
    --browser hadn't been given, rather than giving up.

    Preference order otherwise: the user's own OS-level default
    browser first (see _default_browser_launch() - Christer explicitly
    asked for this over the fixed list below, once bv-live's own
    hardcoded Edge-before-Chrome order surprised him), then the fixed
    Edge/Chrome/Firefox priority list (_find_browser()) if the default
    couldn't be determined/recognized or actually launching it failed,
    then plain webbrowser.open_new() (still opens *something*, just
    possibly a tab again) if nothing else worked at all.
    """

    if browser != "default":
        paths, commands, flag = _BROWSER_OVERRIDES[browser]
        found = _find_browser(paths, commands)
        if found is not None:
            try:
                subprocess.Popen([found, flag, url])
                return
            except OSError:
                pass

    default = _default_browser_launch()
    if default is not None:
        exe_path, flag = default
        try:
            subprocess.Popen([exe_path, flag, url])
            return
        except OSError:
            pass

    chromium = _find_browser(_CHROMIUM_PATHS, _CHROMIUM_COMMANDS)
    if chromium is not None:
        try:
            subprocess.Popen([chromium, "--new-window", url])
            return
        except OSError:
            pass

    firefox = _find_browser(_FIREFOX_PATHS, _FIREFOX_COMMANDS)
    if firefox is not None:
        try:
            subprocess.Popen([firefox, "-new-window", url])
            return
        except OSError:
            pass

    webbrowser.open_new(url)


def _open_browser_soon(url: str, browser: str = "default") -> None:
    """Schedule _open_new_window(url, browser) to run shortly, on a
    background timer thread rather than inline - called just before
    uvicorn.run() below, which then blocks the main thread until the
    server stops, so opening the browser has to happen out-of-line to
    not get stuck waiting behind it.

    A plain function (not inlined into _run()) so tests can swap out
    threading.Timer for something that fires immediately instead of
    actually waiting/spawning a thread.
    """

    threading.Timer(
        BROWSER_OPEN_DELAY_SECONDS, _open_new_window, args=(url, browser)
    ).start()


def _run(args: argparse.Namespace) -> int:
    """Run bv-live for already-parsed arguments."""

    path = config_path(args.config_dir, args.id)

    try:
        config = load_camera_config(path)
    except CameraConfigError as exc:
        print(f"bv-live: {exc}", file=sys.stderr)
        return EXIT_CONFIG_ERROR

    if not config.endpoints:
        print(
            f"bv-live: {path}: no [[endpoint]] entries found",
            file=sys.stderr,
        )
        return EXIT_CONFIG_ERROR

    try:
        endpoint, client = connect(config.endpoints, timeout=args.timeout)
    except CameraUnreachableError as exc:
        print(f"bv-live: {exc}", file=sys.stderr)
        return EXIT_UNREACHABLE

    try:
        import uvicorn
    except ImportError as exc:
        print(
            f"bv-live: uvicorn is not installed ({exc}) - "
            "pip install uvicorn fastapi",
            file=sys.stderr,
        )
        return EXIT_MISSING_DEPENDENCY

    # Imported here, not at module level - see live/__init__.py's
    # docstring: app.py pulls in fastapi, so it should only ever be
    # imported once bv-live itself actually runs (same convention
    # web/app.py already follows for bv-web).
    from ..live.app import create_live_app

    app = create_live_app(
        client,
        camera_name=config.name,
        osm_cache_dir=config.archive / ".osm_cache",
        map_zoom_meters=args.map_zoom,
        gsensor_window_seconds=args.gsensor_window,
    )

    url = f"http://{args.host}:{args.port}/"
    print(
        f"bv-live: serving {config.name} (via {endpoint.name}) at {url} - "
        "press Ctrl-C to stop"
    )

    if not args.no_browser:
        # A browser can't navigate to a wildcard bind address - open
        # it against localhost instead, even though uvicorn itself
        # still binds to whatever --host was actually given.
        browser_host = "127.0.0.1" if args.host in ("0.0.0.0", "::") else args.host
        _open_browser_soon(f"http://{browser_host}:{args.port}/", args.browser)

    uvicorn.run(app, host=args.host, port=args.port)

    return EXIT_OK


def main(argv: list[str] | None = None) -> int:
    """Run bv-live."""

    args = parse_args(argv)
    return run_cli("bv-live", lambda: _run(args))


if __name__ == "__main__":
    raise SystemExit(main())
