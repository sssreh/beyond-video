from pathlib import Path

from blackvue.cli import bv_live as bv_live_module
from blackvue.core.endpoint import Endpoint


def test_parse_args_defaults():
    args = bv_live_module.parse_args(["mycar"])

    assert args.id == "mycar"
    assert args.timeout == 5
    assert args.host == "127.0.0.1"
    assert args.port == 8100
    assert args.map_zoom == bv_live_module.DEFAULT_ZOOM_METERS
    assert args.gsensor_window == bv_live_module.DEFAULT_WINDOW_SECONDS
    assert args.browser == "default"


def test_parse_args_overrides():
    args = bv_live_module.parse_args(
        [
            "mycar",
            "--host", "0.0.0.0",
            "--port", "9000",
            "--map-zoom", "250",
            "--gsensor-window", "30",
            "--no-browser",
            "--browser", "chrome",
        ]
    )

    assert args.host == "0.0.0.0"
    assert args.port == 9000
    assert args.map_zoom == 250.0
    assert args.gsensor_window == 30.0
    assert args.no_browser is True
    assert args.browser == "chrome"


def test_parse_args_no_browser_defaults_to_false():
    args = bv_live_module.parse_args(["mycar"])

    assert args.no_browser is False


def test_parse_args_rejects_an_unknown_browser_choice():
    import pytest

    with pytest.raises(SystemExit):
        bv_live_module.parse_args(["mycar", "--browser", "safari"])


class _FakeConfig:
    name = "MyCar"
    endpoints = [Endpoint(name="home", address="10.99.77.1")]
    archive = Path("/tmp/archive")
    target = Path("/tmp/whatever")


def test_run_exits_config_error_when_no_endpoints_configured(monkeypatch, capsys):
    class _NoEndpointsConfig:
        name = "MyCar"
        endpoints: list = []
        target = Path("/tmp/whatever")

    monkeypatch.setattr(
        bv_live_module, "load_camera_config", lambda path: _NoEndpointsConfig()
    )
    monkeypatch.setattr(
        bv_live_module, "config_path", lambda config_dir, id_: Path("/tmp/fake.toml")
    )

    args = bv_live_module.parse_args(["mycar"])
    code = bv_live_module._run(args)

    assert code == bv_live_module.EXIT_CONFIG_ERROR


def test_run_exits_config_error_on_bad_config(monkeypatch, capsys):
    from blackvue.core.camera_config import CameraConfigError

    def _raise(path):
        raise CameraConfigError("bad config")

    monkeypatch.setattr(bv_live_module, "load_camera_config", _raise)
    monkeypatch.setattr(
        bv_live_module, "config_path", lambda config_dir, id_: Path("/tmp/fake.toml")
    )

    args = bv_live_module.parse_args(["mycar"])
    code = bv_live_module._run(args)

    err = capsys.readouterr().err
    assert code == bv_live_module.EXIT_CONFIG_ERROR
    assert "bad config" in err


def test_run_exits_unreachable_when_connect_fails(monkeypatch, capsys):
    from blackvue.core.connection import CameraUnreachableError

    monkeypatch.setattr(bv_live_module, "load_camera_config", lambda path: _FakeConfig())
    monkeypatch.setattr(
        bv_live_module, "config_path", lambda config_dir, id_: Path("/tmp/fake.toml")
    )

    def _raise(endpoints, timeout):
        raise CameraUnreachableError("no endpoint answered")

    monkeypatch.setattr(bv_live_module, "connect", _raise)

    args = bv_live_module.parse_args(["mycar"])
    code = bv_live_module._run(args)

    err = capsys.readouterr().err
    assert code == bv_live_module.EXIT_UNREACHABLE
    assert "no endpoint answered" in err


