"""
Camera configuration.

Copyright (C) 2026 Christer R. (sssreh)

SPDX-License-Identifier: GPL-3.0-or-later
"""

from __future__ import annotations

import json
import os
import sys
import time
import tomllib
from dataclasses import dataclass
from dataclasses import field
from pathlib import Path

from .endpoint import Endpoint

MAX_ID_LENGTH = 128
MAX_NAME_LENGTH = 128

# Camera ids share default_config_dir() with bv-web's own web-users.cfg
# accounts file (see web/users.py's default_users_path(), which
# imports WEB_USERS_ID rather than hardcoding "web-users" a second
# time so the two can never drift) in the unconfigured/native-install
# case - config_path()'s own f"{id_}.cfg" means a camera id of
# "web-users" would resolve to the exact same path the accounts file
# lives at, so it's reserved rather than left to collide silently.
# validate_id() rejects it outright; list_camera_ids() also skips it
# defensively (e.g. for a config directory that already had a stray
# web-users.cfg on disk before this reservation existed).
WEB_USERS_ID = "web-users"
RESERVED_CAMERA_IDS = frozenset({WEB_USERS_ID})

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

# Same override pattern as _CONFIG_DIR_ENV_VAR above, for
# default_logs_dir() below - a Docker deployment needs this pointed at
# a mounted host folder too, the same reasoning as BEYOND_VIDEO_CONFIG_DIR/
# BEYOND_VIDEO_USERS_FILE.
_LOGS_DIR_ENV_VAR = "BEYOND_VIDEO_LOGS_DIR"


class CameraConfigError(Exception):
    """Raised when a camera configuration cannot be loaded or is invalid."""


# The original default, superseded below by ~/beyond-video-data/.config -
# kept as its own name so the one-time migration logic (and its tests) can
# refer to "the old place" without re-deriving it inline. Christer's own
# framing: ".config" read oddly holding non-config app state once logs/
# history joined it, and a dotfolder isn't somewhere he'd browse to by
# default - see WORKING_CONTEXT.md's beyond-video-data/ discussion.
def _legacy_config_dir() -> Path:
    return Path.home() / ".config" / "beyond-video"


def _new_config_dir() -> Path:
    return Path.home() / "beyond-video-data" / ".config"


def default_config_dir() -> Path:
    """Return the default directory camera configs (and bv-web's
    web-users.cfg, see web/users.py's default_users_path()) live in -
    the BEYOND_VIDEO_CONFIG_DIR environment variable if set (see its
    own comment above), otherwise ~/beyond-video-data/.config.

    One-time auto-migration: this used to default to
    ~/.config/beyond-video. If the new location doesn't exist yet but
    the old one does, the old folder is renamed (not copied) into the
    new location - same home-directory tree, so a plain Path.rename()
    needs no cross-device copy - and a one-line notice is printed so
    it's not a silent surprise the first time a bv-* command runs
    after upgrading. After that one move, the new location exists, so
    every later call just returns it directly - no ongoing dual-path
    checking. If the rename itself fails (permissions, concurrent
    access, etc.) this falls back to returning the old location rather
    than losing every camera config - not fatal, and the same check
    just runs again on the next invocation."""

    override = os.environ.get(_CONFIG_DIR_ENV_VAR)
    if override:
        return Path(override)

    new_dir = _new_config_dir()
    if new_dir.exists():
        return new_dir

    old_dir = _legacy_config_dir()
    if old_dir.exists():
        try:
            new_dir.parent.mkdir(parents=True, exist_ok=True)
            old_dir.rename(new_dir)
        except OSError as exc:
            print(
                f"beyond-video: couldn't move config folder from {old_dir} "
                f"to {new_dir} ({exc}) - using the old location for now.",
                file=sys.stderr,
            )
            return old_dir
        print(
            f"beyond-video: moved config folder from {old_dir} to "
            f"{new_dir} (new default location).",
            file=sys.stderr,
        )
        return new_dir

    return new_dir


def default_logs_dir() -> Path:
    """Return the default directory persistent command-history/output
    logs live in - the BEYOND_VIDEO_LOGS_DIR environment variable if
    set, otherwise ~/beyond-video-data/logs (a sibling of
    default_config_dir()'s own ~/beyond-video-data/.config, same
    parent folder - see WORKING_CONTEXT.md's beyond-video-data/
    discussion). No migration needed here, unlike default_config_dir():
    there was never an old default for this - persistent logging is a
    new feature, not a relocated one."""

    override = os.environ.get(_LOGS_DIR_ENV_VAR)
    if override:
        return Path(override)
    return Path.home() / "beyond-video-data" / "logs"


def config_path(config_dir: Path, id_: str) -> Path:
    """Return the config file path for a camera system id."""

    return config_dir / f"{id_}.cfg"


