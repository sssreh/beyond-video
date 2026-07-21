import struct
import subprocess
from datetime import timedelta

from blackvue.archive.asset import Asset
from blackvue.archive.asset_file import AssetFile
from blackvue.archive.recording import Recording
from blackvue.archive.recording_id import RecordingId
from blackvue.export.trip_export import export_trip
from blackvue.export.trip_export import folder_name_for_trip
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
