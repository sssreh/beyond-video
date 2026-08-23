import json
from pathlib import Path

from blackvue.adapters.blackvue.adapter import BlackVueAdapter
from blackvue.archive.asset import Asset
from blackvue.archive.asset_file import AssetFile
from blackvue.archive.recording import Recording
from blackvue.archive.recording_id import RecordingId
from blackvue.cli import bv_stats
from blackvue.cli.bv_stats import main
from blackvue.cli.bv_stats import parse_args


def _make_recording_with_stats(recording_id: str, tmp_path: Path, stats: dict) -> Recording:
    recording = Recording(id=RecordingId(recording_id))
    path = tmp_path / f"{recording_id}.stats.json"
    path.write_text(json.dumps(stats), encoding="utf-8")
    recording.assets[Asset.RECORDING_STATS] = AssetFile(
        asset=Asset.RECORDING_STATS, path=path
    )
    return recording


class _FakeArchive:
    def __init__(self, recordings):
        self.recordings = recordings

    def __call__(self, path):
        return self


class _FakeAdapter(BlackVueAdapter):
    """See test_bv_search.py's own _FakeAdapter for the full reasoning -
    mirrored here unchanged."""

    def __init__(self, archive):
        self._archive = archive

    def open_archive(self, path):
        return self._archive(path)


def test_parse_args_defaults():
    args = parse_args(["/some/archive"])

    assert args.path == "/some/archive"
    assert args.group == "all"
    assert args.fields == list(bv_stats.DEFAULT_FIELDS)
    assert args.list_fields is False
    assert args.json is False
    assert args.trace is False


def test_parse_args_path_defaults_to_cwd():
    args = parse_args([])

    assert args.path == "."


def test_parse_args_group_choice():
    args = parse_args(["/some/archive", "--group", "weekday"])

    assert args.group == "weekday"


def test_parse_args_fields_parses_comma_separated_list():
    args = parse_args(["/some/archive", "--fields", "distance_km,max_speed_kmh"])

    assert args.fields == ["distance_km", "max_speed_kmh"]


def test_parse_args_fields_all():
    args = parse_args(["/some/archive", "--fields", "all"])

    assert set(args.fields) == set(bv_stats.STAT_FIELDS)


def test_parse_args_fields_rejects_unknown_field():
    with pytest_raises_system_exit():
        parse_args(["/some/archive", "--fields", "not_a_real_field"])


def pytest_raises_system_exit():
    import pytest

    return pytest.raises(SystemExit)


def test_run_list_fields_prints_every_field_and_needs_no_archive(tmp_path):
    args = parse_args(["--list-fields"])
    messages = []

    exit_code = bv_stats._run(args, say=messages.append, warn=messages.append)

    assert exit_code == bv_stats.EXIT_OK
    joined = "\n".join(messages)
    for field_key in bv_stats.STAT_FIELDS:
        assert field_key in joined


def test_run_no_recordings_in_range(monkeypatch, tmp_path):
    monkeypatch.setattr(
        bv_stats, "get_adapter", lambda adapter_id: _FakeAdapter(_FakeArchive([]))
    )
    args = parse_args([str(tmp_path)])
    messages = []

    exit_code = bv_stats._run(args, say=messages.append, warn=messages.append)

    assert exit_code == bv_stats.EXIT_OK
    assert any("no recordings found in range" in m for m in messages)


def test_run_no_recordings_have_stats_yet(monkeypatch, tmp_path):
    recording = Recording(id=RecordingId("20260823_100000_NF"))
    monkeypatch.setattr(
        bv_stats,
        "get_adapter",
        lambda adapter_id: _FakeAdapter(_FakeArchive([recording])),
    )
    args = parse_args([str(tmp_path)])
    messages = []

    exit_code = bv_stats._run(args, say=messages.append, warn=messages.append)

    assert exit_code == bv_stats.EXIT_OK
    assert any("have a Stats asset yet" in m for m in messages)


def test_run_reports_aggregated_totals(monkeypatch, tmp_path):
    recording = _make_recording_with_stats(
        "20260823_100000_NF",
        tmp_path,
        {"distance_km": 5.0, "avg_speed_kmh": 40.0, "max_speed_kmh": 90.0},
    )
    monkeypatch.setattr(
        bv_stats,
        "get_adapter",
        lambda adapter_id: _FakeAdapter(_FakeArchive([recording])),
    )
    args = parse_args(
        [str(tmp_path), "--fields", "distance_km,avg_speed_kmh,max_speed_kmh"]
    )
    messages = []

    exit_code = bv_stats._run(args, say=messages.append, warn=messages.append)

    assert exit_code == bv_stats.EXIT_OK
    joined = "\n".join(messages)
    assert "5.00 km" in joined
    assert "40.0 km/h" in joined
    assert "90.0 km/h" in joined


def test_run_json_output_is_valid_json_with_expected_shape(monkeypatch, tmp_path):
    recording = _make_recording_with_stats(
        "20260823_100000_NF", tmp_path, {"distance_km": 5.0}
    )
    monkeypatch.setattr(
        bv_stats,
        "get_adapter",
        lambda adapter_id: _FakeAdapter(_FakeArchive([recording])),
    )
    args = parse_args([str(tmp_path), "--fields", "distance_km", "--json"])
    messages = []

    exit_code = bv_stats._run(args, say=messages.append, warn=messages.append)

    assert exit_code == bv_stats.EXIT_OK
    # The JSON payload is whichever say() call contains it - the
    # started/finished timing lines around it aren't valid JSON, so
    # find the one message that does parse.
    payload = None
    for message in messages:
        try:
            payload = json.loads(message)
        except (ValueError, TypeError):
            continue
    assert payload is not None
    assert payload[0]["key"] == "all"
    assert payload[0]["recordings"] == ["20260823_100000_NF"]
    assert payload[0]["values"]["distance_km"] == 5.0


def test_run_skips_recordings_without_stats_and_reports_count(monkeypatch, tmp_path):
    with_stats = _make_recording_with_stats(
        "20260823_100000_NF", tmp_path, {"distance_km": 5.0}
    )
    without_stats = Recording(id=RecordingId("20260823_110000_NF"))
    monkeypatch.setattr(
        bv_stats,
        "get_adapter",
        lambda adapter_id: _FakeAdapter(
            _FakeArchive([with_stats, without_stats])
        ),
    )
    args = parse_args([str(tmp_path), "--fields", "distance_km"])
    messages = []

    exit_code = bv_stats._run(args, say=messages.append, warn=messages.append)

    assert exit_code == bv_stats.EXIT_OK
    assert any("1 of 2 recording(s)" in m for m in messages)


def test_main_resolves_a_camera_id_to_its_configured_target(tmp_path, capsys):
    from blackvue.core.camera_config import CameraConfig
    from blackvue.core.camera_config import config_path
    from blackvue.core.camera_config import save_camera_config

    archive = tmp_path / "archive"
    archive.mkdir()

    config_dir = tmp_path / "config"
    save_camera_config(
        config_path(config_dir, "Kirby"),
        CameraConfig(id="Kirby", name="Kirby", archive=archive),
    )

    exit_code = main(["Kirby", "--config-dir", str(config_dir)])

    out = capsys.readouterr().out

    assert exit_code == 0
    assert str(archive) in out
    assert "no recordings found in range" in out
