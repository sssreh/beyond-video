from pathlib import Path

from blackvue.archive.asset import Asset
from blackvue.archive.asset_file import AssetFile
from blackvue.archive.recording import Recording
from blackvue.archive.recording_id import RecordingId
from blackvue.cli import bv_search
from blackvue.cli.bv_search import main
from blackvue.cli.bv_search import parse_args
from blackvue.core.camera_config import CameraConfig
from blackvue.core.camera_config import config_path
from blackvue.core.camera_config import save_camera_config


def test_main_resolves_a_camera_id_to_its_configured_target(tmp_path, capsys):
    archive = tmp_path / "archive"
    archive.mkdir()

    config_dir = tmp_path / "config"
    save_camera_config(
        config_path(config_dir, "Kirby"),
        CameraConfig(id="Kirby", name="Kirby", archive=archive),
    )

    exit_code = main(
        ["Kirby", "--config-dir", str(config_dir), "--text", "traffic"]
    )

    out = capsys.readouterr().out

    assert exit_code == 0
    assert str(archive) in out
    assert "no recordings found in range" in out


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
    assert args.trace is False
    assert args.fov == bv_search.DEFAULT_HORIZONTAL_FOV_DEGREES


def test_parse_args_fov_override():
    args = parse_args(["/some/archive", "--text", "traffic", "--fov", "90"])

    assert args.fov == 90.0


def test_parse_args_trace_flag_sets_true():
    args = parse_args(["/some/archive", "--text", "traffic", "--trace"])

    assert args.trace is True


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


def test_run_text_match_is_preceded_by_a_blank_line(monkeypatch, tmp_path):
    recording = _make_recording(
        "20260715_120500_N",
        tmp_path,
        {Asset.TRANSCRIPT: "Heavy traffic near the roundabout.\n"},
    )
    monkeypatch.setattr(bv_search, "Archive", _FakeArchive([recording]))

    args = parse_args([str(tmp_path), "--text", "traffic"])
    messages = []
    bv_search._run(args, say=messages.append, warn=messages.append)

    recording_index = messages.index("20260715_120500_N")
    assert messages[recording_index - 1] == ""


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


def _make_test_video(path: Path, duration_seconds: float = 3.0) -> None:
    import subprocess

    subprocess.run(
        [
            "ffmpeg", "-y",
            "-f", "lavfi",
            "-i", f"testsrc=size=320x240:rate=10:duration={duration_seconds}",
            "-pix_fmt", "yuv420p",
            str(path),
        ],
        capture_output=True, text=True, check=True,
    )


def test_run_near_renders_zoom_outputs_when_front_video_exists(monkeypatch, tmp_path):
    import blackvue.search as search_module
    from datetime import datetime
    from blackvue.telemetry.gps_reader import GpsFix

    recording = Recording(id=RecordingId("20260715_123000_N"))
    gps_path = tmp_path / "20260715_123000_N.gps"
    gps_path.write_text("irrelevant")
    recording.assets[Asset.GPS] = AssetFile(asset=Asset.GPS, path=gps_path)

    front_path = tmp_path / "20260715_123000_N.mp4"
    _make_test_video(front_path)
    recording.assets[Asset.FRONT] = AssetFile(asset=Asset.FRONT, path=front_path)

    # Heading north, target ~57 degrees to the right - inside the
    # default 136-degree FOV, so a real crop/zoom should be rendered.
    fix = GpsFix(
        timestamp=datetime(2026, 7, 15, 12, 30, 1),
        valid=True,
        latitude=59.3293,
        longitude=18.0686,
        speed_kmh=30.0,
        course=0.0,
    )
    monkeypatch.setattr(search_module, "read_gps", lambda path: (fix,))
    monkeypatch.setattr(bv_search, "Archive", _FakeArchive([recording]))

    args = parse_args(
        [str(tmp_path), "--near", "59.3295,18.0692", "--radius", "100"]
    )
    messages = []
    exit_code = bv_search._run(args, say=messages.append, warn=messages.append)

    assert exit_code == bv_search.EXIT_OK
    thumbnail_line = next(m for m in messages if "zoom thumbnail:" in m)
    clip_line = next(m for m in messages if "zoom clip:" in m)
    thumbnail_path = Path(thumbnail_line.split("zoom thumbnail: ", 1)[1])
    clip_path = Path(clip_line.split("zoom clip: ", 1)[1])
    assert thumbnail_path.exists()
    assert clip_path.exists()


