"""
Tests for archive/exif.py - read_exif(), exif_datetime_original(),
exif_gps_fix(), normalize_photo_orientation().

Christer, following the GIF classification question: "Maybe we need
exif now." All fixtures write real EXIF data via Pillow's own
Image.Exif mapping (im.getexif(), passed back into im.save(exif=...))
so these tests exercise the real Pillow read/write round trip rather
than hand-rolled EXIF bytes.
"""

from datetime import datetime
from pathlib import Path

from PIL import Image

from blackvue.archive.exif import exif_datetime_original
from blackvue.archive.exif import exif_gps_fix
from blackvue.archive.exif import normalize_photo_orientation
from blackvue.archive.exif import read_exif
from blackvue.telemetry.gps_reader import GpsFix

# Tag ids match archive/exif.py's own private constants - duplicated
# here (not imported) so these tests would actually notice if the
# module started reading the wrong tag id.
_TAG_ORIENTATION = 274
_TAG_DATETIME_ORIGINAL = 36867
_TAG_GPS_IFD = 34853


def _make_plain_photo(path: Path, size=(100, 60)) -> None:
    Image.new("RGB", size, (200, 100, 50)).save(path)


def _make_photo_with_exif(
    path: Path,
    *,
    size=(100, 60),
    orientation: int | None = None,
    datetime_original: str | None = None,
    gps: dict | None = None,
) -> None:
    image = Image.new("RGB", size, (200, 100, 50))
    exif = image.getexif()
    if orientation is not None:
        exif[_TAG_ORIENTATION] = orientation
    if datetime_original is not None:
        exif[_TAG_DATETIME_ORIGINAL] = datetime_original
    if gps is not None:
        exif[_TAG_GPS_IFD] = gps
    image.save(path, exif=exif)


# ---------------------------------------------------------------------------
# read_exif()
# ---------------------------------------------------------------------------


def test_read_exif_returns_none_for_a_photo_with_no_exif(tmp_path):
    path = tmp_path / "plain.jpg"
    _make_plain_photo(path)

    assert read_exif(path) is None


def test_read_exif_returns_none_for_an_unreadable_file(tmp_path):
    path = tmp_path / "corrupt.jpg"
    path.write_bytes(b"not a real jpeg")

    assert read_exif(path) is None


def test_read_exif_returns_none_for_a_missing_file(tmp_path):
    assert read_exif(tmp_path / "does_not_exist.jpg") is None


def test_read_exif_returns_data_when_present(tmp_path):
    path = tmp_path / "tagged.jpg"
    _make_photo_with_exif(path, datetime_original="2026:07:15 13:32:55")

    exif = read_exif(path)

    assert exif is not None
    assert exif.get(_TAG_DATETIME_ORIGINAL) == "2026:07:15 13:32:55"


# ---------------------------------------------------------------------------
# exif_datetime_original()
# ---------------------------------------------------------------------------


def test_exif_datetime_original_parses_a_real_tag(tmp_path):
    path = tmp_path / "tagged.jpg"
    _make_photo_with_exif(path, datetime_original="2026:07:15 13:32:55")

    assert exif_datetime_original(path) == datetime(2026, 7, 15, 13, 32, 55)


def test_exif_datetime_original_returns_none_without_the_tag(tmp_path):
    path = tmp_path / "plain.jpg"
    _make_plain_photo(path)

    assert exif_datetime_original(path) is None


def test_exif_datetime_original_returns_none_for_unparseable_value(tmp_path):
    path = tmp_path / "tagged.jpg"
    # Wrong format (dashes instead of colons in the date part) - EXIF's
    # own DateTimeOriginal format is "YYYY:MM:DD HH:MM:SS".
    _make_photo_with_exif(path, datetime_original="2026-07-15 13:32:55")

    assert exif_datetime_original(path) is None


# ---------------------------------------------------------------------------
# exif_gps_fix()
# ---------------------------------------------------------------------------


