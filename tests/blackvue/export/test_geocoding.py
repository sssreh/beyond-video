import json
from urllib.error import URLError

import pytest

from blackvue.export import geocoding as geocoding_module
from blackvue.export.geocoding import GeocodeResult
from blackvue.export.geocoding import _spacing_variants
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


# ---------------------------------------------------------------------------
# _spacing_variants() / forward_geocode()'s spelling-variant retry -
# Christer's real report: "actually i got vår nygård again, both
# 'Vårby gård' and 'Vårbygård' should work" - Nominatim's own search
# index is picky about Swedish compound place names being run together
# vs. spaced out, so both spellings need to resolve to the same place
# regardless of which one voice/text input happens to produce.
# ---------------------------------------------------------------------------


def test_spacing_variants_single_word_no_suffix_match_returns_itself_only():
    # Deliberately not "Stockholm" - it ends in "holm", one of
    # _SWEDISH_COMPOUND_SUFFIXES, so it *does* get a "Stock holm"
    # variant generated (harmless in practice: forward_geocode() tries
    # the literal name first and "Stockholm" alone always matches, so
    # that variant is never actually queried - but it means
    # _spacing_variants() itself isn't a safe "no suffix overlap"
    # example for this particular assertion).
    assert _spacing_variants("Slussen") == ["Slussen"]


def test_spacing_variants_inserts_space_before_known_suffix():
    assert _spacing_variants("Vårbygård") == ["Vårbygård", "Vårby gård"]


def test_spacing_variants_tries_concatenated_form_when_already_spaced():
    assert _spacing_variants("Vårby gård") == ["Vårby gård", "Vårbygård"]


def test_spacing_variants_multi_word_query_only_tries_concatenated():
    # Not a Swedish-compound-suffix case at all - just confirms the
    # generic "has a space -> also try fully concatenated" branch
    # doesn't misfire into the suffix-insertion branch too.
    assert _spacing_variants("a place that does not exist anywhere") == [
        "a place that does not exist anywhere",
        "aplacethatdoesnotexistanywhere",
    ]


def test_spacing_variants_short_suffix_only_word_not_split():
    # "gård" alone is entirely the suffix - nothing left before it to
    # split off, so no variant is generated.
    assert _spacing_variants("gård") == ["gård"]


def _fake_urlopen_by_query(responses: dict, *, captured: list | None = None):
    """Like _fake_urlopen() above, but keyed by the request's `q=`
    query-string value - needed to simulate Nominatim finding one
    spelling variant but not another, which the stateless single-
    payload _fake_urlopen() can't express."""

    from urllib.parse import parse_qs
    from urllib.parse import urlparse

    def urlopen(request, timeout=None):
        if captured is not None:
            captured.append(request)
        query = parse_qs(urlparse(request.full_url).query)
        q = query["q"][0]
        payload = responses.get(q, [])
        return _FakeResponse(json.dumps(payload).encode("utf-8"))

    return urlopen


def test_forward_geocode_falls_back_to_spaced_variant(monkeypatch):
    payload = [{"lat": "59.25", "lon": "17.95"}]
    captured = []
    monkeypatch.setattr(
        geocoding_module,
        "urlopen",
        _fake_urlopen_by_query({"Vårby gård": payload}, captured=captured),
    )

    result = forward_geocode("Vårbygård")

    assert result == GeocodeResult(point=(59.25, 17.95), lines=())
    # Both the literal query and the spaced fallback were tried, in
    # that order - the literal one first, genuinely came back empty.
    assert len(captured) == 2


def test_forward_geocode_uses_literal_query_first_without_extra_requests(
    monkeypatch,
):
    payload = [{"lat": "59.25", "lon": "17.95"}]
    captured = []
    monkeypatch.setattr(
        geocoding_module,
        "urlopen",
        _fake_urlopen_by_query({"Vårbygård": payload}, captured=captured),
    )

    result = forward_geocode("Vårbygård")

    assert result == GeocodeResult(point=(59.25, 17.95), lines=())
    # The literal spelling already matched - no need to try the spaced
    # variant at all.
    assert len(captured) == 1


def test_forward_geocode_returns_none_when_no_variant_matches(monkeypatch):
    monkeypatch.setattr(geocoding_module, "urlopen", _fake_urlopen_by_query({}))

    result = forward_geocode("Vårbygård")

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
    # 2, not 1: "nowhere at all" has spaces, so forward_geocode()'s
    # spacing-variant retry (_spacing_variants()) also tries the fully
    # concatenated form before giving up - both genuinely come back
    # empty here. That happens once, on the first (uncached) call; the
    # second call is a pure cache hit and adds no further requests.
    assert len(captured) == 2
