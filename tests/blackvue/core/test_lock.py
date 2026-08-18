"""
Tests for core/lock.py - the per-archive asset-generation lock
manifest bv-lock writes and bv-generate reads (see that module's own
docstring for the "never run bv-generate on 2019-2025 again, unless a
new asset shows up" rationale).

Copyright (C) 2026 Christer R. (sssreh)

SPDX-License-Identifier: GPL-3.0-or-later
"""

from __future__ import annotations

import json

import pytest

from blackvue.core.lock import LOCKABLE_ASSETS
from blackvue.core.lock import LockEntry
from blackvue.core.lock import LockError
from blackvue.core.lock import LockManifest
from blackvue.core.lock import add_lock_assets
from blackvue.core.lock import assets_fully_locked
from blackvue.core.lock import load_lock_manifest
from blackvue.core.lock import lock_manifest_path
from blackvue.core.lock import remove_lock_assets
from blackvue.core.lock import save_lock_manifest
from blackvue.lexicaltimeparser import LexicalTimeParser


def _interval(timestamp=None, from_=None, until=None):
    return LexicalTimeParser(
        timestamp=timestamp, from_=from_, until=until
    ).parse()


# ---------------------------------------------------------------------------
# lock_manifest_path / load_lock_manifest / save_lock_manifest
# ---------------------------------------------------------------------------


def test_lock_manifest_path_is_a_sibling_of_the_recordings(tmp_path):
    assert lock_manifest_path(tmp_path) == tmp_path / ".bv-lock.json"


def test_load_lock_manifest_returns_empty_when_no_file_exists(tmp_path):
    manifest = load_lock_manifest(tmp_path)

    assert manifest.entries == []


def test_save_then_load_round_trips_entries(tmp_path):
    manifest = add_lock_assets(
        LockManifest(), _interval(timestamp="2019"), ["get-duration", "transcribe"]
    )

    save_lock_manifest(tmp_path, manifest)
    reloaded = load_lock_manifest(tmp_path)

    assert len(reloaded.entries) == 1
    assert reloaded.entries[0].first == manifest.entries[0].first
    assert reloaded.entries[0].last == manifest.entries[0].last
    assert reloaded.entries[0].assets == {"get-duration", "transcribe"}


def test_save_lock_manifest_creates_the_archive_directory_if_missing(tmp_path):
    archive = tmp_path / "not-yet-created"
    manifest = add_lock_assets(
        LockManifest(), _interval(timestamp="2020"), ["extract-audio"]
    )

    save_lock_manifest(archive, manifest)

    assert archive.exists()
    assert (archive / ".bv-lock.json").exists()


def test_save_lock_manifest_writes_readable_json(tmp_path):
    manifest = add_lock_assets(
        LockManifest(), _interval(timestamp="2019"), ["describe-scene"]
    )

    save_lock_manifest(tmp_path, manifest)

    data = json.loads((tmp_path / ".bv-lock.json").read_text())
    assert data["entries"][0]["assets"] == ["describe-scene"]


def test_load_lock_manifest_raises_lock_error_for_invalid_json(tmp_path):
    (tmp_path / ".bv-lock.json").write_text("not json{{{")

    with pytest.raises(LockError):
        load_lock_manifest(tmp_path)


def test_load_lock_manifest_raises_lock_error_for_missing_fields(tmp_path):
    (tmp_path / ".bv-lock.json").write_text(
        json.dumps({"entries": [{"first": "20190000_000000"}]})
    )

    with pytest.raises(LockError):
        load_lock_manifest(tmp_path)


# ---------------------------------------------------------------------------
# add_lock_assets
# ---------------------------------------------------------------------------


def test_add_lock_assets_creates_a_new_entry(tmp_path):
    manifest = add_lock_assets(
        LockManifest(), _interval(timestamp="2019"), ["get-duration"]
    )

    assert len(manifest.entries) == 1
    assert manifest.entries[0].assets == {"get-duration"}


def test_add_lock_assets_merges_into_an_existing_entry_for_the_same_range():
    interval = _interval(timestamp="2019")
    manifest = add_lock_assets(LockManifest(), interval, ["get-duration"])
    manifest = add_lock_assets(manifest, interval, ["transcribe"])

    assert len(manifest.entries) == 1
    assert manifest.entries[0].assets == {"get-duration", "transcribe"}


def test_add_lock_assets_does_not_merge_a_different_range():
    manifest = add_lock_assets(
        LockManifest(), _interval(timestamp="2019"), ["get-duration"]
    )
    manifest = add_lock_assets(
        manifest, _interval(timestamp="2020"), ["get-duration"]
    )

    assert len(manifest.entries) == 2


