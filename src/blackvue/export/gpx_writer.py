"""
GPX 1.1 track file writer for bv-export.

Copyright (C) 2026 Christer R. (sssreh)

SPDX-License-Identifier: GPL-3.0-or-later
"""

from __future__ import annotations

from pathlib import Path
from xml.etree import ElementTree as ET

from ..telemetry.gps_reader import GpsFix

_GPX_NAMESPACE = "http://www.topografix.com/GPX/1/1"


def write_gpx(
    fixes: tuple[GpsFix, ...],
    path: Path,
    *,
    name: str | None = None,
) -> None:
    """Write a sequence of GPS fixes out as a GPX 1.1 track file.

    Invalid fixes (no fix, or no position) are skipped. Fixes are
    written in the order given - callers are responsible for sorting
    them chronologically first (see blackvue.export.trip_export,
    which merges and sorts fixes across a trip's recordings before
    calling this).
    """

    gpx = ET.Element(
        "gpx",
        {
            "version": "1.1",
            "creator": "beyond-video bv-export",
            "xmlns": _GPX_NAMESPACE,
        },
    )

    trk = ET.SubElement(gpx, "trk")
    if name:
        ET.SubElement(trk, "name").text = name

    trkseg = ET.SubElement(trk, "trkseg")

    for fix in fixes:
        if not fix.valid or fix.latitude is None or fix.longitude is None:
            continue

        trkpt = ET.SubElement(
            trkseg,
            "trkpt",
            {"lat": repr(fix.latitude), "lon": repr(fix.longitude)},
        )
        ET.SubElement(trkpt, "time").text = (
            fix.timestamp.strftime("%Y-%m-%dT%H:%M:%SZ")
        )

        if fix.speed_kmh is not None or fix.course is not None:
            extensions = ET.SubElement(trkpt, "extensions")
            if fix.speed_kmh is not None:
                # km/h -> m/s, the unit GPX extensions conventionally
                # use for speed.
                ET.SubElement(extensions, "speed").text = repr(
                    fix.speed_kmh / 3.6
                )
            if fix.course is not None:
                ET.SubElement(extensions, "course").text = repr(fix.course)

    tree = ET.ElementTree(gpx)
    if hasattr(ET, "indent"):
        ET.indent(tree)

    path.parent.mkdir(parents=True, exist_ok=True)
    tree.write(path, encoding="UTF-8", xml_declaration=True)