def test_run_reports_missing_uvicorn_cleanly(monkeypatch, capsys):
    # This sandbox has no uvicorn installed for real, but force the
    # ImportError deterministically (same trick test_bv_web.py's own
    # "missing uvicorn" test uses) so this stays correct even in an
    # environment where uvicorn happens to be present - see
    # WORKING_CONTEXT.md's verification note on this being
    # unexercisable end-to-end (the real uvicorn.run()/create_live_app()
    # path needs fastapi/uvicorn actually installed) in this sandbox.
    monkeypatch.setattr(bv_live_module, "load_camera_config", lambda path: _FakeConfig())
    monkeypatch.setattr(
        bv_live_module, "config_path", lambda config_dir, id_: Path("/tmp/fake.toml")
    )
    monkeypatch.setattr(
        bv_live_module,
        "connect",
        lambda endpoints, timeout: (endpoints[0], object()),
    )

    import builtins

    real_import = builtins.__import__

    def fake_import(name, *args, **kwargs):
        if name == "uvicorn":
            raise ImportError("no module named uvicorn")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)

    args = bv_live_module.parse_args(["mycar"])
    code = bv_live_module._run(args)

    err = capsys.readouterr().err
    assert code == bv_live_module.EXIT_MISSING_DEPENDENCY
    assert "uvicorn is not installed" in err


def test_open_browser_soon_schedules_a_timer_that_opens_the_url(monkeypatch):
    calls = []

    class _FakeTimer:
        def __init__(self, delay, func, args=()):
            calls.append((delay, func, args))

        def start(self):
            pass

    monkeypatch.setattr(bv_live_module.threading, "Timer", _FakeTimer)

    bv_live_module._open_browser_soon("http://127.0.0.1:8100/")

    assert len(calls) == 1
    delay, func, args = calls[0]
    assert delay == bv_live_module.BROWSER_OPEN_DELAY_SECONDS
    assert func is bv_live_module._open_new_window
    assert args == ("http://127.0.0.1:8100/", "default")


def test_open_browser_soon_passes_an_explicit_browser_choice_through(monkeypatch):
    calls = []

    class _FakeTimer:
        def __init__(self, delay, func, args=()):
            calls.append((delay, func, args))

        def start(self):
            pass

    monkeypatch.setattr(bv_live_module.threading, "Timer", _FakeTimer)

    bv_live_module._open_browser_soon("http://127.0.0.1:8100/", "chrome")

    assert len(calls) == 1
    _, _, args = calls[0]
    assert args == ("http://127.0.0.1:8100/", "chrome")


def test_open_new_window_uses_the_detected_default_browser_when_available(
    monkeypatch,
):
    # Christer: "I want it to detect my OS-level default browser and
    # use that, unless there is something that breaks the function."
    # When _default_browser_launch() finds one, _open_new_window()
    # must use it directly rather than falling through to the fixed
    # Edge/Chrome/Firefox priority list.
    monkeypatch.setattr(
        bv_live_module, "_default_browser_launch",
        lambda: (r"C:\Program Files\Google\Chrome\Application\chrome.exe", "--new-window"),
    )

    def _fail_if_called(paths, commands):
        raise AssertionError("should not fall back to the fixed priority list")

    monkeypatch.setattr(bv_live_module, "_find_browser", _fail_if_called)

    calls = []
    monkeypatch.setattr(bv_live_module.subprocess, "Popen", lambda cmd: calls.append(cmd))

    bv_live_module._open_new_window("http://127.0.0.1:8100/")

    assert calls == [
        [
            r"C:\Program Files\Google\Chrome\Application\chrome.exe",
            "--new-window",
            "http://127.0.0.1:8100/",
        ]
    ]


def test_open_new_window_uses_an_explicit_browser_override_when_given(monkeypatch):
    # Christer: Windows kept reporting Edge as his default even after
    # changing it in Settings and rebooting - --browser lets him skip
    # OS-default detection entirely and pick a specific browser.
    def _fail_if_called():
        raise AssertionError("should not consult OS-default detection")

    monkeypatch.setattr(bv_live_module, "_default_browser_launch", _fail_if_called)
    monkeypatch.setattr(
        bv_live_module, "_find_browser",
        lambda paths, commands: (
            r"C:\Program Files\Google\Chrome\Application\chrome.exe"
            if paths is bv_live_module._CHROME_PATHS
            else None
        ),
    )

    calls = []
    monkeypatch.setattr(bv_live_module.subprocess, "Popen", lambda cmd: calls.append(cmd))

    bv_live_module._open_new_window("http://127.0.0.1:8100/", "chrome")

    assert calls == [
        [
            r"C:\Program Files\Google\Chrome\Application\chrome.exe",
            "--new-window",
            "http://127.0.0.1:8100/",
        ]
    ]


