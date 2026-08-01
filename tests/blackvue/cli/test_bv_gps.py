from pathlib import Path

import pytest

from blackvue.cli import bv_gps as bv_gps_module
from blackvue.core.blackvue_client import NoGpsDataError
from blackvue.core.endpoint import Endpoint
from blackvue.domain.live_gps_fix import LiveGpsFix
from blackvue.generate.media import MediaToolError


def test_coordinate_pair_formats_lat_lon_pasteable_into_maps():
    fix = LiveGpsFix(latitude=59.334591, longitude=18.063240)

    assert bv_gps_module.coordinate_pair(fix) == "59.334591,18.06324"


def test_google_maps_url_wraps_the_coordinate_pair():
    fix = LiveGpsFix(latitude=59.334591, longitude=18.063240)

    assert bv_gps_module.google_maps_url(fix) == (
        "https://www.google.com/maps?q=59.334591,18.06324"
    )


def test_parse_args_defaults():
    args = bv_gps_module.parse_args(["mycar"])

    assert args.id == "mycar"
    assert args.host is None
    assert args.timeout == 5
    assert args.no_address is False


def test_parse_args_no_address_flag():
    args = bv_gps_module.parse_args(["mycar", "--no-address"])

    assert args.no_address is True


def test_parse_args_host_alone():
    args = bv_gps_module.parse_args(["--host", "192.168.1.42"])

    assert args.id is None
    assert args.host == "192.168.1.42"


def test_parse_args_host_accepts_a_port():
    args = bv_gps_module.parse_args(["--host", "192.168.1.42:8080"])

    assert args.host == "192.168.1.42:8080"


def test_parse_args_requires_id_or_host(capsys):
    with pytest.raises(SystemExit):
        bv_gps_module.parse_args([])

    assert "required" in capsys.readouterr().err


def test_parse_args_rejects_both_id_and_host(capsys):
    with pytest.raises(SystemExit):
        bv_gps_module.parse_args(["mycar", "--host", "192.168.1.42"])

    assert "not allowed" in capsys.readouterr().err


class _FakeConfig:
    name = "MyCar"
    endpoints = [Endpoint(name="home", address="10.99.77.1")]
    target = Path("/tmp/whatever")


class _FakeClient:
    def __init__(self, result):
        self._result = result

    def live_gps(self):
        if isinstance(self._result, Exception):
            raise self._result
        return self._result


def _stub_connection(monkeypatch, live_gps_result):
    monkeypatch.setattr(
        bv_gps_module, "load_camera_config", lambda path: _FakeConfig()
    )
    monkeypatch.setattr(
        bv_gps_module, "config_path", lambda config_dir, id_: Path("/tmp/fake.toml")
    )
    monkeypatch.setattr(
        bv_gps_module,
        "connect",
        lambda endpoints, timeout: (endpoints[0], _FakeClient(live_gps_result)),
    )


def test_run_prints_coordinates_link_and_address(monkeypatch, capsys):
    _stub_connection(monkeypatch, LiveGpsFix(59.334591, 18.063240))
    monkeypatch.setattr(
        bv_gps_module, "reverse_geocode", lambda lat, lon: "Some Street 1, Stockholm"
    )

    args = bv_gps_module.parse_args(["mycar"])
    code = bv_gps_module._run(args)

    out = capsys.readouterr().out
    assert code == bv_gps_module.EXIT_OK
    assert "Coordinates: 59.334591,18.06324" in out
    assert "Google Maps: https://www.google.com/maps?q=59.334591,18.06324" in out
    assert "Address: Some Street 1, Stockholm" in out


def test_run_no_address_skips_the_geocoding_lookup(monkeypatch, capsys):
    _stub_connection(monkeypatch, LiveGpsFix(59.334591, 18.063240))

    def _unexpected_call(lat, lon):
        raise AssertionError("reverse_geocode should not be called with --no-address")

    monkeypatch.setattr(bv_gps_module, "reverse_geocode", _unexpected_call)

    args = bv_gps_module.parse_args(["mycar", "--no-address"])
    code = bv_gps_module._run(args)

    out = capsys.readouterr().out
    assert code == bv_gps_module.EXIT_OK
    assert "Address:" not in out


def test_run_reports_unavailable_address_on_geocoding_failure(monkeypatch, capsys):
    _stub_connection(monkeypatch, LiveGpsFix(59.334591, 18.063240))
    monkeypatch.setattr(
        bv_gps_module,
        "reverse_geocode",
        lambda lat, lon: (_ for _ in ()).throw(MediaToolError("network down")),
    )

    args = bv_gps_module.parse_args(["mycar"])
    code = bv_gps_module._run(args)

    out = capsys.readouterr().out
    assert code == bv_gps_module.EXIT_OK
    assert "Address: unavailable (network down)" in out


