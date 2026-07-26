import json
import subprocess

import pytest

from blackvue.cli import bv_export as bv_export_module
from blackvue.cli.bv_export import bv_export
from blackvue.cli.bv_export import main
from blackvue.export import trip_export as trip_export_module
from blackvue.export.osm_roads import Road
from blackvue.export.parking_transition import DEFAULT_PARKING_TRANSITION_CLIPS
from blackvue.export.parking_transition import RANDOM_PARKING_TRANSITION_POOL


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


def test_bv_export_does_not_bridge_a_gap_by_default(tmp_path):
    # Movement-based bridging is off by default - confirmed on a real
    # archive to have no ceiling on how large a gap it'll bridge (a
    # single GPS speed reading bridged a genuine 6-day gap into one
    # trip). Even with real movement evidence at the edge of the gap,
    # a bare bv_export() call should still split on --max-gap alone.
    archive = tmp_path / "archive"
    archive.mkdir()
    target = tmp_path / "out"

    _make_video(archive / "20260720_100000_NF.mp4")
    (archive / "20260720_100000_N.gps").write_text(
        "[1700000000000]$GPRMC,120000.00,A,4807.038,N,01131.000,E,"
        "30.00,45.00,010124,,,A*6D\n"
    )
    _make_video(archive / "20260720_103000_NF.mp4")

    bv_export(str(archive), target=str(target))

    assert (target / "trip_20260720_100000_20260720_100000").is_dir()
    assert (target / "trip_20260720_103000_20260720_103000").is_dir()
    assert not (
        target / "trip_20260720_100000_20260720_103000"
    ).exists()


def test_bv_export_movement_flag_bridges_a_gap_with_gps_evidence(tmp_path):
    archive = tmp_path / "archive"
    archive.mkdir()
    target = tmp_path / "out"

    _make_video(archive / "20260720_100000_NF.mp4")
    (archive / "20260720_100000_N.gps").write_text(
        "[1700000000000]$GPRMC,120000.00,A,4807.038,N,01131.000,E,"
        "30.00,45.00,010124,,,A*6D\n"
    )
    _make_video(archive / "20260720_103000_NF.mp4")

    bv_export(str(archive), target=str(target), movement=True)

    assert (target / "trip_20260720_100000_20260720_103000").is_dir()


def test_main_movement_flag_bridges_a_gap_with_gps_evidence(tmp_path):
    archive = tmp_path / "archive"
    archive.mkdir()
    target = tmp_path / "out"

    _make_video(archive / "20260720_100000_NF.mp4")
    (archive / "20260720_100000_N.gps").write_text(
        "[1700000000000]$GPRMC,120000.00,A,4807.038,N,01131.000,E,"
        "30.00,45.00,010124,,,A*6D\n"
    )
    _make_video(archive / "20260720_103000_NF.mp4")

    exit_code = main([str(archive), "--target", str(target), "--movement"])

    assert exit_code == 0
    assert (target / "trip_20260720_100000_20260720_103000").is_dir()


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


def test_bv_export_only_self_heals_recordings_in_the_exported_trip(tmp_path):
    archive = tmp_path / "archive"
    archive.mkdir()
    target = tmp_path / "out"

    _make_video(archive / "20260720_100000_NF.mp4")
    _make_video(archive / "20260720_100200_NF.mp4")
    _make_video(archive / "20260720_120000_NF.mp4")

    # --timestamp scopes this run to the first trip only (the second,
    # two hours later, is well outside the default 5-minute max_gap).
    bv_export(str(archive), target=str(target), timestamp="20260720_1000")

    assert (archive / "20260720_100000_N.duration.txt").exists()
    assert (archive / "20260720_100200_N.duration.txt").exists()
    assert not (archive / "20260720_120000_N.duration.txt").exists()


def test_bv_export_duration_heal_archive_heals_every_recording(tmp_path):
    archive = tmp_path / "archive"
    archive.mkdir()
    target = tmp_path / "out"

    _make_video(archive / "20260720_100000_NF.mp4")
    _make_video(archive / "20260720_120000_NF.mp4")

    bv_export(
        str(archive), target=str(target), timestamp="20260720_1000",
        duration_heal_archive=True,
    )

    assert (archive / "20260720_100000_N.duration.txt").exists()
    # Not part of the exported trip, but --duration-heal-archive widens
    # the self-heal beyond just the trip being exported - this small,
    # two-recording archive happens to fit entirely inside
    # build_for_interval()'s own bounded search window on its first
    # pass (see the *_is_bounded_not_archive_wide test below for the
    # case where it doesn't), so both still get healed here.
    assert (archive / "20260720_120000_N.duration.txt").exists()


def test_bv_export_duration_heal_archive_is_bounded_not_archive_wide(tmp_path):
    # --duration-heal-archive used to mean a real archive-wide ffprobe
    # pass, every recording, regardless of how small the actual
    # request was - it's now bounded to whatever build_for_interval()
    # 's own search actually settles on (see trip_builder.py), so a
    # trip far away from the requested one should come out unhealed.
    from datetime import datetime, timedelta

    archive = tmp_path / "archive"
    archive.mkdir()
    target = tmp_path / "out"

    # Ten separate trips - a real, unrelated 30-minute gap (well over
    # the default 5-minute max_gap) between each one - only the first
    # of which is actually requested.
    start = datetime(2026, 7, 20, 10, 0, 0)
    stamps = [
        (start + timedelta(minutes=30 * i)).strftime("%Y%m%d_%H%M%S")
        for i in range(10)
    ]
    for stamp in stamps:
        _make_video(archive / f"{stamp}_NF.mp4")

    bv_export(
        str(archive), target=str(target), timestamp=stamps[0],
        duration_heal_archive=True,
    )

    assert (archive / f"{stamps[0]}_N.duration.txt").exists()
    assert not (archive / f"{stamps[-1]}_N.duration.txt").exists()


def test_bv_export_rejects_duration_heal_archive_with_no_duration(tmp_path):
    archive = tmp_path / "archive"
    archive.mkdir()
    target = tmp_path / "out"

    _make_video(archive / "20260720_100000_NF.mp4")

    with pytest.raises(SystemExit):
        bv_export(
            str(archive), target=str(target),
            duration=False, duration_heal_archive=True,
        )


