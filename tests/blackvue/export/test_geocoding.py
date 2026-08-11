import json
from urllib.error import URLError

import pytest

from blackvue.export import geocoding as geocoding_module
from blackvue.export.geocoding import GeocodeResult
from blackvue.export.geocoding import forward_geocode
from blackvue.export.geocoding import load_or_forward_geocode
from blackvue.generate.media import MediaToolError


class _FakeResponse:
    def __init__(self, data: bytes):
        self._data = data

    def read(self, size=-1):
        return self._data

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return False


def _fake_urlopen(payload, *, captured: list | None = None):
    def urlopen(request, timeout=None):
        if captured is not None:
            captured.append(request)
        return _FakeResponse(json.dumps(payload).encode("utf-8"))

    return urlopen


@pytest.fixture(autouse=True)
def _no_throttle_sleep(monkeypatch):
    # forward_geocode()/reverse_geocode() share a process-wide throttle
    # gate - reset it and stub out the sleep so back-to-back tests
    # don't actually wait on each other.
    monkeypatch.setattr(geocoding_module, "_last_request_time", None)
    monkeypatch.setattr(geocoding_module.time, "sleep", lambda seconds: None)


def test_forward_geocode_returns_point_from_best_match(monkeypatch):
    payload = [{"lat": "59.3293", "lon": "18.0686"}]
    captured = []
    monkeypatch.setattr(
        geocoding_module, "urlopen", _fake_urlopen(payload, captured=captured)
    )

    result = forward_geocode("Stockholm")

    assert result == GeocodeResult(point=(59.3293, 18.0686), lines=())
    assert len(captured) == 1
    assert "Stockholm" in captured[0].full_url or "Stockholm" in str(captured[0].full_url)


def test_forward_geocode_requests_polygon_geojson(monkeypatch):
    payload = [{"lat": "59.3293", "lon": "18.0686"}]
    captured = []
    monkeypatch.setattr(
        geocoding_module, "urlopen", _fake_urlopen(payload, captured=captured)
    )

    forward_geocode("Stockholm")

    assert "polygon_geojson=1" in str(captured[0].full_url)


def test_forward_geocode_returns_line_geometry_for_a_road_linestring(monkeypatch):
    payload = [
        {
            "lat": "59.33",
            "lon": "18.07",
            "geojson": {
                "type": "LineString",
                "coordinates": [[18.05, 59.31], [18.06, 59.32], [18.07, 59.33]],
            },
        }
    ]
    monkeypatch.setattr(geocoding_module, "urlopen", _fake_urlopen(payload))

    result = forward_geocode("A long road")

    assert result.point == (59.33, 18.07)
    assert result.lines == (
        ((59.31, 18.05), (59.32, 18.06), (59.33, 18.07)),
    )


def test_forward_geocode_returns_multiple_lines_for_a_multilinestring(monkeypatch):
    payload = [
        {
            "lat": "59.33",
            "lon": "18.07",
            "geojson": {
                "type": "MultiLineString",
                "coordinates": [
                    [[18.05, 59.31], [18.06, 59.32]],
                    [[18.08, 59.34], [18.09, 59.35]],
                ],
            },
        }
    ]
    monkeypatch.setattr(geocoding_module, "urlopen", _fake_urlopen(payload))

    result = forward_geocode("A split road")

    assert result.lines == (
        ((59.31, 18.05), (59.32, 18.06)),
        ((59.34, 18.08), (59.35, 18.09)),
    )


def test_forward_geocode_uses_exterior_ring_for_a_polygon(monkeypatch):
    payload = [
        {
            "lat": "59.33",
            "lon": "18.07",
            "geojson": {
                "type": "Polygon",
                "coordinates": [
                    [[18.05, 59.31], [18.06, 59.32], [18.07, 59.31], [18.05, 59.31]],
                    [[18.055, 59.315], [18.06, 59.315], [18.055, 59.315]],
                ],
            },
        }
    ]
    monkeypatch.setattr(geocoding_module, "urlopen", _fake_urlopen(payload))

    result = forward_geocode("A park")

    assert result.lines == (
        ((59.31, 18.05), (59.32, 18.06), (59.31, 18.07), (59.31, 18.05)),
    )


def test_forward_geocode_ignores_point_geojson(monkeypatch):
    payload = [
        {
            "lat": "59.33",
            "lon": "18.07",
            "geojson": {"type": "Point", "coordinates": [18.07, 59.33]},
        }
    ]
    monkeypatch.setattr(geocoding_module, "urlopen", _fake_urlopen(payload))

    result = forward_geocode("An address")

    assert result.lines == ()


