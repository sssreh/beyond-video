"""
Tests for adapters/gopro/adapter.py - GoProAdapter.

Shares its recursive-scan machinery with FolderAdapter (see
_recursive_scan.py and this module's own docstring) - the scan-side
tests below are deliberately a much smaller subset of
test_folder_adapter.py's (that module's tests already cover the
shared code path in full; duplicating them here would just be testing
_recursive_scan.py twice). What's specific to GoProAdapter and tested
here: real GPS/g-sensor telemetry via embedded GPMF, the capability
guards for what it still doesn't support, and - the explicit case
Christer's design note called for - a mixed-content archive (a real
GPMF-shaped clip, a video with no GPMF track, a non-video file)
scanning successfully as a whole with per-recording telemetry
degradation rather than an all-or-nothing failure.
"""

from __future__ import annotations

import os
import struct
from pathlib import Path

import pytest

from blackvue.adapters import registry
from blackvue.adapters.base import AdapterCapabilityError
from blackvue.adapters.gopro.adapter import GoProAdapter
from blackvue.adapters.telemetry_bridge import read_recording_gps
from blackvue.adapters.telemetry_bridge import read_recording_gsensor
from blackvue.archive.asset import Asset


@pytest.fixture()
def adapter():
    return GoProAdapter()


def _touch(path: Path, *, size: int = 10, mtime: float | None = None) -> Path:
    path.write_bytes(b"x" * size)
    if mtime is not None:
        os.utime(path, (mtime, mtime))
    return path


# ---------------------------------------------------------------------------
# Synthetic MP4 + GPMF builder - a smaller copy of test_gopro_gpmf.py's own
# (see that file's module docstring for why this approach exists at all:
# no ffmpeg/MP4Box mux path was usable in this sandbox for a synthetic
# 'gpmd' stream, so the fixture is a hand-built, minimal-but-real MP4).
# ---------------------------------------------------------------------------


def _box(box_type: bytes, payload: bytes) -> bytes:
    return struct.pack(">I4s", 8 + len(payload), box_type) + payload


def _klv(fourcc: str, type_char: str, size: int, repeat: int, payload: bytes) -> bytes:
    header = fourcc.encode("ascii") + type_char.encode("ascii") + struct.pack(">BH", size, repeat)
    data = header + payload
    pad = (-len(payload)) % 4
    return data + b"\x00" * pad


def _build_devc() -> bytes:
    scal_gps = _klv(
        "SCAL", "l", 4, 5,
        b"".join(struct.pack(">i", v) for v in [10000000, 10000000, 1000, 1000, 1000]),
    )
    gpsf_klv = _klv("GPSF", "L", 4, 1, struct.pack(">I", 3))
    gpsu_klv = _klv("GPSU", "c", 16, 1, b"250101120000.000")
    gps5_klv = _klv("GPS5", "l", 20, 1, struct.pack(">5i", 599170000, 105170000, 50000, 500, 520))
    strm_gps_body = scal_gps + gpsf_klv + gpsu_klv + gps5_klv
    strm_gps = _klv("STRM", "\x00", 1, len(strm_gps_body), strm_gps_body)

    scal_accl = _klv("SCAL", "l", 4, 1, struct.pack(">i", 418))
    stmp_klv = _klv("STMP", "L", 4, 1, struct.pack(">I", 0))
    accl_klv = _klv("ACCL", "s", 6, 1, struct.pack(">3h", 10, -5, 1000))
    strm_accl_body = scal_accl + stmp_klv + accl_klv
    strm_accl = _klv("STRM", "\x00", 1, len(strm_accl_body), strm_accl_body)

    body = strm_gps + strm_accl
    return _klv("DEVC", "\x00", 1, len(body), body)


def _write_gopro_style_mp4(path: Path, *, with_gpmf: bool, mtime: float | None = None) -> None:
    """Write a minimal-but-real MP4 to `path` - with a real 'gpmd'
    GPMF track (`with_gpmf=True`) or without one at all (`False`,
    simulating a re-encoded/trimmed clip, or any other plain video
    that happens to sit in a GoPro folder)."""

    samples = [_build_devc()] if with_gpmf else []
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
        if not with_gpmf:
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
    if mtime is not None:
        os.utime(path, (mtime, mtime))


# ---------------------------------------------------------------------------
# Manifest / registration sanity.
# ---------------------------------------------------------------------------


def test_manifest_is_the_real_gopro_manifest(adapter):
    assert adapter.manifest.adapter_id == "gopro"
    assert adapter.manifest == registry.load_adapter_manifest("gopro")


def test_registered_under_gopro_id():
    assert registry.get_adapter("gopro").manifest.adapter_id == "gopro"


def test_manifest_declares_gps_and_gsensor_but_not_network_or_config(adapter):
    assert adapter.manifest.supports("gps")
    assert adapter.manifest.supports("gsensor")
    assert not adapter.manifest.supports("network_connect")
    assert not adapter.manifest.supports("config_snapshot")


# ---------------------------------------------------------------------------
# open_archive() - shared _recursive_scan.py machinery, spot-checked (full
# coverage lives in test_folder_adapter.py - see module docstring).
# ---------------------------------------------------------------------------


def test_open_archive_finds_a_video_and_stores_it_under_front(adapter, tmp_path):
    _write_gopro_style_mp4(tmp_path / "GH010001.MP4", with_gpmf=True, mtime=1700000000)

    archive = adapter.open_archive(tmp_path)

    assert len(archive.recordings) == 1
    assert archive.recordings[0].has(Asset.FRONT)