def test_bv_export_gap_tolerance_can_be_tightened(tmp_path):
    archive = tmp_path / "archive"
    archive.mkdir()
    target = tmp_path / "out"

    _make_video(archive / "20260720_100000_NF.mp4")
    _make_video(archive / "20260720_101005_NF.mp4")

    bv_export(str(archive), target=str(target), gap_tolerance_seconds=0)

    assert (target / "trip_20260720_100000_20260720_100000").is_dir()
    assert (target / "trip_20260720_101005_20260720_101005").is_dir()


def test_bv_export_timestamp_filter_exports_the_whole_trip_it_overlaps(
    tmp_path
):
    # A --timestamp narrow enough to match only the *middle* recording
    # of a 3-recording trip must still export the trip in full - not
    # just the one matching recording. Trips are detected across the
    # whole archive first, then kept if any of their own recordings
    # fall in the requested range; filtering recordings by the range
    # *before* trip detection (the original approach) would have
    # silently truncated this trip to just its middle recording.
    archive = tmp_path / "archive"
    archive.mkdir()
    target = tmp_path / "out"

    _make_video(archive / "20260720_100000_NF.mp4")
    _make_video(archive / "20260720_100200_NF.mp4")
    _make_video(archive / "20260720_100400_NF.mp4")

    # Exact match on the middle recording's own timestamp - as narrow
    # a --timestamp as this archive allows.
    bv_export(str(archive), target=str(target), timestamp="20260720_100200")

    assert (target / "trip_20260720_100000_20260720_100400").is_dir()
    assert not (target / "trip_20260720_100200_20260720_100200").exists()


def test_bv_export_timestamp_filter_excludes_trips_that_dont_overlap(
    tmp_path
):
    # A trip entirely outside the requested range is still excluded -
    # exporting the whole overlapping trip doesn't mean exporting
    # everything.
    archive = tmp_path / "archive"
    archive.mkdir()
    target = tmp_path / "out"

    _make_video(archive / "20260720_100000_NF.mp4")
    _make_video(archive / "20260720_120000_NF.mp4")

    bv_export(str(archive), target=str(target), timestamp="20260720_1000")

    assert (target / "trip_20260720_100000_20260720_100000").is_dir()
    assert not (target / "trip_20260720_120000_20260720_120000").exists()


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


def test_main_leaves_duration_heal_archive_false_by_default(tmp_path, monkeypatch):
    captured = {}

    def _fake_bv_export(**kwargs):
        captured.update(kwargs)
        return 0

    monkeypatch.setattr(bv_export_module, "bv_export", _fake_bv_export)

    archive = tmp_path / "archive"
    archive.mkdir()
    target = tmp_path / "out"

    main(["--target", str(target), str(archive)])

    assert captured["duration_heal_archive"] is False