def test_open_new_window_uses_brave_as_an_explicit_override(monkeypatch):
    def _fail_if_called():
        raise AssertionError("should not consult OS-default detection")

    monkeypatch.setattr(bv_live_module, "_default_browser_launch", _fail_if_called)
    monkeypatch.setattr(
        bv_live_module, "_find_browser",
        lambda paths, commands: (
            r"C:\Program Files\BraveSoftware\Brave-Browser\Application\brave.exe"
            if paths is bv_live_module._BRAVE_PATHS
            else None
        ),
    )

    calls = []
    monkeypatch.setattr(bv_live_module.subprocess, "Popen", lambda cmd: calls.append(cmd))

    bv_live_module._open_new_window("http://127.0.0.1:8100/", "brave")

    assert calls == [
        [
            r"C:\Program Files\BraveSoftware\Brave-Browser\Application\brave.exe",
            "--new-window",
            "http://127.0.0.1:8100/",
        ]
    ]


def test_open_new_window_falls_back_when_explicit_browser_not_found(monkeypatch):
    monkeypatch.setattr(bv_live_module, "_find_browser", lambda paths, commands: None)
    monkeypatch.setattr(
        bv_live_module, "_default_browser_launch",
        lambda: ("default-browser-path", "--new-window"),
    )

    calls = []
    monkeypatch.setattr(bv_live_module.subprocess, "Popen", lambda cmd: calls.append(cmd))

    bv_live_module._open_new_window("http://127.0.0.1:8100/", "firefox")

    assert calls == [["default-browser-path", "--new-window", "http://127.0.0.1:8100/"]]


def test_open_new_window_falls_back_when_explicit_browser_popen_fails(monkeypatch):
    monkeypatch.setattr(
        bv_live_module, "_find_browser",
        lambda paths, commands: (
            "edge-path" if paths is bv_live_module._EDGE_PATHS else None
        ),
    )
    monkeypatch.setattr(
        bv_live_module, "_default_browser_launch",
        lambda: ("default-browser-path", "--new-window"),
    )

    calls = []

    def _fake_popen(cmd):
        if cmd[0] == "edge-path":
            raise OSError("could not launch")
        calls.append(cmd)

    monkeypatch.setattr(bv_live_module.subprocess, "Popen", _fake_popen)

    bv_live_module._open_new_window("http://127.0.0.1:8100/", "edge")

    assert calls == [["default-browser-path", "--new-window", "http://127.0.0.1:8100/"]]


def test_open_new_window_falls_back_to_priority_list_when_default_browser_launch_finds_nothing(
    monkeypatch,
):
    monkeypatch.setattr(bv_live_module, "_default_browser_launch", lambda: None)
    monkeypatch.setattr(
        bv_live_module, "_find_browser",
        lambda paths, commands: (
            "chrome-path" if paths is bv_live_module._CHROMIUM_PATHS else None
        ),
    )

    calls = []
    monkeypatch.setattr(bv_live_module.subprocess, "Popen", lambda cmd: calls.append(cmd))

    bv_live_module._open_new_window("http://127.0.0.1:8100/")

    assert calls == [["chrome-path", "--new-window", "http://127.0.0.1:8100/"]]


