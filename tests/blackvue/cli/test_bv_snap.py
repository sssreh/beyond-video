"""
Tests for cli/bv_snap.py - the standalone bv-snap command. Mirrors
tests/blackvue/cli/test_bv_gps.py's own connection-setup stubbing
(_stub_connection() below is a near-literal copy of that file's own
helper, since bv_snap.py's _run() copies bv_gps.py's own connection
block almost verbatim).

Copyright (C) 2026 Christer R. (sssreh)

SPDX-License-Identifier: GPL-3.0-or-later
"""

from __future__ import annotations

from pathlib import Path

import pytest

from blackvue.cli import bv_snap as bv_snap_module
from blackvue.core.endpoint import Endpoint


def test_parse_args_defaults():
    args = bv_snap_module.parse_args(["mycar", "--output", "/tmp/snaps"])

    assert args.id == "mycar"
    assert args.host is None
    assert args.timeout == 5
    assert args.output == Path("/tmp/snaps")
    assert args.direction is None


def test_parse_args_output_is_required(capsys):
    with pytest.raises(SystemExit):
        bv_snap_module.parse_args(["mycar"])

    assert "required" in capsys.readouterr().err


def test_parse_args_requires_id_or_host(capsys):
    with pytest.raises(SystemExit):
        bv_snap_module.parse_args(["--output", "/tmp/snaps"])

    assert "required" in capsys.readouterr().err


def test_parse_args_rejects_both_id_and_host(capsys):
    with pytest.raises(SystemExit):
        bv_snap_module.parse_args(
            ["mycar", "--host", "192.168.1.42", "--output", "/tmp/snaps"]
        )

    assert "not allowed" in capsys.readouterr().err


def test_parse_args_direction_is_repeatable():
    args = bv_snap_module.parse_args(
        ["mycar", "--output", "/tmp/snaps", "--direction", "F", "--direction", "R"]
    )

    assert args.direction == ["F", "R"]


def test_parse_args_direction_rejects_unknown_letters(capsys):
    with pytest.raises(SystemExit):
        bv_snap_module.parse_args(
            ["mycar", "--output", "/tmp/snaps", "--direction", "X"]
        )

    assert "invalid choice" in capsys.readouterr().err


class _FakeConfig:
    name = "MyCar"
    endpoints = [Endpoint(name="home", address="10.99.77.1")]
    target = Path("/tmp/whatever")


class _FakeClient:
    def __init__(self, snapshots):
        self._snapshots = snapshots

    def snapshot(self, directions):
        return {d: self._snapshots[d] for d in directions if d in self._snapshots}


def _stub_connection(monkeypatch, snapshots):
    monkeypatch.setattr(
        bv_snap_module, "load_camera_config", lambda path: _FakeConfig()
    )
    monkeypatch.setattr(
        bv_snap_module, "config_path", lambda config_dir, id_: Path("/tmp/fake.toml")
    )
    monkeypatch.setattr(
        bv_snap_module,
        "connect",
        lambda endpoints, timeout: (endpoints[0], _FakeClient(snapshots)),
    )


def test_run_saves_every_direction_and_reports_each_path(monkeypatch, capsys, tmp_path):
    _stub_connection(
        monkeypatch, {"F": b"front-bytes", "R": b"rear-bytes", "I": b"interior-bytes"}
    )

    args = bv_snap_module.parse_args(["mycar", "--output", str(tmp_path)])
    code = bv_snap_module._run(args)

    out = capsys.readouterr().out
    assert code == bv_snap_module.EXIT_OK
    assert "F: saved" in out
    assert "R: saved" in out
    assert "I: saved" in out
    assert len(list(tmp_path.glob("snap_*_F.jpg"))) == 1


def test_run_warns_but_still_succeeds_when_one_direction_is_missing(
    monkeypatch, capsys, tmp_path
):
    # Interior support is unconfirmed on some hardware (see
    # blackvue_client.py's own snapshot() docstring) - a partial
    # result (F/R only) is still a successful run, not a failure.
    _stub_connection(monkeypatch, {"F": b"front-bytes", "R": b"rear-bytes"})

    args = bv_snap_module.parse_args(["mycar", "--output", str(tmp_path)])
    code = bv_snap_module._run(args)

    err = capsys.readouterr().err
    assert code == bv_snap_module.EXIT_OK
    assert "no snapshot received for direction I" in err


def test_run_exits_no_snapshots_when_every_direction_fails(
    monkeypatch, capsys, tmp_path
):
    _stub_connection(monkeypatch, {})

    args = bv_snap_module.parse_args(["mycar", "--output", str(tmp_path)])
    code = bv_snap_module._run(args)

    err = capsys.readouterr().err
    assert code == bv_snap_module.EXIT_NO_SNAPSHOTS
    assert "no snapshot received for any direction" in err


def test_run_honors_an_explicit_direction_subset(monkeypatch, capsys, tmp_path):
    _stub_connection(
        monkeypatch, {"F": b"front-bytes", "R": b"rear-bytes", "I": b"interior-bytes"}
    )

    args = bv_snap_module.parse_args(
        ["mycar", "--output", str(tmp_path), "--direction", "F"]
    )
    code = bv_snap_module._run(args)

    out = capsys.readouterr().out
    assert code == bv_snap_module.EXIT_OK
    assert "F: saved" in out
    assert "R: saved" not in out
    assert "I: saved" not in out


def test_run_exits_unreachable_cleanly(monkeypatch, capsys, tmp_path):
    def _fake_connect(endpoints, timeout):
        from blackvue.core.connection import CameraUnreachableError

        raise CameraUnreachableError(
            f"no configured endpoint could be reached: "
            f"{endpoints[0].name} ({endpoints[0].address}): timed out"
        )

    monkeypatch.setattr(
        bv_snap_module, "load_camera_config", lambda path: _FakeConfig()
    )
    monkeypatch.setattr(
        bv_snap_module, "config_path", lambda config_dir, id_: Path("/tmp/fake.toml")
    )
    monkeypatch.setattr(bv_snap_module, "connect", _fake_connect)

    args = bv_snap_module.parse_args(["mycar", "--output", str(tmp_path)])
    code = bv_snap_module._run(args)

    assert code == bv_snap_module.EXIT_UNREACHABLE
    assert "10.99.77.1" in capsys.readouterr().err


def test_run_exits_config_error_when_no_endpoints_configured(
    monkeypatch, capsys, tmp_path
):
    class _NoEndpointsConfig:
        name = "MyCar"
        endpoints: list = []
        target = Path("/tmp/whatever")

    monkeypatch.setattr(
        bv_snap_module, "load_camera_config", lambda path: _NoEndpointsConfig()
    )
    monkeypatch.setattr(
        bv_snap_module, "config_path", lambda config_dir, id_: Path("/tmp/fake.toml")
    )

    args = bv_snap_module.parse_args(["mycar", "--output", str(tmp_path)])
    code = bv_snap_module._run(args)

    assert code == bv_snap_module.EXIT_CONFIG_ERROR