def default_archive_dir(id_: str) -> Path:
    """Return a suggested default directory for a new camera's Archive
    field - `~/beyond-video/archive/<id>`, offered as bv-config's
    wizard's own [default] value so creating a new camera doesn't
    require typing a full path by hand for the common case.

    Deliberately nested by id (unlike default_config_dir(), which
    holds every camera's own already-namespaced `<id>.cfg` file
    directly) so two cameras' suggested defaults never collide.
    Purely a suggestion: always overridable in the wizard, and this
    function never creates the directory itself - save_camera_config()
    doesn't touch it either; only bv-download actually writes files
    there, and it already creates its destination as needed.
    """

    return Path.home() / "beyond-video" / "archive" / id_


def default_target_dir(archive: Path) -> Path:
    """Return a suggested default Target (bv-export destination)
    directory, parallel to a camera's own Archive (download)
    directory - the same archive-vs-trips sibling relationship this
    project's own Docker deployment already uses (see docs/DEPLOY.md's
    data/archive vs data/trips layout, and docker-compose.yml's own
    volumes of the same names).

    Swaps the last path component literally named "archive" (case-
    insensitive, so "Archive"/"ARCHIVE" also match) for "trips" -
    `.../archive/Kirby` suggests `.../trips/Kirby`, and a NAS
    deployment's `/data/archive` suggests `/data/trips`. Falls back to
    a plain `trips` directory next to Archive itself
    (`archive.parent / "trips"`) when no component is literally named
    "archive" - a custom Archive that doesn't follow that convention
    still gets a sensible, clearly-different-from-Archive suggestion
    rather than no suggestion at all.

    Purely a suggestion, like default_archive_dir(): always overridable
    in the wizard, and never created by this function itself.
    """

    parts = list(archive.parts)

    for index in range(len(parts) - 1, -1, -1):
        if parts[index].lower() == "archive":
            parts[index] = "trips"
            return Path(*parts)

    return archive.parent / "trips"


def default_snapshots_dir(id_: str) -> Path:
    """Return the default directory bv-web saves a camera's bv-snap
    F/R/I snapshots into - `~/beyond-video-data/snapshots/<id>`, a
    sibling of default_logs_dir()'s own `~/beyond-video-data/logs`
    (not default_archive_dir()'s `~/beyond-video/archive/<id>` -
    Christer was explicit that a snap's save location should be kept
    separate from the recording archive, since a snap is a one-off
    grab, not part of a recording).

    Unlike bv-snap's own CLI, which takes a user-supplied --output
    path directly (Christer: "A dedicated --output path the user
    passes"), bv-web has no free-text arbitrary-path field anywhere
    in its forms (see WEB_ARCHITECTURE.md) - every web-triggered job
    works off camera-id-scoped defaults instead. This is that default
    for the web trigger; nested by id like default_archive_dir() so
    two cameras' snapshots never collide. Always created on demand by
    save_snapshots() itself, same as bv-snap's own --output."""

    return Path.home() / "beyond-video-data" / "snapshots" / id_


def _looks_like_path(value: str) -> bool:
    """True if `value` should be treated as a literal filesystem path
    rather than a possible camera system id - the disambiguation
    resolve_archive_path() uses, same escape-hatch shape `git` itself
    uses for `git checkout ./file` vs. a branch of the same name.

    A camera id is validated elsewhere (validate_id()) to be plain
    ASCII alphanumeric/underscore/hyphen with no path separators at
    all, so anything that looks path-shaped - starts with an explicit
    "./"/"../" (POSIX or Windows spelling), is exactly "." or "..", is
    an OS-absolute path (covers a Windows drive letter like "C:\\..."
    via os.path.isabs()), or contains any path separator at all (e.g.
    "some/dir" or "some\\dir" with no leading "./") - is never a valid
    id in the first place, so treating it as a literal path can't
    accidentally shadow a real camera.
    """

    if value in (".", ".."):
        return True

    if value.startswith(("./", "../", ".\\", "..\\")):
        return True

    if os.path.isabs(value):
        return True

    if os.sep in value or (os.altsep and os.altsep in value):
        return True

    return False