def test_open_archive_prefers_gpmf_gpsu_anchor_over_file_mtime(adapter, tmp_path):
    # Real report: a synthetic recording id based on file mtime can
    # reflect when a clip was copied/downloaded onto Christer's
    # machine rather than when it was actually recorded - risking two
    # different physical clips colliding into the same id. The GPMF
    # stream's own GPSU anchor (task #930) is real device-clock capture
    # time, so it must win over mtime whenever it's available. Proven
    # here by setting mtime to a date far from the synthetic GPSU value
    # baked into _build_devc() (2025-01-01 12:00:00) and checking the
    # resolved id reflects the GPSU date, not the mtime one.
    _write_gopro_style_mp4(tmp_path / "GH010001.MP4", with_gpmf=True, mtime=1700000000)

    archive = adapter.open_archive(tmp_path)
    recording_id = archive.recordings[0].id

    assert recording_id.value.startswith("20250101")


# ---------------------------------------------------------------------------
# read_gps() / read_gsensor() - real embedded-GPMF telemetry.
# ---------------------------------------------------------------------------


def test_read_gps_returns_fixes_from_a_real_gpmf_video(adapter, tmp_path):
    video = tmp_path / "clip.mp4"
    _write_gopro_style_mp4(video, with_gpmf=True)

    fixes = adapter.read_gps(video)

    assert len(fixes) == 1
    assert fixes[0].latitude == pytest.approx(59.917)


def test_read_gsensor_returns_samples_from_a_real_gpmf_video(adapter, tmp_path):
    video = tmp_path / "clip.mp4"
    _write_gopro_style_mp4(video, with_gpmf=True)

    samples = adapter.read_gsensor(video)

    assert len(samples) == 1
    assert (samples[0].x, samples[0].y, samples[0].z) == (10, -5, 1000)


def test_read_gps_raises_media_tool_error_for_a_video_with_no_gpmf_track(adapter, tmp_path):
    from blackvue.generate.media import MediaToolError

    video = tmp_path / "no_telemetry.mp4"
    _write_gopro_style_mp4(video, with_gpmf=False)

    with pytest.raises(MediaToolError):
        adapter.read_gps(video)


# ---------------------------------------------------------------------------
# Capability guards - connect()/config_snapshot_seconds() are not supported
# by this adapter's manifest, same as FolderAdapter.
# ---------------------------------------------------------------------------


def test_connect_raises_capability_error(adapter):
    with pytest.raises(AdapterCapabilityError):
        adapter.connect([])


def test_config_snapshot_seconds_raises_capability_error(adapter):
    with pytest.raises(AdapterCapabilityError):
        adapter.config_snapshot_seconds("text")


# ---------------------------------------------------------------------------
# The mixed-content-folder degradation test - per Christer's design note:
# "The worst archive case would be a mix of everything video/picture but
# then it should regress to plain folder and have minimal options." A
# real GoPro folder/card is realistically clean GPMF clips, clips with no
# usable GPMF stream, and non-video files all together - the whole scan
# must succeed, and each recording's telemetry must degrade
# independently rather than one bad file taking down the others.
# ---------------------------------------------------------------------------


def test_mixed_content_folder_scans_fully_with_per_recording_telemetry_degradation(
    adapter, tmp_path
):
    # A real GPMF-shaped clip - has both GPS and g-sensor data.
    _write_gopro_style_mp4(tmp_path / "GH010001.MP4", with_gpmf=True, mtime=1700000000)
    # A video that matches video_extensions but has no GPMF track at all
    # (re-encoded, trimmed by another tool, older firmware, ...).
    _write_gopro_style_mp4(tmp_path / "GH010002.MP4", with_gpmf=False, mtime=1700000100)
    # Non-video files - already excluded by video_extensions matching,
    # same as FolderAdapter today.
    _touch(tmp_path / "GOPR0001.JPG", mtime=1700000200)
    _touch(tmp_path / "notes.txt", mtime=1700000300)

    # The scan itself must not raise or drop either video.
    archive = adapter.open_archive(tmp_path)
    assert len(archive.recordings) == 2

    # Identify the two recordings by their actual telemetry content rather
    # than by sorted-id order: the GPMF clip's GPSU device-clock anchor
    # (task #930's timestamp-resolution fix) takes priority over its file
    # mtime, so its synthetic id no longer necessarily sorts adjacent to
    # the no-GPMF clip's mtime-derived one - the two synthetic GPSU/mtime
    # values in this test aren't chosen to agree, and don't need to.
    recordings_with_gps = [
        r for r in archive.recordings if len(read_recording_gps(adapter, r)) == 1
    ]
    recordings_without_gps = [
        r for r in archive.recordings if r not in recordings_with_gps
    ]
    assert len(recordings_with_gps) == 1
    assert len(recordings_without_gps) == 1
    with_telemetry = recordings_with_gps[0]
    without_telemetry = recordings_without_gps[0]

    # The clean clip: real telemetry, via the adapter-aware bridge every
    # pipeline caller actually uses (trip_export.py, search.py, ...).
    assert len(read_recording_gps(adapter, with_telemetry)) == 1
    assert len(read_recording_gsensor(adapter, with_telemetry)) == 1

    # The no-GPMF clip: absent, not fatal - telemetry_bridge.py's own
    # "missing/bad telemetry is absent, not an error" contract, the same
    # thing that already happens today for an ordinary FolderAdapter
    # recording with no telemetry at all.
    assert read_recording_gps(adapter, without_telemetry) == ()
    assert read_recording_gsensor(adapter, without_telemetry) == ()