def test_main_sets_duration_heal_archive_true_when_flag_given(
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

    main(["--target", str(target), str(archive), "--duration-heal-archive"])

    assert captured["duration_heal_archive"] is True


def test_main_leaves_include_parking_false_by_default(tmp_path, monkeypatch):
    captured = {}

    def _fake_bv_export(**kwargs):
        captured.update(kwargs)
        return 0

    monkeypatch.setattr(bv_export_module, "bv_export", _fake_bv_export)

    archive = tmp_path / "archive"
    archive.mkdir()
    target = tmp_path / "out"

    main(["--target", str(target), str(archive)])

    assert captured["include_parking"] is False
    assert captured["parking_transition_image"] is None
    assert captured["parking_transition_clip"] is None
    assert captured["parking_transition_random"] is False


def test_main_sets_include_parking_true_when_flag_given(tmp_path, monkeypatch):
    captured = {}

    def _fake_bv_export(**kwargs):
        captured.update(kwargs)
        return 0

    monkeypatch.setattr(bv_export_module, "bv_export", _fake_bv_export)

    archive = tmp_path / "archive"
    archive.mkdir()
    target = tmp_path / "out"

    main(["--target", str(target), str(archive), "--include-parking"])

    assert captured["include_parking"] is True


def test_main_forwards_parking_transition_image(tmp_path, monkeypatch):
    captured = {}

    def _fake_bv_export(**kwargs):
        captured.update(kwargs)
        return 0

    monkeypatch.setattr(bv_export_module, "bv_export", _fake_bv_export)

    archive = tmp_path / "archive"
    archive.mkdir()
    target = tmp_path / "out"
    icon = tmp_path / "no_parking.png"

    main([
        "--target", str(target), str(archive),
        "--parking-transition-image", str(icon),
    ])

    assert captured["parking_transition_image"] == str(icon)


def test_main_forwards_parking_transition_clip(tmp_path, monkeypatch):
    captured = {}

    def _fake_bv_export(**kwargs):
        captured.update(kwargs)
        return 0

    monkeypatch.setattr(bv_export_module, "bv_export", _fake_bv_export)

    archive = tmp_path / "archive"
    archive.mkdir()
    target = tmp_path / "out"
    clip = tmp_path / "no_parking.mp4"

    main([
        "--target", str(target), str(archive),
        "--parking-transition-clip", str(clip),
    ])

    assert captured["parking_transition_clip"] == str(clip)


def test_bv_export_rejects_parking_transition_image_with_clip_together(tmp_path):
    archive = tmp_path / "archive"
    archive.mkdir()
    target = tmp_path / "out"

    with pytest.raises(SystemExit):
        bv_export(
            str(archive), target=str(target),
            parking_transition_image=str(tmp_path / "image.png"),
            parking_transition_clip=str(tmp_path / "clip.mp4"),
        )


def test_bv_export_rejects_parking_transition_random_with_image_together(
    tmp_path,
):
    archive = tmp_path / "archive"
    archive.mkdir()
    target = tmp_path / "out"

    with pytest.raises(SystemExit):
        bv_export(
            str(archive), target=str(target),
            parking_transition_image=str(tmp_path / "image.png"),
            parking_transition_random=True,
        )


def test_bv_export_rejects_parking_transition_random_with_clip_together(
    tmp_path,
):
    archive = tmp_path / "archive"
    archive.mkdir()
    target = tmp_path / "out"

    with pytest.raises(SystemExit):
        bv_export(
            str(archive), target=str(target),
            parking_transition_clip=str(tmp_path / "clip.mp4"),
            parking_transition_random=True,
        )


def test_main_leaves_parking_transition_random_false_by_default(
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

    assert captured["parking_transition_random"] is False


def test_main_sets_parking_transition_random_true_when_flag_given(
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
        "--parking-transition-random",
    ])

    assert captured["parking_transition_random"] is True


def _parking_archive(archive):
    # A minimal on-disk archive - drive, park, drive - just enough for
    # TripBuilder to detect one trip with a mid-trip Parking recording.
    # export_trip() itself is monkeypatched by every test below that
    # uses this (to just capture its own kwargs), so these clips never
    # actually get rendered/looked at - they only need to exist and
    # parse as a valid trip.
    _make_video(archive / "20260720_100000_NF.mp4")
    _make_video(archive / "20260720_100010_PF.mp4")
    _make_video(archive / "20260720_100020_NF.mp4")


def test_bv_export_leaves_the_plain_frame_by_default(tmp_path, monkeypatch):
    # Reverted from an earlier session's "random by default" design -
    # Christer, on reflection: "do you think i was carried away" -
    # concluded a personal export tool showing a different one of his
    # own clips each run, unannounced, wasn't the kind of predictable
    # behaviour a default should have. Plain frame by default now,
    # matching export_trip()'s own neutral default exactly - random.
    # choice() must never even be called without an explicit
    # --parking-transition-random.
    archive = tmp_path / "archive"
    archive.mkdir()
    target = tmp_path / "out"
    _parking_archive(archive)

    captured_calls = []

    def _fake_export_trip(trip, folder, **kwargs):
        captured_calls.append(kwargs)
        return trip_export_module.ExportResult()

    monkeypatch.setattr(bv_export_module, "export_trip", _fake_export_trip)

    def _fail_choice(pool):
        raise AssertionError(
            "random.choice() should not be called without "
            "--parking-transition-random"
        )

    monkeypatch.setattr(bv_export_module.random, "choice", _fail_choice)

    exit_code = bv_export(str(archive), target=str(target))

    assert exit_code == 0
    assert len(captured_calls) == 1
    assert captured_calls[0]["parking_transition_clip"] is None
    assert captured_calls[0]["parking_transition_image"] is None


def test_bv_export_picks_a_random_bundled_clip_when_random_flag_given(
    tmp_path, monkeypatch
):
    archive = tmp_path / "archive"
    archive.mkdir()
    target = tmp_path / "out"
    _parking_archive(archive)

    captured_calls = []

    def _fake_export_trip(trip, folder, **kwargs):
        captured_calls.append(kwargs)
        return trip_export_module.ExportResult()

    monkeypatch.setattr(bv_export_module, "export_trip", _fake_export_trip)

    picked_pools = []

    def _fake_choice(pool):
        picked_pools.append(pool)
        return pool[0]

    monkeypatch.setattr(bv_export_module.random, "choice", _fake_choice)

    exit_code = bv_export(
        str(archive), target=str(target), parking_transition_random=True
    )

    assert exit_code == 0
    assert len(captured_calls) == 1
    # random.choice() was drawn from exactly the full 7-entry pool (six
    # clips + the procedural frame's own None sentinel), not some other
    # list.
    assert picked_pools == [RANDOM_PARKING_TRANSITION_POOL]
    assert (
        captured_calls[0]["parking_transition_clip"]
        == DEFAULT_PARKING_TRANSITION_CLIPS[0]
    )
    # No override given, so image stays unset - the clip pick doesn't
    # also set the image.
    assert captured_calls[0]["parking_transition_image"] is None


def test_bv_export_random_pool_can_land_on_the_procedural_frame(
    tmp_path, monkeypatch
):
    # Christer: "I think your clip should be in there to[o]" - the plain
    # procedural frame (RANDOM_PARKING_TRANSITION_POOL's own `None`
    # entry) has to be a real possible outcome of the random pick, not
    # just something reachable via an explicit opt-out.
    archive = tmp_path / "archive"
    archive.mkdir()
    target = tmp_path / "out"
    _parking_archive(archive)

    captured_calls = []

    def _fake_export_trip(trip, folder, **kwargs):
        captured_calls.append(kwargs)
        return trip_export_module.ExportResult()

    monkeypatch.setattr(bv_export_module, "export_trip", _fake_export_trip)

    def _fake_choice(pool):
        return None

    monkeypatch.setattr(bv_export_module.random, "choice", _fake_choice)

    exit_code = bv_export(
        str(archive), target=str(target), parking_transition_random=True
    )

    assert exit_code == 0
    # None all the way through - export_trip() sees neither a clip nor
    # an image, which is exactly what already makes it draw the plain
    # procedural frame itself. No special-casing needed for this
    # outcome.
    assert captured_calls[0]["parking_transition_clip"] is None
    assert captured_calls[0]["parking_transition_image"] is None


def test_bv_export_skips_the_random_pick_when_parking_transition_image_given(
    tmp_path, monkeypatch
):
    archive = tmp_path / "archive"
    archive.mkdir()
    target = tmp_path / "out"
    _parking_archive(archive)

    captured_calls = []

    def _fake_export_trip(trip, folder, **kwargs):
        captured_calls.append(kwargs)
        return trip_export_module.ExportResult()

    monkeypatch.setattr(bv_export_module, "export_trip", _fake_export_trip)

    def _fail_choice(pool):
        raise AssertionError(
            "random.choice() should not be called when "
            "--parking-transition-image is given"
        )

    monkeypatch.setattr(bv_export_module.random, "choice", _fail_choice)

    image = tmp_path / "no_parking.png"

    exit_code = bv_export(
        str(archive), target=str(target),
        parking_transition_image=str(image),
    )

    assert exit_code == 0
    assert captured_calls[0]["parking_transition_clip"] is None
    assert captured_calls[0]["parking_transition_image"] == image


def test_bv_export_skips_the_random_pick_when_parking_transition_clip_given(
    tmp_path, monkeypatch
):
    archive = tmp_path / "archive"
    archive.mkdir()
    target = tmp_path / "out"
    _parking_archive(archive)

    captured_calls = []

    def _fake_export_trip(trip, folder, **kwargs):
        captured_calls.append(kwargs)
        return trip_export_module.ExportResult()

    monkeypatch.setattr(bv_export_module, "export_trip", _fake_export_trip)

    def _fail_choice(pool):
        raise AssertionError(
            "random.choice() should not be called when "
            "--parking-transition-clip is given"
        )

    monkeypatch.setattr(bv_export_module.random, "choice", _fail_choice)

    clip = tmp_path / "my_own_clip.mp4"

    exit_code = bv_export(
        str(archive), target=str(target),
        parking_transition_clip=str(clip),
    )

    assert exit_code == 0
    assert captured_calls[0]["parking_transition_clip"] == clip


def test_bv_export_skips_the_random_pick_when_include_parking_is_true(
    tmp_path, monkeypatch
):
    archive = tmp_path / "archive"
    archive.mkdir()
    target = tmp_path / "out"
    _parking_archive(archive)

    captured_calls = []

    def _fake_export_trip(trip, folder, **kwargs):
        captured_calls.append(kwargs)
        return trip_export_module.ExportResult()

    monkeypatch.setattr(bv_export_module, "export_trip", _fake_export_trip)

    def _fail_choice(pool):
        raise AssertionError(
            "random.choice() should not be called when --include-parking "
            "is given - nothing is ever transitioned/skipped"
        )

    monkeypatch.setattr(bv_export_module.random, "choice", _fail_choice)

    exit_code = bv_export(
        str(archive), target=str(target), include_parking=True
    )

    assert exit_code == 0
    assert captured_calls[0]["parking_transition_clip"] is None


def test_bv_export_picks_a_fresh_random_clip_per_trip(tmp_path, monkeypatch):
    # Two separate trips (a real gap - well past the default --max-gap
    # - between them so TripBuilder detects two, not one), each with
    # its own mid-trip Parking recording - the random pick should be
    # re-drawn for the second trip rather than reusing the first trip's
    # own choice.
    archive = tmp_path / "archive"
    archive.mkdir()
    target = tmp_path / "out"

    _make_video(archive / "20260720_100000_NF.mp4")
    _make_video(archive / "20260720_100010_PF.mp4")
    _make_video(archive / "20260720_100020_NF.mp4")

    _make_video(archive / "20260720_120000_NF.mp4")
    _make_video(archive / "20260720_120010_PF.mp4")
    _make_video(archive / "20260720_120020_NF.mp4")

    captured_calls = []

    def _fake_export_trip(trip, folder, **kwargs):
        captured_calls.append(kwargs)
        return trip_export_module.ExportResult()

    monkeypatch.setattr(bv_export_module, "export_trip", _fake_export_trip)

    # Cycle through two distinct clips, one per call, so the two trips
    # can't coincidentally land on the same pick.
    choices = iter(DEFAULT_PARKING_TRANSITION_CLIPS[:2])

    def _fake_choice(pool):
        return next(choices)

    monkeypatch.setattr(bv_export_module.random, "choice", _fake_choice)

    exit_code = bv_export(
        str(archive), target=str(target), parking_transition_random=True
    )

    assert exit_code == 0
    assert len(captured_calls) == 2
    picks = [call["parking_transition_clip"] for call in captured_calls]
    assert picks == list(DEFAULT_PARKING_TRANSITION_CLIPS[:2])


def test_main_uses_the_default_stitch_layout_when_stitch_flag_given(
    tmp_path, monkeypatch
):
    # Default changed from a fixed 'side_by_side' to the 'auto' sentinel
    # once layout auto-picking existed - export_trip() is what resolves
    # 'auto' to a concrete layout from the trip's own GPS shape (see
    # test_trip_export.py's own auto-pick tests), not this CLI layer.
    captured = {}

    def _fake_bv_export(**kwargs):
        captured.update(kwargs)
        return 0

    monkeypatch.setattr(bv_export_module, "bv_export", _fake_bv_export)

    archive = tmp_path / "archive"
    archive.mkdir()
    target = tmp_path / "out"

    main(["--target", str(target), str(archive), "--stitch"])

    assert captured["stitch_layout"] == "auto"


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


def test_main_accepts_rearview_mirror_as_a_stitch_layout(tmp_path, monkeypatch):
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
        "--stitch", "--stitch-layout", "rearview_mirror",
    ])

    assert captured["stitch_layout"] == "rearview_mirror"


