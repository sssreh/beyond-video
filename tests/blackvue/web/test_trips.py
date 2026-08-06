from blackvue.web.trips import TripCache
from blackvue.web.trips import scan_trip
from blackvue.web.trips import scan_trips


def _write_trip_log(folder, label="trip_20260715_133458_20260715_141235"):
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "trip.log").write_text(
        f"=== bv-export trip log: {label} ===\n"
        "Started: 2026-07-15T13:34:58\n"
        "Command: bv-export /archive --target /trips\n"
    )


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


def test_scan_trip_flags_gpx_srt_lrc_gsensor_map(tmp_path):
    folder = tmp_path / "trip_1"
    _write_trip_log(folder)
    (folder / "trip.gpx").write_bytes(b"")
    (folder / "trip.srt").write_bytes(b"")
    (folder / "trip.lrc").write_bytes(b"")
    (folder / "map.mp4").write_bytes(b"")
    (folder / "gsensor.mp4").write_bytes(b"")

    trip = scan_trip(folder)

    assert trip.gpx is True
    assert trip.srt is True
    assert trip.lrc is True
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
