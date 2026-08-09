from xml.etree import ElementTree as ET

from blackvue.export.kml_writer import gpx_to_kml

# Matches gpx_writer's own namespace - see test_gpx_writer.py's identical
# constant.
_GPX_NAMESPACE = "{http://www.topografix.com/GPX/1/1}"
_KML_NAMESPACE = "{http://www.opengis.net/kml/2.2}"

_SAMPLE_GPX = """<?xml version="1.0" encoding="UTF-8"?>
<gpx version="1.1" creator="beyond-video bv-export" xmlns="http://www.topografix.com/GPX/1/1">
  <trk>
    <trkseg>
      <trkpt lat="59.334591" lon="18.06324">
        <time>2026-07-15T13:34:58Z</time>
      </trkpt>
      <trkpt lat="59.335" lon="18.064">
        <time>2026-07-15T13:35:08Z</time>
      </trkpt>
    </trkseg>
  </trk>
</gpx>
"""

_EMPTY_GPX = """<?xml version="1.0" encoding="UTF-8"?>
<gpx version="1.1" creator="beyond-video bv-export" xmlns="http://www.topografix.com/GPX/1/1">
  <trk>
    <trkseg></trkseg>
  </trk>
</gpx>
"""


def test_gpx_to_kml_produces_valid_xml_with_track_and_start_point(tmp_path):
    gpx_path = tmp_path / "trip.gpx"
    gpx_path.write_text(_SAMPLE_GPX)

    kml_text = gpx_to_kml(gpx_path, name="trip_20260715_133458_20260715_141235")
    assert kml_text is not None

    root = ET.fromstring(kml_text)
    assert root.tag == f"{_KML_NAMESPACE}kml"

    document = root.find(f"{_KML_NAMESPACE}Document")
    assert document is not None
    assert document.find(f"{_KML_NAMESPACE}name").text == (
        "trip_20260715_133458_20260715_141235"
    )

    placemarks = document.findall(f"{_KML_NAMESPACE}Placemark")
    assert len(placemarks) == 2

    start_point = placemarks[0].find(
        f"{_KML_NAMESPACE}Point/{_KML_NAMESPACE}coordinates"
    )
    # KML coordinates are lon,lat - the opposite order from GPX's
    # lat/lon attributes.
    assert start_point.text == "18.06324,59.334591"

    line_string = placemarks[1].find(
        f"{_KML_NAMESPACE}LineString/{_KML_NAMESPACE}coordinates"
    )
    assert line_string.text == "18.06324,59.334591 18.064,59.335"


def test_gpx_to_kml_defaults_name_to_the_file_stem(tmp_path):
    gpx_path = tmp_path / "trip.gpx"
    gpx_path.write_text(_SAMPLE_GPX)

    kml_text = gpx_to_kml(gpx_path)

    root = ET.fromstring(kml_text)
    document = root.find(f"{_KML_NAMESPACE}Document")
    assert document.find(f"{_KML_NAMESPACE}name").text == "trip"


def test_gpx_to_kml_returns_none_for_a_track_with_no_points(tmp_path):
    gpx_path = tmp_path / "trip.gpx"
    gpx_path.write_text(_EMPTY_GPX)

    assert gpx_to_kml(gpx_path) is None


def test_gpx_to_kml_returns_none_for_a_missing_file(tmp_path):
    assert gpx_to_kml(tmp_path / "does_not_exist.gpx") is None


def test_gpx_to_kml_returns_none_for_malformed_xml(tmp_path):
    gpx_path = tmp_path / "trip.gpx"
    gpx_path.write_text("not valid xml at all <<<")

    assert gpx_to_kml(gpx_path) is None


def test_gpx_to_kml_starts_with_an_xml_declaration(tmp_path):
    gpx_path = tmp_path / "trip.gpx"
    gpx_path.write_text(_SAMPLE_GPX)

    kml_text = gpx_to_kml(gpx_path)

    assert kml_text.startswith('<?xml version="1.0" encoding="UTF-8"?>')
