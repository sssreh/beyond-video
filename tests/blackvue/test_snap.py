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

import subprocess

from blackvue.snap import open_with_default_app
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


def test_save_snapshots_includes_the_label_in_the_filename(tmp_path):
    paths = save_snapshots(
        {"F": b"data"}, tmp_path, timestamp="20260821_180512", label="Kirby"
    )

    assert paths["F"].name == "snap_Kirby_20260821_180512_F.jpg"


def test_save_snapshots_omits_the_label_segment_when_none_given(tmp_path):
    paths = save_snapshots({"F": b"data"}, tmp_path, timestamp="20260821_180512")

    assert paths["F"].name == "snap_20260821_180512_F.jpg"


def test_save_snapshots_sanitizes_an_unsafe_label(tmp_path):
    # A bare --host can include ":PORT" - not filename-safe on Windows.
    paths = save_snapshots(
        {"F": b"data"},
        tmp_path,
        timestamp="20260821_180512",
        label="192.168.1.42:8080",
    )

    assert paths["F"].name == "snap_192.168.1.42_8080_20260821_180512_F.jpg"


def test_save_snapshots_treats_an_empty_label_the_same_as_none(tmp_path):
    paths = save_snapshots(
        {"F": b"data"}, tmp_path, timestamp="20260821_180512", label=""
    )

    assert paths["F"].name == "snap_20260821_180512_F.jpg"


# ---------------------------------------------------------------------------
# open_with_default_app() - the "let me actually see the picture" half of
# `bv-snap --open` (Christer: bv-snap is "almost useless" without this,
# since it otherwise only writes files and prints their paths).
# ---------------------------------------------------------------------------


def test_open_with_default_app_uses_startfile_on_windows(monkeypatch, tmp_path):
    path = tmp_path / "snap_F.jpg"
    path.write_bytes(b"data")

    calls = []
    monkeypatch.setattr("blackvue.snap.sys.platform", "win32")
    # os.startfile only exists on real Windows - add it as an attribute
    # so this test can run (and be meaningfully asserted) on any OS.
    monkeypatch.setattr(
        "blackvue.snap.os.startfile", lambda p: calls.append(p), raising=False
    )

    result = open_with_default_app(path)

    assert result is True
    assert calls == [str(path)]


def test_open_with_default_app_uses_open_on_macos(monkeypatch, tmp_path):
    path = tmp_path / "snap_F.jpg"
    path.write_bytes(b"data")

    calls = []
    monkeypatch.setattr("blackvue.snap.sys.platform", "darwin")
    monkeypatch.setattr(
        "blackvue.snap.subprocess.run",
        lambda args, **kwargs: calls.append(args),
    )

    result = open_with_default_app(path)

    assert result is True
    assert calls == [["open", str(path)]]


def test_open_with_default_app_uses_xdg_open_on_linux(monkeypatch, tmp_path):
    path = tmp_path / "snap_F.jpg"
    path.write_bytes(b"data")

    calls = []
    monkeypatch.setattr("blackvue.snap.sys.platform", "linux")
    monkeypatch.setattr(
        "blackvue.snap.subprocess.run",
        lambda args, **kwargs: calls.append(args),
    )

    result = open_with_default_app(path)

    assert result is True
    assert calls == [["xdg-open", str(path)]]


def test_open_with_default_app_returns_false_on_missing_launcher(
    monkeypatch, tmp_path
):
    # e.g. xdg-open not installed on a headless NAS - shouldn't raise,
    # just report failure so the caller can warn and move on.
    path = tmp_path / "snap_F.jpg"
    path.write_bytes(b"data")

    monkeypatch.setattr("blackvue.snap.sys.platform", "linux")

    def _raise(*args, **kwargs):
        raise FileNotFoundError("xdg-open not found")

    monkeypatch.setattr("blackvue.snap.subprocess.run", _raise)

    assert open_with_default_app(path) is False


def test_open_with_default_app_returns_false_on_nonzero_exit(monkeypatch, tmp_path):
    path = tmp_path / "snap_F.jpg"
    path.write_bytes(b"data")

    monkeypatch.setattr("blackvue.snap.sys.platform", "linux")

    def _raise(*args, **kwargs):
        raise subprocess.CalledProcessError(1, args)

    monkeypatch.setattr("blackvue.snap.subprocess.run", _raise)

    assert open_with_default_app(path) is False
