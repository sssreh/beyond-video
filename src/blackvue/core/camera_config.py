"""
Camera configuration.

Copyright (C) 2026 Christer R. (sssreh)

SPDX-License-Identifier: GPL-3.0-or-later
"""

from __future__ import annotations

import json
import tomllib
from dataclasses import dataclass
from dataclasses import field
from pathlib import Path

from .endpoint import Endpoint

MAX_ID_LENGTH = 128
MAX_NAME_LENGTH = 128


class CameraConfigError(Exception):
    """Raised when a camera configuration cannot be loaded or is invalid."""


def default_config_dir() -> Path:
    """Return the default directory camera configs live in."""

    return Path.home() / ".config" / "beyond-video"


def config_path(config_dir: Path, id_: str) -> Path:
    """Return the config file path for a camera system id."""

    return config_dir / f"{id_}.cfg"


def validate_id(id_: str) -> None:
    """Validate a camera system id.

    An id is pure ASCII alphanumeric, at most 128 characters.
    """

    if not id_:
        raise CameraConfigError("id must not be empty")

    if len(id_) > MAX_ID_LENGTH:
        raise CameraConfigError(
            f"id is too long ({len(id_)} > {MAX_ID_LENGTH} characters)"
        )

    if not id_.isascii() or not id_.isalnum():
        raise CameraConfigError(
            f"id must be ASCII alphanumeric: {id_!r}"
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
