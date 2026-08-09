"""
GPX-to-KML converter for bv-web's "Open in Google Earth" trip download.

Copyright (C) 2026 Christer R. (sssreh)

SPDX-License-Identifier: GPL-3.0-or-later
"""

from __future__ import annotations

from pathlib import Path
from xml.etree import ElementTree as ET

# Matches gpx_writer.write_gpx()'s own namespace (export/gpx_writer.py)
# - a module-private constant there, redefined here rather than
# imported (the same choice web/trips.py already made for the same
# reason - see its own comment), to avoid pulling in a whole extra
# module dependency for one string.
_GPX_NAMESPACE = "http://www.topografix.com/GPX/1/1"

_KML_NAMESPACE = "http://www.opengis.net/kml/2.2"


def gpx_to_kml(gpx_path: Path, *, name: str | None = None) -> str | None:
    """Convert a GPX 1.1 track file (as written by
    gpx_writer.write_gpx()) into a KML 2.2 document string, or None if
    `gpx_path` is missing, unparseable, or has no trackpoints.

    Built for bv-web's "Open in Google Earth" trip download (see
    web/app.py's trip_kml() route): Google Earth Pro can open a .gpx
    file directly via File > Open, but Google Earth Web - the
    browser-based version most people reach for first - only accepts
    KML/KMZ through its own Import flow, not GPX.

    Rather than writing a trip.kml file alongside trip.gpx during the
    offline bv-export pipeline, this generates KML on the fly from a
    trip's existing trip.gpx - the same on-demand-generation pattern
    trip_location() already uses for reverse-geocoding (see
    web/trips.py's first_gpx_point()). One less artifact to keep in
    sync if trip.gpx is ever regenerated, and every existing trip
    (already exported, no re-export needed) gets KML export for free.

    Produces one LineString Placemark for the whole track plus a Point
    Placemark marking the start - the pair someone opening a trip in
    Google Earth actually wants: the route itself, and something to
    click to fly straight to where it began.
    """

    try:
        root = ET.parse(gpx_path).getroot()
    except (OSError, ET.ParseError):
        return None

    points: list[tuple[float, float]] = []
    for trkpt in root.findall(f".//{{{_GPX_NAMESPACE}}}trkpt"):
        try:
            points.append(
                (float(trkpt.attrib["lat"]), float(trkpt.attrib["lon"]))
            )
        except (KeyError, ValueError):
            continue

    if not points:
        return None

    track_name = name or gpx_path.stem

    kml = ET.Element("kml", {"xmlns": _KML_NAMESPACE})
    document = ET.SubElement(kml, "Document")
    ET.SubElement(document, "name").text = track_name

    # KML colors are AABBGGRR, not RRGGBB - opaque red.
    track_style = ET.SubElement(document, "Style", {"id": "track"})
    line_style = ET.SubElement(track_style, "LineStyle")
    ET.SubElement(line_style, "color").text = "ff0000ff"
    ET.SubElement(line_style, "width").text = "4"

    start_lat, start_lon = points[0]
    start_placemark = ET.SubElement(document, "Placemark")
    ET.SubElement(start_placemark, "name").text = "Start"
    start_point = ET.SubElement(start_placemark, "Point")
    ET.SubElement(start_point, "coordinates").text = f"{start_lon},{start_lat}"

    track_placemark = ET.SubElement(document, "Placemark")
    ET.SubElement(track_placemark, "name").text = track_name
    ET.SubElement(track_placemark, "styleUrl").text = "#track"
    line_string = ET.SubElement(track_placemark, "LineString")
    ET.SubElement(line_string, "tessellate").text = "1"
    # KML coordinates are "lon,lat[,alt]" triples, space-separated -
    # the opposite attribute order from GPX's lat/lon pair. No
    # altitude is written, same as gpx_writer never writing <ele>.
    ET.SubElement(line_string, "coordinates").text = " ".join(
        f"{lon},{lat}" for lat, lon in points
    )

    tree = ET.ElementTree(kml)
    if hasattr(ET, "indent"):
        ET.indent(tree)

    body = ET.tostring(kml, encoding="unicode")
    return f'<?xml version="1.0" encoding="UTF-8"?>\n{body}'