def test_main_uses_the_default_stitch_mirror_size_when_not_given(
    tmp_path, monkeypatch
):
    # bv-export's CLI deliberately overrides stitch.py's own plain/neutral
    # DEFAULT_MIRROR_SIZE_PERCENT with its own opinionated
    # _DEFAULT_CLI_MIRROR_SIZE_PERCENT (see that constant's own comment,
    # task #89) - main()'s own default has to match the CLI's constant,
    # not the library's.
    captured = {}

    def _fake_bv_export(**kwargs):
        captured.update(kwargs)
        return 0

    monkeypatch.setattr(bv_export_module, "bv_export", _fake_bv_export)

    archive = tmp_path / "archive"
    archive.mkdir()
    target = tmp_path / "out"

    main(["--target", str(target), str(archive), "--stitch"])

    assert (
        captured["stitch_mirror_size"]
        == bv_export_module._DEFAULT_CLI_MIRROR_SIZE_PERCENT
    )


def test_main_parses_an_explicit_stitch_mirror_size(tmp_path, monkeypatch):
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
        "--stitch", "--stitch-layout", "rearview_mirror",
        "--stitch-mirror-size", "40",
    ])

    assert captured["stitch_mirror_size"] == 40.0


def test_main_rejects_an_out_of_range_stitch_mirror_size(tmp_path):
    archive = tmp_path / "archive"
    archive.mkdir()
    target = tmp_path / "out"

    with pytest.raises(SystemExit):
        main([
            "--target", str(target), str(archive),
            "--stitch", "--stitch-mirror-size", "99",
        ])


def test_main_uses_the_default_stitch_mirror_radius_when_not_given(
    tmp_path, monkeypatch
):
    from blackvue.export.stitch import DEFAULT_MIRROR_RADIUS_PERCENT

    captured = {}

    def _fake_bv_export(**kwargs):
        captured.update(kwargs)
        return 0

    monkeypatch.setattr(bv_export_module, "bv_export", _fake_bv_export)

    archive = tmp_path / "archive"
    archive.mkdir()
    target = tmp_path / "out"

    main(["--target", str(target), str(archive), "--stitch"])

    assert captured["stitch_mirror_radius"] == DEFAULT_MIRROR_RADIUS_PERCENT


