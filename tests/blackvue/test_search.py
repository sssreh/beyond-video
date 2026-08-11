from datetime import datetime
from pathlib import Path

import pytest

from blackvue.archive.asset import Asset
from blackvue.archive.asset_file import AssetFile
from blackvue.archive.recording import Recording
from blackvue.archive.recording_id import RecordingId
from blackvue.generate.media import MediaToolError
from blackvue.search import TEXT_SEARCH_ASSETS
from blackvue.search import GeoMatch
from blackvue.search import TextMatch
from blackvue.search import search_near
from blackvue.search import search_text
from blackvue.telemetry.gps_reader import GpsFix


def test_text_search_assets_all_covers_every_group():
    assert TEXT_SEARCH_ASSETS["all"] == (
        Asset.TRANSCRIPT,
        Asset.TRANSCRIPT_DIARIZED,
        Asset.TRANSLATION,
        Asset.TRANSLATION_DIARIZED,
        Asset.SCENE_DESCRIPTION,
        Asset.SCENE_DESCRIPTION_REAR,
    )


def _make_recording(recording_id: str, tmp_path: Path, assets: dict) -> Recording:
    recording = Recording(id=RecordingId(recording_id))
    for asset, text in assets.items():
        path = tmp_path / f"{recording_id}.{asset.name.lower()}.txt"
        path.write_text(text, encoding="utf-8")
        recording.assets[asset] = AssetFile(asset=asset, path=path)
    return recording


def test_search_text_finds_matching_lines_case_insensitive(tmp_path):
    recording = _make_recording(
        "20260715_120000_N",
        tmp_path,
        {Asset.TRANSCRIPT: "Line one.\nHeavy TRAFFIC ahead.\nLine three.\n"},
    )

    matches = search_text(recording, "traffic")

    assert len(matches) == 1
    assert isinstance(matches[0], TextMatch)
    assert matches[0].asset is Asset.TRANSCRIPT
    assert matches[0].line_number == 2
    assert "TRAFFIC" in matches[0].line


def test_search_text_case_sensitive_excludes_different_case(tmp_path):
    recording = _make_recording(
        "20260715_121000_N",
        tmp_path,
        {Asset.TRANSCRIPT: "Heavy TRAFFIC ahead.\n"},
    )

    matches = search_text(recording, "traffic", case_sensitive=True)

    assert matches == []


def test_search_text_regex_mode(tmp_path):
    recording = _make_recording(
        "20260715_122000_N",
        tmp_path,
        {Asset.TRANSCRIPT: "Speed was 42 km/h.\nNo numbers here.\n"},
    )

    matches = search_text(recording, r"\d+ km/h", regex=True)

    assert len(matches) == 1
    assert matches[0].line_number == 1


def test_search_text_invalid_regex_raises_media_tool_error(tmp_path):
    recording = _make_recording(
        "20260715_123000_N", tmp_path, {Asset.TRANSCRIPT: "text"}
    )

    with pytest.raises(MediaToolError):
        search_text(recording, "(unclosed", regex=True)


def test_search_text_skips_assets_the_recording_does_not_have(tmp_path):
    recording = _make_recording(
        "20260715_124000_N", tmp_path, {Asset.TRANSCRIPT: "traffic jam"}
    )

    matches = search_text(
        recording, "traffic", assets=TEXT_SEARCH_ASSETS["scene"]
    )

    assert matches == []


def test_search_text_restricted_to_asset_category(tmp_path):
    recording = _make_recording(
        "20260715_125000_N",
        tmp_path,
        {
            Asset.TRANSCRIPT: "traffic jam",
            Asset.SCENE_DESCRIPTION: "no traffic visible",
        },
    )

    matches = search_text(
        recording, "traffic", assets=TEXT_SEARCH_ASSETS["scene"]
    )

    assert len(matches) == 1
    assert matches[0].asset is Asset.SCENE_DESCRIPTION


def test_search_text_raises_media_tool_error_on_unreadable_file(tmp_path):
    recording = _make_recording(
        "20260715_126000_N", tmp_path, {Asset.TRANSCRIPT: "text"}
    )
    # Delete the file out from under the AssetFile reference so the
    # read fails with a real OSError.
    recording.file(Asset.TRANSCRIPT).path.unlink()

    with pytest.raises(MediaToolError):
        search_text(recording, "text")


def _fix(lat, lon, *, valid=True, minutes=0):
    return GpsFix(
        timestamp=datetime(2026, 7, 15, 12, minutes, 0),
        valid=valid,
        latitude=lat,
        longitude=lon,
        speed_kmh=10.0,
        course=90.0,
    )


def test_search_near_returns_closest_fix_within_radius(tmp_path, monkeypatch):
    import blackvue.search as search_module

    recording = Recording(id=RecordingId("20260715_130000_N"))
    gps_path = tmp_path / "20260715_130000_N.gps"
    gps_path.write_text("irrelevant - read_gps is faked below")
    recording.assets[Asset.GPS] = AssetFile(asset=Asset.GPS, path=gps_path)

    fixes = (
        _fix(59.3300, 18.0700, minutes=0),  # ~roughly close
        _fix(59.3293, 18.0686, minutes=1),  # exact target - closest
        _fix(60.0000, 19.0000, minutes=2),  # far away
    )
    monkeypatch.setattr(search_module, "read_gps", lambda path: fixes)

    match = search_near(recording, 59.3293, 18.0686, radius_meters=1000)

    assert isinstance(match, GeoMatch)
    assert match.fix.latitude == 59.3293
    assert match.fix.longitude == 18.0686
    assert match.distance_meters == pytest.approx(0.0, abs=1.0)


