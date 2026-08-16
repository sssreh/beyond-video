"""
Tests for adapters/gopro/gpmf.py - the GPMF (GoPro Metadata Format)
MP4 box locator and KLV decoder.

Since no real GoPro footage or working ffmpeg/MP4Box mux path was
available in this sandbox (see gpmf.py's own module docstring), every
test here drives the parser against a hand-built, minimal-but-real MP4
file: a moov with exactly one trak (handler_type 'meta', stsd fourcc
'gpmd') pointing at two GPMF samples in mdat, each a real KLV-encoded
DEVC block. `_write_synthetic_gopro_mp4()` below is that builder -
the same approach used to validate this module during development,
kept here as the regression fixture.
"""

from __future__ import annotations

import struct
from pathlib import Path

import pytest

from blackvue.adapters.gopro import gpmf
from blackvue.generate.media import MediaToolError


# ---------------------------------------------------------------------------
# Synthetic MP4 + GPMF builder - see module docstring.
# ---------------------------------------------------------------------------


def _box(box_type: bytes, payload: bytes) -> bytes:
    return struct.pack(">I4s", 8 + len(payload), box_type) + payload


def _klv(fourcc: str, type_char: str, size: int, repeat: int, payload: bytes) -> bytes:
    header = fourcc.encode("ascii") + type_char.encode("ascii") + struct.pack(">BH", size, repeat)
    data = header + payload
    pad = (-len(payload)) % 4
    return data + b"\x00" * pad


def _build_devc(
    gpsu_text: bytes,
    gps5_rows: list[tuple[int, int, int, int, int]],
    accl_rows: list[tuple[int, int, int]],
    stmp_us: int,
    gpsf: int = 3,
    include_gps: bool = True,
    include_accl: bool = True,
) -> bytes:
    """Build one DEVC container's raw KLV bytes - a single GPMF
    "sample" (roughly one second of telemetry). Every field this
    module's parser reads is included; `include_gps`/`include_accl`
    let a test omit one stream entirely (a real block with GPS lock
    lost, or with no g-sensor STRM at all)."""

    body = b""

    if include_gps:
        scal_gps = _klv(
            "SCAL", "l", 4, 5,
            b"".join(struct.pack(">i", v) for v in [10000000, 10000000, 1000, 1000, 1000]),
        )
        gpsf_klv = _klv("GPSF", "L", 4, 1, struct.pack(">I", gpsf))
        gpsu_klv = _klv("GPSU", "c", 16, 1, gpsu_text)
        gps5_payload = b"".join(struct.pack(">5i", *row) for row in gps5_rows)
        gps5_klv = _klv("GPS5", "l", 20, len(gps5_rows), gps5_payload)
        strm_gps_body = scal_gps + gpsf_klv + gpsu_klv + gps5_klv
        body += _klv("STRM", "\x00", 1, len(strm_gps_body), strm_gps_body)

    if include_accl:
        scal_accl = _klv("SCAL", "l", 4, 1, struct.pack(">i", 418))
        stmp_klv = _klv("STMP", "L", 4, 1, struct.pack(">I", stmp_us))
        accl_payload = b"".join(struct.pack(">3h", *row) for row in accl_rows)
        accl_klv = _klv("ACCL", "s", 6, len(accl_rows), accl_payload)
        strm_accl_body = scal_accl + stmp_klv + accl_klv
        body += _klv("STRM", "\x00", 1, len(strm_accl_body), strm_accl_body)

    return _klv("DEVC", "\x00", 1, len(body), body)


