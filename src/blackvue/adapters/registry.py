"""
Camera adapter registry.

Maps an adapter_id (CameraConfig.adapter - see core/camera_config.py, and
docs/CAMERA_ADAPTERS.md) to the CameraAdapter class that implements it.
Deliberately a plain, explicitly-populated dict rather than any kind of
import-time self-registration/plugin-discovery magic - see this module's
own registration block at the bottom. get_adapter() always returns a
single instance for the one adapter_id asked for; nothing here ever
iterates "every adapter" for a given camera - see docs/CAMERA_ADAPTERS.md's
"exactly one active adapter at a time" section for why that's a
deliberate design choice, not an oversight.

STATUS: "blackvue" and "folder" are both registered. bv-ls and bv-web's
archive browser call get_adapter() (see cli/bv_ls.py and
web/archive_browser.py) - see docs/CAMERA_ADAPTERS.md for what's still
queued (bv-download SD-card import, more adapter variants, bv-analyze).
"""

from __future__ import annotations

from pathlib import Path

from .base import CameraAdapter
from .blackvue.adapter import BlackVueAdapter
from .folder.adapter import FolderAdapter
from .manifest import AdapterManifest
from .manifest import load_manifest

_ADAPTERS_DIR = Path(__file__).parent


class AdapterNotFoundError(KeyError):
    """Raised by get_adapter()/load_adapter_manifest() when no adapter
    is registered under the requested adapter_id."""


_REGISTRY: dict[str, type[CameraAdapter]] = {}


def register(adapter_id: str, adapter_class: type[CameraAdapter]) -> None:
    """Register `adapter_class` under `adapter_id`.

    Exists mainly so tests can register a fake adapter without touching
    the real registry populated at the bottom of this module - real
    adapters are expected to register themselves right here, not via a
    call from application code.
    """

    _REGISTRY[adapter_id] = adapter_class


def registered_adapter_ids() -> list[str]:
    """Return every registered adapter_id, sorted."""

    return sorted(_REGISTRY)


def get_adapter(adapter_id: str) -> CameraAdapter:
    """Instantiate and return the adapter registered under `adapter_id`.

    Raises AdapterNotFoundError - not a bare KeyError - so a caller can
    give a helpful message (e.g. "camera Kirby's config names adapter
    'gopro', but no such adapter is registered yet") rather than a raw
    traceback.
    """

    try:
        adapter_class = _REGISTRY[adapter_id]
    except KeyError:
        raise AdapterNotFoundError(
            f"no adapter registered for {adapter_id!r} "
            f"(registered: {registered_adapter_ids()})"
        ) from None

    return adapter_class()


def manifest_path(adapter_id: str) -> Path:
    """Return the manifest.json path for `adapter_id`, registered or
    not - a bare path lookup, no registry check, useful for tooling
    (e.g. a future bv-analyze) that wants to inspect a manifest without
    needing the adapter's code hooks to be implemented yet."""

    return _ADAPTERS_DIR / adapter_id / "manifest.json"


def load_adapter_manifest(adapter_id: str) -> AdapterManifest:
    """Load and validate `adapter_id`'s manifest.json.

    Raises AdapterNotFoundError if `adapter_id` isn't registered
    (checked before touching the filesystem, so this gives the same
    clear error as get_adapter() rather than a raw FileNotFoundError
    for a typo'd id); ManifestError (see manifest.py) if the file exists
    but doesn't parse/validate.
    """

    if adapter_id not in _REGISTRY:
        raise AdapterNotFoundError(
            f"no adapter registered for {adapter_id!r} "
            f"(registered: {registered_adapter_ids()})"
        )

    return load_manifest(manifest_path(adapter_id))


# ---------------------------------------------------------------------------
# Real adapters register themselves here, one line each.
# ---------------------------------------------------------------------------

register("blackvue", BlackVueAdapter)
register("folder", FolderAdapter)
