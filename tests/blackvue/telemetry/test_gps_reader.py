from datetime import datetime
from pathlib import Path

from blackvue.telemetry.gps_reader import _nmea_coordinate_to_decimal
from blackvue.telemetry.gps_reader import read_gps

# All coordinates/timestamps below are fabricated for testing - not
# derived from any real recording.


def test_nmea_coordinate_to_decimal_handles_latitude_north():
    # 48 degrees, 07.038 minutes.
    assert _nmea_coordinate_to_decimal("4807.038", "N") == 48 + 7.038 / 60


def test_nmea_coordinate_to_decimal_handles_longitude_east_three_digit_degrees():
    # 011 degrees, 31.000 minutes - the extra leading degree digit is
    # what distinguishes longitude from latitude in NMEA, and the
    # decimal-point-relative parsing must handle it the same way.
    assert _nmea_coordinate_to_decimal("01131.000", "E") == 11 + 31 / 60


def test_nmea_coordinate_to_decimal_negates_south_and_west():
    north = _nmea_coordinate_to_decimal("4807.038", "N")
    south = _nmea_coordinate_to_decimal("4807.038", "S")
    east = _nmea_coordinate_to_decimal("01131.000", "E")
    west = _nmea_coordinate_to_decimal("01131.000", "W")

    assert south == -north
    assert west == -east


def test_read_gps_parses_a_valid_fix(tmp_path):
    path = tmp_path / "sample.gps"
    path.write_text(
        "[1700000001000]$GPGGA,120001.00,4807.038,N,01131.000,E,1,"
        "04,2.35,31.2,M,24.3,M,,*67\n"
        "\n"
        "[1700000001000]$GPRMC,120001.00,A,4807.038,N,01131.000,E,"
        "10.00,45.00,010124,,,A*6D\n"
        "\n"
        "[1700000001000]$GPVTG,45.00,T,,M,10.00,N,18.52,K,A*05\n"
    )

    fixes = read_gps(path)

    assert len(fixes) == 1
    fix = fixes[0]

    assert fix.timestamp == datetime.utcfromtimestamp(1700000001.0)
    assert fix.valid is True
    assert fix.latitude == 48 + 7.038 / 60
    assert fix.longitude == 11 + 31 / 60
    # 10.00 knots -> km/h.
    assert round(fix.speed_kmh, 3) == round(10.00 * 1.852, 3)
    assert fix.course == 45.00


def test_read_gps_treats_no_fix_status_as_invalid_with_no_position(
    tmp_path,
):
    path = tmp_path / "sample.gps"
    path.write_text(
        "[1700000000000]$GPRMC,120000.00,V,,,,,,,010124,,,N*7F\n"
    )

    fixes = read_gps(path)

    assert len(fixes) == 1
    fix = fixes[0]

    assert fix.valid is False
    assert fix.latitude is None
    assert fix.longitude is None
    assert fix.speed_kmh is None
    assert fix.course is None


def test_read_gps_handles_sentences_concatenated_without_a_newline(
    tmp_path,
):
    # Real files sometimes end one sentence and immediately start the
    # next bracket group with no newline in between - the parser must
    # not rely on line boundaries.
    path = tmp_path / "sample.gps"
    path.write_text(
        "[1700000000000]$GPRMC,120000.00,V,,,,,,,010124,,,N*7F"
        "[1700000001000]$GPRMC,120001.00,A,4807.038,N,01131.000,E,"
        "10.00,45.00,010124,,,A*6D"
    )

    fixes = read_gps(path)

    assert len(fixes) == 2
    assert fixes[0].valid is False
    assert fixes[1].valid is True
    assert fixes[1].timestamp == datetime.utcfromtimestamp(1700000001.0)


def test_read_gps_ignores_non_rmc_sentences(tmp_path):
    path = tmp_path / "sample.gps"
    path.write_text(
        "[1700000000000]$GPGSA,A,1,,,,,,,,,,,,,99.99,99.99,99.99*30\n"
        "[1700000000000]$GPGSV,4,1,13,03,29,120,,04,74,100,*7D\n"
        "[1700000000000]$GPGLL,,,,,120000.00,V,N*4A\n"
    )

    fixes = read_gps(path)

    assert fixes == ()


def test_read_gps_returns_empty_tuple_for_empty_file(tmp_path):
    path = tmp_path / "empty.gps"
    path.write_text("")

    assert read_gps(path) == ()
