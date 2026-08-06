"""
Camera configuration.

Copyright (C) 2026 Christer R. (sssreh)

SPDX-License-Identifier: GPL-3.0-or-later
"""

from __future__ import annotations

import json
import os
import time
import tomllib
from dataclasses import dataclass
from dataclasses import field
from pathlib import Path

from .endpoint import Endpoint

MAX_ID_LENGTH = 128
MAX_NAME_LENGTH = 128

# Every bv-* CLI's own --config-dir flag defaults to
# default_config_dir() - fine for a native/bare-metal install, but
# bv-web's Docker container has no persistent $HOME (a fresh, empty
# one baked into the image, recreated on every rebuild) and no
# per-invocation flag to override it: unlike bv-cli's one-off
# `docker-compose run` commands (which always get an explicit
# --config-dir /data/config), bv-web is a single long-running process
# whose own camera-picker (app.py's _camera_options()/
# _find_camera_archive()) and in-process job runner
# (jobs.py's start_bv_config()/start_bv_gps(), deliberately kept to a
# "curated subset, not every CLI flag" of options - see their own
# docstrings) call default_config_dir() with no override at all. This
# environment variable lets docker-compose.yml point every one of
# those call sites at the same persisted host folder bv-cli/native CLI
# use, without adding a --config-dir flag to bv-web's own CLI surface
# or threading it through create_app()/JobRunner - confirmed missing
# entirely on a real deployment: Christer's bv-web container could see
# zero cameras (empty pick-list, archive browser 404ing every camera
# id) until this was set, since $HOME/.config/beyond-video inside the
# container was never mounted to anything.
_CONFIG_DIR_ENV_VAR = "BEYOND_VIDEO_CONFIG_DIR"


class CameraConfigError(Exception):
    """Raised when a camera configuration cannot be loaded or is invalid."""


def default_config_dir() -> Path:
    """Return the default directory camera configs live in - the
    BEYOND_VIDEO_CONFIG_DIR environment variable if set (see its own
    comment above), otherwise ~/.config/beyond-video."""

    override = os.environ.get(_CONFIG_DIR_ENV_VAR)
    if override:
        return Path(override)
    return Path.home() / ".config" / "beyond-video"


def config_path(config_dir: Path, id_: str) -> Path:
    """Return the config file path for a camera system id."""

    return config_dir / f"{id_}.cfg"


def list_camera_ids(config_dir: Path) -> list[str]:
    """Return every camera system id with a config file in config_dir,
    sorted alphabetically.

    Just filenames (each *.cfg file's stem) - deliberately doesn't
    load/validate the contents, so one corrupt config can't make the
    whole listing fail; a caller that wants a human-friendly label per
    id (e.g. bv-web's job-trigger forms) can load_camera_config() each
    one itself and fall back to the bare id if that raises.

    Every bv-* command that takes a camera id still requires it
    explicitly (see e.g. bv_config.py's `id` positional argument) -
    this exists for UIs that want to offer a pick-list rather than
    making someone remember/retype an id, not to change that.
    Returns an empty list if config_dir doesn't exist yet (e.g. before
    bv-config has ever been run).
    """

    if not config_dir.is_dir():
        return []

    return sorted(path.stem for path in config_dir.glob("*.cfg") if path.is_file())


def validate_id(id_: str) -> None:
    """Validate a camera system id.

    An id is ASCII alphanumeric plus underscore/hyphen, at most 128
    characters. Underscore and hyphen were added specifically so a
    camera's archive can be split into per-year ids like "Kirby_2019"
    .. "Kirby_2026" (each with its own .cfg and a `target` pointing
    at that year's own subfolder) - a manual way to shrink an
    otherwise huge single archive-browser page, on top of (not
    instead of) archive_recording_list.html's own lazy-loaded
    thumbnails. Still excludes "/", "\\", space, and anything non-
    ASCII: the id becomes both a filename (config_path()'s
    f"{id_}.cfg") and a raw URL path segment (bv-web's
    /archive/{camera_id}/... routes), so it needs to stay
    filesystem-safe and URL-safe without escaping - underscore and
    hyphen are both, the characters excluded here generally aren't.
    """

    if not id_:
        raise CameraConfigError("id must not be empty")

    if len(id_) > MAX_ID_LENGTH:
        raise CameraConfigError(
            f"id is too long ({len(id_)} > {MAX_ID_LENGTH} characters)"
        )

    if not id_.isascii() or not all(c.isalnum() or c in "_-" for c in id_):
        raise CameraConfigError(
            f"id must be ASCII alphanumeric, underscore, or hyphen: {id_!r}"
        )