def test_forward_geocode_returns_none_for_no_match(monkeypatch):
    monkeypatch.setattr(geocoding_module, "urlopen", _fake_urlopen([]))

    result = forward_geocode("a place that does not exist anywhere")

    assert result is None


def test_forward_geocode_raises_media_tool_error_on_network_failure(monkeypatch):
    def urlopen(request, timeout=None):
        raise URLError("no route to host")

    monkeypatch.setattr(geocoding_module, "urlopen", urlopen)

    with pytest.raises(MediaToolError):
        forward_geocode("Stockholm")


def test_forward_geocode_raises_media_tool_error_on_malformed_json(monkeypatch):
    def urlopen(request, timeout=None):
        return _FakeResponse(b"not json")

    monkeypatch.setattr(geocoding_module, "urlopen", urlopen)

    with pytest.raises(MediaToolError):
        forward_geocode("Stockholm")


def test_forward_geocode_raises_media_tool_error_on_malformed_payload(monkeypatch):
    # Missing "lat"/"lon" keys entirely.
    monkeypatch.setattr(
        geocoding_module, "urlopen", _fake_urlopen([{"unexpected": "shape"}])
    )

    with pytest.raises(MediaToolError):
        forward_geocode("Stockholm")


def test_load_or_forward_geocode_fetches_and_caches(tmp_path, monkeypatch):
    payload = [{"lat": "59.3293", "lon": "18.0686"}]
    captured = []
    monkeypatch.setattr(
        geocoding_module, "urlopen", _fake_urlopen(payload, captured=captured)
    )

    cache_dir = tmp_path / ".osm_cache"
    result = load_or_forward_geocode("Stockholm", cache_dir)

    assert result == GeocodeResult(point=(59.3293, 18.0686), lines=())
    assert len(captured) == 1

    cache_files = list(cache_dir.glob("geocode_place_*.json"))
    assert len(cache_files) == 1

    # Second call for the same name hits the cache, no further request.
    result_again = load_or_forward_geocode("Stockholm", cache_dir)
    assert result_again == GeocodeResult(point=(59.3293, 18.0686), lines=())
    assert len(captured) == 1


def test_load_or_forward_geocode_round_trips_line_geometry_through_the_cache(
    tmp_path, monkeypatch
):
    payload = [
        {
            "lat": "59.33",
            "lon": "18.07",
            "geojson": {
                "type": "LineString",
                "coordinates": [[18.05, 59.31], [18.07, 59.33]],
            },
        }
    ]
    captured = []
    monkeypatch.setattr(
        geocoding_module, "urlopen", _fake_urlopen(payload, captured=captured)
    )

    cache_dir = tmp_path / ".osm_cache"
    result = load_or_forward_geocode("A long road", cache_dir)
    assert result.lines == (((59.31, 18.05), (59.33, 18.07)),)

    # Cache hit - same line geometry comes back without a second request.
    result_again = load_or_forward_geocode("A long road", cache_dir)
    assert result_again.lines == (((59.31, 18.05), (59.33, 18.07)),)
    assert len(captured) == 1


def test_load_or_forward_geocode_normalizes_query_for_cache_hits(tmp_path, monkeypatch):
    payload = [{"lat": "59.3293", "lon": "18.0686"}]
    captured = []
    monkeypatch.setattr(
        geocoding_module, "urlopen", _fake_urlopen(payload, captured=captured)
    )

    cache_dir = tmp_path / ".osm_cache"
    load_or_forward_geocode("  Stockholm  ", cache_dir)
    # Different casing/whitespace, same normalized query - should reuse
    # the same cache file rather than firing a second request.
    load_or_forward_geocode("stockholm", cache_dir)

    assert len(captured) == 1


def test_load_or_forward_geocode_caches_genuine_no_result(tmp_path, monkeypatch):
    captured = []
    monkeypatch.setattr(
        geocoding_module, "urlopen", _fake_urlopen([], captured=captured)
    )

    cache_dir = tmp_path / ".osm_cache"
    result = load_or_forward_geocode("nowhere at all", cache_dir)
    assert result is None

    result_again = load_or_forward_geocode("nowhere at all", cache_dir)
    assert result_again is None
    assert len(captured) == 1
