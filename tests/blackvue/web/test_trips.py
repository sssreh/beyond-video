from blackvue.core.camera_config import CameraConfig
from blackvue.core.camera_config import config_path
from blackvue.core.camera_config import save_camera_config
from blackvue.web.trips import TripCache
from blackvue.web.trips import first_gpx_point
from blackvue.web.trips import scan_all_trips
from blackvue.web.trips import scan_trip
from blackvue.web.trips import scan_trips

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


def _write_trip_log(folder, label="trip_20260715_133458_20260715_141235"):
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "trip.log").write_text(
        f"=== bv-export trip log: {label} ===\n"
        "Started: 2026-07-15T13:34:58\n"
        "Command: bv-export /archive --target /trips\n"
    )


# ---------------------------------------------------------------------------
# first_gpx_point() - added so trip_detail.html can show a "Show start
# location" link the same way the archive browser's own /location route
# already does for a single recording (see app.py's archive_recording_
# location() and its new trip_location() sibling).
# ---------------------------------------------------------------------------


def test_first_gpx_point_returns_the_first_trackpoint(tmp_path):
    gpx_path = tmp_path / "trip.gpx"
    gpx_path.write_text(_SAMPLE_GPX)

    assert first_gpx_point(gpx_path) == (59.334591, 18.06324)


def test_first_gpx_point_returns_none_for_a_track_with_no_points(tmp_path):
    gpx_path = tmp_path / "trip.gpx"
    gpx_path.write_text(_EMPTY_GPX)

    assert first_gpx_point(gpx_path) is None


def test_first_gpx_point_returns_none_for_a_missing_file(tmp_path):
    assert first_gpx_point(tmp_path / "does_not_exist.gpx") is None


def test_first_gpx_point_returns_none_for_malformed_xml(tmp_path):
    gpx_path = tmp_path / "trip.gpx"
    gpx_path.write_text("not valid xml at all <<<")

    assert first_gpx_point(gpx_path) is None


def test_scan_trip_returns_none_without_a_trip_log(tmp_path):
    folder = tmp_path / "not_a_trip"
    folder.mkdir()
    (folder / "front.mp4").write_bytes(b"")

    assert scan_trip(folder) is None


def test_scan_trip_reads_label_from_trip_log(tmp_path):
    folder = tmp_path / "Holiday_trip_20260715_133458_20260715_141235"
    _write_trip_log(folder, label="trip_20260715_133458_20260715_141235")

    trip = scan_trip(folder)

    assert trip is not None
    # The real label (no --prefix) comes from trip.log, not the
    # folder name (which does carry the "Holiday_" prefix).
    assert trip.label == "trip_20260715_133458_20260715_141235"
    assert trip.id == folder.name


def test_scan_trip_derives_prefix_from_folder_name_vs_label(tmp_path):
    folder = tmp_path / "Holiday_trip_20260715_133458_20260715_141235"
    _write_trip_log(folder, label="trip_20260715_133458_20260715_141235")

    trip = scan_trip(folder)

    assert trip.prefix == "Holiday"


def test_scan_trip_prefix_is_none_without_one(tmp_path):
    folder = tmp_path / "trip_20260715_133458_20260715_141235"
    _write_trip_log(folder, label="trip_20260715_133458_20260715_141235")

    trip = scan_trip(folder)

    assert trip.prefix is None


def test_scan_trip_prefix_is_none_when_folder_name_falls_back_to_label(tmp_path):
    # _read_trip_label() failed to parse trip.log, so label just
    # became folder.name itself (see the fallback test below) -
    # there's no real label to diff the folder name against, so
    # prefix must not be invented from nothing.
    folder = tmp_path / "trip_20260715_133458_20260715_141235"
    folder.mkdir()
    (folder / "trip.log").write_text("garbage, not a real trip.log\n")

    trip = scan_trip(folder)

    assert trip.prefix is None


def test_scan_trip_falls_back_to_folder_name_if_log_is_unparseable(tmp_path):
    folder = tmp_path / "trip_20260715_133458_20260715_141235"
    folder.mkdir()
    (folder / "trip.log").write_text("garbage, not a real trip.log\n")

    trip = scan_trip(folder)

    assert trip is not None
    assert trip.label == folder.name


def test_scan_trip_prefers_stitch_over_front_and_rear(tmp_path):
    folder = tmp_path / "trip_1"
    _write_trip_log(folder)
    (folder / "front.mp4").write_bytes(b"")
    (folder / "rear.mp4").write_bytes(b"")
    (folder / "stitch.mp4").write_bytes(b"")

    trip = scan_trip(folder)

    assert trip.videos == ("stitch.mp4", "front.mp4", "rear.mp4")
    assert trip.primary_video == "stitch.mp4"


