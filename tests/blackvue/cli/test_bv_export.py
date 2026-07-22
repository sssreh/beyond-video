import subprocess

import pytest

from blackvue.cli import bv_export as bv_export_module
from blackvue.cli.bv_export import bv_export
from blackvue.cli.bv_export import main
from blackvue.export import trip_export as trip_export_module
from blackvue.export.osm_roads import Road


def _make_video(path, duration_seconds: float = 0.5) -> None:
    subprocess.run(
        [
            "ffmpeg", "-y",
            "-f", "lavfi",
            "-i", "testsrc=size=64x64:rate=10",
            "-t", str(duration_seconds),
            str(path),
        ],
        capture_output=True,
        text=True,
        check=True,
    )


def test_main_reports_a_missing_archive_path_cleanly(tmp_path, capsys):
    missing = tmp_path / "no-such-archive"
    target = tmp_path / "out"

    exit_code = main([str(missing), "--target", str(target)])

    err = capsys.readouterr().err

    assert exit_code == 1
    assert "bv-export" in err
    assert str(missing) in err
    assert "Traceback" not in err


def test_bv_export_creates_a_trip_folder(tmp_path, capsys):
    archive = tmp_path / "archive"
    archive.mkdir()
    target = tmp_path / "out"

    _make_video(archive / "20260720_100000_NF.mp4")
    _make_video(archive / "20260720_100200_NF.mp4")

    exit_code = bv_export(str(archive), target=str(target))

    assert exit_code == 0
    folder = target / "trip_20260720_100000_20260720_100200"
    assert folder.is_dir()
    assert (folder / "front.mp4").exists()

    out = capsys.readouterr().out
    assert str(folder) in out


def test_bv_export_applies_prefix(tmp_path):
    archive = tmp_path / "archive"
    archive.mkdir()
    target = tmp_path / "out"

    _make_video(archive / "20260720_100000_NF.mp4")

    bv_export(str(archive), target=str(target), prefix="Holiday")

    folder = target / "Holiday_trip_20260720_100000_20260720_100000"
    assert folder.is_dir()


def test_bv_export_keeps_existing_files_by_default_when_noninteractive(
    tmp_path,
):
    # Non-interactive (no tty, which is how tests always run) and no
    # --overwrite: existing trip folders are left alone except for
    # whatever this run actually regenerates - e.g. an earlier --map
    # run's map.mp4 should survive a later plain export.
    archive = tmp_path / "archive"
    archive.mkdir()
    target = tmp_path / "out"

    _make_video(archive / "20260720_100000_NF.mp4")

    folder = target / "trip_20260720_100000_20260720_100000"
    folder.mkdir(parents=True)
    stale_file = folder / "stale.txt"
    stale_file.write_text("leftover from a previous run")

    bv_export(str(archive), target=str(target))

    assert folder.is_dir()
    assert stale_file.exists(), "should be left alone without --overwrite"
    assert (folder / "front.mp4").exists()


def test_bv_export_overwrite_flag_wipes_existing_folder(tmp_path):
    archive = tmp_path / "archive"
    archive.mkdir()
    target = tmp_path / "out"

    _make_video(archive / "20260720_100000_NF.mp4")

    folder = target / "trip_20260720_100000_20260720_100000"
    folder.mkdir(parents=True)
    stale_file = folder / "stale.txt"
    stale_file.write_text("leftover from a previous run")

    bv_export(str(archive), target=str(target), overwrite=True)

    assert folder.is_dir()
    assert not stale_file.exists()
    assert (folder / "front.mp4").exists()


