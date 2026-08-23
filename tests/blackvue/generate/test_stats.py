from datetime import datetime, timedelta
from pathlib import Path

from blackvue.archive.asset import Asset
from blackvue.archive.asset_file import AssetFile
from blackvue.archive.recording import Recording
from blackvue.archive.recording_id import RecordingId
from blackvue.generate.stats import compute_recording_stats
from blackvue.telemetry.gps_reader import GpsFix
from blackvue.telemetry.gsensor_reader import GSensorSample


class _FakeManifest:
    def __init__(self, *, gps=True, gsensor=True):
        self._gps = gps
        self._gsensor = gsensor

    def supports(self, capability):
        if capability == "gps":
            return self._gps
        if capability == "gsensor":
            return self._gsensor
        return False

    gps_source_asset = "GPS"
    gsensor_source_asset = "GSENSOR"


class _FakeAdapter:
    """A minimal CameraAdapter stand-in - compute_recording_stats()
    only ever reaches the adapter through adapters/telemetry_bridge.py,
    which just needs manifest.supports()/*_source_asset and
    read_gps()/read_gsensor() taking a path and returning fixtures."""

    def __init__(self, *, fixes=(), samples=(), gps=True, gsensor=True):
        self.manifest = _FakeManifest(gps=gps, gsensor=gsensor)
        self._fixes = fixes
        self._samples = samples

    def read_gps(self, path):
        return self._fixes

    def read_gsensor(self, path):
        return self._samples


def _recording(*, with_gps=True, with_gsensor=True):
    rid = RecordingId.parse("20260823_100000_NF.mp4")
    recording = Recording(rid)
    if with_gps:
        recording.assets[Asset.GPS] = AssetFile(asset=Asset.GPS, path=Path("/fake.gps"))
    if with_gsensor:
        recording.assets[Asset.GSENSOR] = AssetFile(
            asset=Asset.GSENSOR, path=Path("/fake.3gf")
        )
    return recording


def _fix(offset_seconds, lat, lon, speed_kmh=None, *, valid=True, altitude=None):
    return GpsFix(
        timestamp=datetime(2026, 8, 23, 10, 0, 0) + timedelta(seconds=offset_seconds),
        valid=valid,
        latitude=lat,
        longitude=lon,
        speed_kmh=speed_kmh,
        course=0.0,
        altitude_meters=altitude,
    )


def test_compute_recording_stats_no_telemetry_at_all():
    recording = _recording(with_gps=False, with_gsensor=False)
    adapter = _FakeAdapter(gps=False, gsensor=False)

    stats = compute_recording_stats(recording, adapter)

    assert stats["has_gps"] is False
    assert stats["start_gps"] is None
    assert stats["end_gps"] is None
    assert stats["distance_km"] is None
    assert stats["duration_seconds"] is None
    assert stats["max_gforce_x"] is None


def test_compute_recording_stats_gps_and_gsensor_fields():
    recording = _recording()
    fixes = (
        _fix(0, 59.00, 18.00, speed_kmh=0.0, altitude=10.0),
        _fix(60, 59.01, 18.01, speed_kmh=50.0, altitude=20.0),
    )
    samples = (
        GSensorSample(offset=timedelta(seconds=0), x=0, y=0, z=1000),
        GSensorSample(offset=timedelta(seconds=170), x=50, y=-300, z=1000),
    )
    adapter = _FakeAdapter(fixes=fixes, samples=samples)

    stats = compute_recording_stats(recording, adapter)

    assert stats["has_gps"] is True
    assert stats["start_gps"] == {"lat": 59.00, "lon": 18.00}
    assert stats["end_gps"] == {"lat": 59.01, "lon": 18.01}
    assert stats["distance_km"] > 0
    assert stats["max_speed_kmh"] == 50.0
    assert stats["min_altitude_m"] == 10.0
    assert stats["max_altitude_m"] == 20.0
    assert stats["elevation_gain_m"] == 10.0
    # Per-axis g-force is peak/average of the *absolute* reading - see
    # generate/stats.py's own docstring for why (unconfirmed sign
    # convention, so magnitude of deviation is the meaningful signal).
    assert stats["max_gforce_x"] == 50
    assert stats["avg_gforce_x"] == 25.0
    assert stats["max_gforce_y"] == 300
    assert stats["avg_gforce_y"] == 150.0
    # No video asset -> falls back to the g-sensor's own last offset.
    assert stats["duration_seconds"] == 170


def test_compute_recording_stats_duration_falls_back_to_gps_last_fix_vs_id_timestamp():
    # No video, no g-sensor at all - the last-resort GPS fallback pairs
    # recording.id.timestamp (the camera's own filename clock) against
    # the *last* GPS fix, not the first - see _resolve_duration()'s
    # docstring for why (satellite-acquisition delay only ever affects
    # the first fix).
    recording = _recording(with_gsensor=False)
    fixes = (
        _fix(90, 59.00, 18.00, speed_kmh=0.0),  # first fix arrives late
        _fix(150, 59.01, 18.01, speed_kmh=10.0),
    )
    adapter = _FakeAdapter(fixes=fixes, gsensor=False)

    stats = compute_recording_stats(recording, adapter)

    assert stats["duration_seconds"] == 150


def test_compute_recording_stats_fewer_than_two_positioned_fixes():
    recording = _recording(with_gsensor=False)
    adapter = _FakeAdapter(fixes=(_fix(0, 59.00, 18.00),), gsensor=False)

    stats = compute_recording_stats(recording, adapter)

    assert stats["has_gps"] is True  # one real fix still counts as "has a position"
    assert stats["distance_km"] is None
    assert stats["avg_speed_kmh"] is None
    assert stats["moving_seconds"] is None


def test_compute_recording_stats_distance_km_rounded_to_three_decimals(monkeypatch):
    # compute_trip_stats() itself returns raw, unrounded haversine-sum
    # floats (e.g. 0.017387641027105147) - compute_recording_stats()
    # rounds distance_km to 3 decimals (~1m precision) before it lands
    # in the per-recording .stats.json, since a single ~3min recording
    # is almost always sub-1km. Patched at its real module (not the
    # deferred local import inside compute_recording_stats()) so the
    # patch is visible regardless of import timing.
    import blackvue.export.trip_stats as trip_stats_module

    class _FakeTripStats:
        distance_km = 0.017387641027105147
        average_speed_kmh = 12.3
        max_speed_kmh = 20.0
        moving_seconds = 30
        idle_seconds = 5
        min_altitude_meters = None
        max_altitude_meters = None
        elevation_gain_meters = None

    monkeypatch.setattr(
        trip_stats_module, "compute_trip_stats", lambda fixes: _FakeTripStats()
    )

    recording = _recording(with_gsensor=False)
    fixes = (_fix(0, 59.00, 18.00), _fix(60, 59.01, 18.01))
    adapter = _FakeAdapter(fixes=fixes, gsensor=False)

    stats = compute_recording_stats(recording, adapter)

    assert stats["distance_km"] == 0.017


def test_compute_recording_stats_no_gsensor_data_leaves_gforce_none():
    recording = _recording(with_gsensor=False)
    adapter = _FakeAdapter(
        fixes=(_fix(0, 59.00, 18.00, 0.0), _fix(60, 59.01, 18.01, 10.0)),
        gsensor=False,
    )

    stats = compute_recording_stats(recording, adapter)

    assert stats["max_gforce_x"] is None
    assert stats["avg_gforce_z"] is None