def test_main_parses_an_explicit_stitch_mirror_radius(tmp_path, monkeypatch):
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
        "--stitch", "--stitch-layout", "rearview_mirror",
        "--stitch-mirror-radius", "50",
    ])

    assert captured["stitch_mirror_radius"] == 50.0


def test_main_rejects_an_out_of_range_stitch_mirror_radius(tmp_path):
    archive = tmp_path / "archive"
    archive.mkdir()
    target = tmp_path / "out"

    with pytest.raises(SystemExit):
        main([
            "--target", str(target), str(archive),
            "--stitch", "--stitch-mirror-radius", "150",
        ])


def test_main_uses_the_default_stitch_mirror_zoom_when_not_given(
    tmp_path, monkeypatch
):
    # Same reasoning as the mirror-size test above - bv-export's CLI
    # overrides stitch.py's neutral DEFAULT_MIRROR_ZOOM_PERCENT with its
    # own _DEFAULT_CLI_MIRROR_ZOOM_PERCENT (task #89).
    captured = {}

    def _fake_bv_export(**kwargs):
        captured.update(kwargs)
        return 0

    monkeypatch.setattr(bv_export_module, "bv_export", _fake_bv_export)

    archive = tmp_path / "archive"
    archive.mkdir()
    target = tmp_path / "out"

    main(["--target", str(target), str(archive), "--stitch"])

    assert (
        captured["stitch_mirror_zoom"]
        == bv_export_module._DEFAULT_CLI_MIRROR_ZOOM_PERCENT
    )


def test_main_parses_an_explicit_stitch_mirror_zoom(tmp_path, monkeypatch):
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
        "--stitch", "--stitch-layout", "rearview_mirror",
        "--stitch-mirror-zoom", "40",
    ])

    assert captured["stitch_mirror_zoom"] == 40.0


def test_main_rejects_an_out_of_range_stitch_mirror_zoom(tmp_path):
    archive = tmp_path / "archive"
    archive.mkdir()
    target = tmp_path / "out"

    with pytest.raises(SystemExit):
        main([
            "--target", str(target), str(archive),
            "--stitch", "--stitch-mirror-zoom", "100",
        ])


def test_main_uses_the_default_stitch_mirror_pan_when_not_given(
    tmp_path, monkeypatch
):
    # pan_x is unchanged from stitch.py's own neutral default (Christer
    # only asked for pan_y to change - see
    # _DEFAULT_CLI_MIRROR_PAN_Y_PERCENT's own comment), so it's still
    # checked against the library constant; pan_y is bv-export's own
    # opinionated CLI default (task #89) and has to be checked against
    # that instead.
    from blackvue.export.stitch import DEFAULT_MIRROR_PAN_X_PERCENT

    captured = {}

    def _fake_bv_export(**kwargs):
        captured.update(kwargs)
        return 0

    monkeypatch.setattr(bv_export_module, "bv_export", _fake_bv_export)

    archive = tmp_path / "archive"
    archive.mkdir()
    target = tmp_path / "out"

    main(["--target", str(target), str(archive), "--stitch"])

    assert captured["stitch_mirror_pan_x"] == DEFAULT_MIRROR_PAN_X_PERCENT
    assert (
        captured["stitch_mirror_pan_y"]
        == bv_export_module._DEFAULT_CLI_MIRROR_PAN_Y_PERCENT
    )


def test_main_parses_explicit_stitch_mirror_pan_x_and_pan_y(
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
        "--stitch", "--stitch-layout", "rearview_mirror",
        "--stitch-mirror-pan-x", "-40", "--stitch-mirror-pan-y", "75",
    ])

    assert captured["stitch_mirror_pan_x"] == -40.0
    assert captured["stitch_mirror_pan_y"] == 75.0


def test_main_rejects_an_out_of_range_stitch_mirror_pan_x(tmp_path):
    archive = tmp_path / "archive"
    archive.mkdir()
    target = tmp_path / "out"

    with pytest.raises(SystemExit):
        main([
            "--target", str(target), str(archive),
            "--stitch", "--stitch-mirror-pan-x", "150",
        ])


def test_main_rejects_an_out_of_range_stitch_mirror_pan_y(tmp_path):
    archive = tmp_path / "archive"
    archive.mkdir()
    target = tmp_path / "out"

    with pytest.raises(SystemExit):
        main([
            "--target", str(target), str(archive),
            "--stitch", "--stitch-mirror-pan-y", "-150",
        ])


def test_bv_export_stitch_mirror_pan_flags_produce_a_video(tmp_path):
    archive = tmp_path / "archive"
    archive.mkdir()
    target = tmp_path / "out"

    _make_video(archive / "20260720_100000_NF.mp4")
    _make_video(archive / "20260720_100000_NR.mp4")

    exit_code = bv_export(
        str(archive), target=str(target),
        stitch_layout="rearview_mirror", stitch_mirror_zoom=50.0,
        stitch_mirror_pan_x=-100.0, stitch_mirror_pan_y=0.0,
    )

    assert exit_code == 0
    trip_folder = target / "trip_20260720_100000_20260720_100000"
    assert (trip_folder / "stitch.mp4").exists()


def test_bv_export_stitch_rearview_mirror_flag_produces_a_video(tmp_path):
    archive = tmp_path / "archive"
    archive.mkdir()
    target = tmp_path / "out"

    _make_video(archive / "20260720_100000_NF.mp4")
    _make_video(archive / "20260720_100000_NR.mp4")

    exit_code = bv_export(
        str(archive), target=str(target),
        stitch_layout="rearview_mirror", stitch_mirror_size=40.0,
    )

    assert exit_code == 0
    trip_folder = target / "trip_20260720_100000_20260720_100000"
    assert (trip_folder / "stitch.mp4").exists()


