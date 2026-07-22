import subprocess

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


def test_bv_export_refreshes_an_existing_folder(tmp_path):
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
    assert not stale_file.exists()
    assert (folder / "front.mp4").exists()


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
