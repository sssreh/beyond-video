from pathlib import Path

import pytest

from blackvue.core.camera_config import CameraConfig
from blackvue.core.camera_config import CameraConfigError
from blackvue.core.camera_config import config_path
from blackvue.core.camera_config import default_config_dir
from blackvue.core.camera_config import list_camera_ids
from blackvue.core.camera_config import load_camera_config
from blackvue.core.camera_config import save_camera_config
from blackvue.core.camera_config import validate_id
from blackvue.core.camera_config import validate_name
from blackvue.core.endpoint import Endpoint


def test_config_path():
    assert config_path(Path("/cfg"), "Kirby") == Path("/cfg/Kirby.cfg")


# ---------------------------------------------------------------------------
# default_config_dir() - BEYOND_VIDEO_CONFIG_DIR env var override, added so
# bv-web's Docker container (no persistent $HOME) can be pointed at the same
# host folder bv-cli's `docker-compose run --config-dir /data/config`
# invocations use. See core/camera_config.py's own comment on this.
# ---------------------------------------------------------------------------


def test_default_config_dir_uses_env_var_override_when_set(monkeypatch):
    monkeypatch.setenv("BEYOND_VIDEO_CONFIG_DIR", "/data/camera-config")

    assert default_config_dir() == Path("/data/camera-config")


def test_default_config_dir_falls_back_to_home_when_unset(monkeypatch):
    monkeypatch.delenv("BEYOND_VIDEO_CONFIG_DIR", raising=False)

    assert default_config_dir() == Path.home() / ".config" / "beyond-video"


def test_default_config_dir_falls_back_to_home_when_empty(monkeypatch):
    # An empty string is falsy - treated the same as unset, not as "use the
    # current directory" (Path("")'s own surprising meaning).
    monkeypatch.setenv("BEYOND_VIDEO_CONFIG_DIR", "")

    assert default_config_dir() == Path.home() / ".config" / "beyond-video"


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


def test_list_camera_ids_returns_every_cfg_stem_sorted(tmp_path):
    (tmp_path / "zebra.cfg").write_text('target = "/x"\n')
    (tmp_path / "Kirby.cfg").write_text('target = "/x"\n')
    (tmp_path / "acorn.cfg").write_text('target = "/x"\n')

    assert list_camera_ids(tmp_path) == ["Kirby", "acorn", "zebra"]


def test_list_camera_ids_ignores_non_cfg_files(tmp_path):
    (tmp_path / "Kirby.cfg").write_text('target = "/x"\n')
    (tmp_path / "notes.txt").write_text("not a config\n")
    (tmp_path / "backup.cfg.bak").write_text("also not a config\n")

    assert list_camera_ids(tmp_path) == ["Kirby"]


def test_list_camera_ids_ignores_subdirectories_named_like_a_config(tmp_path):
    (tmp_path / "Kirby.cfg").mkdir()

    assert list_camera_ids(tmp_path) == []


def test_list_camera_ids_empty_when_dir_has_no_configs(tmp_path):
    assert list_camera_ids(tmp_path) == []


def test_list_camera_ids_empty_when_dir_does_not_exist(tmp_path):
    assert list_camera_ids(tmp_path / "does-not-exist") == []


def test_list_camera_ids_does_not_raise_on_a_corrupt_config(tmp_path):
    # list_camera_ids() is filename-only (no tomllib.load()) so one
    # unparsable .cfg can't break the whole listing - load_camera_
    # config() is what a caller would use per-id to get a friendly
    # display name, and that's expected to fail for this file, but the
    # listing itself must still succeed.
    (tmp_path / "broken.cfg").write_text("this is not valid TOML {{{\n")

    assert list_camera_ids(tmp_path) == ["broken"]