def test_scan_trip_falls_back_to_front_without_stitch(tmp_path):
    folder = tmp_path / "trip_1"
    _write_trip_log(folder)
    (folder / "rear.mp4").write_bytes(b"")
    (folder / "front.mp4").write_bytes(b"")

    trip = scan_trip(folder)

    assert trip.primary_video == "front.mp4"


def test_scan_trip_has_no_primary_video_when_none_exist(tmp_path):
    folder = tmp_path / "trip_1"
    _write_trip_log(folder)

    trip = scan_trip(folder)

    assert trip.videos == ()
    assert trip.primary_video is None


def test_scan_trip_finds_map_zoom_variants(tmp_path):
    folder = tmp_path / "trip_1"
    _write_trip_log(folder)
    (folder / "map_zoom_50m.mp4").write_bytes(b"")
    (folder / "map_zoom_200m.mp4").write_bytes(b"")

    trip = scan_trip(folder)

    assert trip.map_zoom_videos == ("map_zoom_200m.mp4", "map_zoom_50m.mp4")


def test_scan_trip_flags_gpx_srt_gsensor_map(tmp_path):
    folder = tmp_path / "trip_1"
    _write_trip_log(folder)
    (folder / "trip.gpx").write_bytes(b"")
    (folder / "trip.srt").write_bytes(b"")
    (folder / "map.mp4").write_bytes(b"")
    (folder / "gsensor.mp4").write_bytes(b"")

    trip = scan_trip(folder)

    assert trip.gpx is True
    assert trip.srt is True
    assert trip.map_video == "map.mp4"
    assert trip.gsensor_video == "gsensor.mp4"


def test_known_filenames_matches_what_actually_exists(tmp_path):
    folder = tmp_path / "trip_1"
    _write_trip_log(folder)
    (folder / "stitch.mp4").write_bytes(b"")
    (folder / "trip.gpx").write_bytes(b"")

    trip = scan_trip(folder)

    assert trip.known_filenames == frozenset({"stitch.mp4", "trip.gpx"})
    assert "front.mp4" not in trip.known_filenames


def test_scan_trip_flags_trip_info(tmp_path):
    # Christer: "are trip summary still there" - trip_info.txt is
    # written unconditionally by every export (task #124), but wasn't
    # tracked in TripAssets at all before this - scan_trip() needs to
    # notice it exists the same way it already notices gpx/srt.
    folder = tmp_path / "trip_1"
    _write_trip_log(folder)
    (folder / "trip_info.txt").write_text("Duration: 0:10:00\n", encoding="utf-8")

    trip = scan_trip(folder)

    assert trip.trip_info is True
    assert "trip_info.txt" in trip.known_filenames


def test_scan_trip_trip_info_false_when_file_absent(tmp_path):
    folder = tmp_path / "trip_1"
    _write_trip_log(folder)

    trip = scan_trip(folder)

    assert trip.trip_info is False
    assert "trip_info.txt" not in trip.known_filenames


def test_trip_summary_parses_label_value_pairs(tmp_path):
    folder = tmp_path / "trip_1"
    _write_trip_log(folder)
    (folder / "trip_info.txt").write_text(
        "Started: 2026-07-21 12:41:08\n"
        "Duration: 0:06:02\n"
        "Distance: 3.42 km\n",
        encoding="utf-8",
    )

    trip = scan_trip(folder)

    assert trip.trip_summary == [
        ("Started", "2026-07-21 12:41:08"),
        ("Duration", "0:06:02"),
        ("Distance", "3.42 km"),
    ]


def test_trip_summary_empty_when_trip_info_missing(tmp_path):
    folder = tmp_path / "trip_1"
    _write_trip_log(folder)

    trip = scan_trip(folder)

    assert trip.trip_summary == []


def test_trip_summary_degrades_quietly_if_file_becomes_unreadable(tmp_path):
    # trip_info=True is set at scan time, but the file itself could
    # still vanish (or become unreadable) between the scan and a later
    # access of the trip_summary property - same "don't 500 the page
    # over a secondary file" convention every other optional asset
    # here follows.
    folder = tmp_path / "trip_1"
    _write_trip_log(folder)
    (folder / "trip_info.txt").write_text("Duration: 0:10:00\n", encoding="utf-8")
    trip = scan_trip(folder)
    (folder / "trip_info.txt").unlink()

    assert trip.trip_summary == []