def test_bv_export_interactive_prompt_wipes_when_answered_yes(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(bv_export_module, "_interactive", lambda: True)
    monkeypatch.setattr("builtins.input", lambda _prompt: "w")

    archive = tmp_path / "archive"
    archive.mkdir()
    target = tmp_path / "out"

    _make_video(archive / "20260720_100000_NF.mp4")

    folder = target / "trip_20260720_100000_20260720_100000"
    folder.mkdir(parents=True)
    stale_file = folder / "stale.txt"
    stale_file.write_text("leftover from a previous run")

    bv_export(str(archive), target=str(target))

    assert not stale_file.exists()


def test_bv_export_interactive_prompt_keeps_on_default_answer(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(bv_export_module, "_interactive", lambda: True)
    monkeypatch.setattr("builtins.input", lambda _prompt: "")

    archive = tmp_path / "archive"
    archive.mkdir()
    target = tmp_path / "out"

    _make_video(archive / "20260720_100000_NF.mp4")

    folder = target / "trip_20260720_100000_20260720_100000"
    folder.mkdir(parents=True)
    stale_file = folder / "stale.txt"
    stale_file.write_text("leftover from a previous run")

    bv_export(str(archive), target=str(target))

    assert stale_file.exists()


def test_bv_export_interactive_prompt_only_asked_once_per_run(
    tmp_path, monkeypatch
):
    calls = []

    def fake_input(prompt):
        calls.append(prompt)
        return "w"

    monkeypatch.setattr(bv_export_module, "_interactive", lambda: True)
    monkeypatch.setattr("builtins.input", fake_input)

    archive = tmp_path / "archive"
    archive.mkdir()
    target = tmp_path / "out"

    _make_video(archive / "20260720_100000_NF.mp4")
    _make_video(archive / "20260720_100500_NF.mp4")

    for suffix in ("100000", "100500"):
        folder = target / f"trip_20260720_{suffix}_20260720_{suffix}"
        folder.mkdir(parents=True)
        (folder / "stale.txt").write_text("leftover")

    bv_export(str(archive), target=str(target), max_gap_minutes=1)

    assert len(calls) == 1, "should only prompt once for the whole run"


def test_bv_export_dry_run_writes_nothing(tmp_path, capsys):
    archive = tmp_path / "archive"
    archive.mkdir()
    target = tmp_path / "out"

    _make_video(archive / "20260720_100000_NF.mp4")

    exit_code = bv_export(str(archive), target=str(target), dry_run=True)

    out = capsys.readouterr().out

    assert exit_code == 0
    assert not target.exists()
    assert "dry run" in out


def test_bv_export_respects_max_gap(tmp_path):
    archive = tmp_path / "archive"
    archive.mkdir()
    target = tmp_path / "out"

    _make_video(archive / "20260720_100000_NF.mp4")
    _make_video(archive / "20260720_100500_NF.mp4")

    bv_export(str(archive), target=str(target), max_gap_minutes=1)

    assert (target / "trip_20260720_100000_20260720_100000").is_dir()
    assert (target / "trip_20260720_100500_20260720_100500").is_dir()
    assert not (
        target / "trip_20260720_100000_20260720_100500"
    ).exists()


def test_bv_export_uses_duration_file_to_avoid_a_false_split(tmp_path):
    archive = tmp_path / "archive"
    archive.mkdir()
    target = tmp_path / "out"

    _make_video(archive / "20260720_100000_NF.mp4")
    (archive / "20260720_100000_N.duration.txt").write_text("720\n")
    _make_video(archive / "20260720_101100_NF.mp4")

    bv_export(str(archive), target=str(target))

    assert (
        target / "trip_20260720_100000_20260720_101100"
    ).is_dir()


def test_bv_export_gap_tolerance_can_be_tightened(tmp_path):
    archive = tmp_path / "archive"
    archive.mkdir()
    target = tmp_path / "out"

    _make_video(archive / "20260720_100000_NF.mp4")
    _make_video(archive / "20260720_101005_NF.mp4")

    bv_export(str(archive), target=str(target), gap_tolerance_seconds=0)

    assert (target / "trip_20260720_100000_20260720_100000").is_dir()
    assert (target / "trip_20260720_101005_20260720_101005").is_dir()


def test_bv_export_reports_nothing_to_export_for_empty_range(tmp_path, capsys):
    archive = tmp_path / "archive"
    archive.mkdir()
    target = tmp_path / "out"

    exit_code = bv_export(str(archive), target=str(target))

    out = capsys.readouterr().out

    assert exit_code == 0
    assert "nothing to export" in out
    assert not target.exists()


def _fake_roads(*_args, **_kwargs):
    return (Road(points=((59.30, 18.00), (59.31, 18.02))),)


def _write_gps(path, epoch_ms, lat_str, ns, lon_str, ew, speed_knots=10.0):
    path.write_text(
        f"[{epoch_ms}]$GPRMC,120000.00,A,{lat_str},{ns},{lon_str},{ew},"
        f"{speed_knots},45.00,200726,,,A*6D\n"
    )


def test_bv_export_map_flag_renders_map_video(tmp_path, monkeypatch):
    monkeypatch.setattr(
        trip_export_module, "load_or_fetch_roads", _fake_roads
    )

    archive = tmp_path / "archive"
    archive.mkdir()
    target = tmp_path / "out"

    _make_video(archive / "20260720_100000_NF.mp4")
    _write_gps(
        archive / "20260720_100000_N.gps",
        1784555901000, "5917.94615", "N", "01805.17070", "E",
    )

    exit_code = bv_export(str(archive), target=str(target), render_map=True)

    assert exit_code == 0
    folder = target / "trip_20260720_100000_20260720_100000"
    # A single fix means bounding_box_for_fixes() has something to
    # bound but render_map_video() itself needs >= 2 positioned fixes
    # to draw a route from, so no map.mp4 is expected here - this
    # confirms the flag doesn't crash the export in that case.
    assert folder.is_dir()
    assert not (folder / "map.mp4").exists()


def test_bv_export_map_flag_renders_map_video_with_a_real_route(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(
        trip_export_module, "load_or_fetch_roads", _fake_roads
    )

    archive = tmp_path / "archive"
    archive.mkdir()
    target = tmp_path / "out"

    _make_video(archive / "20260720_100000_NF.mp4")
    _write_gps(
        archive / "20260720_100000_N.gps",
        1784555901000, "5917.94615", "N", "01805.17070", "E",
    )
    _make_video(archive / "20260720_100010_NF.mp4")
    _write_gps(
        archive / "20260720_100010_N.gps",
        1784555911000, "5918.94615", "N", "01806.17070", "E",
    )

    exit_code = bv_export(str(archive), target=str(target), render_map=True)

    assert exit_code == 0
    folder = target / "trip_20260720_100000_20260720_100010"
    assert (folder / "map.mp4").exists()


def test_bv_export_map_icon_flag_uses_a_custom_marker_image(tmp_path, monkeypatch):
    from PIL import Image

    monkeypatch.setattr(
        trip_export_module, "load_or_fetch_roads", _fake_roads
    )

    archive = tmp_path / "archive"
    archive.mkdir()
    target = tmp_path / "out"

    _make_video(archive / "20260720_100000_NF.mp4")
    _write_gps(
        archive / "20260720_100000_N.gps",
        1784555901000, "5917.94615", "N", "01805.17070", "E",
    )
    _make_video(archive / "20260720_100010_NF.mp4")
    _write_gps(
        archive / "20260720_100010_N.gps",
        1784555911000, "5918.94615", "N", "01806.17070", "E",
    )

    icon_path = tmp_path / "car.png"
    Image.new("RGBA", (16, 16), (0, 0, 255, 255)).save(icon_path)

    exit_code = bv_export(
        str(archive),
        target=str(target),
        render_map=True,
        map_icon=str(icon_path),
    )

    assert exit_code == 0
    folder = target / "trip_20260720_100000_20260720_100010"
    assert (folder / "map.mp4").exists()


def test_bv_export_map_zoom_flag_produces_a_scrolling_video(tmp_path, monkeypatch):
    monkeypatch.setattr(
        trip_export_module, "load_or_fetch_roads", _fake_roads
    )

    archive = tmp_path / "archive"
    archive.mkdir()
    target = tmp_path / "out"

    _make_video(archive / "20260720_100000_NF.mp4")
    _write_gps(
        archive / "20260720_100000_N.gps",
        1784555901000, "5917.94615", "N", "01805.17070", "E",
    )
    _make_video(archive / "20260720_100010_NF.mp4")
    _write_gps(
        archive / "20260720_100010_N.gps",
        1784555911000, "5918.94615", "N", "01806.17070", "E",
    )

    exit_code = bv_export(
        str(archive), target=str(target), render_map=True, map_zoom_meters=75.0,
    )

    assert exit_code == 0
    folder = target / "trip_20260720_100000_20260720_100010"
    # --map and --map-zoom together produce two separate files: the
    # static whole-trip overview, and its own zoomed follow-camera
    # video, named after its own zoom radius.
    assert (folder / "map.mp4").exists()
    assert (folder / "map_zoom_75m.mp4").exists()


def test_bv_export_map_zoom_flag_works_without_the_map_flag(tmp_path, monkeypatch):
    monkeypatch.setattr(
        trip_export_module, "load_or_fetch_roads", _fake_roads
    )

    archive = tmp_path / "archive"
    archive.mkdir()
    target = tmp_path / "out"

    _make_video(archive / "20260720_100000_NF.mp4")
    _write_gps(
        archive / "20260720_100000_N.gps",
        1784555901000, "5917.94615", "N", "01805.17070", "E",
    )
    _make_video(archive / "20260720_100010_NF.mp4")
    _write_gps(
        archive / "20260720_100010_N.gps",
        1784555911000, "5918.94615", "N", "01806.17070", "E",
    )

    exit_code = bv_export(
        str(archive), target=str(target), map_zoom_meters=120.0,
    )

    assert exit_code == 0
    folder = target / "trip_20260720_100000_20260720_100010"
    assert (folder / "map_zoom_120m.mp4").exists()
    # --map itself was never given, so no static overview.
    assert not (folder / "map.mp4").exists()


def test_main_uses_the_default_zoom_when_map_zoom_given_with_no_value(
    tmp_path, monkeypatch
):
    from blackvue.export.osm_roads import DEFAULT_ZOOM_RADIUS_METERS

    captured = {}

    def _fake_bv_export(**kwargs):
        captured.update(kwargs)
        return 0

    monkeypatch.setattr(bv_export_module, "bv_export", _fake_bv_export)

    archive = tmp_path / "archive"
    archive.mkdir()
    target = tmp_path / "out"

    main(["--target", str(target), str(archive), "--map", "--map-zoom"])

    assert captured["map_zoom_meters"] == DEFAULT_ZOOM_RADIUS_METERS


def test_main_uses_an_explicit_map_zoom_value_when_given(tmp_path, monkeypatch):
    captured = {}

    def _fake_bv_export(**kwargs):
        captured.update(kwargs)
        return 0

    monkeypatch.setattr(bv_export_module, "bv_export", _fake_bv_export)

    archive = tmp_path / "archive"
    archive.mkdir()
    target = tmp_path / "out"

    main(["--target", str(target), str(archive), "--map", "--map-zoom", "50"])

    assert captured["map_zoom_meters"] == 50.0


def test_main_leaves_map_zoom_as_none_when_the_flag_is_absent(
    tmp_path, monkeypatch
):
    captured = {}

    def _fake_bv_export(**kwargs):
        captured.update(kwargs)
        return 0

    monkeypatch.setattr(bv_export_module, "bv_export", _fake_bv_export)

    archive = tmp_path / "archive"
    archive.mkdir()
    target = tmp_path / "out"

    main(["--target", str(target), str(archive), "--map"])

    assert captured["map_zoom_meters"] is None


def test_bv_export_stitch_flag_produces_a_composed_video(tmp_path):
    archive = tmp_path / "archive"
    archive.mkdir()
    target = tmp_path / "out"

    _make_video(archive / "20260720_100000_NF.mp4")
    _make_video(archive / "20260720_100000_NR.mp4")

    exit_code = bv_export(
        str(archive), target=str(target), stitch_layout="side_by_side",
    )

    assert exit_code == 0
    folder = target / "trip_20260720_100000_20260720_100000"
    assert (folder / "stitch.mp4").exists()


def test_bv_export_without_stitch_flag_writes_no_stitch_video(tmp_path):
    archive = tmp_path / "archive"
    archive.mkdir()
    target = tmp_path / "out"

    _make_video(archive / "20260720_100000_NF.mp4")
    _make_video(archive / "20260720_100000_NR.mp4")

    exit_code = bv_export(str(archive), target=str(target))

    assert exit_code == 0
    folder = target / "trip_20260720_100000_20260720_100000"
    assert not (folder / "stitch.mp4").exists()


def test_main_leaves_stitch_layout_as_none_when_stitch_flag_is_absent(
    tmp_path, monkeypatch
):
    captured = {}

    def _fake_bv_export(**kwargs):
        captured.update(kwargs)
        return 0

    monkeypatch.setattr(bv_export_module, "bv_export", _fake_bv_export)

    archive = tmp_path / "archive"
    archive.mkdir()
    target = tmp_path / "out"

    main(["--target", str(target), str(archive)])

    assert captured["stitch_layout"] is None


def test_main_leaves_debug_false_by_default(tmp_path, monkeypatch):
    captured = {}

    def _fake_bv_export(**kwargs):
        captured.update(kwargs)
        return 0

    monkeypatch.setattr(bv_export_module, "bv_export", _fake_bv_export)

    archive = tmp_path / "archive"
    archive.mkdir()
    target = tmp_path / "out"

    main(["--target", str(target), str(archive)])

    assert captured["debug"] is False


def test_main_sets_debug_true_when_debug_flag_given(tmp_path, monkeypatch):
    captured = {}

    def _fake_bv_export(**kwargs):
        captured.update(kwargs)
        return 0

    monkeypatch.setattr(bv_export_module, "bv_export", _fake_bv_export)

    archive = tmp_path / "archive"
    archive.mkdir()
    target = tmp_path / "out"

    main(["--target", str(target), str(archive), "--debug"])

    assert captured["debug"] is True


def test_main_uses_the_default_stitch_layout_when_stitch_flag_given(
    tmp_path, monkeypatch
):
    captured = {}

    def _fake_bv_export(**kwargs):
        captured.update(kwargs)
        return 0

    monkeypatch.setattr(bv_export_module, "bv_export", _fake_bv_export)

    archive = tmp_path / "archive"
    archive.mkdir()
    target = tmp_path / "out"

    main(["--target", str(target), str(archive), "--stitch"])

    assert captured["stitch_layout"] == "side_by_side"


def test_main_uses_an_explicit_stitch_layout_when_given(tmp_path, monkeypatch):
    captured = {}

    def _fake_bv_export(**kwargs):
        captured.update(kwargs)
        return 0

    monkeypatch.setattr(bv_export_module, "bv_export", _fake_bv_export)

    archive = tmp_path / "archive"
    archive.mkdir()
    target = tmp_path / "out"

    main([
        "--target", str(target), str(archive),
        "--stitch", "--stitch-layout", "top_down",
    ])

    assert captured["stitch_layout"] == "top_down"


def test_bv_export_stitch_resolution_flag_produces_a_scaled_down_video(
    tmp_path
):
    import json

    archive = tmp_path / "archive"
    archive.mkdir()
    target = tmp_path / "out"

    _make_video(archive / "20260720_100000_NF.mp4")
    _make_video(archive / "20260720_100000_NR.mp4")

    exit_code = bv_export(
        str(archive), target=str(target),
        stitch_layout="side_by_side", stitch_resolution=(320, 240),
    )

    assert exit_code == 0
    stitch_path = (
        target / "trip_20260720_100000_20260720_100000" / "stitch.mp4"
    )
    result = subprocess.run(
        [
            "ffprobe", "-v", "error",
            "-select_streams", "v:0",
            "-show_entries", "stream=width,height",
            "-of", "json",
            str(stitch_path),
        ],
        capture_output=True, text=True, check=True,
    )
    stream = json.loads(result.stdout)["streams"][0]
    assert (stream["width"], stream["height"]) == (320, 240)


def test_main_parses_stitch_resolution_from_the_command_line(
    tmp_path, monkeypatch
):
    captured = {}

    def _fake_bv_export(**kwargs):
        captured.update(kwargs)
        return 0

    monkeypatch.setattr(bv_export_module, "bv_export", _fake_bv_export)

    archive = tmp_path / "archive"
    archive.mkdir()
    target = tmp_path / "out"

    main([
        "--target", str(target), str(archive),
        "--stitch", "--stitch-resolution", "320x240",
        "--stitch-bitrate", "256k",
    ])

    assert captured["stitch_resolution"] == (320, 240)
    assert captured["stitch_bitrate"] == "256k"


def test_main_rejects_a_malformed_stitch_resolution(tmp_path, capsys):
    archive = tmp_path / "archive"
    archive.mkdir()
    target = tmp_path / "out"

    # argparse's own type= validation fails before run_cli() ever gets
    # control, so this is a SystemExit(2) straight out of
    # parser.parse_args(), not a normal int return.
    with pytest.raises(SystemExit) as exc_info:
        main([
            "--target", str(target), str(archive),
            "--stitch", "--stitch-resolution", "not-a-resolution",
        ])

    assert exc_info.value.code == 2
    assert "invalid resolution" in capsys.readouterr().err


def test_bv_export_a_later_plain_export_keeps_an_earlier_maps_map_video(
    tmp_path, monkeypatch
):
    # The exact scenario Christer asked about: export once with --map
    # (expensive - builds map.mp4), then export the same trip again
    # without --map, non-interactively. map.mp4 should survive.
    monkeypatch.setattr(
        trip_export_module, "load_or_fetch_roads", _fake_roads
    )

    archive = tmp_path / "archive"
    archive.mkdir()
    target = tmp_path / "out"

    _make_video(archive / "20260720_100000_NF.mp4")
    _write_gps(
        archive / "20260720_100000_N.gps",
        1784555901000, "5917.94615", "N", "01805.17070", "E",
    )
    _make_video(archive / "20260720_100010_NF.mp4")
    _write_gps(
        archive / "20260720_100010_N.gps",
        1784555911000, "5918.94615", "N", "01806.17070", "E",
    )

    bv_export(str(archive), target=str(target), render_map=True)

    folder = target / "trip_20260720_100000_20260720_100010"
    assert (folder / "map.mp4").exists()
    map_mtime = (folder / "map.mp4").stat().st_mtime

    exit_code = bv_export(str(archive), target=str(target))

    assert exit_code == 0
    assert (folder / "map.mp4").exists()
    assert (folder / "map.mp4").stat().st_mtime == map_mtime, (
        "plain re-export shouldn't touch the folder at all, let alone "
        "rebuild the expensive map"
    )


def test_bv_export_gsensor_video_flag_renders_gsensor_video(tmp_path):
    from blackvue.telemetry.gsensor_reader import GSensorSample
    from blackvue.telemetry.gsensor_reader import write_gsensor

    archive = tmp_path / "archive"
    archive.mkdir()
    target = tmp_path / "out"

    _make_video(archive / "20260720_100000_NF.mp4")
    from datetime import timedelta

    write_gsensor(
        (
            GSensorSample(offset=timedelta(seconds=0), x=0, y=0, z=900),
            GSensorSample(offset=timedelta(seconds=1), x=200, y=-100, z=950),
        ),
        archive / "20260720_100000_N.3gf",
    )

    exit_code = bv_export(str(archive), target=str(target), render_gsensor=True)

    assert exit_code == 0
    folder = target / "trip_20260720_100000_20260720_100000"
    assert (folder / "gsensor.mp4").exists()


def test_bv_export_without_gsensor_video_flag_writes_no_video(tmp_path):
    from blackvue.telemetry.gsensor_reader import GSensorSample
    from blackvue.telemetry.gsensor_reader import write_gsensor
    from datetime import timedelta

    archive = tmp_path / "archive"
    archive.mkdir()
    target = tmp_path / "out"

    _make_video(archive / "20260720_100000_NF.mp4")
    write_gsensor(
        (
            GSensorSample(offset=timedelta(seconds=0), x=0, y=0, z=900),
            GSensorSample(offset=timedelta(seconds=1), x=200, y=-100, z=950),
        ),
        archive / "20260720_100000_N.3gf",
    )

    exit_code = bv_export(str(archive), target=str(target))

    assert exit_code == 0
    folder = target / "trip_20260720_100000_20260720_100000"
    assert not (folder / "gsensor.mp4").exists()


def test_bv_export_merges_srt_across_a_trip(tmp_path):
    from blackvue.generate.speech import SpeechSegment
    from blackvue.generate.subtitles import format_srt

    archive = tmp_path / "archive"
    archive.mkdir()
    target = tmp_path / "out"

    _make_video(archive / "20260720_100000_NF.mp4")
    (archive / "20260720_100000_N.srt").write_text(
        format_srt((SpeechSegment(0.0, 1.0, "hello"),))
    )
    _make_video(archive / "20260720_100010_NF.mp4")
    (archive / "20260720_100010_N.srt").write_text(
        format_srt((SpeechSegment(0.0, 1.0, "world"),))
    )

    exit_code = bv_export(str(archive), target=str(target))

    assert exit_code == 0
    folder = target / "trip_20260720_100000_20260720_100010"
    srt_text = (folder / "trip.srt").read_text()
    assert "hello" in srt_text
    assert "world" in srt_text
    assert "00:00:10,000 --> 00:00:11,000" in srt_text


def test_bv_export_without_map_flag_never_touches_roads(tmp_path, monkeypatch):
    def _refuse(*_args, **_kwargs):
        raise AssertionError("should not fetch roads when --map is off")

    monkeypatch.setattr(trip_export_module, "load_or_fetch_roads", _refuse)

    archive = tmp_path / "archive"
    archive.mkdir()
    target = tmp_path / "out"

    _make_video(archive / "20260720_100000_NF.mp4")
    _write_gps(
        archive / "20260720_100000_N.gps",
        1784555901000, "5917.94615", "N", "01805.17070", "E",
    )

    exit_code = bv_export(str(archive), target=str(target))

    assert exit_code == 0
