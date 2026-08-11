from pathlib import Path

import pytest

from blackvue.core.camera_config import CameraConfig
from blackvue.core.camera_config import CameraConfigCache
from blackvue.core.camera_config import CameraConfigError
from blackvue.core.camera_config import config_path
from blackvue.core.camera_config import default_config_dir
from blackvue.core.camera_config import default_target_dir
from blackvue.core.camera_config import list_camera_ids
from blackvue.core.camera_config import load_camera_config
from blackvue.core.camera_config import resolve_archive_path
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
    ["", "has space", "kåge", "x" * 129, "slash/id", "back\\slash"],
)
def test_validate_id_rejects(id_):
    with pytest.raises(CameraConfigError):
        validate_id(id_)


# ---------------------------------------------------------------------------
# default_target_dir() - the suggested [default] bv-config's wizard offers
# for a brand-new camera's Target prompt, so creating one doesn't require
# typing a full path by hand for the common case (see bv_config.py's
# run_wizard()).
# ---------------------------------------------------------------------------


def test_default_target_dir_nests_under_home_by_camera_id():
    assert default_target_dir("Kirby") == Path.home() / "beyond-video" / "archive" / "Kirby"


def test_default_target_dir_differs_per_camera_id():
    # Deliberately nested by id (unlike default_config_dir()) so two
    # cameras' suggested defaults never collide.
    assert default_target_dir("Kirby") != default_target_dir("Wren")


@pytest.mark.parametrize(
    "id_",
    # Underscore/hyphen accepted specifically for per-year camera ids
    # like "Kirby_2019" - see validate_id()'s own docstring.
    ["Kirby123", "x" * 128, "Kirby_2019", "Kirby-2019", "_-_"],
)
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


def test_save_and_load_round_trip_with_output(tmp_path):
    path = tmp_path / "Kirby.cfg"

    config = CameraConfig(
        id="Kirby",
        name="Kirby",
        target=Path("/volume1/dashcam/Kirby"),
        output=Path("/volume1/exports/Kirby"),
        endpoints=[],
    )

    save_camera_config(path, config)

    loaded = load_camera_config(path)

    assert loaded == config
    assert loaded.output == Path("/volume1/exports/Kirby")


def test_output_defaults_to_none_when_omitted(tmp_path):
    path = tmp_path / "Kirby.cfg"
    path.write_text('target = "/volume1/dashcam/Kirby"\n')

    loaded = load_camera_config(path)

    assert loaded.output is None


def test_save_omits_output_line_when_not_set(tmp_path):
    path = tmp_path / "Kirby.cfg"

    config = CameraConfig(id="Kirby", name="Kirby", target=Path("/x"))
    save_camera_config(path, config)

    assert "output" not in path.read_text()


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


# ---------------------------------------------------------------------------
# CameraConfigCache - added for bv-web's archive browser: every thumbnail
# request, the detail page, and every HTTP range request during video
# playback resolves a camera id to its config, and that was re-reading and
# re-parsing the same .cfg file from scratch every time. Same short-TTL
# pattern as web/trips.py's TripCache and web/archive_browser.py's
# ArchiveRecordingCache - see this class's own docstring. time.monotonic()
# is monkeypatched here (rather than a real time.sleep()) to control TTL
# expiry deterministically and instantly, matching those two caches' tests.
# ---------------------------------------------------------------------------


class _FakeClock:
    def __init__(self, start=0.0):
        self.value = start

    def __call__(self):
        return self.value


def test_camera_config_cache_reuses_result_within_ttl(tmp_path, monkeypatch):
    import blackvue.core.camera_config as camera_config_module

    clock = _FakeClock()
    monkeypatch.setattr(camera_config_module.time, "monotonic", clock)

    (tmp_path / "Kirby.cfg").write_text('target = "/data/archive"\n')

    cache = CameraConfigCache(ttl_seconds=2.0)
    first = cache.get(tmp_path, "Kirby")

    # The .cfg changes after the first (real) load - a second get() still
    # within the TTL should return the exact same cached CameraConfig, not
    # notice the change yet.
    (tmp_path / "Kirby.cfg").write_text('target = "/data/other"\n')
    clock.value += 1.0
    second = cache.get(tmp_path, "Kirby")

    assert second is first
    assert second.target == Path("/data/archive")


