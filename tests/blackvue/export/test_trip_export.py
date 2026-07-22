import struct
import subprocess
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


def _trip_with_two_gps_fixes(source_dir):
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

    first = Recording(
        id=RecordingId("20260720_100000_N"),
        assets={Asset.GPS: AssetFile(Asset.GPS, gps_a)},
    )
    second = Recording(
        id=RecordingId("20260720_100010_N"),
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