def test_search_near_returns_none_when_nothing_in_radius(tmp_path, monkeypatch):
    import blackvue.search as search_module

    recording = Recording(id=RecordingId("20260715_131000_N"))
    gps_path = tmp_path / "20260715_131000_N.gps"
    gps_path.write_text("irrelevant")
    recording.assets[Asset.GPS] = AssetFile(asset=Asset.GPS, path=gps_path)

    monkeypatch.setattr(
        search_module, "read_gps", lambda path: (_fix(60.0, 19.0),)
    )

    match = search_near(recording, 59.3293, 18.0686, radius_meters=100)

    assert match is None


def test_search_near_skips_invalid_fixes(tmp_path, monkeypatch):
    import blackvue.search as search_module

    recording = Recording(id=RecordingId("20260715_132000_N"))
    gps_path = tmp_path / "20260715_132000_N.gps"
    gps_path.write_text("irrelevant")
    recording.assets[Asset.GPS] = AssetFile(asset=Asset.GPS, path=gps_path)

    fixes = (
        _fix(59.3293, 18.0686, valid=False),  # invalid, at target - skipped
        _fix(None, None, valid=True),  # valid but no position - skipped
    )
    monkeypatch.setattr(search_module, "read_gps", lambda path: fixes)

    match = search_near(recording, 59.3293, 18.0686, radius_meters=1000)

    assert match is None


def test_search_near_returns_none_when_recording_has_no_gps(tmp_path):
    recording = Recording(id=RecordingId("20260715_133000_N"))

    match = search_near(recording, 59.3293, 18.0686, radius_meters=1000)

    assert match is None


# --- line-geometry (--place resolving to a road/area) matching ---


def test_point_to_segment_distance_meters_is_near_zero_on_the_segment():
    from blackvue.search import _point_to_segment_distance_meters

    # A short east-west segment; a point that lies on it (roughly
    # halfway) should be ~0m away.
    distance = _point_to_segment_distance_meters(
        59.320, 18.060, (59.320, 18.050), (59.320, 18.070)
    )

    assert distance == pytest.approx(0.0, abs=1.0)


def test_point_to_segment_distance_meters_clamps_to_nearest_endpoint():
    from blackvue.search import _point_to_segment_distance_meters
    from blackvue.search import _haversine_distance_meters

    # A point far beyond one end of the segment - the closest point on
    # the segment is the endpoint itself, not an extrapolation past it.
    endpoint_distance = _haversine_distance_meters(59.400, 18.200, 59.320, 18.070)
    projected_distance = _point_to_segment_distance_meters(
        59.400, 18.200, (59.320, 18.050), (59.320, 18.070)
    )

    assert projected_distance == pytest.approx(endpoint_distance, rel=0.01)


def test_min_distance_to_lines_meters_picks_the_closest_segment_across_lines():
    from blackvue.search import _min_distance_to_lines_meters

    lines = (
        ((59.320, 18.050), (59.320, 18.070)),  # nearby line
        ((60.000, 19.000), (60.001, 19.001)),  # far-away line
    )

    distance = _min_distance_to_lines_meters(59.320, 18.060, lines)

    assert distance == pytest.approx(0.0, abs=1.0)


def test_min_distance_to_lines_meters_handles_a_degenerate_single_vertex_line():
    from blackvue.search import _min_distance_to_lines_meters

    # One line, made of exactly one vertex - lines is a tuple of
    # lines, each a tuple of (lat, lon) vertices, so this is
    # `(( one_vertex, ),)`, not `((lat, lon),)`.
    distance = _min_distance_to_lines_meters(
        59.3293, 18.0686, (((59.3293, 18.0686),),)
    )

    assert distance == pytest.approx(0.0, abs=1.0)


def test_search_near_with_lines_finds_a_fix_far_from_the_representative_point(
    tmp_path, monkeypatch
):
    # Simulates a long road: the road's own Nominatim "point" is near
    # one end, but a GPS fix near the *other* end (far from that
    # single point, well outside a normal --radius around it) should
    # still match, since search_near() is told to measure against the
    # road's own line geometry instead.
    import blackvue.search as search_module

    recording = Recording(id=RecordingId("20260715_134000_N"))
    gps_path = tmp_path / "20260715_134000_N.gps"
    gps_path.write_text("irrelevant - read_gps is faked below")
    recording.assets[Asset.GPS] = AssetFile(asset=Asset.GPS, path=gps_path)

    # Road runs roughly 5km from one point to another; representative
    # "point" is the start of the road, the fix is near the far end.
    road_start = (59.320, 18.050)
    road_end = (59.365, 18.050)  # ~5km further north
    fix = _fix(59.364, 18.0501, minutes=0)

    monkeypatch.setattr(search_module, "read_gps", lambda path: (fix,))

    # Plain point-distance search from the road's start point, with a
    # normal radius, would miss this fix entirely.
    no_lines_match = search_near(
        recording, road_start[0], road_start[1], radius_meters=200
    )
    assert no_lines_match is None

    # With the road's line geometry, the same fix (near the line, far
    # from the single representative point) is found.
    with_lines_match = search_near(
        recording,
        road_start[0],
        road_start[1],
        radius_meters=200,
        lines=((road_start, road_end),),
    )
    assert with_lines_match is not None
    assert with_lines_match.distance_meters < 200
