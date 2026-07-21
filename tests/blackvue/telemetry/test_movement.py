from datetime import datetime, timedelta

from blackvue.archive.asset import Asset
from blackvue.archive.asset_file import AssetFile
from blackvue.archive.recording import Recording
from blackvue.archive.recording_id import RecordingId
from blackvue.telemetry.gps_reader import GpsFix
from blackvue.telemetry.gsensor_reader import GSensorSample
from blackvue.telemetry.movement import gps_shows_movement_at_end
from blackvue.telemetry.movement import gps_shows_movement_at_start
from blackvue.telemetry.movement import gsensor_shows_movement_at_end
from blackvue.telemetry.movement import gsensor_shows_movement_at_start
from blackvue.telemetry.movement import movement_bridges_gap

# All coordinates/timestamps below are fabricated for testing.


def _fix(ts: str, *, valid: bool = True, speed_kmh: float | None = 0.0):
    return GpsFix(
        timestamp=datetime.strptime(ts, "%Y%m%d_%H%M%S"),
        valid=valid,
        latitude=59.0 if valid else None,
        longitude=18.0 if valid else None,
        speed_kmh=speed_kmh if valid else None,
        course=0.0 if valid else None,
    )


def _sample(ms: int, x: int, y: int, z: int) -> GSensorSample:
    return GSensorSample(offset=timedelta(milliseconds=ms), x=x, y=y, z=z)


# --- gps_shows_movement_at_end / _at_start ---


def test_gps_shows_movement_at_end_true_when_last_fix_is_fast():
    fixes = (
        _fix("20260720_100000", speed_kmh=0.0),
        _fix("20260720_100010", speed_kmh=40.0),
    )

    assert gps_shows_movement_at_end(fixes) is True


def test_gps_shows_movement_at_end_false_when_edge_is_slow():
    # The two fixes are 30s apart - wider than the 15s edge window -
    # so only the second (slow) fix should be considered.
    fixes = (
        _fix("20260720_100000", speed_kmh=40.0),
        _fix("20260720_100030", speed_kmh=0.0),
    )

    assert gps_shows_movement_at_end(fixes) is False


def test_gps_shows_movement_at_end_ignores_invalid_fixes():
    fixes = (_fix("20260720_100000", valid=False),)

    assert gps_shows_movement_at_end(fixes) is None


def test_gps_shows_movement_at_start_true_when_first_fix_is_fast():
    fixes = (
        _fix("20260720_100000", speed_kmh=40.0),
        _fix("20260720_100010", speed_kmh=0.0),
    )

    assert gps_shows_movement_at_start(fixes) is True


def test_gps_shows_movement_returns_none_for_no_fixes():
    assert gps_shows_movement_at_end(()) is None
    assert gps_shows_movement_at_start(()) is None


# --- gsensor_shows_movement_at_end / _at_start ---


def _quiet_then_noisy(quiet_seconds=60, noisy_seconds=20, step_ms=100):
    # Quiet phase must span well over one DEFAULT_EDGE_WINDOW (15s) so
    # the rolling baseline is computed from genuinely quiet windows,
    # not a mix of quiet and noisy samples in a single window.
    samples = []
    ms = 0
    quiet_end = quiet_seconds * 1000
    while ms < quiet_end:
        samples.append(_sample(ms, 100, 100, 100))
        ms += step_ms

    noisy_end = quiet_end + noisy_seconds * 1000
    i = 0
    while ms < noisy_end:
        # Alternate wildly to create high variance.
        value = 100 + (500 if i % 2 == 0 else -500)
        samples.append(_sample(ms, value, value, value))
        ms += step_ms
        i += 1

    return tuple(samples)


def test_gsensor_shows_movement_at_end_true_for_noisy_tail():
    samples = _quiet_then_noisy()

    assert gsensor_shows_movement_at_end(samples) is True


def test_gsensor_shows_movement_at_start_false_for_quiet_head():
    samples = _quiet_then_noisy()

    assert gsensor_shows_movement_at_start(samples) is False


def test_gsensor_shows_movement_returns_none_for_too_little_data():
    samples = (_sample(0, 1, 2, 3),)

    assert gsensor_shows_movement_at_end(samples) is None
    assert gsensor_shows_movement_at_start(samples) is None


def test_gsensor_shows_movement_false_for_uniformly_quiet_data():
    samples = tuple(_sample(ms, 100, 100, 100) for ms in range(0, 3000, 100))

    assert gsensor_shows_movement_at_end(samples) is False
    assert gsensor_shows_movement_at_start(samples) is False


# --- movement_bridges_gap (integration against real files on disk) ---


def _recording(
    label: str,
    ts: str,
    *,
    gps_path=None,
    gsensor_path=None,
) -> Recording:
    assets = {}
    if gps_path is not None:
        assets[Asset.GPS] = AssetFile(Asset.GPS, gps_path)
    if gsensor_path is not None:
        assets[Asset.GSENSOR] = AssetFile(Asset.GSENSOR, gsensor_path)
    return Recording(id=RecordingId(f"{ts}_{label}"), assets=assets)


def test_movement_bridges_gap_true_when_gps_shows_speed_at_previous_end(
    tmp_path,
):
    gps_path = tmp_path / "prev.gps"
    gps_path.write_text(
        "[1700000000000]$GPRMC,120000.00,A,4807.038,N,01131.000,E,"
        "30.00,45.00,010124,,,A*6D\n"
    )

    previous = _recording("N", "20260720_100000", gps_path=gps_path)
    current = _recording("N", "20260720_101500")

    assert movement_bridges_gap(previous, current) is True


def test_movement_bridges_gap_false_when_no_telemetry_files():
    previous = _recording("N", "20260720_100000")
    current = _recording("N", "20260720_101500")

    assert movement_bridges_gap(previous, current) is False


def test_movement_bridges_gap_false_when_files_show_no_movement(tmp_path):
    gps_path = tmp_path / "prev.gps"
    gps_path.write_text(
        "[1700000000000]$GPRMC,120000.00,A,4807.038,N,01131.000,E,"
        "0.00,45.00,010124,,,A*6D\n"
    )
    gsensor_path = tmp_path / "next.3gf"
    import struct

    gsensor_path.write_bytes(
        b"".join(
            struct.pack(">Ihhh", ms, 100, 100, 100)
            for ms in range(0, 3000, 100)
        )
    )

    previous = _recording("N", "20260720_100000", gps_path=gps_path)
    current = _recording("N", "20260720_101500", gsensor_path=gsensor_path)

    assert movement_bridges_gap(previous, current) is False
