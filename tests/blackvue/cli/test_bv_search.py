from pathlib import Path

from blackvue.archive.asset import Asset
from blackvue.archive.asset_file import AssetFile
from blackvue.archive.recording import Recording
from blackvue.archive.recording_id import RecordingId
from blackvue.cli import bv_search
from blackvue.cli.bv_search import parse_args


def test_parse_args_defaults():
    args = parse_args(["/some/archive", "--text", "traffic"])

    assert args.path == "/some/archive"
    assert args.text == "traffic"
    assert args.asset == "all"
    assert args.regex is False
    assert args.case_sensitive is False
    assert args.near is None
    assert args.place is None
    assert args.radius == bv_search.DEFAULT_RADIUS_METERS


def test_parse_args_path_defaults_to_cwd():
    args = parse_args(["--text", "traffic"])

    assert args.path == "."


def test_parse_args_near_parses_coordinates():
    args = parse_args(["/some/archive", "--near", "59.3293,18.0686"])

    assert args.near == (59.3293, 18.0686)


def test_parse_args_near_rejects_malformed_value():
    with pytest_raises_system_exit():
        parse_args(["/some/archive", "--near", "not-a-coordinate"])


def test_parse_args_near_and_place_are_mutually_exclusive():
    with pytest_raises_system_exit():
        parse_args(
            ["/some/archive", "--near", "59.3293,18.0686", "--place", "Stockholm"]
        )


def pytest_raises_system_exit():
    import pytest

    return pytest.raises(SystemExit)


def _make_recording(recording_id: str, tmp_path: Path, assets: dict) -> Recording:
    recording = Recording(id=RecordingId(recording_id))
    for asset, text in assets.items():
        path = tmp_path / f"{recording_id}.{asset.name.lower()}.txt"
        path.write_text(text, encoding="utf-8")
        recording.assets[asset] = AssetFile(asset=asset, path=path)
    return recording


class _FakeArchive:
    def __init__(self, recordings):
        self.recordings = recordings

    def __call__(self, path):
        return self


def test_run_requires_at_least_one_criterion(tmp_path):
    args = parse_args([str(tmp_path)])

    exit_code = bv_search._run(args, say=lambda m: None, warn=lambda m: None)

    assert exit_code == bv_search.EXIT_ARGS_ERROR


def test_run_no_recordings_in_range(monkeypatch, tmp_path):
    monkeypatch.setattr(bv_search, "Archive", _FakeArchive([]))

    args = parse_args([str(tmp_path), "--text", "traffic"])
    messages = []
    exit_code = bv_search._run(args, say=messages.append, warn=messages.append)

    assert exit_code == bv_search.EXIT_OK
    assert any("no recordings found" in m for m in messages)


def test_run_text_match_reports_recording_and_line(monkeypatch, tmp_path):
    recording = _make_recording(
        "20260715_120000_N",
        tmp_path,
        {Asset.TRANSCRIPT: "Heavy traffic near the roundabout.\n"},
    )
    monkeypatch.setattr(bv_search, "Archive", _FakeArchive([recording]))

    args = parse_args([str(tmp_path), "--text", "traffic"])
    messages = []
    exit_code = bv_search._run(args, say=messages.append, warn=messages.append)

    assert exit_code == bv_search.EXIT_OK
    assert any("20260715_120000_N" in m for m in messages)
    assert any("traffic" in m.lower() for m in messages)


def test_run_text_no_match_reports_no_matches(monkeypatch, tmp_path):
    recording = _make_recording(
        "20260715_121000_N", tmp_path, {Asset.TRANSCRIPT: "Smooth sailing.\n"}
    )
    monkeypatch.setattr(bv_search, "Archive", _FakeArchive([recording]))

    args = parse_args([str(tmp_path), "--text", "traffic"])
    messages = []
    exit_code = bv_search._run(args, say=messages.append, warn=messages.append)

    assert exit_code == bv_search.EXIT_OK
    assert any("no matches" in m for m in messages)