def test_open_new_window_falls_back_to_priority_list_when_default_browser_popen_fails(
    monkeypatch,
):
    # The detected default browser was found, but actually launching
    # it failed (a stale registry entry pointing at an uninstalled
    # program, say) - "unless there is something that breaks the
    # function": this must still fall through to the fixed priority
    # list, not give up.
    monkeypatch.setattr(
        bv_live_module, "_default_browser_launch",
        lambda: ("default-browser-path", "--new-window"),
    )
    monkeypatch.setattr(
        bv_live_module, "_find_browser",
        lambda paths, commands: (
            "chrome-path" if paths is bv_live_module._CHROMIUM_PATHS else None
        ),
    )

    calls = []

    def _fake_popen(cmd):
        if cmd[0] == "default-browser-path":
            raise OSError("could not launch")
        calls.append(cmd)

    monkeypatch.setattr(bv_live_module.subprocess, "Popen", _fake_popen)

    bv_live_module._open_new_window("http://127.0.0.1:8100/")

    assert calls == [["chrome-path", "--new-window", "http://127.0.0.1:8100/"]]


def test_open_new_window_launches_a_found_chromium_browser_with_new_window_flag(
    monkeypatch,
):
    monkeypatch.setattr(bv_live_module, "_default_browser_launch", lambda: None)
    monkeypatch.setattr(
        bv_live_module, "_find_browser",
        lambda paths, commands: (
            r"C:\Program Files\Microsoft\Edge\Application\msedge.exe"
            if paths is bv_live_module._CHROMIUM_PATHS
            else None
        ),
    )

    calls = []
    monkeypatch.setattr(bv_live_module.subprocess, "Popen", lambda cmd: calls.append(cmd))

    bv_live_module._open_new_window("http://127.0.0.1:8100/")

    assert calls == [
        [
            r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
            "--new-window",
            "http://127.0.0.1:8100/",
        ]
    ]


def test_open_new_window_falls_back_to_firefox_when_no_chromium_found(monkeypatch):
    monkeypatch.setattr(bv_live_module, "_default_browser_launch", lambda: None)

    def fake_find(paths, commands):
        if paths is bv_live_module._FIREFOX_PATHS:
            return "/usr/bin/firefox"
        return None

    monkeypatch.setattr(bv_live_module, "_find_browser", fake_find)

    calls = []
    monkeypatch.setattr(bv_live_module.subprocess, "Popen", lambda cmd: calls.append(cmd))

    bv_live_module._open_new_window("http://127.0.0.1:8100/")

    assert calls == [["/usr/bin/firefox", "-new-window", "http://127.0.0.1:8100/"]]


def test_open_new_window_falls_back_to_webbrowser_when_nothing_found(monkeypatch):
    monkeypatch.setattr(bv_live_module, "_default_browser_launch", lambda: None)
    monkeypatch.setattr(bv_live_module, "_find_browser", lambda paths, commands: None)

    opened = []
    monkeypatch.setattr(bv_live_module.webbrowser, "open_new", opened.append)

    bv_live_module._open_new_window("http://127.0.0.1:8100/")

    assert opened == ["http://127.0.0.1:8100/"]


def test_open_new_window_falls_back_when_popen_raises(monkeypatch):
    monkeypatch.setattr(bv_live_module, "_default_browser_launch", lambda: None)
    monkeypatch.setattr(
        bv_live_module, "_find_browser",
        lambda paths, commands: (
            "chrome-path" if paths is bv_live_module._CHROMIUM_PATHS else None
        ),
    )

    def _raise(cmd):
        raise OSError("could not launch")

    monkeypatch.setattr(bv_live_module.subprocess, "Popen", _raise)

    opened = []
    monkeypatch.setattr(bv_live_module.webbrowser, "open_new", opened.append)

    bv_live_module._open_new_window("http://127.0.0.1:8100/")

    assert opened == ["http://127.0.0.1:8100/"]


def test_find_browser_checks_paths_before_commands(monkeypatch, tmp_path):
    existing = tmp_path / "browser.exe"
    existing.write_text("")

    monkeypatch.setattr(bv_live_module.shutil, "which", lambda command: "SHOULD_NOT_BE_USED")

    found = bv_live_module._find_browser((str(existing),), ())

    assert found == str(existing)