def test_run_near_reports_zoom_skip_when_target_out_of_frame(monkeypatch, tmp_path):
    import blackvue.search as search_module
    from datetime import datetime
    from blackvue.telemetry.gps_reader import GpsFix

    recording = Recording(id=RecordingId("20260715_123000_N"))
    gps_path = tmp_path / "20260715_123000_N.gps"
    gps_path.write_text("irrelevant")
    recording.assets[Asset.GPS] = AssetFile(asset=Asset.GPS, path=gps_path)

    front_path = tmp_path / "20260715_123000_N.mp4"
    _make_test_video(front_path)
    recording.assets[Asset.FRONT] = AssetFile(asset=Asset.FRONT, path=front_path)

    # Heading north, target due south of the fix - straight behind the
    # car, outside any forward-facing camera's field of view.
    fix = GpsFix(
        timestamp=datetime(2026, 7, 15, 12, 30, 1),
        valid=True,
        latitude=59.3293,
        longitude=18.0686,
        speed_kmh=30.0,
        course=0.0,
    )
    monkeypatch.setattr(search_module, "read_gps", lambda path: (fix,))
    monkeypatch.setattr(bv_search, "Archive", _FakeArchive([recording]))

    args = parse_args(
        [str(tmp_path), "--near", "59.3290,18.0686", "--radius", "100"]
    )
    messages = []
    exit_code = bv_search._run(args, say=messages.append, warn=messages.append)

    assert exit_code == bv_search.EXIT_OK
    assert any("zoom: skipped" in m for m in messages)
    assert not any("zoom thumbnail:" in m for m in messages)


def test_run_near_reports_zoom_failure_but_keeps_the_match(monkeypatch, tmp_path):
    import blackvue.search as search_module
    from datetime import datetime
    from blackvue.telemetry.gps_reader import GpsFix

    recording = Recording(id=RecordingId("20260715_123000_N"))
    gps_path = tmp_path / "20260715_123000_N.gps"
    gps_path.write_text("irrelevant")
    recording.assets[Asset.GPS] = AssetFile(asset=Asset.GPS, path=gps_path)

    # Registered as an asset, but the file doesn't actually exist -
    # ffprobe/ffmpeg should fail cleanly (MediaToolError), not crash
    # the whole search.
    missing_front = tmp_path / "does_not_exist.mp4"
    recording.assets[Asset.FRONT] = AssetFile(asset=Asset.FRONT, path=missing_front)

    fix = GpsFix(
        timestamp=datetime(2026, 7, 15, 12, 30, 1),
        valid=True,
        latitude=59.3293,
        longitude=18.0686,
        speed_kmh=30.0,
        course=0.0,
    )
    monkeypatch.setattr(search_module, "read_gps", lambda path: (fix,))
    monkeypatch.setattr(bv_search, "Archive", _FakeArchive([recording]))

    args = parse_args(
        [str(tmp_path), "--near", "59.3295,18.0692", "--radius", "100"]
    )
    messages = []
    exit_code = bv_search._run(args, say=messages.append, warn=messages.append)

    assert exit_code == bv_search.EXIT_HAD_ERRORS
    assert any("20260715_123000_N" in m for m in messages)
    assert any("GPS:" in m for m in messages)
    assert any("zoom" in m.lower() for m in messages)


def test_run_place_resolution_prints_before_the_started_line(monkeypatch, tmp_path):
    import blackvue.search as search_module
    from datetime import datetime
    from blackvue.telemetry.gps_reader import GpsFix

    recording = Recording(id=RecordingId("20260715_129000_N"))
    gps_path = tmp_path / "20260715_129000_N.gps"
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

    from blackvue.export import geocoding as geocoding_module
    from blackvue.export.geocoding import GeocodeResult

    monkeypatch.setattr(
        geocoding_module,
        "load_or_forward_geocode",
        lambda name, cache_dir, **k: GeocodeResult(point=(59.3293, 18.0686)),
    )

    args = parse_args([str(tmp_path), "--place", "Stockholm", "--radius", "500"])
    messages = []
    bv_search._run(args, say=messages.append, warn=messages.append)

    place_index = next(
        i for i, m in enumerate(messages) if m.startswith("bv-search: 'Stockholm'")
    )
    started_index = next(
        i for i, m in enumerate(messages) if m.startswith("bv-search: started ")
    )
    assert place_index < started_index


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


# ---------------------------------------------------------------------------
# Start/finished timing lines - a wide-range search over a big archive can
# take tens of seconds with nothing else printed in between, so _run()
# reports when it started and how long it took, on every exit path.
# ---------------------------------------------------------------------------


