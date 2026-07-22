import json
from datetime import datetime

import pytest

from blackvue.export import osm_roads as osm_roads_module
from blackvue.export.osm_roads import BoundingBox
from blackvue.export.osm_roads import fetch_roads
from blackvue.export.osm_roads import bounding_box_for_fixes
from blackvue.export.osm_roads import load_or_fetch_roads
from blackvue.generate.media import MediaToolError
from blackvue.telemetry.gps_reader import GpsFix


def _fix(lat, lon, *, valid=True):
    return GpsFix(
        timestamp=datetime(2026, 7, 15, 13, 32, 55),
        valid=valid,
        latitude=lat,
        longitude=lon,
        speed_kmh=50.0,
        course=90.0,
    )


class _FakeResponse:
    def __init__(self, data: bytes):
        self._data = data

    def read(self, size=-1):
        return self._data

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return False


def _fake_urlopen(payload: dict, *, captured: list | None = None):
    def urlopen(request, timeout=None):
        if captured is not None:
            captured.append(request)
        return _FakeResponse(json.dumps(payload).encode("utf-8"))

    return urlopen


_SAMPLE_PAYLOAD = {
    "elements": [
        {
            "type": "way",
            "id": 1,
            "geometry": [
                {"lat": 59.30, "lon": 18.05},
                {"lat": 59.31, "lon": 18.06},
            ],
        },
        {
            "type": "way",
            "id": 2,
            "geometry": [
                {"lat": 59.32, "lon": 18.07},
            ],
        },
        # No geometry: should be skipped, not crash.
        {"type": "way", "id": 3},
        # Not a way: should be ignored entirely.
        {"type": "node", "id": 4, "lat": 59.30, "lon": 18.05},
    ]
}


def test_bounding_box_for_fixes_computes_padded_box():
    fixes = (_fix(59.30, 18.05), _fix(59.32, 18.08))

    bbox = bounding_box_for_fixes(fixes, margin_degrees=0.01)

    assert bbox == BoundingBox(
        min_lat=59.29, min_lon=18.04, max_lat=59.33, max_lon=18.09
    )


def test_bounding_box_for_fixes_ignores_invalid_and_positionless_fixes():
    fixes = (
        _fix(59.30, 18.05, valid=False),
        GpsFix(
            timestamp=datetime(2026, 7, 15, 13, 32, 55),
            valid=True,
            latitude=None,
            longitude=None,
            speed_kmh=None,
            course=None,
        ),
        _fix(59.31, 18.06),
    )

    bbox = bounding_box_for_fixes(fixes, margin_degrees=0.0)

    assert bbox == BoundingBox(
        min_lat=59.31, min_lon=18.06, max_lat=59.31, max_lon=18.06
    )


def test_bounding_box_for_fixes_returns_none_for_no_valid_fixes():
    fixes = (_fix(59.30, 18.05, valid=False),)

    assert bounding_box_for_fixes(fixes) is None


def test_bounding_box_for_fixes_returns_none_for_empty_input():
    assert bounding_box_for_fixes(()) is None


def test_fetch_roads_parses_ways_with_geometry(monkeypatch):
    monkeypatch.setattr(
        osm_roads_module, "urlopen", _fake_urlopen(_SAMPLE_PAYLOAD)
    )

    bbox = BoundingBox(min_lat=59.0, min_lon=18.0, max_lat=59.5, max_lon=18.5)
    roads = fetch_roads(bbox)

    assert len(roads) == 2
    assert roads[0].points == ((59.30, 18.05), (59.31, 18.06))
    assert roads[1].points == ((59.32, 18.07),)


def test_fetch_roads_sends_a_valid_user_agent_and_bbox(monkeypatch):
    captured = []
    monkeypatch.setattr(
        osm_roads_module,
        "urlopen",
        _fake_urlopen(_SAMPLE_PAYLOAD, captured=captured),
    )

    bbox = BoundingBox(min_lat=59.0, min_lon=18.0, max_lat=59.5, max_lon=18.5)
    fetch_roads(bbox)

    assert len(captured) == 1
    request = captured[0]
    assert request.get_header("User-agent") == osm_roads_module.USER_AGENT
    assert request.full_url == osm_roads_module.OVERPASS_URL
    body = request.data.decode("utf-8")
    assert "59.0,18.0,59.5,18.5" in body


def test_fetch_roads_wraps_network_error(monkeypatch):
    from urllib.error import URLError

    def broken_urlopen(request, timeout=None):
        raise URLError("no route to host")

    monkeypatch.setattr(osm_roads_module, "urlopen", broken_urlopen)

    bbox = BoundingBox(min_lat=59.0, min_lon=18.0, max_lat=59.5, max_lon=18.5)

    with pytest.raises(MediaToolError):
        fetch_roads(bbox)


def test_fetch_roads_wraps_bad_json(monkeypatch):
    def urlopen(request, timeout=None):
        return _FakeResponse(b"not json")

    monkeypatch.setattr(osm_roads_module, "urlopen", urlopen)

    bbox = BoundingBox(min_lat=59.0, min_lon=18.0, max_lat=59.5, max_lon=18.5)

    with pytest.raises(MediaToolError):
        fetch_roads(bbox)


def test_load_or_fetch_roads_fetches_and_caches_on_first_call(
    tmp_path, monkeypatch
):
    calls = []
    monkeypatch.setattr(
        osm_roads_module,
        "urlopen",
        _fake_urlopen(_SAMPLE_PAYLOAD, captured=calls),
    )

    bbox = BoundingBox(min_lat=59.0, min_lon=18.0, max_lat=59.5, max_lon=18.5)
    roads = load_or_fetch_roads(bbox, tmp_path)

    assert len(roads) == 2
    assert len(calls) == 1
    cache_files = list(tmp_path.glob("*.json"))
    assert len(cache_files) == 1


def test_load_or_fetch_roads_reuses_cache_without_refetching(
    tmp_path, monkeypatch
):
    def _refuse(request, timeout=None):
        raise AssertionError("should not hit the network on a cache hit")

    bbox = BoundingBox(min_lat=59.0, min_lon=18.0, max_lat=59.5, max_lon=18.5)

    monkeypatch.setattr(
        osm_roads_module, "urlopen", _fake_urlopen(_SAMPLE_PAYLOAD)
    )
    first = load_or_fetch_roads(bbox, tmp_path)

    monkeypatch.setattr(osm_roads_module, "urlopen", _refuse)
    second = load_or_fetch_roads(bbox, tmp_path)

    assert first == second


def test_load_or_fetch_roads_shares_cache_for_rounded_bbox(
    tmp_path, monkeypatch
):
    # Two bounding boxes differing only past the 4-decimal rounding
    # used for the cache key should hit the same cache file.
    calls = []
    monkeypatch.setattr(
        osm_roads_module,
        "urlopen",
        _fake_urlopen(_SAMPLE_PAYLOAD, captured=calls),
    )

    bbox_a = BoundingBox(
        min_lat=59.00001, min_lon=18.00001, max_lat=59.50001, max_lon=18.50001
    )
    bbox_b = BoundingBox(
        min_lat=59.00002, min_lon=18.00002, max_lat=59.50002, max_lon=18.50002
    )

    load_or_fetch_roads(bbox_a, tmp_path)
    load_or_fetch_roads(bbox_b, tmp_path)

    assert len(calls) == 1
