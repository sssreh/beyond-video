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
    # From the sibling $GPGGA sentence sharing this fix's own bracket
    # timestamp (see GpsFix.altitude_meters's own docstring) - the
    # fixture's GGA line above has altitude field "31.2".
    assert fix.altitude_meters == 31.2


def test_read_gps_treats_no_fix_mode_as_invalid_with_no_position(
    tmp_path,
):
    # Both the status field (V) and the mode indicator (N, the last
    # field) agree here - a genuine "no fix at all" sentence, the
    # common case right after a receiver powers on cold.
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


def test_read_gps_treats_void_status_with_a_computed_position_as_valid(
    tmp_path,
):
    # Regression test for a real case Christer found on his own
    # archive: the receiver reports status='V' (not yet confirmed to
    # its own stricter internal accuracy threshold) well after the
    # mode indicator already reports 'A' (a real position has been
    # computed) - confirmed against real files as smooth, physically
    # continuous, non-jumping GPS data, not noise. GpsFix.valid must
    # follow the mode indicator here, not the older status field, or
    # this entire stretch of a real drive's track goes missing.
    path = tmp_path / "sample.gps"
    path.write_text(
        "[1700000001000]$GPRMC,120001.00,V,4807.038,N,01131.000,E,"
        "10.00,45.00,010124,,,A*6D\n"
    )

    fixes = read_gps(path)

    assert len(fixes) == 1
    fix = fixes[0]

    assert fix.valid is True
    assert fix.latitude == 48 + 7.038 / 60
    assert fix.longitude == 11 + 31 / 60
    assert round(fix.speed_kmh, 3) == round(10.00 * 1.852, 3)
    assert fix.course == 45.00


def test_read_gps_uses_the_mode_indicator_not_the_status_field(tmp_path):
    # A contrived, not-seen-in-the-wild combination (status='A' but
    # mode='N') specifically to prove which field actually drives
    # `valid` - if this ever passed with valid=True, that would mean
    # the code regressed to reading the older status field again.
    path = tmp_path / "sample.gps"
    path.write_text(
        "[1700000000000]$GPRMC,120000.00,A,4807.038,N,01131.000,E,"
        "10.00,45.00,010124,,,N*00\n"
    )

    fixes = read_gps(path)

    assert fixes[0].valid is False


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


# ---------------------------------------------------------------------------
# Malformed field *values* (not just the wrong field count) - regression
# tests for a real "Internal Server Error" Christer hit on bv-web's archive
# detail page for an older recording. _parse_rmc()'s docstring always
# claimed a too-malformed sentence returns None, but only the field-count
# check actually did that - a coordinate with no decimal point, or a
# non-numeric speed/course, raised a raw ValueError straight out of
# read_gps() instead. Every caller (bv-export's _merge_gps(), bv-web's
# archive_recording_location route) only guards MediaToolError, so this
# used to be an uncaught crash rather than "skip this one bad sentence."
# ---------------------------------------------------------------------------


def test_read_gps_skips_a_sentence_with_a_malformed_coordinate(tmp_path):
    path = tmp_path / "sample.gps"
    path.write_text(
        # Latitude field has no decimal point - a real coordinate
        # never looks like this, but corrupted data (e.g. the camera
        # losing power mid-write) can produce exactly this shape.
        "[1700000000000]$GPRMC,120000.00,A,4807,N,01131.000,E,"
        "10.00,45.00,010124,,,A*00\n"
        "[1700000001000]$GPRMC,120001.00,A,4807.038,N,01131.000,E,"
        "10.00,45.00,010124,,,A*6D\n"
    )

    fixes = read_gps(path)

    # The malformed sentence is skipped entirely, not raised - only the
    # well-formed second one comes through.
    assert len(fixes) == 1
    assert fixes[0].timestamp == datetime.utcfromtimestamp(1700000001.0)


def test_read_gps_skips_a_sentence_with_a_non_numeric_speed(tmp_path):
    path = tmp_path / "sample.gps"
    path.write_text(
        "[1700000000000]$GPRMC,120000.00,A,4807.038,N,01131.000,E,"
        "notanumber,45.00,010124,,,A*00\n"
    )

    assert read_gps(path) == ()


# ---------------------------------------------------------------------------
# Altitude ($GPGGA enrichment) - Christer asked whether height could be
# calculated from the GPS data at all, for a future stitch-video/playback
# overlay. See GpsFix.altitude_meters's and read_gps()'s own docstrings.
# ---------------------------------------------------------------------------


def test_read_gps_altitude_is_none_without_a_matching_gga_sentence(tmp_path):
    # No $GPGGA at all in this file - a legitimate real-world shape
    # (e.g. an older recording from before this reader parsed GGA),
    # not something that should crash or silently invent a value.
    path = tmp_path / "sample.gps"
    path.write_text(
        "[1700000001000]$GPRMC,120001.00,A,4807.038,N,01131.000,E,"
        "10.00,45.00,010124,,,A*6D\n"
    )

    fixes = read_gps(path)

    assert len(fixes) == 1
    assert fixes[0].altitude_meters is None


def test_read_gps_matches_gga_altitude_by_shared_bracket_timestamp_only(tmp_path):
    # Two ticks, each with its own RMC+GGA pair at a different bracket
    # timestamp - the second tick's altitude must not leak onto the
    # first tick's fix (or vice versa), proving the match is by shared
    # timestamp, not "whichever GGA appeared most recently in the
    # file".
    path = tmp_path / "sample.gps"
    path.write_text(
        "[1700000001000]$GPGGA,120001.00,4807.038,N,01131.000,E,1,"
        "04,2.35,31.2,M,24.3,M,,*67\n"
        "[1700000001000]$GPRMC,120001.00,A,4807.038,N,01131.000,E,"
        "10.00,45.00,010124,,,A*6D\n"
        "[1700000002000]$GPGGA,120002.00,4807.038,N,01131.000,E,1,"
        "04,2.35,99.9,M,24.3,M,,*68\n"
        "[1700000002000]$GPRMC,120002.00,A,4807.038,N,01131.000,E,"
        "10.00,45.00,010124,,,A*6E\n"
    )

    fixes = read_gps(path)

    assert len(fixes) == 2
    assert fixes[0].altitude_meters == 31.2
    assert fixes[1].altitude_meters == 99.9


def test_read_gps_altitude_is_none_when_the_gga_sentence_is_malformed(tmp_path):
    # A $GPGGA with the wrong field count (corrupted, same class of
    # real-world damage _parse_rmc()'s own malformed-sentence tests
    # cover) must not crash read_gps() or poison the RMC fix - just
    # leave altitude_meters as None for that tick.
    path = tmp_path / "sample.gps"
    path.write_text(
        "[1700000001000]$GPGGA,120001.00,4807.038,N,01131.000,E,1*67\n"
        "[1700000001000]$GPRMC,120001.00,A,4807.038,N,01131.000,E,"
        "10.00,45.00,010124,,,A*6D\n"
    )

    fixes = read_gps(path)

    assert len(fixes) == 1
    assert fixes[0].altitude_meters is None