def test_run_prints_started_and_finished_lines_around_a_search(monkeypatch, tmp_path):
    monkeypatch.setattr(bv_search, "Archive", _FakeArchive([]))

    args = parse_args([str(tmp_path), "--text", "traffic"])
    messages = []
    bv_search._run(args, say=messages.append, warn=messages.append)

    assert any(m.startswith("bv-search: started ") for m in messages)
    assert any(
        m.startswith("bv-search: finished ") and m.rstrip().endswith("s)")
        for m in messages
    )
    # started comes before finished.
    started_index = next(
        i for i, m in enumerate(messages) if m.startswith("bv-search: started ")
    )
    finished_index = next(
        i for i, m in enumerate(messages) if m.startswith("bv-search: finished ")
    )
    assert started_index < finished_index


def test_run_prints_finished_line_even_when_the_time_parser_rejects_input(
    monkeypatch, tmp_path
):
    monkeypatch.setattr(bv_search, "Archive", _FakeArchive([]))

    args = parse_args([str(tmp_path), "--text", "traffic", "--timestamp", "abc"])
    messages = []
    exit_code = bv_search._run(args, say=messages.append, warn=messages.append)

    assert exit_code == bv_search.EXIT_ARGS_ERROR
    assert any(m.startswith("bv-search: started ") for m in messages)
    assert any(m.startswith("bv-search: finished ") for m in messages)


def test_run_does_not_print_started_or_finished_when_no_criteria_given(tmp_path):
    args = parse_args([str(tmp_path)])
    messages = []
    exit_code = bv_search._run(args, say=messages.append, warn=messages.append)

    # The args-error path returns before any real work starts, so there's
    # nothing worth timing - matches every other bv-* CLI's own usage-
    # error behavior (no run summary for a command that never ran).
    assert exit_code == bv_search.EXIT_ARGS_ERROR
    assert not any("started" in m or "finished" in m for m in messages)


# ---------------------------------------------------------------------------
# DotProgress / --trace - mirrors bv-download's own DotProgress, printing a
# heartbeat '.' to real stdout (not through the injected `say`) every
# TRACE_INTERVAL_RECORDINGS recordings searched.
# ---------------------------------------------------------------------------


def test_dot_progress_prints_nothing_below_the_interval(capsys):
    progress = bv_search.DotProgress(interval=5)

    for _ in range(4):
        progress.tick()

    assert capsys.readouterr().out == ""


def test_dot_progress_prints_one_dot_per_interval_crossed(capsys):
    progress = bv_search.DotProgress(interval=5)

    for _ in range(5):
        progress.tick()

    assert capsys.readouterr().out == "."


def test_dot_progress_prints_multiple_dots_over_many_ticks(capsys):
    progress = bv_search.DotProgress(interval=5)

    for _ in range(12):  # crosses 5 and 10
        progress.tick()

    assert capsys.readouterr().out == ".."


def test_dot_progress_finish_prints_nothing_when_no_dots_were_printed(capsys):
    progress = bv_search.DotProgress(interval=5)

    progress.finish()

    assert capsys.readouterr().out == ""


def test_dot_progress_finish_prints_a_trailing_newline_when_dots_were_printed(capsys):
    progress = bv_search.DotProgress(interval=5)

    for _ in range(5):
        progress.tick()
    capsys.readouterr()  # discard the dot itself

    progress.finish()

    assert capsys.readouterr().out == "\n"


def test_run_trace_flag_prints_progress_dots(monkeypatch, tmp_path, capsys):
    real_dot_progress = bv_search.DotProgress
    monkeypatch.setattr(bv_search, "DotProgress", lambda: real_dot_progress(interval=2))

    recordings = [
        _make_recording(f"2026071512{i:04d}_N", tmp_path, {Asset.TRANSCRIPT: "traffic"})
        for i in range(5)
    ]
    monkeypatch.setattr(bv_search, "Archive", _FakeArchive(recordings))

    args = parse_args([str(tmp_path), "--text", "traffic", "--trace"])
    exit_code = bv_search._run(args, say=lambda m: None, warn=lambda m: None)

    out = capsys.readouterr().out

    assert exit_code == bv_search.EXIT_OK
    # 5 recordings, interval 2 -> 2 dots plus the closing newline.
    assert out.count(".") == 2
    assert out.endswith("\n")


def test_run_without_trace_flag_prints_no_dots(monkeypatch, tmp_path, capsys):
    recordings = [
        _make_recording(f"2026071512{i:04d}_N", tmp_path, {Asset.TRANSCRIPT: "traffic"})
        for i in range(5)
    ]
    monkeypatch.setattr(bv_search, "Archive", _FakeArchive(recordings))

    args = parse_args([str(tmp_path), "--text", "traffic"])
    bv_search._run(args, say=lambda m: None, warn=lambda m: None)

    assert "." not in capsys.readouterr().out
