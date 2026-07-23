import calendar
import json
import re
import struct
import subprocess
from datetime import datetime
from datetime import timedelta

from blackvue.archive.asset import Asset
from blackvue.archive.asset_file import AssetFile
from blackvue.archive.recording import Recording
from blackvue.archive.recording_id import RecordingId
from blackvue.export import trip_export as trip_export_module
from blackvue.export.osm_roads import Road
from blackvue.export.trip_export import export_trip
from blackvue.export.trip_export import folder_name_for_trip
from blackvue.generate.media import MediaToolError
from blackvue.generate.speech import SpeechSegment
from blackvue.generate.subtitles import format_lrc
from blackvue.generate.subtitles import format_srt
from blackvue.telemetry.gsensor_reader import read_gsensor
from blackvue.trip.trip import Trip


def _make_video(path, duration_seconds: float) -> None:
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


def _video_size(path) -> tuple[int, int]:
    result = subprocess.run(
        [
            "ffprobe", "-v", "error",
            "-select_streams", "v:0",
            "-show_entries", "stream=width,height",
            "-of", "json",
            str(path),
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    stream = json.loads(result.stdout)["streams"][0]
    return stream["width"], stream["height"]


def _make_audio(path, duration_seconds: float = 1.0) -> None:
    subprocess.run(
        [
            "ffmpeg", "-y",
            "-f", "lavfi",
            "-i", f"sine=frequency=440:duration={duration_seconds}",
            "-c:a", "aac",
            str(path),
        ],
        capture_output=True,
        text=True,
        check=True,
    )


def _has_audio_stream(path) -> bool:
    result = subprocess.run(
        [
            "ffprobe", "-v", "error",
            "-select_streams", "a:0",
            "-show_entries", "stream=codec_name",
            "-of", "json",
            str(path),
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    return bool(json.loads(result.stdout)["streams"])


def _gsensor_bytes(*records) -> bytes:
    return b"".join(struct.pack(">Ihhh", ms, x, y, z) for ms, x, y, z in records)


def test_folder_name_for_trip_with_and_without_prefix():
    first = Recording(id=RecordingId("20260715_100000_N"))
    last = Recording(id=RecordingId("20260715_100500_N"))
    trip = Trip((first, last))

    assert folder_name_for_trip(trip, None) == (
        "trip_20260715_100000_20260715_100500"
    )
    assert folder_name_for_trip(trip, "Holiday") == (
        "Holiday_trip_20260715_100000_20260715_100500"
    )


def test_export_trip_writes_everything_available(tmp_path):
    source_dir = tmp_path / "archive"
    source_dir.mkdir()
    dest_dir = tmp_path / "export"

    front_a = source_dir / "front_a.mp4"
    front_b = source_dir / "front_b.mp4"
    _make_video(front_a, 1.0)
    _make_video(front_b, 1.0)

    gps_a = source_dir / "a.gps"
    gps_a.write_text(
        "[1700000000000]$GPRMC,120000.00,A,4807.038,N,01131.000,E,"
        "10.00,45.00,010124,,,A*6D\n"
    )
    gps_b = source_dir / "b.gps"
    gps_b.write_text(
        "[1700000060000]$GPRMC,120100.00,A,4808.038,N,01132.000,E,"
        "12.00,45.00,010124,,,A*6D\n"
    )

    gsensor_a = source_dir / "a.3gf"
    gsensor_a.write_bytes(_gsensor_bytes((0, 1, 2, 3), (100, 4, 5, 6)))
    gsensor_b = source_dir / "b.3gf"
    gsensor_b.write_bytes(_gsensor_bytes((0, 7, 8, 9)))

    transcript_a = source_dir / "a.transcript.txt"
    transcript_a.write_text("First recording speech.", encoding="utf-8")

    first = Recording(
        id=RecordingId("20260720_100000_N"),
        assets={
            Asset.FRONT: AssetFile(Asset.FRONT, front_a),
            Asset.GPS: AssetFile(Asset.GPS, gps_a),
            Asset.GSENSOR: AssetFile(Asset.GSENSOR, gsensor_a),
            Asset.TRANSCRIPT: AssetFile(Asset.TRANSCRIPT, transcript_a),
        },
    )
    second = Recording(
        id=RecordingId("20260720_100100_N"),
        assets={
            Asset.FRONT: AssetFile(Asset.FRONT, front_b),
            Asset.GPS: AssetFile(Asset.GPS, gps_b),
            Asset.GSENSOR: AssetFile(Asset.GSENSOR, gsensor_b),
        },
    )
    trip = Trip((first, second))

    result = export_trip(trip, dest_dir)

    assert result.front_video == dest_dir / "front.mp4"
    assert result.front_video.exists()
    assert result.rear_video is None
    assert result.audio is None

    assert result.gpx == dest_dir / "trip.gpx"
    assert result.gpx.exists()

    assert result.gsensor == dest_dir / "trip.3gf"
    samples = read_gsensor(result.gsensor)
    # First recording's samples keep their own offsets (0, 100ms);
    # second recording started 60s after the trip start, so its one
    # sample should be rebased to 60000ms.
    assert samples[0].offset == timedelta(milliseconds=0)
    assert samples[1].offset == timedelta(milliseconds=100)
    assert samples[2].offset == timedelta(milliseconds=60000)
    assert (samples[2].x, samples[2].y, samples[2].z) == (7, 8, 9)

    assert result.text == (dest_dir / "transcript.txt",)
    assert "First recording speech." in result.text[0].read_text()

    assert result.warnings == ()


def test_export_trip_concatenates_front_rear_audio_independently(
    tmp_path, monkeypatch
):
    # front/rear/audio concatenation now run concurrently (see
    # export_trip()'s comment) - the property that actually matters
    # for correctness, not the threading itself, is that one of them
    # failing doesn't block or lose the other two.
    def _selective_concat(sources, destination):
        if destination.name == "front.mp4":
            raise MediaToolError("simulated front failure")
        destination.parent.mkdir(parents=True, exist_ok=True)
        if destination.suffix == ".mp4":
            # A real (tiny) video, not just placeholder bytes - rear
            # ends up the sole video export_trip() probes for subtitle
            # padding once front fails, and a fake file would fail
            # that probe too, adding an unrelated second warning this
            # test isn't about.
            _make_video(destination, 1.0)
        else:
            destination.write_bytes(b"fake-audio")

    monkeypatch.setattr(
        trip_export_module, "concatenate_media", _selective_concat
    )

    source_dir = tmp_path / "archive"
    source_dir.mkdir()
    dest_dir = tmp_path / "export"

    front_a = source_dir / "front_a.mp4"
    rear_a = source_dir / "rear_a.mp4"
    audio_a = source_dir / "audio_a.aac"
    front_a.write_bytes(b"x")
    rear_a.write_bytes(b"x")
    audio_a.write_bytes(b"x")

    trip = Trip((
        Recording(
            id=RecordingId("20260720_100000_N"),
            assets={
                Asset.FRONT: AssetFile(Asset.FRONT, front_a),
                Asset.REAR: AssetFile(Asset.REAR, rear_a),
                Asset.AUDIO: AssetFile(Asset.AUDIO, audio_a),
            },
        ),
    ))

    result = export_trip(trip, dest_dir)

    assert result.front_video is None
    assert result.rear_video == dest_dir / "rear.mp4"
    assert result.audio == dest_dir / "audio.aac"
    assert result.rear_video.exists()
    assert result.audio.exists()
    assert len(result.warnings) == 1


def test_export_trip_skips_missing_assets_cleanly(tmp_path):
    dest_dir = tmp_path / "export"
    trip = Trip((Recording(id=RecordingId("20260720_100000_N")),))

    result = export_trip(trip, dest_dir)

    assert result.front_video is None
    assert result.rear_video is None
    assert result.audio is None
    assert result.gpx is None
    assert result.gsensor is None
    assert result.text == ()
    assert dest_dir.exists()


def _epoch_ms(timestamp: datetime) -> int:
    """Convert a naive datetime (RecordingId.timestamp/GpsFix.timestamp
    - see gps_reader.py's own docstring on why the two are directly
    comparable, both "UTC-equivalent" naive datetimes) into the Unix
    epoch milliseconds a raw .gps file's own [timestamp] bracket would
    encode for it - so a fixture's GPS fix timestamps land at a
    realistic offset from its recording's own filename timestamp,
    rather than some unrelated fixed epoch. That distinction matters
    for anything exercising render_map_video()'s trip-start-anchored
    timeline (see map_video.py) - an arbitrary, unrelated GPS epoch
    would make the trip's real start look wildly earlier/later than
    every GPS fix, not just "GPS data starts a bit into the trip".
    """

    return calendar.timegm(timestamp.timetuple()) * 1000


def _trip_with_two_gps_fixes(source_dir):
    first_id = RecordingId("20260720_100000_N")
    second_id = RecordingId("20260720_100010_N")

    gps_a = source_dir / "a.gps"
    gps_a.write_text(
        f"[{_epoch_ms(first_id.timestamp)}]$GPRMC,120000.00,A,4807.038,N,"
        "01131.000,E,10.00,45.00,010124,,,A*6D\n"
    )
    gps_b = source_dir / "b.gps"
    gps_b.write_text(
        f"[{_epoch_ms(second_id.timestamp)}]$GPRMC,120010.00,A,4808.038,N,"
        "01132.000,E,12.00,45.00,010124,,,A*6D\n"
    )

    first = Recording(
        id=first_id,
        assets={Asset.GPS: AssetFile(Asset.GPS, gps_a)},
    )
    second = Recording(
        id=second_id,
        assets={Asset.GPS: AssetFile(Asset.GPS, gps_b)},
    )
    return Trip((first, second))


def _fake_roads(*_args, **_kwargs):
    return (Road(points=((48.07, 11.31), (48.08, 11.32))),)


def test_export_trip_skips_map_by_default(tmp_path, monkeypatch):
    def _refuse(*_args, **_kwargs):
        raise AssertionError("should not fetch roads when render_map=False")

    monkeypatch.setattr(trip_export_module, "load_or_fetch_roads", _refuse)

    source_dir = tmp_path / "archive"
    source_dir.mkdir()
    dest_dir = tmp_path / "export"
    trip = _trip_with_two_gps_fixes(source_dir)

    result = export_trip(trip, dest_dir)

    assert result.map is None
    assert not (dest_dir / "map.mp4").exists()


def test_export_trip_render_map_produces_a_video(tmp_path, monkeypatch):
    monkeypatch.setattr(
        trip_export_module, "load_or_fetch_roads", _fake_roads
    )

    source_dir = tmp_path / "archive"
    source_dir.mkdir()
    dest_dir = tmp_path / "export"
    trip = _trip_with_two_gps_fixes(source_dir)

    result = export_trip(trip, dest_dir, render_map=True)

    assert result.map == dest_dir / "map.mp4"
    assert result.map.exists()
    assert result.warnings == ()


def test_export_trip_render_map_zoom_produces_a_separate_file_alongside_map(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(
        trip_export_module, "load_or_fetch_roads", _fake_roads
    )

    calls = []

    def _capture_zoom(fixes, roads, bbox, destination, **kwargs):
        calls.append((destination, kwargs))
        return destination

    monkeypatch.setattr(
        trip_export_module, "render_map_video", _capture_zoom
    )

    source_dir = tmp_path / "archive"
    source_dir.mkdir()
    dest_dir = tmp_path / "export"
    trip = _trip_with_two_gps_fixes(source_dir)

    result = export_trip(trip, dest_dir, render_map=True, map_zoom_meters=75.0)

    # Two separate renders: the static map.mp4 (no zoom_meters) and its
    # own map_zoom_75m.mp4 (zoom_meters=75.0) - not one video reused
    # for both.
    assert len(calls) == 2
    destinations = {destination for destination, _kwargs in calls}
    assert destinations == {
        dest_dir / "map.mp4", dest_dir / "map_zoom_75m.mp4",
    }
    zoom_kwargs = next(
        kwargs for destination, kwargs in calls
        if destination == dest_dir / "map_zoom_75m.mp4"
    )
    assert zoom_kwargs["zoom_meters"] == 75.0
    static_kwargs = next(
        kwargs for destination, kwargs in calls
        if destination == dest_dir / "map.mp4"
    )
    assert static_kwargs["zoom_meters"] is None

    assert result.map == dest_dir / "map.mp4"
    assert result.map_zoom == dest_dir / "map_zoom_75m.mp4"


def test_export_trip_render_map_zoom_alone_skips_the_static_map(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(
        trip_export_module, "load_or_fetch_roads", _fake_roads
    )

    source_dir = tmp_path / "archive"
    source_dir.mkdir()
    dest_dir = tmp_path / "export"
    trip = _trip_with_two_gps_fixes(source_dir)

    result = export_trip(trip, dest_dir, map_zoom_meters=120.0)

    assert result.map is None
    assert not (dest_dir / "map.mp4").exists()
    assert result.map_zoom == dest_dir / "map_zoom_120m.mp4"
    assert result.map_zoom.exists()


def test_export_trip_formats_the_map_zoom_filename_without_a_trailing_zero(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(
        trip_export_module, "load_or_fetch_roads", _fake_roads
    )

    source_dir = tmp_path / "archive"
    source_dir.mkdir()
    dest_dir = tmp_path / "export"
    trip = _trip_with_two_gps_fixes(source_dir)

    result = export_trip(trip, dest_dir, map_zoom_meters=75.5)

    assert result.map_zoom == dest_dir / "map_zoom_75.5m.mp4"


def test_export_trip_render_map_defaults_cache_dir_next_to_destination(
    tmp_path, monkeypatch
):
    captured = []

    def _capture_cache_dir(bbox, cache_dir, **_kwargs):
        captured.append(cache_dir)
        return _fake_roads()

    monkeypatch.setattr(
        trip_export_module, "load_or_fetch_roads", _capture_cache_dir
    )

    source_dir = tmp_path / "archive"
    source_dir.mkdir()
    dest_dir = tmp_path / "target" / "trip_folder"
    trip = _trip_with_two_gps_fixes(source_dir)

    export_trip(trip, dest_dir, render_map=True)

    assert captured == [dest_dir.parent / ".osm_cache"]


def test_export_trip_render_map_warns_instead_of_failing_on_fetch_error(
    tmp_path, monkeypatch
):
    def _broken(*_args, **_kwargs):
        raise MediaToolError("could not reach the Overpass API")

    monkeypatch.setattr(trip_export_module, "load_or_fetch_roads", _broken)

    source_dir = tmp_path / "archive"
    source_dir.mkdir()
    dest_dir = tmp_path / "export"
    trip = _trip_with_two_gps_fixes(source_dir)

    result = export_trip(trip, dest_dir, render_map=True)

    assert result.map is None
    assert len(result.warnings) == 1
    assert "map" in result.warnings[0]
    # The rest of the export still succeeded despite the map failure.
    assert result.gpx is not None


def test_export_trip_render_map_uses_a_custom_icon_when_given(
    tmp_path, monkeypatch
):
    from PIL import Image

    monkeypatch.setattr(
        trip_export_module, "load_or_fetch_roads", _fake_roads
    )

    source_dir = tmp_path / "archive"
    source_dir.mkdir()
    dest_dir = tmp_path / "export"
    trip = _trip_with_two_gps_fixes(source_dir)

    icon_path = tmp_path / "car.png"
    Image.new("RGBA", (16, 16), (0, 0, 255, 255)).save(icon_path)

    result = export_trip(trip, dest_dir, render_map=True, map_icon=icon_path)

    assert result.map == dest_dir / "map.mp4"
    assert result.map.exists()
    assert result.warnings == ()


def test_export_trip_render_map_warns_instead_of_failing_on_a_bad_icon_path(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(
        trip_export_module, "load_or_fetch_roads", _fake_roads
    )

    source_dir = tmp_path / "archive"
    source_dir.mkdir()
    dest_dir = tmp_path / "export"
    trip = _trip_with_two_gps_fixes(source_dir)

    result = export_trip(
        trip,
        dest_dir,
        render_map=True,
        map_icon=tmp_path / "does-not-exist.png",
    )

    assert result.map is None
    assert len(result.warnings) == 1
    assert "map" in result.warnings[0]
    # The rest of the export still succeeded despite the bad icon path.
    assert result.gpx is not None


def _trip_with_gsensor_samples(source_dir):
    gsensor_a = source_dir / "a.3gf"
    gsensor_a.write_bytes(
        _gsensor_bytes((0, 100, -200, 900), (500, -300, 400, 950))
    )
    gsensor_b = source_dir / "b.3gf"
    gsensor_b.write_bytes(_gsensor_bytes((0, 200, 100, 980)))

    first = Recording(
        id=RecordingId("20260720_100000_N"),
        assets={Asset.GSENSOR: AssetFile(Asset.GSENSOR, gsensor_a)},
    )
    second = Recording(
        id=RecordingId("20260720_100010_N"),
        assets={Asset.GSENSOR: AssetFile(Asset.GSENSOR, gsensor_b)},
    )
    return Trip((first, second))


def test_export_trip_skips_gsensor_video_by_default(tmp_path):
    source_dir = tmp_path / "archive"
    source_dir.mkdir()
    dest_dir = tmp_path / "export"
    trip = _trip_with_gsensor_samples(source_dir)

    result = export_trip(trip, dest_dir)

    assert result.gsensor_video is None
    assert not (dest_dir / "gsensor.mp4").exists()


def test_export_trip_render_gsensor_produces_a_video(tmp_path):
    source_dir = tmp_path / "archive"
    source_dir.mkdir()
    dest_dir = tmp_path / "export"
    trip = _trip_with_gsensor_samples(source_dir)

    result = export_trip(trip, dest_dir, render_gsensor=True)

    assert result.gsensor_video == dest_dir / "gsensor.mp4"
    assert result.gsensor_video.exists()
    assert result.warnings == ()


def test_export_trip_render_gsensor_debug_prints_phase_timing_to_stderr(
    tmp_path, capsys
):
    # Matches the existing concatenation/map/stitch pattern - Christer
    # noticed gsensor rendering was the one phase --debug said nothing
    # about.
    source_dir = tmp_path / "archive"
    source_dir.mkdir()
    dest_dir = tmp_path / "export"
    trip = _trip_with_gsensor_samples(source_dir)

    export_trip(trip, dest_dir, render_gsensor=True, debug=True)

    err = capsys.readouterr().err
    assert "bv-export: gsensor phase took" in err


def test_export_trip_render_gsensor_is_silent_by_default(tmp_path, capsys):
    source_dir = tmp_path / "archive"
    source_dir.mkdir()
    dest_dir = tmp_path / "export"
    trip = _trip_with_gsensor_samples(source_dir)

    export_trip(trip, dest_dir, render_gsensor=True)

    assert capsys.readouterr().err == ""


def test_export_trip_render_gsensor_logs_elapsed_seconds_to_trip_log(tmp_path):
    # trip.log records this regardless of --debug (see export_trip()'s
    # own docstring) - unlike the stderr print above, which only
    # happens under --debug.
    source_dir = tmp_path / "archive"
    source_dir.mkdir()
    dest_dir = tmp_path / "export"
    trip = _trip_with_gsensor_samples(source_dir)

    export_trip(trip, dest_dir, render_gsensor=True)

    log_text = (dest_dir / "trip.log").read_text(encoding="utf-8")
    match = re.search(r"rendered gsensor\.mp4 \((\d+\.\d)s\)", log_text)
    assert match is not None


def test_export_trip_render_gsensor_warns_instead_of_failing_on_encode_error(
    tmp_path, monkeypatch
):
    def _broken(*_args, **_kwargs):
        raise MediaToolError("ffmpeg not found on PATH")

    monkeypatch.setattr(
        trip_export_module, "render_gsensor_video", _broken
    )

    source_dir = tmp_path / "archive"
    source_dir.mkdir()
    dest_dir = tmp_path / "export"
    trip = _trip_with_gsensor_samples(source_dir)

    result = export_trip(trip, dest_dir, render_gsensor=True)

    assert result.gsensor_video is None
    assert len(result.warnings) == 1
    assert "gsensor video" in result.warnings[0]
    # The rest of the export still succeeded despite the failure.
    assert result.gsensor is not None


def _trip_with_front_and_rear(source_dir):
    front_a = source_dir / "front_a.mp4"
    rear_a = source_dir / "rear_a.mp4"
    _make_video(front_a, 1.0)
    _make_video(rear_a, 1.0)

    return Trip((
        Recording(
            id=RecordingId("20260720_100000_N"),
            assets={
                Asset.FRONT: AssetFile(Asset.FRONT, front_a),
                Asset.REAR: AssetFile(Asset.REAR, rear_a),
            },
        ),
    ))


def test_export_trip_skips_stitch_by_default(tmp_path):
    source_dir = tmp_path / "archive"
    source_dir.mkdir()
    dest_dir = tmp_path / "export"
    trip = _trip_with_front_and_rear(source_dir)

    result = export_trip(trip, dest_dir)

    assert result.stitch is None
    assert not (dest_dir / "stitch.mp4").exists()


def test_export_trip_stitch_layout_produces_a_video(tmp_path):
    source_dir = tmp_path / "archive"
    source_dir.mkdir()
    dest_dir = tmp_path / "export"
    trip = _trip_with_front_and_rear(source_dir)

    result = export_trip(trip, dest_dir, stitch_layout="side_by_side")

    assert result.stitch == dest_dir / "stitch.mp4"
    assert result.stitch.exists()
    assert result.warnings == ()


def test_export_trip_stitch_scale_and_max_dimensions_are_forwarded(
    tmp_path, monkeypatch
):
    captured = {}
    original_stitch_cameras = trip_export_module.stitch_cameras

    def _capture_stitch_cameras(*args, **kwargs):
        captured.update(kwargs)
        return original_stitch_cameras(*args, **kwargs)

    monkeypatch.setattr(
        trip_export_module, "stitch_cameras", _capture_stitch_cameras
    )

    source_dir = tmp_path / "archive"
    source_dir.mkdir()
    dest_dir = tmp_path / "export"
    trip = _trip_with_front_and_rear(source_dir)

    export_trip(
        trip, dest_dir, stitch_layout="side_by_side",
        stitch_scale=50.0, stitch_max_width=1920, stitch_max_height=1080,
    )

    assert captured["scale"] == 50.0
    assert captured["max_width"] == 1920
    assert captured["max_height"] == 1080


def test_export_trip_stitch_muxes_this_trips_own_concatenated_audio(tmp_path):
    source_dir = tmp_path / "archive"
    source_dir.mkdir()
    dest_dir = tmp_path / "export"

    front_a = source_dir / "front_a.mp4"
    rear_a = source_dir / "rear_a.mp4"
    audio_a = source_dir / "audio_a.aac"
    _make_video(front_a, 1.0)
    _make_video(rear_a, 1.0)
    _make_audio(audio_a, 1.0)

    trip = Trip((
        Recording(
            id=RecordingId("20260720_100000_N"),
            assets={
                Asset.FRONT: AssetFile(Asset.FRONT, front_a),
                Asset.REAR: AssetFile(Asset.REAR, rear_a),
                Asset.AUDIO: AssetFile(Asset.AUDIO, audio_a),
            },
        ),
    ))

    result = export_trip(trip, dest_dir, stitch_layout="side_by_side")

    assert result.audio == dest_dir / "audio.aac"
    assert result.stitch == dest_dir / "stitch.mp4"
    assert _has_audio_stream(result.stitch)
    assert result.warnings == ()


def test_export_trip_stitch_has_no_audio_when_the_trip_has_none(tmp_path):
    source_dir = tmp_path / "archive"
    source_dir.mkdir()
    dest_dir = tmp_path / "export"
    trip = _trip_with_front_and_rear(source_dir)

    result = export_trip(trip, dest_dir, stitch_layout="side_by_side")

    assert result.audio is None
    assert not _has_audio_stream(result.stitch)


def test_export_trip_stitch_rearview_mirror_produces_a_video(tmp_path):
    source_dir = tmp_path / "archive"
    source_dir.mkdir()
    dest_dir = tmp_path / "export"
    trip = _trip_with_front_and_rear(source_dir)

    result = export_trip(trip, dest_dir, stitch_layout="rearview_mirror")

    assert result.stitch == dest_dir / "stitch.mp4"
    assert result.stitch.exists()
    assert result.warnings == ()


def test_export_trip_stitch_mirror_size_is_forwarded(tmp_path, monkeypatch):
    source_dir = tmp_path / "archive"
    source_dir.mkdir()
    dest_dir = tmp_path / "export"
    trip = _trip_with_front_and_rear(source_dir)

    captured = {}
    original_stitch_cameras = trip_export_module.stitch_cameras

    def _capture_stitch_cameras(*args, **kwargs):
        captured.update(kwargs)
        return original_stitch_cameras(*args, **kwargs)

    monkeypatch.setattr(
        trip_export_module, "stitch_cameras", _capture_stitch_cameras
    )

    export_trip(
        trip, dest_dir,
        stitch_layout="rearview_mirror", stitch_mirror_size=40.0,
    )

    assert captured["mirror_size"] == 40.0


def test_export_trip_stitch_mirror_radius_is_forwarded(tmp_path, monkeypatch):
    source_dir = tmp_path / "archive"
    source_dir.mkdir()
    dest_dir = tmp_path / "export"
    trip = _trip_with_front_and_rear(source_dir)

    captured = {}
    original_stitch_cameras = trip_export_module.stitch_cameras

    def _capture_stitch_cameras(*args, **kwargs):
        captured.update(kwargs)
        return original_stitch_cameras(*args, **kwargs)

    monkeypatch.setattr(
        trip_export_module, "stitch_cameras", _capture_stitch_cameras
    )

    export_trip(
        trip, dest_dir,
        stitch_layout="rearview_mirror", stitch_mirror_radius=50.0,
    )

    assert captured["mirror_radius"] == 50.0


def test_export_trip_stitch_mirror_zoom_is_forwarded(tmp_path, monkeypatch):
    source_dir = tmp_path / "archive"
    source_dir.mkdir()
    dest_dir = tmp_path / "export"
    trip = _trip_with_front_and_rear(source_dir)

    captured = {}
    original_stitch_cameras = trip_export_module.stitch_cameras

    def _capture_stitch_cameras(*args, **kwargs):
        captured.update(kwargs)
        return original_stitch_cameras(*args, **kwargs)

    monkeypatch.setattr(
        trip_export_module, "stitch_cameras", _capture_stitch_cameras
    )

    export_trip(
        trip, dest_dir,
        stitch_layout="rearview_mirror", stitch_mirror_zoom=40.0,
    )

    assert captured["mirror_zoom"] == 40.0


def test_export_trip_stitch_mirror_pan_is_forwarded(tmp_path, monkeypatch):
    source_dir = tmp_path / "archive"
    source_dir.mkdir()
    dest_dir = tmp_path / "export"
    trip = _trip_with_front_and_rear(source_dir)

    captured = {}
    original_stitch_cameras = trip_export_module.stitch_cameras

    def _capture_stitch_cameras(*args, **kwargs):
        captured.update(kwargs)
        return original_stitch_cameras(*args, **kwargs)

    monkeypatch.setattr(
        trip_export_module, "stitch_cameras", _capture_stitch_cameras
    )

    export_trip(
        trip, dest_dir,
        stitch_layout="rearview_mirror",
        stitch_mirror_pan_x=-25.0, stitch_mirror_pan_y=60.0,
    )

    assert captured["mirror_pan_x"] == -25.0
    assert captured["mirror_pan_y"] == 60.0


def test_export_trip_stitch_mirror_icon_is_forwarded(tmp_path, monkeypatch):
    from PIL import Image
    from PIL import ImageDraw

    source_dir = tmp_path / "archive"
    source_dir.mkdir()
    dest_dir = tmp_path / "export"
    trip = _trip_with_front_and_rear(source_dir)

    captured = {}
    original_stitch_cameras = trip_export_module.stitch_cameras

    def _capture_stitch_cameras(*args, **kwargs):
        captured.update(kwargs)
        return original_stitch_cameras(*args, **kwargs)

    monkeypatch.setattr(
        trip_export_module, "stitch_cameras", _capture_stitch_cameras
    )

    icon_path = tmp_path / "mirror.png"
    image = Image.new("RGB", (40, 40), (0, 0, 0))
    ImageDraw.Draw(image).rectangle((10, 10, 29, 29), fill=(255, 255, 255))
    image.save(icon_path)

    export_trip(
        trip, dest_dir,
        stitch_layout="rearview_mirror", stitch_mirror_icon=icon_path,
    )

    assert captured["mirror_icon"] == icon_path


def test_export_trip_stitch_mirror_icon_warns_instead_of_failing_on_a_bad_icon_path(
    tmp_path
):
    source_dir = tmp_path / "archive"
    source_dir.mkdir()
    dest_dir = tmp_path / "export"
    trip = _trip_with_front_and_rear(source_dir)

    result = export_trip(
        trip, dest_dir,
        stitch_layout="rearview_mirror",
        stitch_mirror_icon=tmp_path / "does-not-exist.png",
    )

    # Unlike a bad --map-icon (which fails the map entirely), a bad
    # --stitch-mirror-icon still produces a full stitch.mp4 - it just
    # falls back to the plain procedural inset instead of the photo
    # composite. See stitch.py's own is_mirror/mirror_icon handling.
    assert result.stitch == dest_dir / "stitch.mp4"
    assert result.stitch.exists()
    assert len(result.warnings) == 1
    assert "mirror icon" in result.warnings[0]


def _trip_with_front_rear_and_gps_shape(source_dir, *, east_west: bool):
    front_a = source_dir / "front_a.mp4"
    rear_a = source_dir / "rear_a.mp4"
    _make_video(front_a, 1.0)
    _make_video(rear_a, 1.0)

    gps_a = source_dir / "a.gps"
    gps_a.write_text(
        "[1700000000000]$GPRMC,120000.00,A,4807.038,N,01131.000,E,"
        "10.00,45.00,010124,,,A*6D\n"
    )
    gps_b = source_dir / "b.gps"
    # A large step on the axis this trip should run along, a tiny one
    # on the other - same real-world-shape idea test_stitch.py's own
    # pick_stitch_layout() tests use, just expressed as raw NMEA
    # sentences here since export_trip() reads GPS from recordings,
    # not from pre-built GpsFix objects.
    if east_west:
        gps_b.write_text(
            "[1700000010000]$GPRMC,120010.00,A,4807.238,N,01141.000,E,"
            "12.00,45.00,010124,,,A*6D\n"
        )
    else:
        gps_b.write_text(
            "[1700000010000]$GPRMC,120010.00,A,4907.038,N,01131.010,E,"
            "12.00,45.00,010124,,,A*6D\n"
        )

    return Trip((
        Recording(
            id=RecordingId("20260720_100000_N"),
            assets={
                Asset.FRONT: AssetFile(Asset.FRONT, front_a),
                Asset.REAR: AssetFile(Asset.REAR, rear_a),
                Asset.GPS: AssetFile(Asset.GPS, gps_a),
            },
        ),
        Recording(
            id=RecordingId("20260720_100010_N"),
            assets={Asset.GPS: AssetFile(Asset.GPS, gps_b)},
        ),
    ))


def test_export_trip_stitch_auto_layout_picks_side_by_side_for_east_west(
    tmp_path
):
    from blackvue.export.stitch import AUTO_LAYOUT

    source_dir = tmp_path / "archive"
    source_dir.mkdir()
    dest_dir = tmp_path / "export"
    trip = _trip_with_front_rear_and_gps_shape(source_dir, east_west=True)

    result = export_trip(trip, dest_dir, stitch_layout=AUTO_LAYOUT)

    assert result.warnings == ()
    # side_by_side hstacks - combined width doubles, height unchanged.
    assert _video_size(result.stitch) == (128, 64)


def test_export_trip_stitch_auto_layout_picks_top_down_for_north_south(
    tmp_path
):
    from blackvue.export.stitch import AUTO_LAYOUT

    source_dir = tmp_path / "archive"
    source_dir.mkdir()
    dest_dir = tmp_path / "export"
    trip = _trip_with_front_rear_and_gps_shape(source_dir, east_west=False)

    result = export_trip(trip, dest_dir, stitch_layout=AUTO_LAYOUT)

    assert result.warnings == ()
    # top_down vstacks - combined height doubles, width unchanged.
    assert _video_size(result.stitch) == (64, 128)


def test_export_trip_stitch_auto_layout_falls_back_without_gps_data(
    tmp_path
):
    from blackvue.export.stitch import AUTO_LAYOUT

    source_dir = tmp_path / "archive"
    source_dir.mkdir()
    dest_dir = tmp_path / "export"
    trip = _trip_with_front_and_rear(source_dir)

    result = export_trip(trip, dest_dir, stitch_layout=AUTO_LAYOUT)

    assert len(result.warnings) == 1
    assert "no GPS data to auto-pick" in result.warnings[0]
    # Falls back to side_by_side, same as the CLI's own pre-auto-pick
    # default.
    assert _video_size(result.stitch) == (128, 64)


def test_export_trip_stitch_explicit_layout_is_never_overridden_by_auto_pick(
    tmp_path
):
    # An east-west trip would auto-pick side_by_side - explicitly
    # asking for top_down instead must still be honored exactly, since
    # auto-pick only ever applies to AUTO_LAYOUT itself.
    source_dir = tmp_path / "archive"
    source_dir.mkdir()
    dest_dir = tmp_path / "export"
    trip = _trip_with_front_rear_and_gps_shape(source_dir, east_west=True)

    result = export_trip(trip, dest_dir, stitch_layout="top_down")

    assert result.warnings == ()
    assert _video_size(result.stitch) == (64, 128)


def test_export_trip_stitch_falls_back_to_front_only_with_no_rear(tmp_path):
    source_dir = tmp_path / "archive"
    source_dir.mkdir()
    dest_dir = tmp_path / "export"

    front_only = source_dir / "front_only.mp4"
    _make_video(front_only, 1.0)
    trip = Trip((
        Recording(
            id=RecordingId("20260720_100000_N"),
            assets={Asset.FRONT: AssetFile(Asset.FRONT, front_only)},
        ),
    ))

    result = export_trip(trip, dest_dir, stitch_layout="top_down")

    assert result.stitch == dest_dir / "stitch.mp4"
    assert result.stitch.exists()
    assert result.warnings == ()


def test_export_trip_stitch_warns_instead_of_failing_on_encode_error(
    tmp_path, monkeypatch
):
    def _broken(*_args, **_kwargs):
        raise MediaToolError("ffmpeg not found on PATH")

    monkeypatch.setattr(trip_export_module, "stitch_cameras", _broken)

    source_dir = tmp_path / "archive"
    source_dir.mkdir()
    dest_dir = tmp_path / "export"
    trip = _trip_with_front_and_rear(source_dir)

    result = export_trip(trip, dest_dir, stitch_layout="side_by_side")

    assert result.stitch is None
    assert len(result.warnings) == 1
    assert "stitch" in result.warnings[0]
    # The rest of the export still succeeded despite the stitch failure.
    assert result.front_video is not None


def _trip_with_front_rear_and_gps(source_dir):
    front_a = source_dir / "front_a.mp4"
    rear_a = source_dir / "rear_a.mp4"
    _make_video(front_a, 1.0)
    _make_video(rear_a, 1.0)

    gps_a = source_dir / "a.gps"
    gps_a.write_text(
        "[1700000000000]$GPRMC,120000.00,A,4807.038,N,01131.000,E,"
        "10.00,45.00,010124,,,A*6D\n"
    )
    gps_b = source_dir / "b.gps"
    gps_b.write_text(
        "[1700000010000]$GPRMC,120010.00,A,4808.038,N,01132.000,E,"
        "12.00,45.00,010124,,,A*6D\n"
    )

    return Trip((
        Recording(
            id=RecordingId("20260720_100000_N"),
            assets={
                Asset.FRONT: AssetFile(Asset.FRONT, front_a),
                Asset.REAR: AssetFile(Asset.REAR, rear_a),
                Asset.GPS: AssetFile(Asset.GPS, gps_a),
            },
        ),
        Recording(
            id=RecordingId("20260720_100010_N"),
            assets={Asset.GPS: AssetFile(Asset.GPS, gps_b)},
        ),
    ))


def test_export_trip_stitch_map_adds_a_panel_to_stitch_mp4(tmp_path, monkeypatch):
    monkeypatch.setattr(
        trip_export_module, "load_or_fetch_roads", _fake_roads
    )

    source_dir = tmp_path / "archive"
    source_dir.mkdir()
    dest_dir = tmp_path / "export"
    trip = _trip_with_front_rear_and_gps(source_dir)

    result_plain = export_trip(
        trip, dest_dir / "plain", stitch_layout="side_by_side",
    )
    result_with_map = export_trip(
        trip, dest_dir / "with_map",
        stitch_layout="side_by_side", stitch_map="map",
    )

    assert result_plain.warnings == ()
    assert result_with_map.warnings == ()
    assert result_with_map.stitch.exists()

    plain_size = _video_size(result_plain.stitch)
    with_map_size = _video_size(result_with_map.stitch)
    # Default side for side_by_side is 'down' - width unchanged, height
    # grows to fit the added panel.
    assert with_map_size[0] == plain_size[0]
    assert with_map_size[1] > plain_size[1]


def test_export_trip_stitch_map_side_is_forwarded(tmp_path, monkeypatch):
    monkeypatch.setattr(
        trip_export_module, "load_or_fetch_roads", _fake_roads
    )

    captured = {}
    original_stitch_cameras = trip_export_module.stitch_cameras

    def _capture_stitch_cameras(*args, **kwargs):
        captured.update(kwargs)
        return original_stitch_cameras(*args, **kwargs)

    monkeypatch.setattr(
        trip_export_module, "stitch_cameras", _capture_stitch_cameras
    )

    source_dir = tmp_path / "archive"
    source_dir.mkdir()
    dest_dir = tmp_path / "export"
    trip = _trip_with_front_rear_and_gps(source_dir)

    export_trip(
        trip, dest_dir,
        stitch_layout="top_down", stitch_map="map", stitch_map_side="right",
    )

    assert captured["map_mode"] == "map"
    assert captured["map_side"] == "right"
    assert len(captured["map_fixes"]) == 2
    assert len(captured["map_roads"]) == 1


def test_export_trip_stitch_map_size_is_forwarded(tmp_path, monkeypatch):
    monkeypatch.setattr(
        trip_export_module, "load_or_fetch_roads", _fake_roads
    )

    captured = {}
    original_stitch_cameras = trip_export_module.stitch_cameras

    def _capture_stitch_cameras(*args, **kwargs):
        captured.update(kwargs)
        return original_stitch_cameras(*args, **kwargs)

    monkeypatch.setattr(
        trip_export_module, "stitch_cameras", _capture_stitch_cameras
    )

    source_dir = tmp_path / "archive"
    source_dir.mkdir()
    dest_dir = tmp_path / "export"
    trip = _trip_with_front_rear_and_gps(source_dir)

    export_trip(
        trip, dest_dir,
        stitch_layout="top_down", stitch_map="map", stitch_map_size=35.0,
    )

    assert captured["map_size"] == 35.0


def test_export_trip_stitch_map_skipped_without_stitch_map_flag(
    tmp_path, monkeypatch
):
    def _refuse(*_args, **_kwargs):
        raise AssertionError(
            "should not fetch roads for stitch when stitch_map isn't given"
        )

    monkeypatch.setattr(trip_export_module, "load_or_fetch_roads", _refuse)

    source_dir = tmp_path / "archive"
    source_dir.mkdir()
    dest_dir = tmp_path / "export"
    trip = _trip_with_front_rear_and_gps(source_dir)

    result = export_trip(trip, dest_dir, stitch_layout="side_by_side")

    assert result.stitch.exists()
    assert result.warnings == ()


def test_export_trip_stitch_gsensor_uses_a_freshly_rendered_file(tmp_path):
    source_dir = tmp_path / "archive"
    source_dir.mkdir()
    dest_dir = tmp_path / "export"

    gsensor_a = source_dir / "a.3gf"
    gsensor_a.write_bytes(_gsensor_bytes((0, 100, -50, 900), (100, 90, -40, 950)))
    front_a = source_dir / "front_a.mp4"
    rear_a = source_dir / "rear_a.mp4"
    _make_video(front_a, 1.0)
    _make_video(rear_a, 1.0)

    trip = Trip((
        Recording(
            id=RecordingId("20260720_100000_N"),
            assets={
                Asset.FRONT: AssetFile(Asset.FRONT, front_a),
                Asset.REAR: AssetFile(Asset.REAR, rear_a),
                Asset.GSENSOR: AssetFile(Asset.GSENSOR, gsensor_a),
            },
        ),
    ))

    result = export_trip(
        trip, dest_dir,
        stitch_layout="side_by_side", render_gsensor=True, stitch_gsensor=True,
    )

    assert result.gsensor_video == dest_dir / "gsensor.mp4"
    assert result.stitch.exists()
    assert result.warnings == ()


def test_export_trip_stitch_gsensor_reuses_a_file_from_an_earlier_run(
    tmp_path
):
    # render_gsensor=False this run - gsensor.mp4 already sitting in
    # the destination folder from some earlier run should still be
    # picked up (bv-export's own keep-existing-files-by-default
    # behavior), not just this run's own fresh render.
    source_dir = tmp_path / "archive"
    source_dir.mkdir()
    dest_dir = tmp_path / "export"
    dest_dir.mkdir()

    _make_video(dest_dir / "gsensor.mp4", 1.0)

    front_a = source_dir / "front_a.mp4"
    rear_a = source_dir / "rear_a.mp4"
    _make_video(front_a, 1.0)
    _make_video(rear_a, 1.0)

    trip = Trip((
        Recording(
            id=RecordingId("20260720_100000_N"),
            assets={
                Asset.FRONT: AssetFile(Asset.FRONT, front_a),
                Asset.REAR: AssetFile(Asset.REAR, rear_a),
            },
        ),
    ))

    result = export_trip(
        trip, dest_dir, stitch_layout="side_by_side", stitch_gsensor=True,
    )

    assert result.gsensor_video is None
    assert result.stitch.exists()
    assert result.warnings == ()


def test_export_trip_stitch_gsensor_reuse_debug_prints_to_stderr(
    tmp_path, capsys
):
    # Christer: "gsensor file doesn't give any output when the video
    # already exist" - every other phase prints something to stderr
    # under --debug, but the reuse path (render_gsensor=False, an
    # existing gsensor.mp4 already sitting in the destination folder)
    # printed nothing at all, unlike a fresh render's own "gsensor
    # phase took Xs" line.
    source_dir = tmp_path / "archive"
    source_dir.mkdir()
    dest_dir = tmp_path / "export"
    dest_dir.mkdir()

    _make_video(dest_dir / "gsensor.mp4", 1.0)

    front_a = source_dir / "front_a.mp4"
    rear_a = source_dir / "rear_a.mp4"
    _make_video(front_a, 1.0)
    _make_video(rear_a, 1.0)

    trip = Trip((
        Recording(
            id=RecordingId("20260720_100000_N"),
            assets={
                Asset.FRONT: AssetFile(Asset.FRONT, front_a),
                Asset.REAR: AssetFile(Asset.REAR, rear_a),
            },
        ),
    ))

    export_trip(
        trip, dest_dir, stitch_layout="side_by_side", stitch_gsensor=True,
        debug=True,
    )

    err = capsys.readouterr().err
    assert "gsensor.mp4 already exists" in err
    assert "reusing for stitch overlay" in err


def test_export_trip_stitch_gsensor_reuse_is_silent_by_default(tmp_path, capsys):
    source_dir = tmp_path / "archive"
    source_dir.mkdir()
    dest_dir = tmp_path / "export"
    dest_dir.mkdir()

    _make_video(dest_dir / "gsensor.mp4", 1.0)

    front_a = source_dir / "front_a.mp4"
    rear_a = source_dir / "rear_a.mp4"
    _make_video(front_a, 1.0)
    _make_video(rear_a, 1.0)

    trip = Trip((
        Recording(
            id=RecordingId("20260720_100000_N"),
            assets={
                Asset.FRONT: AssetFile(Asset.FRONT, front_a),
                Asset.REAR: AssetFile(Asset.REAR, rear_a),
            },
        ),
    ))

    export_trip(
        trip, dest_dir, stitch_layout="side_by_side", stitch_gsensor=True,
    )

    assert capsys.readouterr().err == ""


def test_export_trip_stitch_gsensor_warns_when_trip_has_no_gsensor_data(
    tmp_path
):
    # This trip's recording has no GSENSOR asset at all - no flag can
    # ever produce a gsensor.mp4 for it, so the warning should say so
    # plainly rather than pointing at --gsensor-video, which would be
    # wrong advice (see the sibling "not yet rendered" test below for
    # the case where that advice is correct).
    source_dir = tmp_path / "archive"
    source_dir.mkdir()
    dest_dir = tmp_path / "export"
    trip = _trip_with_front_and_rear(source_dir)

    result = export_trip(
        trip, dest_dir, stitch_layout="side_by_side", stitch_gsensor=True,
    )

    assert result.stitch.exists()
    assert len(result.warnings) == 1
    assert "no g-sensor data for this trip" in result.warnings[0]
    assert "--gsensor-video" not in result.warnings[0]


def test_export_trip_stitch_gsensor_warns_when_gsensor_mp4_not_yet_rendered(
    tmp_path
):
    # This trip DOES have g-sensor data (a GSENSOR asset), but this
    # run neither rendered it (render_gsensor=False) nor has an
    # earlier run's gsensor.mp4 sitting in the destination folder -
    # here the "go run --gsensor-video" advice is the correct one.
    source_dir = tmp_path / "archive"
    source_dir.mkdir()
    dest_dir = tmp_path / "export"

    gsensor_a = source_dir / "a.3gf"
    gsensor_a.write_bytes(_gsensor_bytes((0, 100, -50, 900), (100, 90, -40, 950)))
    front_a = source_dir / "front_a.mp4"
    rear_a = source_dir / "rear_a.mp4"
    _make_video(front_a, 1.0)
    _make_video(rear_a, 1.0)

    trip = Trip((
        Recording(
            id=RecordingId("20260720_100000_N"),
            assets={
                Asset.FRONT: AssetFile(Asset.FRONT, front_a),
                Asset.REAR: AssetFile(Asset.REAR, rear_a),
                Asset.GSENSOR: AssetFile(Asset.GSENSOR, gsensor_a),
            },
        ),
    ))

    result = export_trip(
        trip, dest_dir, stitch_layout="side_by_side", stitch_gsensor=True,
    )

    assert result.stitch.exists()
    assert len(result.warnings) == 1
    assert "gsensor.mp4 not found" in result.warnings[0]
    assert "--gsensor-video" in result.warnings[0]


def test_export_trip_stitch_gsensor_options_are_forwarded(tmp_path, monkeypatch):
    source_dir = tmp_path / "archive"
    source_dir.mkdir()
    dest_dir = tmp_path / "export"
    dest_dir.mkdir()

    _make_video(dest_dir / "gsensor.mp4", 1.0)

    front_a = source_dir / "front_a.mp4"
    rear_a = source_dir / "rear_a.mp4"
    _make_video(front_a, 1.0)
    _make_video(rear_a, 1.0)

    trip = Trip((
        Recording(
            id=RecordingId("20260720_100000_N"),
            assets={
                Asset.FRONT: AssetFile(Asset.FRONT, front_a),
                Asset.REAR: AssetFile(Asset.REAR, rear_a),
            },
        ),
    ))

    captured = {}
    original_stitch_cameras = trip_export_module.stitch_cameras

    def _capture_stitch_cameras(*args, **kwargs):
        captured.update(kwargs)
        return original_stitch_cameras(*args, **kwargs)

    monkeypatch.setattr(
        trip_export_module, "stitch_cameras", _capture_stitch_cameras
    )

    export_trip(
        trip, dest_dir,
        stitch_layout="side_by_side", stitch_gsensor=True,
        stitch_gsensor_size=25.0, stitch_gsensor_xy=(5.0, 5.0),
    )

    assert captured["gsensor_video"] == dest_dir / "gsensor.mp4"
    assert captured["gsensor_size"] == 25.0
    assert captured["gsensor_xy"] == (5.0, 5.0)


def _trip_with_front_rear_and_subtitles(source_dir):
    front_a = source_dir / "front_a.mp4"
    rear_a = source_dir / "rear_a.mp4"
    _make_video(front_a, 1.0)
    _make_video(rear_a, 1.0)

    srt_a = source_dir / "a.srt"
    srt_a.write_text(format_srt((SpeechSegment(0.0, 1.0, "hello there"),)))

    return Trip((
        Recording(
            id=RecordingId("20260720_100000_N"),
            assets={
                Asset.FRONT: AssetFile(Asset.FRONT, front_a),
                Asset.REAR: AssetFile(Asset.REAR, rear_a),
                Asset.SUBTITLES: AssetFile(Asset.SUBTITLES, srt_a),
            },
        ),
    ))


def test_export_trip_stitch_subtitles_uses_this_runs_own_trip_srt(tmp_path):
    source_dir = tmp_path / "archive"
    source_dir.mkdir()
    dest_dir = tmp_path / "export"
    trip = _trip_with_front_rear_and_subtitles(source_dir)

    result = export_trip(
        trip, dest_dir, stitch_layout="side_by_side", stitch_subtitles=True,
    )

    # No separate "render it first" step needed, unlike
    # stitch_gsensor - trip.srt is always written earlier in this same
    # call whenever the trip has transcript data at all.
    assert result.srt == dest_dir / "trip.srt"
    assert result.stitch.exists()
    assert result.warnings == ()


def test_export_trip_stitch_subtitles_options_are_forwarded(
    tmp_path, monkeypatch
):
    source_dir = tmp_path / "archive"
    source_dir.mkdir()
    dest_dir = tmp_path / "export"
    trip = _trip_with_front_rear_and_subtitles(source_dir)

    captured = {}
    original_stitch_cameras = trip_export_module.stitch_cameras

    def _capture_stitch_cameras(*args, **kwargs):
        captured.update(kwargs)
        return original_stitch_cameras(*args, **kwargs)

    monkeypatch.setattr(
        trip_export_module, "stitch_cameras", _capture_stitch_cameras
    )

    export_trip(
        trip, dest_dir,
        stitch_layout="side_by_side", stitch_subtitles=True,
        stitch_subtitles_background=False,
    )

    assert captured["subtitles_path"] == dest_dir / "trip.srt"
    assert captured["subtitles_background"] is False


def test_export_trip_stitch_subtitles_skipped_without_the_flag(
    tmp_path, monkeypatch
):
    source_dir = tmp_path / "archive"
    source_dir.mkdir()
    dest_dir = tmp_path / "export"
    trip = _trip_with_front_rear_and_subtitles(source_dir)

    captured = {}
    original_stitch_cameras = trip_export_module.stitch_cameras

    def _capture_stitch_cameras(*args, **kwargs):
        captured.update(kwargs)
        return original_stitch_cameras(*args, **kwargs)

    monkeypatch.setattr(
        trip_export_module, "stitch_cameras", _capture_stitch_cameras
    )

    result = export_trip(trip, dest_dir, stitch_layout="side_by_side")

    # trip.srt still gets written (merge_srt() isn't gated behind
    # stitch_subtitles at all), but it's not passed on to the stitch
    # call without the flag.
    assert result.srt == dest_dir / "trip.srt"
    assert captured["subtitles_path"] is None


def test_export_trip_stitch_subtitles_warns_when_no_transcript_data(
    tmp_path
):
    source_dir = tmp_path / "archive"
    source_dir.mkdir()
    dest_dir = tmp_path / "export"
    trip = _trip_with_front_and_rear(source_dir)

    result = export_trip(
        trip, dest_dir, stitch_layout="side_by_side", stitch_subtitles=True,
    )

    assert result.srt is None
    assert result.stitch.exists()
    assert len(result.warnings) == 1
    assert "no transcript data" in result.warnings[0]


def test_export_trip_merges_srt_and_lrc_with_rebased_timestamps(tmp_path):
    source_dir = tmp_path / "archive"
    source_dir.mkdir()
    dest_dir = tmp_path / "export"

    srt_a = source_dir / "a.srt"
    srt_a.write_text(
        format_srt((SpeechSegment(0.0, 2.0, "first recording"),))
    )
    lrc_a = source_dir / "a.lrc"
    lrc_a.write_text(format_lrc((SpeechSegment(0.0, 0.0, "first recording"),)))

    srt_b = source_dir / "b.srt"
    srt_b.write_text(
        format_srt((SpeechSegment(0.0, 1.0, "second recording"),))
    )
    lrc_b = source_dir / "b.lrc"
    lrc_b.write_text(
        format_lrc((SpeechSegment(0.0, 0.0, "second recording"),))
    )

    first = Recording(
        id=RecordingId("20260720_100000_N"),
        assets={
            Asset.SUBTITLES: AssetFile(Asset.SUBTITLES, srt_a),
            Asset.LYRICS: AssetFile(Asset.LYRICS, lrc_a),
        },
    )
    second = Recording(
        id=RecordingId("20260720_100100_N"),
        assets={
            Asset.SUBTITLES: AssetFile(Asset.SUBTITLES, srt_b),
            Asset.LYRICS: AssetFile(Asset.LYRICS, lrc_b),
        },
    )
    trip = Trip((first, second))

    result = export_trip(trip, dest_dir)

    assert result.srt == dest_dir / "trip.srt"
    srt_text = result.srt.read_text()
    assert "00:00:00,000 --> 00:00:02,000" in srt_text
    assert "first recording" in srt_text
    # Second recording started 60s after the first.
    assert "00:01:00,000 --> 00:01:01,000" in srt_text
    assert "second recording" in srt_text

    assert result.lrc == dest_dir / "trip.lrc"
    lrc_text = result.lrc.read_text()
    assert "[00:00.00] first recording" in lrc_text
    assert "[01:00.00] second recording" in lrc_text


def test_export_trip_skips_srt_lrc_when_no_recording_has_them(tmp_path):
    dest_dir = tmp_path / "export"
    trip = Trip((Recording(id=RecordingId("20260720_100000_N")),))

    result = export_trip(trip, dest_dir)

    assert result.srt is None
    assert result.lrc is None
    assert not (dest_dir / "trip.srt").exists()
    assert not (dest_dir / "trip.lrc").exists()


def test_export_trip_always_writes_a_trip_log(tmp_path):
    dest_dir = tmp_path / "export"
    trip = Trip((Recording(id=RecordingId("20260720_100000_N")),))

    export_trip(trip, dest_dir)

    log_text = (dest_dir / "trip.log").read_text(encoding="utf-8")
    assert "=== bv-export trip log:" in log_text
    assert trip.label in log_text
    assert "Started:" in log_text
    assert "Finished:" in log_text


def test_export_trip_writes_the_given_command_line_into_the_trip_log(tmp_path):
    dest_dir = tmp_path / "export"
    trip = Trip((Recording(id=RecordingId("20260720_100000_N")),))

    export_trip(
        trip, dest_dir, command_line="bv-export --target out --stitch"
    )

    log_text = (dest_dir / "trip.log").read_text(encoding="utf-8")
    assert "Command: bv-export --target out --stitch" in log_text


def test_export_trip_writes_membership_reasons_into_the_trip_log(tmp_path):
    dest_dir = tmp_path / "export"
    first = Recording(id=RecordingId("20260720_100000_N"))
    second = Recording(id=RecordingId("20260720_100100_N"))
    trip = Trip((first, second))

    reasons = {
        first.id: "first recording in the archive",
        second.id: "continues the trip - gap since ... was 60.0s, within threshold",
    }

    export_trip(trip, dest_dir, reasons=reasons)

    log_text = (dest_dir / "trip.log").read_text(encoding="utf-8")
    assert "--- Trip membership ---" in log_text
    assert f"{first.id}: first recording in the archive" in log_text
    assert f"{second.id}: continues the trip" in log_text


def test_export_trip_logs_concatenation_and_gpx_steps(tmp_path):
    source_dir = tmp_path / "archive"
    source_dir.mkdir()
    dest_dir = tmp_path / "export"

    front_a = source_dir / "front_a.mp4"
    _make_video(front_a, 1.0)
    gps_a = source_dir / "a.gps"
    gps_a.write_text(
        "[1700000000000]$GPRMC,120000.00,A,4807.038,N,01131.000,E,"
        "10.00,45.00,010124,,,A*6D\n"
    )

    trip = Trip((
        Recording(
            id=RecordingId("20260720_100000_N"),
            assets={
                Asset.FRONT: AssetFile(Asset.FRONT, front_a),
                Asset.GPS: AssetFile(Asset.GPS, gps_a),
            },
        ),
    ))

    export_trip(trip, dest_dir)

    log_text = (dest_dir / "trip.log").read_text(encoding="utf-8")
    assert "--- Export steps ---" in log_text
    assert "starting concatenation (front/rear/audio)" in log_text
    assert "concatenated front.mp4 from 1 recording(s)" in log_text
    assert "no source recordings for rear.mp4 - skipped" in log_text
    assert "wrote trip.gpx (1 fix(es))" in log_text


def test_export_trip_logs_a_starting_line_before_the_stitch_render(tmp_path):
    source_dir = tmp_path / "archive"
    source_dir.mkdir()
    dest_dir = tmp_path / "export"

    front_a = source_dir / "front_a.mp4"
    _make_video(front_a, 1.0)

    trip = Trip((
        Recording(
            id=RecordingId("20260720_100000_N"),
            assets={Asset.FRONT: AssetFile(Asset.FRONT, front_a)},
        ),
    ))

    export_trip(trip, dest_dir, stitch_layout="side_by_side")

    log_text = (dest_dir / "trip.log").read_text(encoding="utf-8")
    # A "starting" line lets a hung run be diagnosed by which phase it
    # was in, not just phases that finished (see trip_log.py's own
    # docstring) - it must appear even for a fast test render.
    assert "starting stitch.mp4 render (layout=side_by_side)" in log_text
    assert "rendered stitch.mp4" in log_text


def test_export_trip_pads_srt_lrc_to_match_the_real_video_length(tmp_path):
    # Christer's real-world case: the last stretch of a trip is quiet,
    # so Whisper's segments (and the resulting .srt/.lrc) end well
    # before the video actually does. export_trip() should pad the
    # merged subtitle files out to the concatenated video's real
    # (ffprobe-measured) length.
    source_dir = tmp_path / "archive"
    source_dir.mkdir()
    dest_dir = tmp_path / "export"

    video_path = source_dir / "front.mp4"
    _make_video(video_path, duration_seconds=5.0)

    srt_path = source_dir / "a.srt"
    srt_path.write_text(
        format_srt((SpeechSegment(0.0, 1.0, "hello"),))
    )
    lrc_path = source_dir / "a.lrc"
    lrc_path.write_text(
        format_lrc((SpeechSegment(0.0, 0.0, "hello"),))
    )

    recording = Recording(
        id=RecordingId("20260720_100000_N"),
        assets={
            Asset.FRONT: AssetFile(Asset.FRONT, video_path),
            Asset.SUBTITLES: AssetFile(Asset.SUBTITLES, srt_path),
            Asset.LYRICS: AssetFile(Asset.LYRICS, lrc_path),
        },
    )
    trip = Trip((recording,))

    result = export_trip(trip, dest_dir)

    srt_text = result.srt.read_text()
    assert "hello" in srt_text
    # A second, empty cue was appended ending at (approximately) the
    # video's real 5s length - not stopping at 1s where "hello" ended.
    assert "\n2\n" in srt_text
    assert "--> 00:00:05,000" in srt_text

    lrc_text = result.lrc.read_text()
    lines = lrc_text.splitlines()
    assert lines[0] == "[00:00.00] hello"
    assert len(lines) == 2
    assert lines[1].startswith("[00:0")  # padding line near the 5s mark