def test_run_text_asset_restricts_search(monkeypatch, tmp_path):
    recording = _make_recording(
        "20260715_122000_N",
        tmp_path,
        {
            Asset.TRANSCRIPT: "traffic jam",
            Asset.SCENE_DESCRIPTION: "quiet street",
        },
    )
    monkeypatch.setattr(bv_search, "Archive", _FakeArchive([recording]))

    args = parse_args(
        [str(tmp_path), "--text", "traffic", "--asset", "scene"]
    )
    messages = []
    exit_code = bv_search._run(args, say=messages.append, warn=messages.append)

    assert exit_code == bv_search.EXIT_OK
    assert any("no matches" in m for m in messages)


def test_run_near_reports_geo_match(monkeypatch, tmp_path):
    import blackvue.search as search_module
    from datetime import datetime
    from blackvue.telemetry.gps_reader import GpsFix

    recording = Recording(id=RecordingId("20260715_123000_N"))
    gps_path = tmp_path / "20260715_123000_N.gps"
    gps_path.write_text("irrelevant")
    recording.assets[Asset.GPS] = AssetFile(asset=Asset.GPS, path=gps_path)

    fix = GpsFix(
        timestamp=datetime(2026, 7, 15, 12, 30, 0),
        valid=True,
        latitude=59.3293,
        longitude=18.0686,
        speed_kmh=10.0,
        course=90.0,
    )
    monkeypatch.setattr(search_module, "read_gps", lambda path: (fix,))
    monkeypatch.setattr(bv_search, "Archive", _FakeArchive([recording]))

    args = parse_args(
        [str(tmp_path), "--near", "59.3293,18.0686", "--radius", "500"]
    )
    messages = []
    exit_code = bv_search._run(args, say=messages.append, warn=messages.append)

    assert exit_code == bv_search.EXIT_OK
    assert any("20260715_123000_N" in m for m in messages)
    assert any("GPS:" in m for m in messages)


def test_run_combines_text_and_near_with_and_semantics(monkeypatch, tmp_path):
    import blackvue.search as search_module
    from datetime import datetime
    from blackvue.telemetry.gps_reader import GpsFix

    # Matches text but is far away - should be excluded.
    far_recording = _make_recording(
        "20260715_124000_N", tmp_path, {Asset.TRANSCRIPT: "traffic jam"}
    )
    far_gps = tmp_path / "20260715_124000_N.gps"
    far_gps.write_text("irrelevant")
    far_recording.assets[Asset.GPS] = AssetFile(asset=Asset.GPS, path=far_gps)

    # Matches both text and location.
    near_recording = _make_recording(
        "20260715_125000_N", tmp_path, {Asset.TRANSCRIPT: "traffic jam"}
    )
    near_gps = tmp_path / "20260715_125000_N.gps"
    near_gps.write_text("irrelevant")
    near_recording.assets[Asset.GPS] = AssetFile(asset=Asset.GPS, path=near_gps)

    far_fix = GpsFix(
        timestamp=datetime(2026, 7, 15, 12, 40, 0),
        valid=True,
        latitude=60.0,
        longitude=19.0,
        speed_kmh=10.0,
        course=90.0,
    )
    near_fix = GpsFix(
        timestamp=datetime(2026, 7, 15, 12, 50, 0),
        valid=True,
        latitude=59.3293,
        longitude=18.0686,
        speed_kmh=10.0,
        course=90.0,
    )

    def fake_read_gps(path):
        return (far_fix,) if "124000" in str(path) else (near_fix,)

    monkeypatch.setattr(search_module, "read_gps", fake_read_gps)
    monkeypatch.setattr(
        bv_search, "Archive", _FakeArchive([far_recording, near_recording])
    )

    args = parse_args(
        [
            str(tmp_path),
            "--text",
            "traffic",
            "--near",
            "59.3293,18.0686",
            "--radius",
            "500",
        ]
    )
    messages = []
    exit_code = bv_search._run(args, say=messages.append, warn=messages.append)

    assert exit_code == bv_search.EXIT_OK
    assert any("20260715_125000_N" in m for m in messages)
    assert not any("20260715_124000_N" in m for m in messages)


