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


def test_parse_args_overrides():
    args = bv_live_module.parse_args(
        [
            "mycar",
            "--host", "0.0.0.0",
            "--port", "9000",
            "--map-zoom", "250",
            "--gsensor-window", "30",
        ]
    )

    assert args.host == "0.0.0.0"
    assert args.port == 9000
    assert args.map_zoom == 250.0
    assert args.gsensor_window == 30.0


class _FakeConfig:
    name = "MyCar"
    endpoints = [Endpoint(name="home", address="10.99.77.1")]
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
