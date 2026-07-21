from datetime import datetime
from xml.etree import ElementTree as ET

from blackvue.export.gpx_writer import write_gpx
from blackvue.telemetry.gps_reader import GpsFix

# Fabricated coordinates/timestamps for testing.

_NAMESPACE = "{http://www.topografix.com/GPX/1/1}"


def _fix(ts: str, lat=59.0, lon=18.0, speed_kmh=36.0, course=90.0, valid=True):
    return GpsFix(
        timestamp=datetime.strptime(ts, "%Y%m%d_%H%M%S"),
        valid=valid,
        latitude=lat if valid else None,
        longitude=lon if valid else None,
        speed_kmh=speed_kmh if valid else None,
        course=course if valid else None,
    )


def test_write_gpx_produces_valid_xml_with_expected_points(tmp_path):
    fixes = (
        _fix("20260720_100000", lat=59.0, lon=18.0),
        _fix("20260720_100010", lat=59.001, lon=18.001),
    )

    path = tmp_path / "trip.gpx"
    write_gpx(fixes, path, name="trip_20260720_100000_20260720_100010")

    tree = ET.parse(path)
    root = tree.getroot()

    assert root.tag == f"{_NAMESPACE}gpx"

    name_el = root.find(f"{_NAMESPACE}trk/{_NAMESPACE}name")
    assert name_el.text == "trip_20260720_100000_20260720_100010"

    points = root.findall(
        f"{_NAMESPACE}trk/{_NAMESPACE}trkseg/{_NAMESPACE}trkpt"
    )
    assert len(points) == 2
    assert points[0].attrib["lat"] == repr(59.0)
    assert points[0].attrib["lon"] == repr(18.0)

    time_el = points[0].find(f"{_NAMESPACE}time")
    assert time_el.text == "2026-07-20T10:00:00Z"


def test_write_gpx_skips_invalid_fixes(tmp_path):
    fixes = (
        _fix("20260720_100000", valid=False),
        _fix("20260720_100010", lat=59.0, lon=18.0),
    )

    path = tmp_path / "trip.gpx"
    write_gpx(fixes, path)

    tree = ET.parse(path)
    points = tree.getroot().findall(
        f"{_NAMESPACE}trk/{_NAMESPACE}trkseg/{_NAMESPACE}trkpt"
    )
    assert len(points) == 1


def test_write_gpx_includes_speed_and_course_extensions(tmp_path):
    fixes = (_fix("20260720_100000", speed_kmh=36.0, course=90.0),)

    path = tmp_path / "trip.gpx"
    write_gpx(fixes, path)

    tree = ET.parse(path)
    trkpt = tree.getroot().find(
        f"{_NAMESPACE}trk/{_NAMESPACE}trkseg/{_NAMESPACE}trkpt"
    )
    speed_el = trkpt.find(f"{_NAMESPACE}extensions/{_NAMESPACE}speed")
    course_el = trkpt.find(f"{_NAMESPACE}extensions/{_NAMESPACE}course")

    # 36 km/h -> 10 m/s.
    assert float(speed_el.text) == 10.0
    assert float(course_el.text) == 90.0


def test_write_gpx_handles_no_fixes(tmp_path):
    path = tmp_path / "trip.gpx"
    write_gpx((), path)

    tree = ET.parse(path)
    points = tree.getroot().findall(
        f"{_NAMESPACE}trk/{_NAMESPACE}trkseg/{_NAMESPACE}trkpt"
    )
    assert points == []
