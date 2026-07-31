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
    assert args.timeout == 5
    assert args.no_address is False


def test_parse_args_no_address_flag():
    args = bv_gps_module.parse_args(["mycar", "--no-address"])

    assert args.no_address is True


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