def test_scan_trip_flags_trip_narrative(tmp_path):
    # Christer asked whether ElevenLabs would help with speech-to-text
    # (unrelated no), which surfaced that trip_summary.txt - the real
    # AI-written trip narrative bv-export's --trip-summary writes
    # (export/trip_export.py's `trip_summary=True` path) - was never
    # tracked in TripAssets at all, unlike trip_info.txt above. Same
    # is-file-present tracking pattern as trip_info.
    folder = tmp_path / "trip_1"
    _write_trip_log(folder)
    (folder / "trip_summary.txt").write_text(
        "The drive was smooth with light traffic.\n", encoding="utf-8"
    )

    trip = scan_trip(folder)

    assert trip.trip_narrative is True
    assert "trip_summary.txt" in trip.known_filenames


def test_scan_trip_trip_narrative_false_when_file_absent(tmp_path):
    folder = tmp_path / "trip_1"
    _write_trip_log(folder)

    trip = scan_trip(folder)

    assert trip.trip_narrative is False
    assert "trip_summary.txt" not in trip.known_filenames


def test_trip_narrative_text_reads_the_file(tmp_path):
    folder = tmp_path / "trip_1"
    _write_trip_log(folder)
    (folder / "trip_summary.txt").write_text(
        "The drive was smooth with light traffic.\n", encoding="utf-8"
    )

    trip = scan_trip(folder)

    assert trip.trip_narrative_text == "The drive was smooth with light traffic."


def test_trip_narrative_text_none_when_file_absent(tmp_path):
    folder = tmp_path / "trip_1"
    _write_trip_log(folder)

    trip = scan_trip(folder)

    assert trip.trip_narrative_text is None


def test_trip_narrative_text_degrades_quietly_if_file_becomes_unreadable(tmp_path):
    # Same "don't 500 the page over a secondary file" convention as
    # test_trip_summary_degrades_quietly_if_file_becomes_unreadable
    # above.
    folder = tmp_path / "trip_1"
    _write_trip_log(folder)
    (folder / "trip_summary.txt").write_text("Narrative text.\n", encoding="utf-8")
    trip = scan_trip(folder)
    (folder / "trip_summary.txt").unlink()

    assert trip.trip_narrative_text is None


def test_scan_trip_finds_a_track_up_map_alongside_a_plain_one(tmp_path):
    # Task #795, Christer: "I think we should have different names for
    # track up and not, then we always get the correct map." A trip
    # re-exported once with --map-track-up and once without now has
    # both map.mp4 and map_tu.mp4 on disk at once - both should be
    # surfaced, not just whichever bv-export wrote last.
    folder = tmp_path / "trip_1"
    _write_trip_log(folder)
    (folder / "map.mp4").write_bytes(b"")
    (folder / "map_tu.mp4").write_bytes(b"")

    trip = scan_trip(folder)

    assert trip.map_video == "map.mp4"
    assert trip.map_video_tu == "map_tu.mp4"


def test_scan_trip_map_video_tu_is_none_when_only_the_plain_one_exists(
    tmp_path,
):
    folder = tmp_path / "trip_1"
    _write_trip_log(folder)
    (folder / "map.mp4").write_bytes(b"")

    trip = scan_trip(folder)

    assert trip.map_video == "map.mp4"
    assert trip.map_video_tu is None


def test_scan_trip_map_zoom_glob_picks_up_track_up_variants_too(tmp_path):
    # map_zoom_videos never needed its own "_tu" field - it's already
    # a glob-built tuple, so a map_zoom_60m_tu.mp4 sibling just shows
    # up in it alongside map_zoom_60m.mp4, distinguished by filename.
    folder = tmp_path / "trip_1"
    _write_trip_log(folder)
    (folder / "map_zoom_60m.mp4").write_bytes(b"")
    (folder / "map_zoom_60m_tu.mp4").write_bytes(b"")

    trip = scan_trip(folder)

    assert trip.map_zoom_videos == ("map_zoom_60m.mp4", "map_zoom_60m_tu.mp4")


def test_known_filenames_includes_the_track_up_map(tmp_path):
    folder = tmp_path / "trip_1"
    _write_trip_log(folder)
    (folder / "map.mp4").write_bytes(b"")
    (folder / "map_tu.mp4").write_bytes(b"")

    trip = scan_trip(folder)

    assert "map.mp4" in trip.known_filenames
    assert "map_tu.mp4" in trip.known_filenames