def test_main_uses_the_default_stitch_mirror_icon_when_not_given(
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

    assert captured["stitch_mirror_icon"] is None


def test_main_parses_an_explicit_stitch_mirror_icon(tmp_path, monkeypatch):
    captured = {}

    def _fake_bv_export(**kwargs):
        captured.update(kwargs)
        return 0

    monkeypatch.setattr(bv_export_module, "bv_export", _fake_bv_export)

    archive = tmp_path / "archive"
    archive.mkdir()
    target = tmp_path / "out"
    icon_path = tmp_path / "mirror.png"

    main([
        "--target", str(target), str(archive),
        "--stitch", "--stitch-layout", "rearview_mirror",
        "--stitch-mirror-icon", str(icon_path),
    ])

    assert captured["stitch_mirror_icon"] == str(icon_path)


def test_bv_export_stitch_mirror_icon_flag_composites_a_photo(tmp_path):
    from PIL import Image
    from PIL import ImageDraw

    archive = tmp_path / "archive"
    archive.mkdir()
    target = tmp_path / "out"

    _make_video(archive / "20260720_100000_NF.mp4")
    _make_video(archive / "20260720_100000_NR.mp4")

    icon_path = tmp_path / "mirror.png"
    image = Image.new("RGB", (40, 40), (0, 0, 0))
    ImageDraw.Draw(image).rectangle((10, 10, 29, 29), fill=(255, 255, 255))
    image.save(icon_path)

    exit_code = bv_export(
        str(archive), target=str(target),
        stitch_layout="rearview_mirror", stitch_mirror_icon=str(icon_path),
    )

    assert exit_code == 0
    trip_folder = target / "trip_20260720_100000_20260720_100000"
    assert (trip_folder / "stitch.mp4").exists()


def test_bv_export_stitch_mirror_icon_flag_warns_instead_of_failing_on_a_bad_path(
    tmp_path
):
    archive = tmp_path / "archive"
    archive.mkdir()
    target = tmp_path / "out"

    _make_video(archive / "20260720_100000_NF.mp4")
    _make_video(archive / "20260720_100000_NR.mp4")

    exit_code = bv_export(
        str(archive), target=str(target),
        stitch_layout="rearview_mirror",
        stitch_mirror_icon=str(tmp_path / "does-not-exist.png"),
    )

    # A bad --stitch-mirror-icon degrades to a warning (falls back to
    # the plain procedural inset) rather than failing the export.
    assert exit_code == 0
    trip_folder = target / "trip_20260720_100000_20260720_100000"
    assert (trip_folder / "stitch.mp4").exists()


def test_main_defaults_stitch_layout_to_auto(tmp_path, monkeypatch):
    captured = {}

    def _fake_bv_export(**kwargs):
        captured.update(kwargs)
        return 0

    monkeypatch.setattr(bv_export_module, "bv_export", _fake_bv_export)

    archive = tmp_path / "archive"
    archive.mkdir()
    target = tmp_path / "out"

    main(["--target", str(target), str(archive), "--stitch"])

    assert captured["stitch_layout"] == "auto"


def _video_size(path):
    import json

    result = subprocess.run(
        [
            "ffprobe", "-v", "error",
            "-select_streams", "v:0",
            "-show_entries", "stream=width,height",
            "-of", "json",
            str(path),
        ],
        capture_output=True, text=True, check=True,
    )
    stream = json.loads(result.stdout)["streams"][0]
    return stream["width"], stream["height"]


def test_bv_export_stitch_without_stitch_layout_auto_picks_from_gps(
    tmp_path
):
    # A real end-to-end run of the default 'auto' --stitch-layout, no
    # --stitch-layout given at all - front+rear plus a sharply
    # east-west GPS shape should land on side_by_side, same as if
    # --stitch-layout side_by_side had been passed explicitly.
    archive = tmp_path / "archive"
    archive.mkdir()
    target = tmp_path / "out"

    _make_video(archive / "20260720_100000_NF.mp4")
    _make_video(archive / "20260720_100000_NR.mp4")
    _write_gps(
        archive / "20260720_100000_N.gps",
        1784555901000, "5917.94615", "N", "01805.17070", "E",
    )
    _make_video(archive / "20260720_100010_NF.mp4")
    _write_gps(
        archive / "20260720_100010_N.gps",
        1784555911000, "5917.94715", "N", "01905.17070", "E",
    )

    exit_code = main([
        "--target", str(target), str(archive), "--stitch",
    ])

    assert exit_code == 0
    # Both recordings (100000, 100010) share one trip - the folder
    # name spans both.
    stitch_path = (
        target / "trip_20260720_100000_20260720_100010" / "stitch.mp4"
    )
    # side_by_side hstacks two 64x64 cameras - combined width doubles.
    assert _video_size(stitch_path) == (128, 64)


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


def test_main_parses_stitch_scale_and_max_dimensions(tmp_path, monkeypatch):
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
        "--stitch", "--stitch-scale", "50",
        "--stitch-max-width", "1920", "--stitch-max-height", "1080",
    ])

    assert captured["stitch_scale"] == 50.0
    assert captured["stitch_max_width"] == 1920
    assert captured["stitch_max_height"] == 1080


def test_main_leaves_stitch_scale_and_max_dimensions_none_by_default(
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

    assert captured["stitch_scale"] is None
    assert captured["stitch_max_width"] is None
    assert captured["stitch_max_height"] is None


def test_main_rejects_an_out_of_range_stitch_scale(tmp_path):
    archive = tmp_path / "archive"
    archive.mkdir()
    target = tmp_path / "out"

    with pytest.raises(SystemExit):
        main([
            "--target", str(target), str(archive),
            "--stitch", "--stitch-scale", "150",
        ])


def test_main_rejects_a_zero_stitch_max_width(tmp_path):
    archive = tmp_path / "archive"
    archive.mkdir()
    target = tmp_path / "out"

    with pytest.raises(SystemExit):
        main([
            "--target", str(target), str(archive),
            "--stitch", "--stitch-max-width", "0",
        ])


def test_main_leaves_stitch_map_as_none_when_stitch_flag_is_absent(
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

    # --stitch-map given without --stitch itself - same "only means
    # anything together with --stitch" convention as --stitch-layout/
    # --stitch-resolution/--stitch-bitrate.
    main(["--target", str(target), str(archive), "--stitch-map"])

    assert captured["stitch_map"] is None


def test_main_uses_the_default_stitch_map_mode_when_bare_flag_given(
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

    main(["--target", str(target), str(archive), "--stitch", "--stitch-map"])

    assert captured["stitch_map"] == "map"


def test_main_parses_an_explicit_stitch_map_mode_and_side(tmp_path, monkeypatch):
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
        "--stitch", "--stitch-map", "zoom", "--stitch-map-side", "right",
    ])

    assert captured["stitch_map"] == "zoom"
    assert captured["stitch_map_side"] == "right"


def test_main_parses_stitch_map_size(tmp_path, monkeypatch):
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
        "--stitch", "--stitch-map", "--stitch-map-size", "35",
    ])

    assert captured["stitch_map_size"] == 35.0