def test_find_browser_falls_back_to_commands_on_path(monkeypatch):
    monkeypatch.setattr(
        bv_live_module.shutil, "which",
        lambda command: "/usr/bin/found" if command == "google-chrome" else None,
    )

    found = bv_live_module._find_browser(("/no/such/path.exe",), ("google-chrome",))

    assert found == "/usr/bin/found"


def test_find_browser_returns_none_when_nothing_matches(monkeypatch):
    monkeypatch.setattr(bv_live_module.shutil, "which", lambda command: None)

    found = bv_live_module._find_browser(("/no/such/path.exe",), ("no-such-command",))

    assert found is None


def test_exe_from_command_parses_a_quoted_path():
    command = r'"C:\Program Files\Google\Chrome\Application\chrome.exe" -- "%1"'

    assert bv_live_module._exe_from_command(command) == (
        r"C:\Program Files\Google\Chrome\Application\chrome.exe"
    )


def test_exe_from_command_parses_an_unquoted_path():
    command = r"C:\Browsers\browser.exe %1"

    assert bv_live_module._exe_from_command(command) == r"C:\Browsers\browser.exe"


def test_exe_from_command_returns_none_for_an_empty_command():
    assert bv_live_module._exe_from_command("") is None
    assert bv_live_module._exe_from_command("   ") is None


def test_exe_from_command_returns_none_for_an_unterminated_quote():
    assert bv_live_module._exe_from_command('"C:\\no\\closing\\quote.exe') is None


def test_windows_default_browser_command_returns_none_without_winreg():
    # This project's own test/CI environment is Linux, where winreg
    # genuinely doesn't exist - a real, meaningful exercise of the
    # ImportError branch, not a mocked-out stand-in for it.
    assert bv_live_module._windows_default_browser_command() is None


def test_default_browser_launch_returns_none_on_non_windows(monkeypatch):
    monkeypatch.setattr(bv_live_module.sys, "platform", "linux")
    monkeypatch.setattr(
        bv_live_module, "_windows_default_browser_command",
        lambda: (_ for _ in ()).throw(AssertionError("should not be reached")),
    )

    assert bv_live_module._default_browser_launch() is None


def test_default_browser_launch_returns_none_when_registry_lookup_fails(monkeypatch):
    monkeypatch.setattr(bv_live_module.sys, "platform", "win32")
    monkeypatch.setattr(
        bv_live_module, "_windows_default_browser_command", lambda: None
    )

    assert bv_live_module._default_browser_launch() is None


def test_default_browser_launch_parses_a_quoted_chromium_command(monkeypatch, tmp_path):
    # Christer: "I want it to detect my OS-level default browser and
    # use that, unless there is something that breaks the function."
    chrome = tmp_path / "chrome.exe"
    chrome.write_text("")

    monkeypatch.setattr(bv_live_module.sys, "platform", "win32")
    monkeypatch.setattr(
        bv_live_module, "_windows_default_browser_command",
        lambda: f'"{chrome}" -- "%1"',
    )

    assert bv_live_module._default_browser_launch() == (str(chrome), "--new-window")


def test_default_browser_launch_parses_an_unquoted_command(monkeypatch, tmp_path):
    msedge = tmp_path / "msedge.exe"
    msedge.write_text("")

    monkeypatch.setattr(bv_live_module.sys, "platform", "win32")
    monkeypatch.setattr(
        bv_live_module, "_windows_default_browser_command", lambda: f"{msedge} %1"
    )

    assert bv_live_module._default_browser_launch() == (str(msedge), "--new-window")


def test_default_browser_launch_recognizes_the_firefox_flag(monkeypatch, tmp_path):
    firefox = tmp_path / "firefox.exe"
    firefox.write_text("")

    monkeypatch.setattr(bv_live_module.sys, "platform", "win32")
    monkeypatch.setattr(
        bv_live_module, "_windows_default_browser_command",
        lambda: f'"{firefox}" -osint -url "%1"',
    )

    assert bv_live_module._default_browser_launch() == (str(firefox), "-new-window")