def _write_synthetic_gopro_mp4(
    path: Path, samples: list[bytes], *, include_gpmd_track: bool = True
) -> None:
    """Write a minimal-but-real MP4 to `path`: ftyp + moov (one trak,
    handler 'meta', stsd fourcc 'gpmd' - unless `include_gpmd_track`
    is False, in which case the trak is omitted entirely, simulating a
    plain video with no embedded GPMF track at all) + mdat holding
    `samples` back to back as the gpmd track's raw sample data."""

    sample_sizes = [len(s) for s in samples]
    ftyp = _box(b"ftyp", b"isom" + struct.pack(">I", 0) + b"isomiso2mp41")

    def build_moov(mdat_payload_offset: int) -> bytes:
        mvhd = _box(
            b"mvhd",
            struct.pack(">B3sIIIIH", 0, b"\x00\x00\x00", 0, 0, 1000, 2, 1000) + b"\x00" * 2
            + struct.pack(">I", 0x00010000) + b"\x00" * 12
            + struct.pack(">9i", 0x00010000, 0, 0, 0, 0x00010000, 0, 0, 0, 0x40000000)
            + b"\x00" * 24 + struct.pack(">I", 2),
        )

        if not include_gpmd_track:
            return _box(b"moov", mvhd)

        hdlr = _box(
            b"hdlr",
            struct.pack(">I4s", 0, b"\x00\x00\x00") + b"meta" + b"\x00" * 12 + b"GPMF handler\x00",
        )
        stsd_entry = _box(b"gpmd", b"\x00" * 6 + struct.pack(">H", 1))
        stsd = _box(b"stsd", struct.pack(">I", 0) + struct.pack(">I", 1) + stsd_entry)
        stsz_entries = b"".join(struct.pack(">I", s) for s in sample_sizes)
        stsz = _box(b"stsz", struct.pack(">III", 0, 0, len(sample_sizes)) + stsz_entries)
        stsc = _box(b"stsc", struct.pack(">I", 0) + struct.pack(">I", 1) + struct.pack(">III", 1, len(sample_sizes), 1))
        stco = _box(b"stco", struct.pack(">I", 0) + struct.pack(">I", 1) + struct.pack(">I", mdat_payload_offset))
        stbl = _box(b"stbl", stsd + stsz + stsc + stco)
        nmhd = _box(b"nmhd", struct.pack(">I", 0))
        dref_entry = _box(b"url ", struct.pack(">I", 1))
        dref = _box(b"dref", struct.pack(">I", 0) + struct.pack(">I", 1) + dref_entry)
        dinf = _box(b"dinf", dref)
        minf = _box(b"minf", nmhd + dinf + stbl)
        mdhd = _box(b"mdhd", struct.pack(">B3sIIIIH", 0, b"\x00\x00\x00", 0, 0, 1000, 2000, 0) + b"\x00\x00")
        mdia = _box(b"mdia", mdhd + hdlr + minf)
        tkhd = _box(
            b"tkhd",
            struct.pack(">B3sIIIiIiHH", 0, b"\x00\x00\x00", 0, 0, 1, 0, 2000, 0, 0, 0)
            + struct.pack(">9i", 0x00010000, 0, 0, 0, 0x00010000, 0, 0, 0, 0x40000000)
            + struct.pack(">II", 0, 0),
        )
        trak = _box(b"trak", tkhd + mdia)
        return _box(b"moov", mvhd + trak)

    moov_probe = build_moov(0)
    mdat_payload_offset = len(ftyp) + len(moov_probe) + 8
    moov = build_moov(mdat_payload_offset)
    assert len(moov) == len(moov_probe)

    mdat = _box(b"mdat", b"".join(samples))

    path.write_bytes(ftyp + moov + mdat)


def _default_samples() -> list[bytes]:
    sample1 = _build_devc(
        b"250101120000.000",
        gps5_rows=[(599170000, 105170000, 50000, 500, 520), (599170100, 105170100, 50010, 505, 525)],
        accl_rows=[(10, -5, 1000), (12, -4, 998)],
        stmp_us=0,
    )
    sample2 = _build_devc(
        b"250101120001.000",
        gps5_rows=[(599170200, 105170200, 50020, 510, 530), (599170300, 105170300, 50030, 515, 535)],
        accl_rows=[(14, -3, 996), (16, -2, 994)],
        stmp_us=1000000,
    )
    return [sample1, sample2]


# ---------------------------------------------------------------------------
# locate_gpmf_stream()
# ---------------------------------------------------------------------------


def test_locate_gpmf_stream_returns_concatenated_sample_bytes(tmp_path):
    path = tmp_path / "clip.mp4"
    samples = _default_samples()
    _write_synthetic_gopro_mp4(path, samples)

    data = gpmf.locate_gpmf_stream(path)

    assert data == b"".join(samples)


def test_locate_gpmf_stream_raises_for_no_gpmd_track(tmp_path):
    path = tmp_path / "no_telemetry.mp4"
    _write_synthetic_gopro_mp4(path, _default_samples(), include_gpmd_track=False)

    with pytest.raises(MediaToolError):
        gpmf.locate_gpmf_stream(path)