def test_main_leaves_stitch_map_size_none_by_default(tmp_path, monkeypatch):
    captured = {}

    def _fake_bv_export(**kwargs):
        captured.update(kwargs)
        return 0

    monkeypatch.setattr(bv_export_module, "bv_export", _fake_bv_export)

    archive = tmp_path / "archive"
    archive.mkdir()
    target = tmp_path / "out"

    main(["--target", str(target), str(archive), "--stitch", "--stitch-map"])

    assert captured["stitch_map_size"] is None


def test_main_rejects_an_out_of_range_stitch_map_size(tmp_path):
    archive = tmp_path / "archive"
    archive.mkdir()
    target = tmp_path / "out"

    with pytest.raises(SystemExit):
        main([
            "--target", str(target), str(archive),
            "--stitch", "--stitch-map", "--stitch-map-size", "99",
        ])


def test_main_leaves_stitch_gsensor_false_when_stitch_flag_is_absent(
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

    main(["--target", str(target), str(archive), "--stitch-gsensor"])

    assert captured["stitch_gsensor"] is False


def test_main_parses_stitch_gsensor_flags(tmp_path, monkeypatch):
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
        "--stitch", "--stitch-gsensor", "--stitch-gsensor-size", "25",
        "--stitch-gsensor-pos", "top-right",
    ])

    assert captured["stitch_gsensor"] is True
    assert captured["stitch_gsensor_size"] == 25.0
    assert captured["stitch_gsensor_pos"] == "top-right"
    assert captured["stitch_gsensor_xy"] is None


def test_main_parses_stitch_gsensor_xy(tmp_path, monkeypatch):
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
        "--stitch", "--stitch-gsensor", "--stitch-gsensor-xy", "80,10",
    ])

    assert captured["stitch_gsensor_xy"] == (80.0, 10.0)
    assert captured["stitch_gsensor_pos"] is None


def test_main_rejects_stitch_gsensor_pos_and_xy_together(tmp_path):
    archive = tmp_path / "archive"
    archive.mkdir()
    target = tmp_path / "out"

    with pytest.raises(SystemExit):
        main([
            "--target", str(target), str(archive),
            "--stitch", "--stitch-gsensor",
            "--stitch-gsensor-pos", "top", "--stitch-gsensor-xy", "1,1",
        ])


def test_main_rejects_an_out_of_range_stitch_gsensor_size(tmp_path):
    archive = tmp_path / "archive"
    archive.mkdir()
    target = tmp_path / "out"

    with pytest.raises(SystemExit):
        main([
            "--target", str(target), str(archive),
            "--stitch", "--stitch-gsensor", "--stitch-gsensor-size", "99",
        ])


def test_main_rejects_an_invalid_stitch_gsensor_position(tmp_path):
    archive = tmp_path / "archive"
    archive.mkdir()
    target = tmp_path / "out"

    with pytest.raises(SystemExit):
        main([
            "--target", str(target), str(archive),
            "--stitch", "--stitch-gsensor",
            "--stitch-gsensor-pos", "left-right",
        ])


def test_bv_export_stitch_gsensor_flag_produces_an_overlaid_video(tmp_path):
    from datetime import timedelta

    from blackvue.telemetry.gsensor_reader import GSensorSample
    from blackvue.telemetry.gsensor_reader import write_gsensor

    archive = tmp_path / "archive"
    archive.mkdir()
    target = tmp_path / "out"

    _make_video(archive / "20260720_100000_NF.mp4")
    _make_video(archive / "20260720_100000_NR.mp4")
    write_gsensor(
        (
            GSensorSample(offset=timedelta(seconds=0), x=0, y=0, z=900),
            GSensorSample(offset=timedelta(seconds=1), x=200, y=-100, z=950),
        ),
        archive / "20260720_100000_N.3gf",
    )

    exit_code = bv_export(
        str(archive), target=str(target),
        stitch_layout="side_by_side", render_gsensor=True,
        stitch_gsensor=True,
    )

    assert exit_code == 0
    trip_folder = target / "trip_20260720_100000_20260720_100000"
    assert (trip_folder / "stitch.mp4").exists()
    assert (trip_folder / "gsensor.mp4").exists()


def test_main_leaves_stitch_subtitles_false_when_stitch_flag_is_absent(
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

    main(["--target", str(target), str(archive), "--stitch-subtitles"])

    assert captured["stitch_subtitles"] is False


def test_main_parses_stitch_subtitles_flag(tmp_path, monkeypatch):
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
        "--stitch", "--stitch-subtitles",
    ])

    assert captured["stitch_subtitles"] is True
    # On by default - --no-subtitles-bg not given.
    assert captured["stitch_subtitles_background"] is True


def test_main_no_subtitles_bg_disables_the_background_default(
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
        "--stitch", "--stitch-subtitles", "--no-subtitles-bg",
    ])

    assert captured["stitch_subtitles_background"] is False


def test_bv_export_stitch_subtitles_flag_burns_the_trip_srt_into_stitch_mp4(
    tmp_path
):
    from blackvue.generate.speech import SpeechSegment
    from blackvue.generate.subtitles import format_srt

    archive = tmp_path / "archive"
    archive.mkdir()
    target = tmp_path / "out"

    _make_video(archive / "20260720_100000_NF.mp4")
    _make_video(archive / "20260720_100000_NR.mp4")
    (archive / "20260720_100000_N.srt").write_text(
        format_srt((SpeechSegment(0.0, 1.0, "hello there"),))
    )

    exit_code = bv_export(
        str(archive), target=str(target),
        stitch_layout="side_by_side", stitch_subtitles=True,
    )

    assert exit_code == 0
    trip_folder = target / "trip_20260720_100000_20260720_100000"
    assert (trip_folder / "stitch.mp4").exists()
    assert (trip_folder / "trip.srt").exists()


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


