from blackvue.export.osm_roads import Area
from blackvue.export.osm_roads import BoundingBox
from blackvue.export.osm_roads import Road
from blackvue.live import map_stream as map_stream_module
from blackvue.live.map_stream import LiveMapRegion
from blackvue.live.map_stream import _bbox_contains
from blackvue.live.map_stream import _heading_from_history
from blackvue.live.map_stream import render_live_map_frame
from blackvue.live.telemetry import GpsSample
from blackvue.live.telemetry import TelemetryState


def test_bbox_contains_true_when_inner_fully_within_outer():
    outer = BoundingBox(min_lat=0.0, min_lon=0.0, max_lat=10.0, max_lon=10.0)
    inner = BoundingBox(min_lat=4.0, min_lon=4.0, max_lat=6.0, max_lon=6.0)

    assert _bbox_contains(outer, inner) is True


def test_bbox_contains_false_when_inner_extends_past_outer():
    outer = BoundingBox(min_lat=0.0, min_lon=0.0, max_lat=10.0, max_lon=10.0)
    inner = BoundingBox(min_lat=-1.0, min_lon=4.0, max_lat=6.0, max_lon=6.0)

    assert _bbox_contains(outer, inner) is False


def test_heading_from_history_returns_none_with_fewer_than_two_samples():
    assert _heading_from_history(()) is None
    assert _heading_from_history((GpsSample(0.0, 1.0, 1.0),)) is None


def test_heading_from_history_computes_a_bearing_for_due_north_travel():
    history = (
        GpsSample(0.0, 0.0, 0.0),
        GpsSample(1.0, 1.0, 0.0),
    )

    heading = _heading_from_history(history)

    assert heading == 0.0


def test_heading_from_history_skips_stationary_duplicate_positions():
    history = (
        GpsSample(0.0, 0.0, 0.0),
        GpsSample(1.0, 1.0, 0.0),
        GpsSample(2.0, 1.0, 0.0),  # same position as previous - GPS jitter
    )

    heading = _heading_from_history(history)

    assert heading == 0.0


def test_heading_from_history_returns_none_when_every_position_is_identical():
    history = (
        GpsSample(0.0, 5.0, 5.0),
        GpsSample(1.0, 5.0, 5.0),
    )

    assert _heading_from_history(history) is None


def test_live_map_region_fetches_and_caches_on_first_call(monkeypatch, tmp_path):
    calls = []

    def fake_load_roads(bbox, cache_dir, **kwargs):
        calls.append(("roads", bbox))
        return (Road(points=((0.0, 0.0), (1.0, 1.0)), highway="residential"),)

    def fake_load_areas(bbox, cache_dir, **kwargs):
        calls.append(("areas", bbox))
        return (Area(points=((0.0, 0.0), (1.0, 0.0), (1.0, 1.0)), kind="water"),)

    monkeypatch.setattr(map_stream_module, "load_or_fetch_roads", fake_load_roads)
    monkeypatch.setattr(map_stream_module, "load_or_fetch_areas", fake_load_areas)

    region = LiveMapRegion(tmp_path)
    region.ensure_covers(59.0, 18.0, 100.0)

    assert len(calls) == 2

    # A second call for a position still within the (much larger,
    # padded) cached region shouldn't trigger another fetch.
    region.ensure_covers(59.0001, 18.0001, 100.0)
    assert len(calls) == 2


def test_live_map_region_refetches_once_the_view_scrolls_outside_the_cache(
    monkeypatch, tmp_path
):
    calls = []

    def fake_load_roads(bbox, cache_dir, **kwargs):
        calls.append(bbox)
        return ()

    monkeypatch.setattr(map_stream_module, "load_or_fetch_roads", fake_load_roads)
    monkeypatch.setattr(map_stream_module, "load_or_fetch_areas", lambda *a, **k: ())

    region = LiveMapRegion(tmp_path)
    region.ensure_covers(59.0, 18.0, 100.0)
    assert len(calls) == 1

    # Far enough away to be well outside the first, padded fetch area.
    region.ensure_covers(60.0, 19.0, 100.0)
    assert len(calls) == 2


def test_render_live_map_frame_returns_a_placeholder_with_no_gps_fix():
    state = TelemetryState()
    region = LiveMapRegion.__new__(LiveMapRegion)  # not used - no fix yet

    image = render_live_map_frame(state, region, width=100, height=100)

    assert image.size == (100, 100)


def test_render_live_map_frame_renders_a_real_frame_once_a_fix_exists(
    monkeypatch, tmp_path
):
    monkeypatch.setattr(
        map_stream_module, "load_or_fetch_roads", lambda *a, **k: ()
    )
    monkeypatch.setattr(
        map_stream_module, "load_or_fetch_areas", lambda *a, **k: ()
    )

    state = TelemetryState()
    state.add_gps(59.334591, 18.063240)
    region = LiveMapRegion(tmp_path)

    image = render_live_map_frame(state, region, width=200, height=200)

    assert image.size == (200, 200)
