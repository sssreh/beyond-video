"""
Tests for blackvue/snap.py - save_snapshots(), the shared "save
captured JPEG bytes to disk" half of bv-snap/bv-gps --snap (see
snap.py's own module docstring; the HTTP capture itself lives on
BlackVueClient.snapshot() and is tested in
tests/blackvue/core/test_blackvue_client.py).

Copyright (C) 2026 Christer R. (sssreh)

SPDX-License-Identifier: GPL-3.0-or-later
"""

from __future__ import annotations

from blackvue.snap import save_snapshots


def test_save_snapshots_writes_one_file_per_direction(tmp_path):
    snapshots = {"F": b"front-bytes", "R": b"rear-bytes", "I": b"interior-bytes"}

    paths = save_snapshots(snapshots, tmp_path, timestamp="20260821_180512")

    assert set(paths.keys()) == {"F", "R", "I"}
    assert paths["F"] == tmp_path / "snap_20260821_180512_F.jpg"
    assert paths["F"].read_bytes() == b"front-bytes"
    assert paths["R"].read_bytes() == b"rear-bytes"
    assert paths["I"].read_bytes() == b"interior-bytes"


def test_save_snapshots_creates_the_output_dir_if_missing(tmp_path):
    output_dir = tmp_path / "does" / "not" / "exist" / "yet"

    paths = save_snapshots({"F": b"data"}, output_dir, timestamp="20260821_180512")

    assert output_dir.is_dir()
    assert paths["F"].read_bytes() == b"data"


def test_save_snapshots_uses_one_shared_timestamp_across_directions(tmp_path):
    paths = save_snapshots(
        {"F": b"a", "R": b"b"}, tmp_path, timestamp="20260821_180512"
    )

    assert paths["F"].name == "snap_20260821_180512_F.jpg"
    assert paths["R"].name == "snap_20260821_180512_R.jpg"


def test_save_snapshots_generates_a_timestamp_when_not_given(tmp_path):
    paths = save_snapshots({"F": b"data"}, tmp_path)

    # Not asserting an exact value (would race the real clock) - just
    # that a real-looking YYYYMMDD_HHMMSS-shaped name was produced.
    name = paths["F"].name
    assert name.startswith("snap_")
    assert name.endswith("_F.jpg")
    timestamp_part = name[len("snap_"):-len("_F.jpg")]
    assert len(timestamp_part) == len("20260821_180512")


def test_save_snapshots_returns_paths_in_the_input_dict_order(tmp_path):
    snapshots = {"R": b"rear", "F": b"front"}

    paths = save_snapshots(snapshots, tmp_path, timestamp="20260821_180512")

    assert list(paths.keys()) == ["R", "F"]


def test_save_snapshots_handles_an_empty_snapshots_dict(tmp_path):
    paths = save_snapshots({}, tmp_path, timestamp="20260821_180512")

    assert paths == {}
