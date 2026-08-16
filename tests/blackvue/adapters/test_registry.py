"""
Tests for adapters/registry.py.
"""

from pathlib import Path

import pytest

from blackvue.adapters import registry
from blackvue.adapters.blackvue.adapter import BlackVueAdapter
from blackvue.adapters.folder.adapter import FolderAdapter
from blackvue.adapters.gopro.adapter import GoProAdapter
from blackvue.adapters.manifest import load_manifest


@pytest.fixture(autouse=True)
def _restore_registry():
    # register("blackvue", ...) runs at import time (bottom of
    # registry.py) - tests that register a fake adapter must not leak
    # into other tests, so snapshot/restore the real dict around every
    # test in this file.
    original = dict(registry._REGISTRY)
    yield
    registry._REGISTRY.clear()
    registry._REGISTRY.update(original)


def test_blackvue_is_registered_by_default():
    assert "blackvue" in registry.registered_adapter_ids()


def test_folder_is_registered_by_default():
    assert "folder" in registry.registered_adapter_ids()


def test_get_adapter_returns_a_blackvue_adapter_instance():
    adapter = registry.get_adapter("blackvue")

    assert isinstance(adapter, BlackVueAdapter)


def test_get_adapter_returns_a_folder_adapter_instance():
    adapter = registry.get_adapter("folder")

    assert isinstance(adapter, FolderAdapter)


def test_gopro_is_registered_by_default():
    assert "gopro" in registry.registered_adapter_ids()


def test_get_adapter_returns_a_gopro_adapter_instance():
    adapter = registry.get_adapter("gopro")

    assert isinstance(adapter, GoProAdapter)


def test_get_adapter_unknown_id_raises_adapter_not_found_error():
    with pytest.raises(registry.AdapterNotFoundError):
        registry.get_adapter("does-not-exist")


def test_get_adapter_error_message_lists_whats_registered():
    with pytest.raises(registry.AdapterNotFoundError, match="blackvue"):
        registry.get_adapter("does-not-exist")


def test_manifest_path_does_not_require_registration():
    # A bare path lookup - useful for tooling that wants to inspect a
    # manifest.json before its adapter code exists (see this function's
    # own docstring).
    path = registry.manifest_path("folder")

    assert path.name == "manifest.json"
    assert path.parent.name == "folder"
    assert path.exists()


def test_load_adapter_manifest_matches_direct_load():
    via_registry = registry.load_adapter_manifest("blackvue")
    direct = load_manifest(registry.manifest_path("blackvue"))

    assert via_registry == direct


def test_load_adapter_manifest_unregistered_id_raises_before_touching_disk():
    with pytest.raises(registry.AdapterNotFoundError):
        registry.load_adapter_manifest("nonexistent-adapter-id")


def test_register_adds_a_new_adapter_id():
    class _FakeAdapter:
        manifest = None

    registry.register("fake", _FakeAdapter)

    assert "fake" in registry.registered_adapter_ids()
    assert isinstance(registry.get_adapter("fake"), _FakeAdapter)


def test_register_can_override_an_existing_id():
    class _ReplacementBlackVueAdapter:
        manifest = None

    registry.register("blackvue", _ReplacementBlackVueAdapter)

    assert isinstance(registry.get_adapter("blackvue"), _ReplacementBlackVueAdapter)
