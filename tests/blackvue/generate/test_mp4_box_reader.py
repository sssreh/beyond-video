from blackvue.generate.mp4_box_reader import read_mp4_info


def _box(box_type: bytes, payload: bytes) -> bytes:
    return (8 + len(payload)).to_bytes(4, "big") + box_type + payload


def _mvhd_v0(timescale: int, duration: int) -> bytes:
    payload = bytearray(20)
    payload[12:16] = timescale.to_bytes(4, "big")
    payload[16:20] = duration.to_bytes(4, "big")
    return bytes(payload)


def _mvhd_v1(timescale: int, duration: int) -> bytes:
    payload = bytearray(32)
    payload[0] = 1  # version
    payload[20:24] = timescale.to_bytes(4, "big")
    payload[24:32] = duration.to_bytes(8, "big")
    return bytes(payload)


def _hdlr(handler_type: bytes) -> bytes:
    payload = bytearray(12)
    payload[8:12] = handler_type
    return bytes(payload)


def _stsz(sample_count: int, sample_size: int = 0) -> bytes:
    payload = bytearray(12)
    payload[4:8] = sample_size.to_bytes(4, "big")
    payload[8:12] = sample_count.to_bytes(4, "big")
    return bytes(payload)


def _video_trak(frame_count: int) -> bytes:
    stbl = _box(b"stsz", _stsz(frame_count, sample_size=100))
    minf = _box(b"minf", _box(b"stbl", stbl))
    mdia = _box(b"hdlr", _hdlr(b"vide")) + minf
    return _box(b"trak", _box(b"mdia", mdia))


def _audio_trak_with_garbage() -> bytes:
    # Simulates a real dashcam's broken vestigial audio track: the
    # stsc payload is nonsense (real cameras produce something like
    # "STSC entry 0 is invalid (first=0 count=0 id=1)"), but the box
    # *sizes* are still self-consistent, so a structural walk that
    # never validates the contents can skip straight past it.
    garbage_stsc = _box(b"stsc", b"\xff" * 40)
    stbl = _box(b"stsz", _stsz(0)) + garbage_stsc
    minf = _box(b"minf", _box(b"stbl", stbl))
    mdia = _box(b"hdlr", _hdlr(b"soun")) + minf
    return _box(b"trak", _box(b"mdia", mdia))


def _build_mp4(mvhd: bytes, *traks: bytes) -> bytes:
    moov_payload = _box(b"mvhd", mvhd) + b"".join(traks)
    moov = _box(b"moov", moov_payload)
    ftyp = _box(b"ftyp", b"isom" + (0).to_bytes(4, "big") + b"isomiso2avc1mp41")
    mdat = _box(b"mdat", b"\x00" * 64)
    return ftyp + moov + mdat


def test_read_mp4_info_reads_duration_and_frame_count(tmp_path):
    data = _build_mp4(
        _mvhd_v0(timescale=30, duration=60),
        _video_trak(frame_count=1800),
        _audio_trak_with_garbage(),
    )
    path = tmp_path / "20260715_133255_PF.mp4"
    path.write_bytes(data)

    info = read_mp4_info(path)

    assert info.duration_seconds == 2.0  # 60 / 30
    assert info.frame_count == 1800


def test_read_mp4_info_ignores_broken_audio_track(tmp_path):
    # The whole point: a garbage audio trak must not prevent reading
    # the (intact) video trak's info.
    data = _build_mp4(
        _mvhd_v0(timescale=25, duration=100),
        _audio_trak_with_garbage(),
        _video_trak(frame_count=42),
    )
    path = tmp_path / "20260715_140000_NF.mp4"
    path.write_bytes(data)

    info = read_mp4_info(path)

    assert info.duration_seconds == 4.0  # 100 / 25
    assert info.frame_count == 42


def test_read_mp4_info_supports_mvhd_version_1(tmp_path):
    data = _build_mp4(
        _mvhd_v1(timescale=1000, duration=5000),
        _video_trak(frame_count=10),
    )
    path = tmp_path / "20260715_150000_NF.mp4"
    path.write_bytes(data)

    info = read_mp4_info(path)

    assert info.duration_seconds == 5.0  # 5000 / 1000
    assert info.frame_count == 10


def test_read_mp4_info_duration_only_when_no_video_track(tmp_path):
    data = _build_mp4(
        _mvhd_v0(timescale=10, duration=30),
        _audio_trak_with_garbage(),
    )
    path = tmp_path / "20260715_160000_NF.mp4"
    path.write_bytes(data)

    info = read_mp4_info(path)

    assert info.duration_seconds == 3.0
    assert info.frame_count is None