def test_add_lock_assets_refreshes_locked_at_on_merge():
    interval = _interval(timestamp="2019")
    manifest = add_lock_assets(
        LockManifest(), interval, ["get-duration"], locked_at="2026-01-01T00:00:00+00:00"
    )
    manifest = add_lock_assets(
        manifest, interval, ["transcribe"], locked_at="2026-02-01T00:00:00+00:00"
    )

    assert manifest.entries[0].locked_at == "2026-02-01T00:00:00+00:00"


def test_add_lock_assets_rejects_an_unknown_asset_name():
    with pytest.raises(LockError):
        add_lock_assets(LockManifest(), _interval(timestamp="2019"), ["bogus"])


def test_lockable_assets_matches_bv_generates_own_action_flags():
    # Not exhaustive proof of parity with bv_generate.py's parser, but
    # a floor: every name this project's docs/examples actually use.
    assert LOCKABLE_ASSETS == frozenset(
        {
            "extract-audio",
            "get-duration",
            "thumbnail",
            "transcribe",
            "translate",
            "srt",
            "describe-scene",
            "diarize",
        }
    )


# ---------------------------------------------------------------------------
# remove_lock_assets
# ---------------------------------------------------------------------------


def test_remove_lock_assets_drops_only_the_named_assets():
    interval = _interval(timestamp="2019")
    manifest = add_lock_assets(
        LockManifest(), interval, ["get-duration", "transcribe", "describe-scene"]
    )

    manifest = remove_lock_assets(manifest, interval, ["describe-scene"])

    assert manifest.entries[0].assets == {"get-duration", "transcribe"}


def test_remove_lock_assets_drops_the_whole_entry_once_empty():
    interval = _interval(timestamp="2019")
    manifest = add_lock_assets(LockManifest(), interval, ["get-duration"])

    manifest = remove_lock_assets(manifest, interval, ["get-duration"])

    assert manifest.entries == []


def test_remove_lock_assets_on_a_never_locked_range_is_a_no_op():
    interval_2019 = _interval(timestamp="2019")
    interval_2020 = _interval(timestamp="2020")
    manifest = add_lock_assets(LockManifest(), interval_2019, ["get-duration"])

    result = remove_lock_assets(manifest, interval_2020, ["get-duration"])

    assert result.entries == manifest.entries


def test_remove_lock_assets_rejects_an_unknown_asset_name():
    interval = _interval(timestamp="2019")
    manifest = add_lock_assets(LockManifest(), interval, ["get-duration"])

    with pytest.raises(LockError):
        remove_lock_assets(manifest, interval, ["bogus"])


# ---------------------------------------------------------------------------
# assets_fully_locked
# ---------------------------------------------------------------------------


def test_assets_fully_locked_true_for_an_exact_match():
    interval = _interval(timestamp="2019")
    manifest = add_lock_assets(
        LockManifest(), interval, ["get-duration", "transcribe"]
    )

    entry = assets_fully_locked(manifest, interval, {"get-duration"})

    assert entry is not None
    assert entry.assets == {"get-duration", "transcribe"}


def test_assets_fully_locked_true_for_a_sub_range_within_a_locked_year():
    year_interval = _interval(timestamp="2019")
    manifest = add_lock_assets(LockManifest(), year_interval, ["get-duration"])

    day_interval = _interval(timestamp="20190715")

    assert assets_fully_locked(manifest, day_interval, {"get-duration"}) is not None


def test_assets_fully_locked_false_for_a_range_crossing_a_lock_boundary():
    manifest = add_lock_assets(
        LockManifest(), _interval(timestamp="2019"), ["get-duration"]
    )

    crossing = _interval(from_="20191225", until="20200105")

    assert assets_fully_locked(manifest, crossing, {"get-duration"}) is None


def test_assets_fully_locked_false_when_one_requested_asset_is_missing():
    manifest = add_lock_assets(
        LockManifest(), _interval(timestamp="2019"), ["get-duration"]
    )

    assert (
        assets_fully_locked(
            manifest, _interval(timestamp="2019"), {"get-duration", "transcribe"}
        )
        is None
    )


def test_assets_fully_locked_false_for_an_unrelated_year():
    manifest = add_lock_assets(
        LockManifest(), _interval(timestamp="2019"), ["get-duration"]
    )

    assert (
        assets_fully_locked(manifest, _interval(timestamp="2020"), {"get-duration"})
        is None
    )


def test_assets_fully_locked_false_for_an_empty_asset_request():
    manifest = add_lock_assets(
        LockManifest(), _interval(timestamp="2019"), ["get-duration"]
    )

    assert assets_fully_locked(manifest, _interval(timestamp="2019"), set()) is None


def test_assets_fully_locked_false_on_an_empty_manifest():
    assert (
        assets_fully_locked(LockManifest(), _interval(timestamp="2019"), {"get-duration"})
        is None
    )
