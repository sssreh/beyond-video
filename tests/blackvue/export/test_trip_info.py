from datetime import datetime, timedelta

from blackvue.export.trip_info import write_trip_info
from blackvue.export.trip_stats import TripStats


def test_write_trip_info_minimal_file_is_just_duration(tmp_path):
    path = tmp_path / "trip_info.txt"

    write_trip_info(path, duration=timedelta(seconds=0))

    assert path.read_text(encoding="utf-8") == "Duration: 0:00:00\n"


def test_write_trip_info_full_file_has_every_field_in_order(tmp_path):
    path = tmp_path / "trip_info.txt"
    stats = TripStats(
        distance_km=3.456,
        average_speed_kmh=42.34,
        max_speed_kmh=61.0,
        moving_seconds=310.0,
        idle_seconds=31.0,
    )

    write_trip_info(
        path,
        duration=timedelta(minutes=5, seconds=41),
        start_timestamp=datetime(2026, 7, 15, 14, 33, 40),
        end_timestamp=datetime(2026, 7, 15, 14, 39, 21),
        stats=stats,
        start_address="1 Fake Street, Fake City",
        end_address="2 Fake Avenue, Fake City",
        has_parking_footage=True,
        total_size_bytes=512 * 1024 * 1024,
    )

    assert path.read_text(encoding="utf-8") == (
        "Started: 2026-07-15 14:33:40\n"
        "Ended: 2026-07-15 14:39:21\n"
        "Duration: 0:05:41\n"
        "Distance: 3.46 km\n"
        "Average speed: 42.3 km/h\n"
        "Max speed: 61.0 km/h\n"
        "Moving time: 0:05:10\n"
        "Idle time: 0:00:31\n"
        "Start location: 1 Fake Street, Fake City\n"
        "End location: 2 Fake Avenue, Fake City\n"
        "Includes Parking-mode footage\n"
        "Total size: 512.00 MB\n"
    )


def test_write_trip_info_omits_stats_with_no_speed_data_but_keeps_distance(tmp_path):
    path = tmp_path / "trip_info.txt"
    stats = TripStats(
        distance_km=1.0,
        average_speed_kmh=None,
        max_speed_kmh=None,
        moving_seconds=None,
        idle_seconds=None,
    )

    write_trip_info(path, duration=timedelta(minutes=1), stats=stats)

    text = path.read_text(encoding="utf-8")
    assert "Distance: 1.00 km" in text
    assert "Average speed" not in text
    assert "Max speed" not in text
    assert "Moving time" not in text
    assert "Idle time" not in text


def test_write_trip_info_omits_parking_line_when_not_parking(tmp_path):
    path = tmp_path / "trip_info.txt"

    write_trip_info(path, duration=timedelta(minutes=1), has_parking_footage=False)

    assert "Parking-mode" not in path.read_text(encoding="utf-8")


def test_write_trip_info_omits_size_line_when_unknown(tmp_path):
    path = tmp_path / "trip_info.txt"

    write_trip_info(path, duration=timedelta(minutes=1), total_size_bytes=None)

    assert "Total size" not in path.read_text(encoding="utf-8")


def test_write_trip_info_size_formatting_across_units(tmp_path):
    cases = [
        (500, "500 B"),
        (2048, "2.00 KB"),
        (5 * 1024 * 1024, "5.00 MB"),
        (3 * 1024 * 1024 * 1024, "3.00 GB"),
    ]

    for size_bytes, expected in cases:
        path = tmp_path / f"trip_info_{size_bytes}.txt"
        write_trip_info(
            path, duration=timedelta(minutes=1), total_size_bytes=size_bytes
        )
        assert f"Total size: {expected}" in path.read_text(encoding="utf-8")


def test_write_trip_info_addresses_are_independently_optional(tmp_path):
    path = tmp_path / "trip_info.txt"

    write_trip_info(
        path,
        duration=timedelta(minutes=1),
        start_address="Only the start is known",
        end_address=None,
    )

    text = path.read_text(encoding="utf-8")
    assert "Start location: Only the start is known" in text
    assert "End location" not in text