def test_exif_gps_fix_converts_dms_to_signed_decimal_degrees(tmp_path):
    path = tmp_path / "tagged.jpg"
    _make_photo_with_exif(
        path,
        gps={
            1: "N",
            2: (59.0, 17.0, 34.0),
            3: "E",
            4: (18.0, 5.0, 17.0),
        },
    )
    timestamp = datetime(2026, 7, 15, 13, 32, 55)

    fix = exif_gps_fix(path, timestamp=timestamp)

    assert isinstance(fix, GpsFix)
    assert fix.timestamp == timestamp
    assert fix.valid is True
    assert fix.latitude is not None and abs(fix.latitude - 59.2928) < 0.001
    assert fix.longitude is not None and abs(fix.longitude - 18.0881) < 0.001
    assert fix.speed_kmh is None
    assert fix.course is None


def test_exif_gps_fix_applies_south_and_west_as_negative(tmp_path):
    path = tmp_path / "tagged.jpg"
    _make_photo_with_exif(
        path,
        gps={
            1: "S",
            2: (33.0, 55.0, 0.0),
            3: "W",
            4: (18.0, 25.0, 0.0),
        },
    )

    fix = exif_gps_fix(path, timestamp=datetime(2026, 1, 1))

    assert fix.latitude < 0
    assert fix.longitude < 0


def test_exif_gps_fix_returns_none_without_a_gps_ifd(tmp_path):
    path = tmp_path / "tagged.jpg"
    _make_photo_with_exif(path, datetime_original="2026:07:15 13:32:55")

    assert exif_gps_fix(path, timestamp=datetime(2026, 1, 1)) is None


def test_exif_gps_fix_returns_none_for_a_photo_with_no_exif(tmp_path):
    path = tmp_path / "plain.jpg"
    _make_plain_photo(path)

    assert exif_gps_fix(path, timestamp=datetime(2026, 1, 1)) is None


# ---------------------------------------------------------------------------
# normalize_photo_orientation()
# ---------------------------------------------------------------------------


def test_normalize_photo_orientation_rotates_a_sideways_photo(tmp_path):
    source = tmp_path / "sideways.jpg"
    destination = tmp_path / "fixed.png"
    # Orientation 6 = rotate 90 CW to display correctly - a 100x60
    # (landscape) source should come out 60x100 (portrait) once
    # corrected.
    _make_photo_with_exif(source, size=(100, 60), orientation=6)

    changed = normalize_photo_orientation(source, destination)

    assert changed is True
    assert destination.exists()
    with Image.open(destination) as fixed:
        assert fixed.size == (60, 100)
        # The Orientation tag should be stripped after correction, so
        # nothing downstream (ffmpeg included) could ever double-apply
        # it.
        fixed_exif = fixed.getexif()
        assert fixed_exif.get(_TAG_ORIENTATION) is None


def test_normalize_photo_orientation_returns_false_without_a_tag(tmp_path):
    source = tmp_path / "plain.jpg"
    destination = tmp_path / "fixed.png"
    _make_plain_photo(source)

    assert normalize_photo_orientation(source, destination) is False
    assert not destination.exists()


def test_normalize_photo_orientation_returns_false_for_normal_orientation(tmp_path):
    source = tmp_path / "tagged.jpg"
    destination = tmp_path / "fixed.png"
    # Orientation 1 = already right-side-up - nothing to correct.
    _make_photo_with_exif(source, orientation=1)

    assert normalize_photo_orientation(source, destination) is False
    assert not destination.exists()


def test_normalize_photo_orientation_returns_false_for_an_unreadable_file(tmp_path):
    source = tmp_path / "corrupt.jpg"
    destination = tmp_path / "fixed.png"
    source.write_bytes(b"not a real jpeg")

    assert normalize_photo_orientation(source, destination) is False
    assert not destination.exists()
