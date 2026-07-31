from blackvue.export.osm_roads import Area
from blackvue.export.osm_roads import BoundingBox
from blackvue.export.osm_roads import Road
from blackvue.live import map_stream as map_stream_module
from blackvue.live.map_stream import LiveMapRegion
from blackvue.live.map_stream import _bbox_contains
from blackvue.live.map_stream import _heading_from_history
from blackvue.live.map_stream import _live_marker_image
from blackvue.live.map_stream import render_live_map_frame
from blackvue.live.telemetry import GpsSample
from blackvue.live.telemetry import TelemetryState


def _reset_marker_image_cache(monkeypatch):
    # _live_marker_image() caches its result in two module-level
    # globals so the bundled asset is only ever loaded/scaled once per
    # process (see its own docstring/comment) - tests that care about
    # the loading path itself (success or failure) need a clean slate
    # each time, not whatever an earlier test already cached.
    monkeypatch.setattr(map_stream_module, "_marker_image", None)
    monkeypatch.setattr(map_stream_module, "_marker_image_load_attempted", False)


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


def test_live_marker_image_loads_the_bundled_red_car_scaled_down(monkeypatch):
    # Christer: "can i have my red car on the bv-live map" - reuses
    # bv-export's own bundled DEFAULT_MAP_ICON_PATH/MARKER_IMAGE_SCALE
    # (see map_video.py) rather than a separate live-only asset.
    _reset_marker_image_cache(monkeypatch)

    image = _live_marker_image()

    assert image is not None
    assert image.mode == "RGBA"

    from PIL import Image as PILImage

    from blackvue.export.map_video import DEFAULT_MAP_ICON_PATH
    from blackvue.export.map_video import MARKER_IMAGE_SCALE

    original = PILImage.open(DEFAULT_MAP_ICON_PATH)
    assert image.width == max(1, round(original.width * MARKER_IMAGE_SCALE))
    assert image.height == max(1, round(original.height * MARKER_IMAGE_SCALE))


def test_live_marker_image_is_only_loaded_once(monkeypatch):
    _reset_marker_image_cache(monkeypatch)

    first = _live_marker_image()
    second = _live_marker_image()

    assert first is second


def test_live_marker_image_falls_back_to_none_if_the_bundled_asset_cant_load(
    monkeypatch, tmp_path
):
    # A bad/missing bundled asset should degrade the live map to the
    # plain procedural arrow it already had, not crash bv-live outright
    # - see _live_marker_image()'s own docstring.
    _reset_marker_image_cache(monkeypatch)
    monkeypatch.setattr(
        map_stream_module, "DEFAULT_MAP_ICON_PATH", tmp_path / "does_not_exist.png"
    )

    assert _live_marker_image() is None


def test_render_live_map_frame_passes_the_marker_image_through_to_render_frame(
    monkeypatch, tmp_path
):
    _reset_marker_image_cache(monkeypatch)
    monkeypatch.setattr(
        map_stream_module, "load_or_fetch_roads", lambda *a, **k: ()
    )
    monkeypatch.setattr(
        map_stream_module, "load_or_fetch_areas", lambda *a, **k: ()
    )

    captured = {}
    real_render_frame = map_stream_module.render_frame

    def _capturing_render_frame(*args, **kwargs):
        captured["marker_image"] = kwargs.get("marker_image")
        return real_render_frame(*args, **kwargs)

    monkeypatch.setattr(map_stream_module, "render_frame", _capturing_render_frame)

    state = TelemetryState()
    state.add_gps(59.334591, 18.063240)
    region = LiveMapRegion(tmp_path)

    render_live_map_frame(state, region, width=200, height=200)

    assert captured["marker_image"] is not None
    assert captured["marker_image"] is _live_marker_image()