def resolve_archive_path(
    path_or_id: str, config_dir: Path
) -> tuple[Path, CameraConfig | None]:
    """Resolve a bv-* archive command's positional PATH argument -
    either a literal archive directory, or a camera system id (the
    same ids bv-config/bv-download/bv-gps/bv-live already take)
    resolved to that camera's own `archive` directory.

    Returns `(resolved_path, camera_config)` - `camera_config` is the
    loaded CameraConfig when `path_or_id` resolved to one (so a caller
    like bv-export can also read its `target` field without a second
    lookup), or None when `path_or_id` was used as a literal path.

    Resolution order: if `path_or_id` already looks like a path (see
    _looks_like_path()), it's used literally without ever touching
    `config_dir` - the explicit escape hatch for a real directory that
    happens to share a name with a configured camera (`./Kirby` or
    `.\\Kirby` instead of bare `Kirby`). Otherwise, a matching camera
    config is tried first; if none exists for that id, `path_or_id`
    falls back to being used as a literal path anyway - the same
    behavior every bv-* archive command already had before this
    existed, so a bare directory name that isn't also a configured
    camera id keeps working exactly as before.

    A camera config that exists but fails to load (CameraConfigError -
    corrupt TOML, missing required key) is not caught here; it
    propagates to the caller rather than silently falling back to a
    literal-path interpretation that would almost certainly also be
    wrong (the directory named after a broken camera config is not a
    sensible fallback archive to search).
    """

    if not _looks_like_path(path_or_id):
        cfg_path = config_path(config_dir, path_or_id)
        if cfg_path.exists():
            config = load_camera_config(cfg_path)
            return config.archive, config

    return Path(path_or_id), None


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

    Skips RESERVED_CAMERA_IDS (bv-web's own web-users.cfg accounts
    file shares this same directory in the unconfigured/native-install
    case, and is not a camera) - validate_id() blocks creating a new
    camera with a reserved id going forward, but this list still needs
    its own defensive skip for a config directory that already had one
    of these files on disk before that check existed.
    """

    if not config_dir.is_dir():
        return []

    return sorted(
        path.stem
        for path in config_dir.glob("*.cfg")
        if path.is_file() and path.stem not in RESERVED_CAMERA_IDS
    )


def validate_id(id_: str) -> None:
    """Validate a camera system id.

    An id is ASCII alphanumeric plus underscore/hyphen, at most 128
    characters. Underscore and hyphen were added specifically so a
    camera's archive can be split into per-year ids like "Kirby_2019"
    .. "Kirby_2026" (each with its own .cfg and an `archive` pointing
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

    if id_ in RESERVED_CAMERA_IDS:
        raise CameraConfigError(
            f"{id_!r} is reserved (it would collide with bv-web's own "
            f"{id_}.cfg accounts file) - pick a different id"
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


#: Default/legacy CameraConfig.adapter value - see the field's own
#: docstring. Every config written before the camera-adapter design
#: (docs/CAMERA_ADAPTERS.md) implicitly meant this; load_camera_config()
#: defaults a missing `adapter` key to it rather than raising, so no
#: existing config needs a manual migration step to keep loading.
DEFAULT_ADAPTER_ID = "blackvue"


@dataclass
class CameraConfig:
    """One camera system: identity, endpoints, the archive directory
    downloads are saved to, and an optional target (export) directory."""

    id: str
    name: str
    archive: Path
    target: Path | None = None
    endpoints: list[Endpoint] = field(default_factory=list)
    #: Which camera adapter (docs/CAMERA_ADAPTERS.md) this config's
    #: archive/endpoints should be read through - an adapter_id matching
    #: one of adapters/<id>/manifest.json. Defaults to "blackvue" for
    #: every config, existing or new: this project has only ever spoken
    #: BlackVue until this field existed, so that's the correct meaning
    #: for a config that predates it, not just a placeholder default.
    #: Read by adapters/registry.get_adapter() throughout bv-ls, bv-web,
    #: bv-export, bv-search, and bv-config's own wizard (which prompts
    #: for it directly - see cli/bv_config.py's run_wizard()).
    adapter: str = DEFAULT_ADAPTER_ID


def load_camera_config(path: Path) -> CameraConfig:
    """Load a camera config from a .cfg (TOML) file.

    Reads either the current key names (`archive`/`target`) or the
    pre-rename ones (`target`/`output` - `target` used to mean the
    download directory now called `archive`, and `output` used to
    mean the bv-export destination now called `target`) - a config
    written before this renaming keeps loading correctly, with no
    manual migration step. Disambiguated by which key is actually
    present: an `archive` key means the file already uses the current
    names (its own `target`, if any, is the new field); no `archive`
    key but a `target` key means the old names (that `target` is the
    archive dir, and `output`, if any, is the new target field).
    Writing always uses the current names (see save_camera_config()),
    so a config self-upgrades in place the next time it's saved (e.g.
    editing it with `bv-config` again).
    """

    try:
        with path.open("rb") as file:
            data = tomllib.load(file)
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise CameraConfigError(f"{path}: {exc}") from exc

    id_ = data.get("id", path.stem)
    name = data.get("name", id_)
    adapter = data.get("adapter", DEFAULT_ADAPTER_ID)

    if "archive" in data:
        archive_value = data["archive"]
        target_value = data.get("target")
    elif "target" in data:
        # Pre-rename config: its `target` was the download directory,
        # its `output` (if any) was the bv-export destination.
        archive_value = data["target"]
        target_value = data.get("output")
    else:
        raise CameraConfigError(f"{path}: missing required key 'archive'")

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
        archive=Path(archive_value),
        target=Path(target_value) if target_value else None,
        endpoints=endpoints,
        adapter=adapter,
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
        f"archive = {_toml_string(str(config.archive))}",
        f"adapter = {_toml_string(config.adapter)}",
    ]

    if config.target is not None:
        lines.append(f"target = {_toml_string(str(config.target))}")

    lines.append("")

    for endpoint in config.endpoints:
        lines.append("[[endpoint]]")
        lines.append(f"name = {_toml_string(endpoint.name)}")
        lines.append(f"address = {_toml_string(endpoint.address)}")
        lines.append("")

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines), encoding="utf-8")