def test_run_place_geocodes_then_searches(monkeypatch, tmp_path):
    import blackvue.search as search_module
    from datetime import datetime
    from blackvue.telemetry.gps_reader import GpsFix

    recording = Recording(id=RecordingId("20260715_126000_N"))
    gps_path = tmp_path / "20260715_126000_N.gps"
    gps_path.write_text("irrelevant")
    recording.assets[Asset.GPS] = AssetFile(asset=Asset.GPS, path=gps_path)

    fix = GpsFix(
        timestamp=datetime(2026, 7, 15, 12, 30, 0),
        valid=True,
        latitude=59.3293,
        longitude=18.0686,
        speed_kmh=10.0,
        course=90.0,
    )
    monkeypatch.setattr(search_module, "read_gps", lambda path: (fix,))
    monkeypatch.setattr(bv_search, "Archive", _FakeArchive([recording]))

    # Fake the deferred import target directly on the geocoding module,
    # since bv_search imports load_or_forward_geocode locally inside
    # _run() rather than at module scope (see its own comment on why).
    from blackvue.export import geocoding as geocoding_module
    from blackvue.export.geocoding import GeocodeResult

    calls = []

    def fake_load_or_forward_geocode(name, cache_dir, **kwargs):
        calls.append((name, cache_dir))
        return GeocodeResult(point=(59.3293, 18.0686))

    monkeypatch.setattr(
        geocoding_module, "load_or_forward_geocode", fake_load_or_forward_geocode
    )

    args = parse_args(
        [str(tmp_path), "--place", "Stockholm", "--radius", "500"]
    )
    messages = []
    exit_code = bv_search._run(args, say=messages.append, warn=messages.append)

    assert exit_code == bv_search.EXIT_OK
    assert len(calls) == 1
    assert calls[0][0] == "Stockholm"
    assert any("Stockholm" in m for m in messages)
    assert any("20260715_126000_N" in m for m in messages)


def test_run_place_with_road_geometry_matches_along_the_whole_road(
    monkeypatch, tmp_path
):
    # A --place match that resolves to a road (GeocodeResult.lines
    # non-empty) should find a GPS fix near the far end of the road,
    # not just near Nominatim's single representative point - this is
    # the whole reason line geometry gets threaded through at all.
    import blackvue.search as search_module
    from datetime import datetime
    from blackvue.telemetry.gps_reader import GpsFix
    from blackvue.export import geocoding as geocoding_module
    from blackvue.export.geocoding import GeocodeResult

    recording = Recording(id=RecordingId("20260715_128000_N"))
    gps_path = tmp_path / "20260715_128000_N.gps"
    gps_path.write_text("irrelevant")
    recording.assets[Asset.GPS] = AssetFile(asset=Asset.GPS, path=gps_path)

    # Fix is ~5km from the road's representative point, but right on
    # the road's own line geometry.
    fix = GpsFix(
        timestamp=datetime(2026, 7, 15, 12, 30, 0),
        valid=True,
        latitude=59.364,
        longitude=18.0501,
        speed_kmh=10.0,
        course=90.0,
    )
    monkeypatch.setattr(search_module, "read_gps", lambda path: (fix,))
    monkeypatch.setattr(bv_search, "Archive", _FakeArchive([recording]))

    road_geometry = GeocodeResult(
        point=(59.320, 18.050),
        lines=(((59.320, 18.050), (59.365, 18.050)),),
    )
    monkeypatch.setattr(
        geocoding_module,
        "load_or_forward_geocode",
        lambda name, cache_dir, **k: road_geometry,
    )

    args = parse_args(
        [str(tmp_path), "--place", "A Long Road", "--radius", "200"]
    )
    messages = []
    exit_code = bv_search._run(args, say=messages.append, warn=messages.append)

    assert exit_code == bv_search.EXIT_OK
    assert any("20260715_128000_N" in m for m in messages)
    assert any("segment" in m for m in messages)


def test_run_place_reports_error_when_not_found(monkeypatch, tmp_path):
    recording = Recording(id=RecordingId("20260715_127000_N"))
    monkeypatch.setattr(bv_search, "Archive", _FakeArchive([recording]))

    from blackvue.export import geocoding as geocoding_module

    monkeypatch.setattr(
        geocoding_module, "load_or_forward_geocode", lambda name, cache_dir, **k: None
    )

    args = parse_args([str(tmp_path), "--place", "Nowhereville"])
    messages = []
    exit_code = bv_search._run(args, say=messages.append, warn=messages.append)

    assert exit_code == bv_search.EXIT_HAD_ERRORS