def test_scan_trips_ignores_non_trip_directories(tmp_path):
    target = tmp_path / "trips"
    target.mkdir()
    _write_trip_log(
        target / "trip_20260715_133458_20260715_141235",
        label="trip_20260715_133458_20260715_141235",
    )
    (target / ".osm_cache").mkdir()

    trips = scan_trips(target)

    assert len(trips) == 1
    assert trips[0].label == "trip_20260715_133458_20260715_141235"


def test_scan_trips_sorts_newest_first(tmp_path):
    target = tmp_path / "trips"
    target.mkdir()
    _write_trip_log(
        target / "trip_a",
        label="trip_20260701_000000_20260701_010000",
    )
    _write_trip_log(
        target / "trip_b",
        label="trip_20260715_000000_20260715_010000",
    )

    trips = scan_trips(target)

    assert [trip.label for trip in trips] == [
        "trip_20260715_000000_20260715_010000",
        "trip_20260701_000000_20260701_010000",
    ]


def test_scan_trips_returns_empty_list_for_missing_target(tmp_path):
    assert scan_trips(tmp_path / "does_not_exist") == []


# ---------------------------------------------------------------------------
# scan_trips() one-level camera-subfolder recursion - added so bv-web can
# browse a single shared parent target (e.g. z:/data/trips) even when each
# camera's own Target defaults to a sibling-of-Archive subfolder nested by
# camera id (.../trips/<camera-id>, see default_target_dir() in
# core/camera_config.py) rather than everyone sharing one flat folder.
# Christer's own framing: "that's a dilemma" once this was pointed out.
# ---------------------------------------------------------------------------


def test_scan_trip_accepts_an_explicit_id_override(tmp_path):
    folder = tmp_path / "Kirby" / "trip_20260715_133458_20260715_141235"
    _write_trip_log(folder, label="trip_20260715_133458_20260715_141235")

    trip = scan_trip(folder, id_="Kirby/trip_20260715_133458_20260715_141235")

    assert trip is not None
    assert trip.id == "Kirby/trip_20260715_133458_20260715_141235"
    # label/videos/etc. are still read from the folder itself, unaffected
    # by the id override.
    assert trip.label == "trip_20260715_133458_20260715_141235"


# ---------------------------------------------------------------------------
# scan_all_trips() - combines every configured camera's own Target
# directory with a flat fallback_target, added for task #762/#765:
# each camera may have its own Target (see bv-config's "Target" prompt),
# and nothing requires them to share one - see the function's own
# docstring, and the "Thats a dilemma"/"you're on your own trip"
# discussion in WORKING_CONTEXT.md this resolves.
# ---------------------------------------------------------------------------


def _save_camera(config_dir, id_, target=None, archive=None):
    save_camera_config(
        config_path(config_dir, id_),
        CameraConfig(
            id=id_,
            name=id_,
            archive=archive or (config_dir / "archive" / id_),
            target=target,
        ),
    )


def test_scan_all_trips_finds_trips_under_a_cameras_own_target(tmp_path):
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    camera_target = tmp_path / "Kirby-trips"
    _save_camera(config_dir, "Kirby", target=camera_target)
    _write_trip_log(
        camera_target / "trip_20260715_133458_20260715_141235",
        label="trip_20260715_133458_20260715_141235",
    )

    trips = scan_all_trips(config_dir, tmp_path / "unused_fallback")

    assert len(trips) == 1
    assert trips[0].id == "Kirby/trip_20260715_133458_20260715_141235"
    assert trips[0].label == "trip_20260715_133458_20260715_141235"


def test_scan_all_trips_combines_camera_and_fallback_trips(tmp_path):
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    camera_target = tmp_path / "Kirby-trips"
    _save_camera(config_dir, "Kirby", target=camera_target)
    _write_trip_log(
        camera_target / "trip_20260715_000000_20260715_010000",
        label="trip_20260715_000000_20260715_010000",
    )

    fallback_target = tmp_path / "shared-trips"
    _write_trip_log(
        fallback_target / "trip_20260701_000000_20260701_010000",
        label="trip_20260701_000000_20260701_010000",
    )

    trips = scan_all_trips(config_dir, fallback_target)

    ids = {trip.id for trip in trips}
    assert ids == {
        "Kirby/trip_20260715_000000_20260715_010000",
        "trip_20260701_000000_20260701_010000",
    }
    # Still sorted newest-label-first across camera + fallback combined.
    assert trips[0].id == "Kirby/trip_20260715_000000_20260715_010000"