def test_bv_export_gsensor_graph_video_flag_renders_gsensor_graph_video(tmp_path):
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

    exit_code = bv_export(
        str(archive), target=str(target), render_gsensor_graph=True
    )

    assert exit_code == 0
    folder = target / "trip_20260720_100000_20260720_100000"
    assert (folder / "gsensor_graph.mp4").exists()
    # Independent of --gsensor-video - the dot-gauge shouldn't have
    # been rendered just because the graph was asked for.
    assert not (folder / "gsensor.mp4").exists()


def test_bv_export_without_gsensor_graph_video_flag_writes_no_video(tmp_path):
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
    assert not (folder / "gsensor_graph.mp4").exists()


def test_main_leaves_stitch_graph_false_when_stitch_flag_is_absent(
    tmp_path, monkeypatch
):
    # Same "only means anything together with --stitch" gating as
    # --stitch-map/--stitch-gsensor's own equivalent tests.
    captured = {}

    def _fake_bv_export(**kwargs):
        captured.update(kwargs)
        return 0

    monkeypatch.setattr(bv_export_module, "bv_export", _fake_bv_export)

    archive = tmp_path / "archive"
    archive.mkdir()
    target = tmp_path / "out"

    main(["--target", str(target), str(archive), "--stitch-graph"])

    assert captured["stitch_graph"] is False


def test_main_parses_stitch_graph_flags(tmp_path, monkeypatch):
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
        "--stitch", "--stitch-graph",
        "--stitch-graph-side", "top", "--stitch-graph-size", "40",
    ])

    assert captured["stitch_graph"] is True
    assert captured["stitch_graph_side"] == "top"
    assert captured["stitch_graph_size"] == 40.0


def test_main_leaves_stitch_graph_side_and_size_none_by_default(
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

    main(["--target", str(target), str(archive), "--stitch", "--stitch-graph"])

    assert captured["stitch_graph_side"] is None
    assert captured["stitch_graph_size"] is None


def test_main_rejects_an_out_of_range_stitch_graph_size(tmp_path):
    archive = tmp_path / "archive"
    archive.mkdir()
    target = tmp_path / "out"

    with pytest.raises(SystemExit):
        main([
            "--target", str(target), str(archive),
            "--stitch", "--stitch-graph", "--stitch-graph-size", "99",
        ])


def test_bv_export_stitch_graph_flag_grows_the_stitch_composite(tmp_path):
    # End-to-end through bv_export()/export_trip()/stitch_cameras()
    # together, not just argparse wiring - confirms `samples` (the
    # trip's own already-merged g-sensor readings, computed once in
    # export_trip() for --gsensor-video's sake too) actually reaches
    # stitch_cameras() as `graph_samples` and grows stitch.mp4's own
    # dimensions, the same way --stitch-map's panel does. Two cameras
    # (side_by_side) so the panel path is actually exercised - the
    # single-camera fallback ignores panels entirely, same as
    # --stitch-map.
    from datetime import timedelta

    from blackvue.telemetry.gsensor_reader import GSensorSample
    from blackvue.telemetry.gsensor_reader import write_gsensor

    archive = tmp_path / "archive"
    archive.mkdir()
    target = tmp_path / "out"

    _make_video(archive / "20260720_100000_NF.mp4")
    _make_video(archive / "20260720_100000_NR.mp4")
    write_gsensor(
        tuple(
            GSensorSample(
                offset=timedelta(milliseconds=i * 200), x=100, y=-50, z=900,
            )
            for i in range(10)
        ),
        archive / "20260720_100000_N.3gf",
    )

    exit_code = bv_export(
        str(archive), target=str(target),
        stitch_layout="side_by_side", stitch_graph=True,
    )

    assert exit_code == 0
    folder = target / "trip_20260720_100000_20260720_100000"
    stitch_path = folder / "stitch.mp4"
    assert stitch_path.exists()

    probe = subprocess.run(
        [
            "ffprobe", "-v", "error",
            "-select_streams", "v:0",
            "-show_entries", "stream=width,height",
            "-of", "json",
            str(stitch_path),
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    stream = json.loads(probe.stdout)["streams"][0]

    # Two 64x64 cameras side by side -> 128x64 before any panel. The
    # graph panel's default 'right' side (--stitch-graph-side not
    # given) hstacks on the right, so height stays 64 and width grows
    # past 128.
    assert stream["height"] == 64
    assert stream["width"] > 128


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


def test_main_writes_the_full_invoking_command_into_the_trip_log(tmp_path):
    archive = tmp_path / "archive"
    archive.mkdir()
    target = tmp_path / "out"

    _make_video(archive / "20260720_100000_NF.mp4")

    exit_code = main([str(archive), "--target", str(target), "--overwrite"])

    assert exit_code == 0
    folder = target / "trip_20260720_100000_20260720_100000"
    log_text = (folder / "trip.log").read_text(encoding="utf-8")
    assert "Command: bv-export" in log_text
    assert str(archive) in log_text
    assert "--target" in log_text
    assert str(target) in log_text
    assert "--overwrite" in log_text


def test_bv_export_forwards_trip_membership_reasons_into_the_trip_log(tmp_path):
    archive = tmp_path / "archive"
    archive.mkdir()
    target = tmp_path / "out"

    # Two recordings close enough together to land in the same trip -
    # TripBuilder.build()'s own reasoning for that ("continues the
    # trip - ... within the ... threshold") should show up verbatim in
    # this trip's trip.log, not just be computed and discarded.
    _make_video(archive / "20260720_100000_NF.mp4")
    _make_video(archive / "20260720_100200_NF.mp4")

    exit_code = bv_export(str(archive), target=str(target))

    assert exit_code == 0
    folder = target / "trip_20260720_100000_20260720_100200"
    log_text = (folder / "trip.log").read_text(encoding="utf-8")
    assert "--- Trip membership ---" in log_text
    assert "first recording in the archive" in log_text
    assert "continues the trip" in log_text