def test_run_exits_no_fix_for_a_zero_reading(monkeypatch, capsys):
    _stub_connection(monkeypatch, LiveGpsFix(0.0, 0.0))

    args = bv_gps_module.parse_args(["mycar"])
    code = bv_gps_module._run(args)

    assert code == bv_gps_module.EXIT_NO_FIX
    assert "no GPS fix currently available" in capsys.readouterr().err


def test_run_exits_protocol_error_when_no_gps_data_found(monkeypatch, capsys):
    _stub_connection(monkeypatch, NoGpsDataError("no GPS reading found"))

    args = bv_gps_module.parse_args(["mycar"])
    code = bv_gps_module._run(args)

    assert code == bv_gps_module.EXIT_PROTOCOL_ERROR


def _forbid_camera_config_lookup(monkeypatch):
    """Assert --host never touches bv-config's own file lookup - if it
    did, this would fail loudly instead of silently reading (or
    failing to read) some real path."""

    def _unexpected(*args, **kwargs):
        raise AssertionError("--host must not touch camera_config lookups")

    monkeypatch.setattr(bv_gps_module, "load_camera_config", _unexpected)
    monkeypatch.setattr(bv_gps_module, "config_path", _unexpected)


def test_run_with_host_skips_camera_config_entirely(monkeypatch, capsys):
    _forbid_camera_config_lookup(monkeypatch)
    monkeypatch.setattr(
        bv_gps_module,
        "connect",
        lambda endpoints, timeout: (
            endpoints[0],
            _FakeClient(LiveGpsFix(59.334591, 18.063240)),
        ),
    )
    monkeypatch.setattr(
        bv_gps_module, "reverse_geocode", lambda lat, lon: "Some Street 1, Stockholm"
    )

    args = bv_gps_module.parse_args(["--host", "192.168.1.42"])
    code = bv_gps_module._run(args)

    out = capsys.readouterr().out
    assert code == bv_gps_module.EXIT_OK
    assert "Coordinates: 59.334591,18.06324" in out


def test_run_with_host_builds_a_single_synthetic_endpoint(monkeypatch):
    _forbid_camera_config_lookup(monkeypatch)
    seen_endpoints = []

    def _fake_connect(endpoints, timeout):
        seen_endpoints.extend(endpoints)
        return endpoints[0], _FakeClient(LiveGpsFix(59.334591, 18.063240))

    monkeypatch.setattr(bv_gps_module, "connect", _fake_connect)
    monkeypatch.setattr(bv_gps_module, "reverse_geocode", lambda lat, lon: "addr")

    args = bv_gps_module.parse_args(["--host", "192.168.1.42:8080"])
    bv_gps_module._run(args)

    assert len(seen_endpoints) == 1
    assert seen_endpoints[0].address == "192.168.1.42:8080"
    assert seen_endpoints[0].name == "192.168.1.42:8080"


def test_run_with_host_reports_unreachable_cleanly(monkeypatch, capsys):
    _forbid_camera_config_lookup(monkeypatch)

    def _fake_connect(endpoints, timeout):
        from blackvue.core.connection import CameraUnreachableError

        raise CameraUnreachableError(
            f"no configured endpoint could be reached: "
            f"{endpoints[0].name} ({endpoints[0].address}): timed out"
        )

    monkeypatch.setattr(bv_gps_module, "connect", _fake_connect)

    args = bv_gps_module.parse_args(["--host", "192.168.1.99"])
    code = bv_gps_module._run(args)

    assert code == bv_gps_module.EXIT_UNREACHABLE
    assert "192.168.1.99" in capsys.readouterr().err


def test_run_with_host_no_fix_uses_the_host_as_the_label_not_config_name(
    monkeypatch, capsys
):
    """Regression test: _run() used to reference config.name here,
    which only exists on the id-based path - a --host run with a
    zero-fix reading would have raised NameError instead of printing
    a clean message. Caught and fixed before committing."""

    _forbid_camera_config_lookup(monkeypatch)
    monkeypatch.setattr(
        bv_gps_module,
        "connect",
        lambda endpoints, timeout: (endpoints[0], _FakeClient(LiveGpsFix(0.0, 0.0))),
    )

    args = bv_gps_module.parse_args(["--host", "192.168.1.42"])
    code = bv_gps_module._run(args)

    assert code == bv_gps_module.EXIT_NO_FIX
    assert "192.168.1.42" in capsys.readouterr().err


def test_run_exits_config_error_when_no_endpoints_configured(monkeypatch, capsys):
    class _NoEndpointsConfig:
        name = "MyCar"
        endpoints: list = []
        target = Path("/tmp/whatever")

    monkeypatch.setattr(
        bv_gps_module, "load_camera_config", lambda path: _NoEndpointsConfig()
    )
    monkeypatch.setattr(
        bv_gps_module, "config_path", lambda config_dir, id_: Path("/tmp/fake.toml")
    )

    args = bv_gps_module.parse_args(["mycar"])
    code = bv_gps_module._run(args)

    assert code == bv_gps_module.EXIT_CONFIG_ERROR
