from pathlib import Path

import pytest

from blackvue.core.camera_config import CameraConfig
from blackvue.core.camera_config import CameraConfigError
from blackvue.core.camera_config import config_path
from blackvue.core.camera_config import load_camera_config
from blackvue.core.camera_config import save_camera_config
from blackvue.core.camera_config import validate_id
from blackvue.core.camera_config import validate_name
from blackvue.core.endpoint import Endpoint


def test_config_path():
    assert config_path(Path("/cfg"), "Kirby") == Path("/cfg/Kirby.cfg")


@pytest.mark.parametrize(
    "id_",
    ["", "has space", "has-dash", "kåge", "x" * 129],
)
def test_validate_id_rejects(id_):
    with pytest.raises(CameraConfigError):
        validate_id(id_)


@pytest.mark.parametrize("id_", ["Kirby123", "x" * 128])
def test_validate_id_accepts(id_):
    validate_id(id_)


def test_validate_name_rejects_empty():
    with pytest.raises(CameraConfigError):
        validate_name("")


def test_validate_name_rejects_too_long():
    with pytest.raises(CameraConfigError):
        validate_name("x" * 129)


@pytest.mark.parametrize(
    "name",
    ["Kirby", "กล้องของดาว", "Kågeröd brown camera"],
)
def test_validate_name_accepts_utf8(name):
    validate_name(name)


def test_save_and_load_round_trip(tmp_path):
    path = tmp_path / "Kirby.cfg"

    config = CameraConfig(
        id="Kirby",
        name="Kågeröd brown camera",
        target=Path("/volume1/dashcam/Kirby"),
        endpoints=[
            Endpoint(name="Wifi", address="192.168.0.1"),
            Endpoint(name="SIM", address="203.0.113.10"),
        ],
    )

    save_camera_config(path, config)

    loaded = load_camera_config(path)

    assert loaded == config


def test_load_missing_target_is_an_error(tmp_path):
    path = tmp_path / "Kirby.cfg"
    path.write_text('id = "Kirby"\nname = "Kirby"\n')

    with pytest.raises(CameraConfigError):
        load_camera_config(path)


def test_load_defaults_id_and_name_from_filename(tmp_path):
    path = tmp_path / "Kirby.cfg"
    path.write_text('target = "/volume1/dashcam/Kirby"\n')

    loaded = load_camera_config(path)

    assert loaded.id == "Kirby"
    assert loaded.name == "Kirby"
    assert loaded.endpoints == []
