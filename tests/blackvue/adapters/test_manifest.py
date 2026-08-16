"""
Tests for adapters/manifest.py - the loader/validator for the camera
adapter manifest.json format (see docs/CAMERA_ADAPTERS.md). No pytest
was available in the sandbox this was originally written in, so these
were first proven correct via a standalone script (see WORKING_CONTEXT.md's
"Design: camera adapter model" entry); this file gives the same coverage
as real, CI-collected tests.
"""

import json
from pathlib import Path

import pytest

from blackvue.adapters.manifest import AdapterManifest
from blackvue.adapters.manifest import load_manifest
from blackvue.adapters.manifest import ManifestError

_ADAPTERS_DIR = (
    Path(__file__).resolve().parents[3] / "src" / "blackvue" / "adapters"
)


# ---------------------------------------------------------------------------
# The two real, shipped manifests - both must load and validate cleanly.
# ---------------------------------------------------------------------------


def test_blackvue_manifest_loads():
    manifest = load_manifest(_ADAPTERS_DIR / "blackvue" / "manifest.json")

    assert manifest.adapter_id == "blackvue"
    assert manifest.archive_layout == "flat"
    assert manifest.primary_direction.code == "F"
    assert manifest.supports("gps")
    assert manifest.supports("network_connect")


def test_folder_manifest_loads():
    manifest = load_manifest(_ADAPTERS_DIR / "folder" / "manifest.json")

    assert manifest.adapter_id == "folder"
    assert manifest.archive_layout == "recursive"
    assert manifest.primary_direction.code == "V"
    assert not manifest.supports("gps")
    assert not manifest.supports("network_connect")
    assert manifest.capabilities["thumbnails"] == "generated"


def test_gopro_manifest_loads():
    manifest = load_manifest(_ADAPTERS_DIR / "gopro" / "manifest.json")

    assert manifest.adapter_id == "gopro"
    assert manifest.archive_layout == "recursive"
    assert manifest.primary_direction.code == "V"
    assert manifest.supports("gps")
    assert manifest.supports("gsensor")
    assert not manifest.supports("network_connect")
    assert manifest.capabilities["thumbnails"] == "generated"
    assert manifest.gps_source_asset == "FRONT"
    assert manifest.gsensor_source_asset == "FRONT"


def test_blackvue_manifest_asset_suffix_table_matches_archive_reader():
    # The manifest's own docstring/docs/CAMERA_ADAPTERS.md claim this is
    # a byte-for-byte transcription of the real ArchiveReader.ASSETS
    # tuple - order included (suffix-overlap resolution depends on it).
    from blackvue.archive.archive_reader import ArchiveReader

    manifest = load_manifest(_ADAPTERS_DIR / "blackvue" / "manifest.json")

    real = [(suffix, asset.name) for suffix, asset in ArchiveReader.ASSETS]
    from_manifest = [(e.suffix, e.asset) for e in manifest.asset_suffix_table]

    assert from_manifest == real


# ---------------------------------------------------------------------------
# supports() / primary_direction
# ---------------------------------------------------------------------------


def test_supports_is_false_for_partial_capability_value(tmp_path):
    manifest = load_manifest(_ADAPTERS_DIR / "folder" / "manifest.json")

    # thumbnails is "generated", not True - supports() only recognizes
    # an explicit True, by design (see AdapterManifest.supports()'s own
    # docstring).
    assert manifest.capabilities["thumbnails"] == "generated"
    assert not manifest.supports("thumbnails")


def test_primary_direction_returns_the_flagged_entry():
    manifest = load_manifest(_ADAPTERS_DIR / "blackvue" / "manifest.json")

    assert manifest.primary_direction.label == "Front"


# ---------------------------------------------------------------------------
# Validation failures - built from a minimal valid manifest, mutated one
# field at a time.
# ---------------------------------------------------------------------------


def _minimal_manifest() -> dict:
    return {
        "adapter_id": "test",
        "schema_version": 1,
        "display_name": "Test",
        "source": {"kind": "local_folder", "requires_network": False},
        "archive_layout": "flat",
        "video_extensions": [".mp4"],
        "kind_vocabulary": [
            {"code": "V", "label": "Video", "low_signal": False, "default_included": True}
        ],
        "direction_vocabulary": [
            {"code": "V", "label": "Video", "primary": True}
        ],
        "asset_suffix_table": [],
        "capabilities": {
            "network_connect": False,
            "download": False,
            "live_view": False,
            "live_telemetry": False,
            "gps": False,
            "gsensor": False,
            "thumbnails": False,
            "config_snapshot": False,
            "multi_direction_video": False,
        },
        "code_hooks_required": [],
        "default_trip_gap_seconds": 300,
    }


def _write(tmp_path: Path, data: dict) -> Path:
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


def test_minimal_manifest_loads(tmp_path):
    # Sanity check on the fixture itself before testing mutations of it.
    manifest = load_manifest(_write(tmp_path, _minimal_manifest()))
    assert manifest.adapter_id == "test"


def test_missing_required_field_raises(tmp_path):
    data = _minimal_manifest()
    del data["capabilities"]

    with pytest.raises(ManifestError):
        load_manifest(_write(tmp_path, data))


def test_unsupported_schema_version_raises(tmp_path):
    data = _minimal_manifest()
    data["schema_version"] = 2

    with pytest.raises(ManifestError):
        load_manifest(_write(tmp_path, data))


def test_bad_source_kind_raises(tmp_path):
    data = _minimal_manifest()
    data["source"]["kind"] = "spaceship"

    with pytest.raises(ManifestError):
        load_manifest(_write(tmp_path, data))


def test_bad_archive_layout_raises(tmp_path):
    data = _minimal_manifest()
    data["archive_layout"] = "diagonal"

    with pytest.raises(ManifestError):
        load_manifest(_write(tmp_path, data))


def test_empty_kind_vocabulary_raises(tmp_path):
    data = _minimal_manifest()
    data["kind_vocabulary"] = []

    with pytest.raises(ManifestError):
        load_manifest(_write(tmp_path, data))


def test_no_primary_direction_raises(tmp_path):
    data = _minimal_manifest()
    data["direction_vocabulary"] = [
        {"code": "F", "label": "Front", "primary": False},
        {"code": "R", "label": "Rear", "primary": False},
    ]

    with pytest.raises(ManifestError, match="exactly one"):
        load_manifest(_write(tmp_path, data))


def test_two_primary_directions_raises(tmp_path):
    data = _minimal_manifest()
    data["direction_vocabulary"] = [
        {"code": "F", "label": "Front", "primary": True},
        {"code": "R", "label": "Rear", "primary": True},
    ]

    with pytest.raises(ManifestError, match="exactly one"):
        load_manifest(_write(tmp_path, data))


def test_missing_capability_key_raises(tmp_path):
    data = _minimal_manifest()
    del data["capabilities"]["gps"]

    with pytest.raises(ManifestError):
        load_manifest(_write(tmp_path, data))


def test_non_positive_default_trip_gap_raises(tmp_path):
    data = _minimal_manifest()
    data["default_trip_gap_seconds"] = 0

    with pytest.raises(ManifestError):
        load_manifest(_write(tmp_path, data))


def test_optional_fields_default_sensibly(tmp_path):
    manifest = load_manifest(_write(tmp_path, _minimal_manifest()))

    assert manifest.description == ""
    assert manifest.filename_pattern is None
    assert manifest.timestamp_source == ()
    assert manifest.grouping_hint == "none"
    assert manifest.unsupported_notes == ()