def test_scan_all_trips_skips_cameras_with_no_target_configured(tmp_path):
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    _save_camera(config_dir, "Kirby", target=None)

    assert scan_all_trips(config_dir, tmp_path / "shared-trips") == []


def test_scan_all_trips_skips_a_corrupt_camera_config(tmp_path):
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    (config_dir / "Broken.cfg").write_text("not valid toml [[[")

    fallback_target = tmp_path / "shared-trips"
    _write_trip_log(fallback_target / "trip_1")

    # A corrupt sibling config shouldn't break discovery of everything
    # else - same "one bad config can't take down the whole listing"
    # rule _camera_options() already follows.
    trips = scan_all_trips(config_dir, fallback_target)
    assert [trip.id for trip in trips] == ["trip_1"]


def test_scan_all_trips_dedupes_a_trip_reachable_both_ways(tmp_path):
    # A camera whose own Target literally *is* the fallback_target -
    # the trip must only be listed once, camera-prefixed (found while
    # scanning the camera first).
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    shared_target = tmp_path / "shared-trips"
    _save_camera(config_dir, "Kirby", target=shared_target)
    _write_trip_log(shared_target / "trip_1")

    trips = scan_all_trips(config_dir, shared_target)

    assert len(trips) == 1
    assert trips[0].id == "Kirby/trip_1"


def test_scan_all_trips_returns_empty_list_when_nothing_configured(tmp_path):
    assert scan_all_trips(tmp_path / "no-config", tmp_path / "no-target") == []


def test_trip_cache_resolves_a_nested_trip_id(tmp_path, monkeypatch):
    import blackvue.web.trips as trips_module

    monkeypatch.setattr(
        trips_module.time, "monotonic", _FakeClock(),
    )

    target = tmp_path / "trips"
    _write_trip_log(target / "Kirby" / "trip_1")

    cache = TripCache(ttl_seconds=2.0)
    trip = cache.get(target, "Kirby/trip_1")

    assert trip is not None
    assert trip.id == "Kirby/trip_1"
    assert trip.folder == target / "Kirby" / "trip_1"


# ---------------------------------------------------------------------------
# TripCache - added so a video player's HTTP range requests (many per
# second while seeking/buffering) don't each redo scan_trip()'s ~nine
# stat()/open() calls against the trip's own folder. See its own docstring
# for why. time.monotonic() is monkeypatched here (rather than a real
# time.sleep()) to control TTL expiry deterministically and instantly.
# ---------------------------------------------------------------------------


class _FakeClock:
    def __init__(self, start=0.0):
        self.value = start

    def __call__(self):
        return self.value


def test_trip_cache_reuses_result_within_ttl(tmp_path, monkeypatch):
    import blackvue.web.trips as trips_module

    clock = _FakeClock()
    monkeypatch.setattr(trips_module.time, "monotonic", clock)

    target = tmp_path / "trips"
    _write_trip_log(target / "trip_1")

    cache = TripCache(ttl_seconds=2.0)
    first = cache.get(target, "trip_1")

    # A file appears after the first (real) scan - a second get() still
    # within the TTL should return the exact same cached TripAssets,
    # not notice the new file yet.
    (target / "trip_1" / "front.mp4").write_bytes(b"")
    clock.value += 1.0
    second = cache.get(target, "trip_1")

    assert second is first
    assert second.videos == ()


def test_trip_cache_rescans_once_ttl_expires(tmp_path, monkeypatch):
    import blackvue.web.trips as trips_module

    clock = _FakeClock()
    monkeypatch.setattr(trips_module.time, "monotonic", clock)

    target = tmp_path / "trips"
    _write_trip_log(target / "trip_1")

    cache = TripCache(ttl_seconds=2.0)
    first = cache.get(target, "trip_1")

    (target / "trip_1" / "front.mp4").write_bytes(b"")
    clock.value += 2.1
    second = cache.get(target, "trip_1")

    assert second is not first
    assert second.videos == ("front.mp4",)


def test_trip_cache_does_not_cache_a_miss(tmp_path, monkeypatch):
    import blackvue.web.trips as trips_module

    clock = _FakeClock()
    monkeypatch.setattr(trips_module.time, "monotonic", clock)

    target = tmp_path / "trips"
    target.mkdir()

    cache = TripCache(ttl_seconds=2.0)
    assert cache.get(target, "trip_1") is None

    # No time has passed at all - if the miss had been cached, this
    # would still return None even though the trip now genuinely
    # exists.
    _write_trip_log(target / "trip_1")
    assert cache.get(target, "trip_1") is not None