def test_locate_gpmf_stream_raises_for_a_non_mp4_file(tmp_path):
    path = tmp_path / "not_a_video.mp4"
    path.write_bytes(b"this is not an mp4 file at all")

    with pytest.raises(MediaToolError):
        gpmf.locate_gpmf_stream(path)


# ---------------------------------------------------------------------------
# extract_gps_fixes() / extract_gsensor_samples()
# ---------------------------------------------------------------------------


def test_extract_gps_fixes_decodes_scaled_lat_lon_and_speed():
    data = b"".join(_default_samples())

    fixes = gpmf.extract_gps_fixes(data)

    assert len(fixes) == 4
    first = fixes[0]
    assert first.latitude == pytest.approx(59.917)
    assert first.longitude == pytest.approx(10.517)
    assert first.speed_kmh == pytest.approx(1.872)
    assert first.valid is True
    assert first.course is None


def test_extract_gps_fixes_spreads_row_timestamps_across_the_block_second():
    data = b"".join(_default_samples())

    fixes = gpmf.extract_gps_fixes(data)

    # Two rows in the first (GPSU-anchored) block -> half a second apart.
    assert (fixes[1].timestamp - fixes[0].timestamp).total_seconds() == pytest.approx(0.5)
    # Second block's GPSU anchor is exactly one second after the first.
    assert (fixes[2].timestamp - fixes[0].timestamp).total_seconds() == pytest.approx(1.0)


def test_extract_gps_fixes_marks_a_no_lock_block_invalid():
    sample = _build_devc(
        b"250101120000.000",
        gps5_rows=[(599170000, 105170000, 50000, 500, 520)],
        accl_rows=[(10, -5, 1000)],
        stmp_us=0,
        gpsf=0,
    )

    fixes = gpmf.extract_gps_fixes(sample)

    assert len(fixes) == 1
    assert fixes[0].valid is False


def test_extract_gps_fixes_skips_a_block_with_no_gps5_stream():
    sample = _build_devc(
        b"250101120000.000",
        gps5_rows=[],
        accl_rows=[(10, -5, 1000)],
        stmp_us=0,
        include_gps=False,
    )

    assert gpmf.extract_gps_fixes(sample) == ()


def test_extract_gsensor_samples_returns_raw_unscaled_values():
    data = b"".join(_default_samples())

    samples = gpmf.extract_gsensor_samples(data)

    assert len(samples) == 4
    assert (samples[0].x, samples[0].y, samples[0].z) == (10, -5, 1000)
    # Raw, not divided by ACCL's own SCAL (418) - see module docstring.
    assert samples[1].x == 12


def test_extract_gsensor_samples_offsets_come_from_stmp_and_interpolate():
    data = b"".join(_default_samples())

    samples = gpmf.extract_gsensor_samples(data)

    assert samples[0].offset.total_seconds() == pytest.approx(0.0)
    assert samples[1].offset.total_seconds() == pytest.approx(0.5)
    assert samples[2].offset.total_seconds() == pytest.approx(1.0)


def test_extract_gsensor_samples_skips_a_block_with_no_accl_stream():
    sample = _build_devc(
        b"250101120000.000",
        gps5_rows=[(599170000, 105170000, 50000, 500, 520)],
        accl_rows=[],
        stmp_us=0,
        include_accl=False,
    )

    assert gpmf.extract_gsensor_samples(sample) == ()


# ---------------------------------------------------------------------------
# read_gps() / read_gsensor() - the two together, end to end from a file.
# ---------------------------------------------------------------------------


def test_read_gps_and_read_gsensor_end_to_end(tmp_path):
    path = tmp_path / "clip.mp4"
    _write_synthetic_gopro_mp4(path, _default_samples())

    assert len(gpmf.read_gps(path)) == 4
    assert len(gpmf.read_gsensor(path)) == 4


def test_read_gps_raises_media_tool_error_for_a_video_with_no_gpmf(tmp_path):
    path = tmp_path / "plain.mp4"
    _write_synthetic_gopro_mp4(path, _default_samples(), include_gpmd_track=False)

    with pytest.raises(MediaToolError):
        gpmf.read_gps(path)

    with pytest.raises(MediaToolError):
        gpmf.read_gsensor(path)
