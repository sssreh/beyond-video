import struct
from datetime import timedelta

from blackvue.generate.media import MediaToolError
from blackvue.telemetry.gsensor_reader import read_gsensor
from blackvue.telemetry.gsensor_reader import write_gsensor


def _record(ms: int, x: int, y: int, z: int) -> bytes:
    return struct.pack(">Ihhh", ms, x, y, z)


def test_read_gsensor_parses_records_in_order(tmp_path):
    path = tmp_path / "sample.3gf"
    path.write_bytes(
        _record(0, 120, -10, 24)
        + _record(100, 118, -12, 30)
        + _record(200, 122, -10, 28)
    )

    samples = read_gsensor(path)

    assert samples == (
        _sample(0, 120, -10, 24),
        _sample(100, 118, -12, 30),
        _sample(200, 122, -10, 28),
    )


def _sample(ms, x, y, z):
    from blackvue.telemetry.gsensor_reader import GSensorSample

    return GSensorSample(offset=timedelta(milliseconds=ms), x=x, y=y, z=z)


def test_read_gsensor_handles_negative_axis_values(tmp_path):
    path = tmp_path / "sample.3gf"
    path.write_bytes(_record(0, -500, -1000, 32000))

    samples = read_gsensor(path)

    assert samples[0].x == -500
    assert samples[0].y == -1000
    assert samples[0].z == 32000


def test_read_gsensor_does_not_wrap_the_timestamp_at_16_bits(tmp_path):
    # The core reason this is a 4-byte field and not 2: a real g-sensor
    # log spans well past 65536ms (65.5s). Confirm a timestamp beyond
    # that boundary round-trips correctly rather than wrapping.
    path = tmp_path / "sample.3gf"
    path.write_bytes(_record(169600, 1, 2, 3))

    samples = read_gsensor(path)

    assert samples[0].offset == timedelta(milliseconds=169600)


def test_read_gsensor_returns_empty_tuple_for_empty_file(tmp_path):
    path = tmp_path / "empty.3gf"
    path.write_bytes(b"")

    assert read_gsensor(path) == ()


def test_write_gsensor_round_trips_through_read_gsensor(tmp_path):
    from blackvue.telemetry.gsensor_reader import GSensorSample

    samples = (
        GSensorSample(offset=timedelta(milliseconds=0), x=1, y=-2, z=3),
        GSensorSample(offset=timedelta(milliseconds=100), x=4, y=5, z=-6),
        GSensorSample(offset=timedelta(seconds=200), x=0, y=0, z=0),
    )

    path = tmp_path / "roundtrip.3gf"
    write_gsensor(samples, path)

    assert read_gsensor(path) == samples


def test_read_gsensor_rejects_a_truncated_file(tmp_path):
    path = tmp_path / "truncated.3gf"
    path.write_bytes(_record(0, 1, 2, 3) + b"\x00\x00\x00")  # 13 bytes

    try:
        read_gsensor(path)
        raised = False
    except MediaToolError as exc:
        raised = True
        assert "truncated" in str(exc) or "multiple" in str(exc)

    assert raised is True
