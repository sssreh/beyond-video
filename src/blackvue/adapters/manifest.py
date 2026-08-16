"""
Camera adapter manifest loading.

This module is the concrete, code-level counterpart to
manifest.schema.json: it loads a manifest.json file into a typed
AdapterManifest and runs a small set of structural checks (required
keys present, right types, ordering/uniqueness invariants) so the
JSON files under adapters/<adapter_id>/manifest.json can actually be
validated rather than just eyeballed.

STATUS: design/schema-only pass (see docs/CAMERA_ADAPTERS.md).
Nothing else in the codebase imports this yet - CameraConfig has no
`adapter` field, no adapter registry exists, and no BlackVueAdapter /
FolderAdapter class implementing the code hooks named in
`code_hooks_required` has been written. This module exists so the two
shipped manifests (adapters/blackvue/manifest.json,
adapters/folder/manifest.json) are provably well-formed today, ahead
of that follow-up work - it is not wired into ArchiveReader, bv-ls,
or anything else that currently runs.

Deliberately dependency-free: no `jsonschema` package is added just
for this. manifest.schema.json is the formal schema (useful for
editor tooling, or a future jsonschema-based check if that dependency
is ever justified elsewhere); the checks below are a hand-rolled
equivalent, sufficient for this pass's purpose of proving the two
example manifests parse and shape-check correctly.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any


class ManifestError(ValueError):
    """A manifest.json file is missing a required field or has a
    value of the wrong shape."""


@dataclass(frozen=True)
class KindEntry:
    code: str
    label: str
    low_signal: bool
    default_included: bool


@dataclass(frozen=True)
class DirectionEntry:
    code: str
    label: str
    primary: bool


@dataclass(frozen=True)
class AssetSuffixEntry:
    suffix: str
    asset: str
    direction: str | None = None
    kind: str | None = None


_REQUIRED_CAPABILITIES = (
    "network_connect",
    "download",
    "live_view",
    "live_telemetry",
    "gps",
    "gsensor",
    "thumbnails",
    "config_snapshot",
    "multi_direction_video",
)


@dataclass(frozen=True)
class AdapterManifest:
    """A loaded, validated adapter manifest.

    Declarative-only: everything here is data. The behaviors an
    actual adapter class must still implement in code (NMEA/binary
    sidecar parsing, camera network protocol, config-snapshot
    parsing, ...) are named in `code_hooks_required` but not
    implemented here - see docs/CAMERA_ADAPTERS.md for the split
    between what this file can express and what can't.
    """

    adapter_id: str
    schema_version: int
    display_name: str
    source_kind: str
    requires_network: bool
    archive_layout: str
    video_extensions: tuple[str, ...]
    kind_vocabulary: tuple[KindEntry, ...]
    direction_vocabulary: tuple[DirectionEntry, ...]
    asset_suffix_table: tuple[AssetSuffixEntry, ...]
    capabilities: dict[str, Any]
    code_hooks_required: tuple[str, ...]
    default_trip_gap_seconds: float
    description: str = ""
    filename_pattern: dict[str, Any] | None = None
    timestamp_source: tuple[str, ...] = ()
    grouping_hint: str = "none"
    unsupported_notes: tuple[str, ...] = ()

    def supports(self, capability: str) -> bool:
        """True only for an explicit `true` in capabilities[capability].

        A partial value like "generated" (see `thumbnails`) returns
        False here on purpose - callers that need to distinguish
        "fully native" from "synthesized on demand" should read
        `.capabilities[capability]` directly instead of calling this.
        """
        return self.capabilities.get(capability) is True

    @property
    def primary_direction(self) -> DirectionEntry:
        """The single direction_vocabulary entry marked primary=true.

        load_manifest() already guarantees exactly one exists, so
        this never raises for a manifest that loaded successfully.
        """
        return next(d for d in self.direction_vocabulary if d.primary)


def _require(data: dict, key: str, manifest_path: Path) -> Any:
    if key not in data:
        raise ManifestError(f"{manifest_path}: missing required field '{key}'")
    return data[key]


def _require_type(value: Any, expected: type | tuple[type, ...], key: str, manifest_path: Path) -> Any:
    # bool is a subclass of int in Python - guard against an int like
    # 1 silently passing a `bool` type check.
    if expected in (bool, (bool,)) and not isinstance(value, bool):
        raise ManifestError(f"{manifest_path}: field '{key}' must be a bool, got {type(value).__name__}")
    if not isinstance(value, expected):
        raise ManifestError(f"{manifest_path}: field '{key}' has type {type(value).__name__}, expected {expected}")
    return value


def load_manifest(path: Path) -> AdapterManifest:
    """Load and structurally validate a manifest.json file.

    Raises ManifestError on any missing/malformed/inconsistent field.
    """
    path = Path(path)
    data = json.loads(path.read_text(encoding="utf-8"))

    adapter_id = _require_type(_require(data, "adapter_id", path), str, "adapter_id", path)
    schema_version = _require_type(_require(data, "schema_version", path), int, "schema_version", path)
    if schema_version != 1:
        raise ManifestError(f"{path}: unsupported schema_version {schema_version}")
    display_name = _require_type(_require(data, "display_name", path), str, "display_name", path)

    source = _require_type(_require(data, "source", path), dict, "source", path)
    source_kind = _require_type(_require(source, "kind", path), str, "source.kind", path)
    if source_kind not in ("network_camera", "local_folder"):
        raise ManifestError(f"{path}: source.kind must be 'network_camera' or 'local_folder', got {source_kind!r}")
    requires_network = _require_type(_require(source, "requires_network", path), bool, "source.requires_network", path)

    archive_layout = _require_type(_require(data, "archive_layout", path), str, "archive_layout", path)
    if archive_layout not in ("flat", "recursive"):
        raise ManifestError(f"{path}: archive_layout must be 'flat' or 'recursive', got {archive_layout!r}")

    video_extensions = tuple(_require_type(_require(data, "video_extensions", path), list, "video_extensions", path))
    if not video_extensions:
        raise ManifestError(f"{path}: video_extensions must not be empty")

    kind_vocabulary = tuple(
        KindEntry(
            code=_require_type(_require(entry, "code", path), str, "kind_vocabulary[].code", path),
            label=_require_type(_require(entry, "label", path), str, "kind_vocabulary[].label", path),
            low_signal=_require_type(_require(entry, "low_signal", path), bool, "kind_vocabulary[].low_signal", path),
            default_included=_require_type(
                _require(entry, "default_included", path), bool, "kind_vocabulary[].default_included", path
            ),
        )
        for entry in _require_type(_require(data, "kind_vocabulary", path), list, "kind_vocabulary", path)
    )
    if not kind_vocabulary:
        raise ManifestError(f"{path}: kind_vocabulary must not be empty")

    direction_vocabulary = tuple(
        DirectionEntry(
            code=_require_type(_require(entry, "code", path), str, "direction_vocabulary[].code", path),
            label=_require_type(_require(entry, "label", path), str, "direction_vocabulary[].label", path),
            primary=_require_type(_require(entry, "primary", path), bool, "direction_vocabulary[].primary", path),
        )
        for entry in _require_type(_require(data, "direction_vocabulary", path), list, "direction_vocabulary", path)
    )
    if not direction_vocabulary:
        raise ManifestError(f"{path}: direction_vocabulary must not be empty")
    primary_count = sum(1 for d in direction_vocabulary if d.primary)
    if primary_count != 1:
        raise ManifestError(
            f"{path}: direction_vocabulary must have exactly one entry with primary=true, found {primary_count}"
        )

    asset_suffix_table = tuple(
        AssetSuffixEntry(
            suffix=_require_type(_require(entry, "suffix", path), str, "asset_suffix_table[].suffix", path),
            asset=_require_type(_require(entry, "asset", path), str, "asset_suffix_table[].asset", path),
            direction=entry.get("direction"),
            kind=entry.get("kind"),
        )
        for entry in _require_type(_require(data, "asset_suffix_table", path), list, "asset_suffix_table", path)
    )

    capabilities = _require_type(_require(data, "capabilities", path), dict, "capabilities", path)
    missing_caps = [c for c in _REQUIRED_CAPABILITIES if c not in capabilities]
    if missing_caps:
        raise ManifestError(f"{path}: capabilities missing keys {missing_caps}")

    code_hooks_required = tuple(
        _require_type(_require(data, "code_hooks_required", path), list, "code_hooks_required", path)
    )

    default_trip_gap_seconds = _require_type(
        _require(data, "default_trip_gap_seconds", path), (int, float), "default_trip_gap_seconds", path
    )
    if default_trip_gap_seconds <= 0:
        raise ManifestError(f"{path}: default_trip_gap_seconds must be > 0")

    return AdapterManifest(
        adapter_id=adapter_id,
        schema_version=schema_version,
        display_name=display_name,
        source_kind=source_kind,
        requires_network=requires_network,
        archive_layout=archive_layout,
        video_extensions=video_extensions,
        kind_vocabulary=kind_vocabulary,
        direction_vocabulary=direction_vocabulary,
        asset_suffix_table=asset_suffix_table,
        capabilities=dict(capabilities),
        code_hooks_required=code_hooks_required,
        default_trip_gap_seconds=float(default_trip_gap_seconds),
        description=data.get("description", ""),
        filename_pattern=data.get("filename_pattern"),
        timestamp_source=tuple(data.get("timestamp_source", ())),
        grouping_hint=data.get("grouping_hint", "none"),
        unsupported_notes=tuple(data.get("unsupported_notes", ())),
    )