def validate_name(name: str) -> None:
    """Validate a camera display name.

    A name is UTF-8 text, at most 128 characters.
    """

    if not name:
        raise CameraConfigError("name must not be empty")

    if len(name) > MAX_NAME_LENGTH:
        raise CameraConfigError(
            f"name is too long ({len(name)} > {MAX_NAME_LENGTH} characters)"
        )


@dataclass
class CameraConfig:
    """One camera system: identity, endpoints, and archive target."""

    id: str
    name: str
    target: Path
    endpoints: list[Endpoint] = field(default_factory=list)


def load_camera_config(path: Path) -> CameraConfig:
    """Load a camera config from a .cfg (TOML) file."""

    try:
        with path.open("rb") as file:
            data = tomllib.load(file)
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise CameraConfigError(f"{path}: {exc}") from exc

    id_ = data.get("id", path.stem)
    name = data.get("name", id_)

    if "target" not in data:
        raise CameraConfigError(f"{path}: missing required key 'target'")

    endpoints: list[Endpoint] = []

    for item in data.get("endpoint", []):
        try:
            endpoints.append(
                Endpoint(
                    name=item["name"],
                    address=item["address"],
                )
            )
        except KeyError as exc:
            raise CameraConfigError(
                f"{path}: endpoint entry missing required key {exc}"
            ) from exc

    return CameraConfig(
        id=id_,
        name=name,
        target=Path(data["target"]),
        endpoints=endpoints,
    )


class CameraConfigCache:
    """Caches load_camera_config() results briefly, per camera id -
    same short-TTL pattern as bv-web's TripCache (web/trips.py) and
    ArchiveRecordingCache (web/archive_browser.py); see their own
    docstrings for the full reasoning. Added for bv-web's archive
    browser specifically: every thumbnail request, the detail page,
    and every HTTP range request during video playback resolves a
    camera id to its archive path first (see app.py's
    _find_camera_archive()), and that was re-reading and re-parsing
    the same small TOML .cfg file from scratch on every single
    request, even though it never changes between requests in a
    burst. Small next to the read_recording() directory-scan bug this
    followed (see WORKING_CONTEXT.md), but genuine, redundant work.

    Lives here rather than in web/app.py, unlike the other two caches
    which live next to the functions they wrap - this module is
    shared with every CLI tool (bv-config, bv-gps, ...), which each
    run once and should always see a config fresh from disk, not
    through a caching layer meant for a burst of concurrent HTTP
    requests. Those callers are unaffected: they keep calling
    load_camera_config() directly, and simply never construct this
    class.

    A load_camera_config() failure (CameraConfigError - a missing or
    corrupt .cfg) is never cached, matching the other two caches'
    "don't cache a miss" rule: it isn't caught here, so it propagates
    straight to the caller (which already 404s on it in bv-web)
    without ever reaching the point where a result would be stored.
    """

    def __init__(self, ttl_seconds: float = 2.0) -> None:
        self._ttl_seconds = ttl_seconds
        self._entries: dict[str, tuple[CameraConfig, float]] = {}

    def get(self, config_dir: Path, id_: str) -> CameraConfig:
        now = time.monotonic()

        cached = self._entries.get(id_)
        if cached is not None:
            config, expires_at = cached
            if now < expires_at:
                return config

        config = load_camera_config(config_path(config_dir, id_))
        self._entries[id_] = (config, now + self._ttl_seconds)
        return config


def _toml_string(value: str) -> str:
    """Render a TOML basic string, keeping UTF-8 text literal."""

    return json.dumps(value, ensure_ascii=False)


def save_camera_config(path: Path, config: CameraConfig) -> None:
    """Save a camera config to a .cfg (TOML) file.

    The file is plain TOML and is meant to be hand-editable.
    """

    lines = [
        f"id = {_toml_string(config.id)}",
        f"name = {_toml_string(config.name)}",
        f"target = {_toml_string(str(config.target))}",
        "",
    ]

    for endpoint in config.endpoints:
        lines.append("[[endpoint]]")
        lines.append(f"name = {_toml_string(endpoint.name)}")
        lines.append(f"address = {_toml_string(endpoint.address)}")
        lines.append("")

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines), encoding="utf-8")