def test_default_browser_launch_returns_none_for_an_unrecognized_browser(
    monkeypatch, tmp_path
):
    # Internet Explorer, or anything else this module doesn't know a
    # new-window flag for - falls back to the fixed priority list
    # rather than guessing at a flag that might not exist.
    iexplore = tmp_path / "iexplore.exe"
    iexplore.write_text("")

    monkeypatch.setattr(bv_live_module.sys, "platform", "win32")
    monkeypatch.setattr(
        bv_live_module, "_windows_default_browser_command",
        lambda: f'"{iexplore}" -- "%1"',
    )

    assert bv_live_module._default_browser_launch() is None


def test_default_browser_launch_returns_none_when_the_resolved_exe_does_not_exist(
    monkeypatch,
):
    # A stale/dangling registry entry (an uninstalled browser) - "unless
    # there is something that breaks the function" means this must not
    # hand back a path that can't actually be launched.
    monkeypatch.setattr(bv_live_module.sys, "platform", "win32")
    monkeypatch.setattr(
        bv_live_module, "_windows_default_browser_command",
        lambda: r'"C:\does\not\exist\chrome.exe" -- "%1"',
    )

    assert bv_live_module._default_browser_launch() is None


def _stub_successful_connection(monkeypatch, sys_module):
    """Get _run() past config-loading/connect()/the uvicorn import
    check, and past the `from ..live.app import create_live_app`
    import (which would otherwise genuinely fail - no fastapi
    installed in this sandbox - see WORKING_CONTEXT.md's verification
    note) by pre-seeding sys.modules with a fake blackvue.live.app and
    a fake uvicorn whose run() just records its own call instead of
    actually serving forever."""

    import types

    monkeypatch.setattr(bv_live_module, "load_camera_config", lambda path: _FakeConfig())
    monkeypatch.setattr(
        bv_live_module, "config_path", lambda config_dir, id_: Path("/tmp/fake.toml")
    )
    monkeypatch.setattr(
        bv_live_module,
        "connect",
        lambda endpoints, timeout: (endpoints[0], object()),
    )

    fake_uvicorn = types.ModuleType("uvicorn")
    uvicorn_calls = []
    fake_uvicorn.run = lambda app, **kwargs: uvicorn_calls.append((app, kwargs))
    monkeypatch.setitem(sys_module.modules, "uvicorn", fake_uvicorn)

    fake_live_app = types.ModuleType("blackvue.live.app")
    fake_live_app.create_live_app = lambda *a, **kw: "FAKE_APP"
    monkeypatch.setitem(sys_module.modules, "blackvue.live.app", fake_live_app)

    return uvicorn_calls


def test_run_opens_the_browser_by_default(monkeypatch, capsys):
    import sys

    _stub_successful_connection(monkeypatch, sys)

    opened = []
    monkeypatch.setattr(
        bv_live_module, "_open_browser_soon",
        lambda url, browser="default": opened.append((url, browser)),
    )

    args = bv_live_module.parse_args(["mycar", "--port", "8100"])
    code = bv_live_module._run(args)

    assert code == bv_live_module.EXIT_OK
    assert opened == [("http://127.0.0.1:8100/", "default")]


def test_run_skips_opening_the_browser_with_no_browser_flag(monkeypatch, capsys):
    import sys

    _stub_successful_connection(monkeypatch, sys)

    opened = []
    monkeypatch.setattr(bv_live_module, "_open_browser_soon", opened.append)

    args = bv_live_module.parse_args(["mycar", "--no-browser"])
    code = bv_live_module._run(args)

    assert code == bv_live_module.EXIT_OK
    assert opened == []


def test_run_opens_localhost_instead_of_a_wildcard_bind_address(monkeypatch, capsys):
    import sys

    _stub_successful_connection(monkeypatch, sys)

    opened = []
    monkeypatch.setattr(
        bv_live_module, "_open_browser_soon",
        lambda url, browser="default": opened.append((url, browser)),
    )

    args = bv_live_module.parse_args(["mycar", "--host", "0.0.0.0", "--port", "9000"])
    code = bv_live_module._run(args)

    assert code == bv_live_module.EXIT_OK
    assert opened == [("http://127.0.0.1:9000/", "default")]