def test_camera_config_cache_reloads_once_ttl_expires(tmp_path, monkeypatch):
    import blackvue.core.camera_config as camera_config_module

    clock = _FakeClock()
    monkeypatch.setattr(camera_config_module.time, "monotonic", clock)

    (tmp_path / "Kirby.cfg").write_text('target = "/data/archive"\n')

    cache = CameraConfigCache(ttl_seconds=2.0)
    first = cache.get(tmp_path, "Kirby")

    (tmp_path / "Kirby.cfg").write_text('target = "/data/other"\n')
    clock.value += 2.1
    second = cache.get(tmp_path, "Kirby")

    assert second is not first
    assert second.target == Path("/data/other")


def test_camera_config_cache_does_not_cache_a_load_failure(tmp_path, monkeypatch):
    import blackvue.core.camera_config as camera_config_module

    clock = _FakeClock()
    monkeypatch.setattr(camera_config_module.time, "monotonic", clock)

    cache = CameraConfigCache(ttl_seconds=2.0)

    with pytest.raises(CameraConfigError):
        cache.get(tmp_path, "Kirby")

    # No time has passed at all - if the failure had been cached, this
    # would still raise even though the config now genuinely exists.
    (tmp_path / "Kirby.cfg").write_text('target = "/data/archive"\n')
    assert cache.get(tmp_path, "Kirby").target == Path("/data/archive")


def test_camera_config_cache_keys_are_per_camera_id(tmp_path, monkeypatch):
    import blackvue.core.camera_config as camera_config_module

    clock = _FakeClock()
    monkeypatch.setattr(camera_config_module.time, "monotonic", clock)

    (tmp_path / "Kirby.cfg").write_text('target = "/data/kirby"\n')
    (tmp_path / "Volvo.cfg").write_text('target = "/data/volvo"\n')

    cache = CameraConfigCache(ttl_seconds=2.0)

    assert cache.get(tmp_path, "Kirby").target == Path("/data/kirby")
    assert cache.get(tmp_path, "Volvo").target == Path("/data/volvo")


# ---------------------------------------------------------------------------
# resolve_archive_path() - the shared CLI-layer resolver bv-ls/bv-generate/
# bv-export/bv-scribe/bv-search all use for their `path` positional, so a
# bare camera id (e.g. "Kirby") resolves to that camera's own `target`
# directory, same ids bv-config/bv-download/bv-gps/bv-live already take. A
# literal path (relative-dot-prefixed, absolute, or containing a separator)
# always wins as an explicit escape hatch, matching git's own ./file-vs-
# branch-name disambiguation.
# ---------------------------------------------------------------------------


def test_resolve_archive_path_resolves_a_known_camera_id(tmp_path):
    (tmp_path / "Kirby.cfg").write_text('target = "/volume1/dashcam/Kirby"\n')

    path, config = resolve_archive_path("Kirby", tmp_path)

    assert path == Path("/volume1/dashcam/Kirby")
    assert config is not None
    assert config.id == "Kirby"


def test_resolve_archive_path_falls_back_to_literal_when_no_such_camera(tmp_path):
    path, config = resolve_archive_path("some_bare_dir", tmp_path)

    assert path == Path("some_bare_dir")
    assert config is None


@pytest.mark.parametrize(
    "path_or_id",
    ["./Kirby", ".\\Kirby", ".", "..", "/abs/Kirby", "sub/Kirby", "sub\\Kirby"],
)
def test_resolve_archive_path_treats_path_shaped_values_as_literal(
    tmp_path, path_or_id
):
    # Even though a "Kirby" camera config exists, every one of these is
    # explicitly path-shaped (the git-style escape hatch) and must never
    # be resolved as the camera id.
    (tmp_path / "Kirby.cfg").write_text('target = "/volume1/dashcam/Kirby"\n')

    path, config = resolve_archive_path(path_or_id, tmp_path)

    assert path == Path(path_or_id)
    assert config is None


def test_resolve_archive_path_propagates_a_broken_camera_config(tmp_path):
    (tmp_path / "Kirby.cfg").write_text("this is not valid TOML {{{\n")

    with pytest.raises(CameraConfigError):
        resolve_archive_path("Kirby", tmp_path)


def test_resolve_archive_path_exposes_camera_output_field(tmp_path):
    (tmp_path / "Kirby.cfg").write_text(
        'target = "/volume1/dashcam/Kirby"\noutput = "/volume1/exports/Kirby"\n'
    )

    _path, config = resolve_archive_path("Kirby", tmp_path)

    assert config.output == Path("/volume1/exports/Kirby")
