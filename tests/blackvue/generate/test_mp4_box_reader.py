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


def _tkhd(width: int, height: int) -> bytes:
    # Real tkhd v0 payload is 84 bytes (4 version/flags + 20 timestamp/
    # track-ID/reserved/duration fields + 8 reserved + 4 layer/group +
    # 4 volume/reserved + 36 matrix + 4 width + 4 height); only the
    # trailing width/height fields matter for _parse_tkhd_dimensions(),
    # so everything before them is left zeroed.
    payload = bytearray(84)
    payload[-8:-4] = (width << 16).to_bytes(4, "big")
    payload[-4:] = (height << 16).to_bytes(4, "big")
    return bytes(payload)


def _stsd(fourcc: bytes) -> bytes:
    # Real stsd payload: 4 bytes version/flags + 4 bytes entry_count,
    # then one "sample entry" per distinct format the track uses - a
    # BlackVue recording only ever has one. A sample entry is itself
    # box-shaped (size + its own 4-character type, which *is* the
    # codec fourcc - "avc1" for H.264, "hvc1" for HEVC, etc.) followed
    # by codec-specific fields _parse_stsd_codec() never reads, so an
    # arbitrary zeroed payload is enough here.
    entry = _box(fourcc, b"\x00" * 16)
    return b"\x00" * 4 + (1).to_bytes(4, "big") + entry


def _video_trak(
    frame_count: int,
    *,
    width: int | None = None,
    height: int | None = None,
    codec_fourcc: bytes | None = None,
) -> bytes:
    stbl_children = _box(b"stsz", _stsz(frame_count, sample_size=100))
    if codec_fourcc is not None:
        stbl_children += _box(b"stsd", _stsd(codec_fourcc))
    minf = _box(b"minf", _box(b"stbl", stbl_children))
    mdia = _box(b"hdlr", _hdlr(b"vide")) + minf
    trak_payload = _box(b"mdia", mdia)
    if width is not None and height is not None:
        trak_payload = _box(b"tkhd", _tkhd(width, height)) + trak_payload
    return _box(b"trak", trak_payload)


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


def test_read_mp4_info_reads_width_and_height_from_tkhd(tmp_path):
    # The real-world motivation: parking_transition.py's
    # probe_video_properties() needs width/height to size a placeholder
    # clip when ffprobe itself refuses to open a Parking-mode
    # time-lapse recording (a known BlackVue container quirk, not
    # per-file corruption - see that function's own docstring).
    data = _build_mp4(
        _mvhd_v0(timescale=30, duration=60),
        _video_trak(frame_count=1800, width=1920, height=1080),
    )
    path = tmp_path / "20260715_133255_PF.mp4"
    path.write_bytes(data)

    info = read_mp4_info(path)

    assert info.width == 1920
    assert info.height == 1080


def test_read_mp4_info_width_and_height_none_without_a_tkhd_box(tmp_path):
    # Missing/unparseable tkhd is non-fatal, same treatment as a
    # missing stsz already gets for frame_count - callers decide
    # whether None dimensions still leave them enough to work with.
    data = _build_mp4(
        _mvhd_v0(timescale=30, duration=60),
        _video_trak(frame_count=1800),
    )
    path = tmp_path / "20260715_133255_PF.mp4"
    path.write_bytes(data)

    info = read_mp4_info(path)

    assert info.width is None
    assert info.height is None


def test_read_mp4_info_ignores_a_non_video_traks_tkhd(tmp_path):
    # An audio track's own tkhd (if it has one) must never be mistaken
    # for the video track's dimensions - only a trak whose hdlr type is
    # 'vide' should ever contribute width/height.
    audio_with_tkhd = _box(
        b"trak",
        _box(b"tkhd", _tkhd(64, 48)) + _box(b"mdia", _box(b"hdlr", _hdlr(b"soun"))),
    )
    data = _build_mp4(
        _mvhd_v0(timescale=30, duration=60),
        audio_with_tkhd,
        _video_trak(frame_count=1800, width=1920, height=1080),
    )
    path = tmp_path / "20260715_133255_PF.mp4"
    path.write_bytes(data)

    info = read_mp4_info(path)

    assert (info.width, info.height) == (1920, 1080)


def test_read_mp4_info_reads_h264_codec_from_stsd(tmp_path):
    # The real-world motivation: a placeholder clip spliced via
    # ffmpeg's concat demuxer (a stream-copy, not a re-encode) has to
    # be encoded in the *same* codec as the real recording it stands
    # in for, or the resulting file mixes two codecs' raw bitstreams
    # under one declared codec - a real bug found on Christer's own 4K
    # HEVC camera (see parking_transition.py's probe_video_properties()
    # docstring).
    data = _build_mp4(
        _mvhd_v0(timescale=30, duration=60),
        _video_trak(frame_count=1800, codec_fourcc=b"avc1"),
    )
    path = tmp_path / "20260715_133255_PF.mp4"
    path.write_bytes(data)

    info = read_mp4_info(path)

    assert info.codec == "h264"


def test_read_mp4_info_reads_hevc_codec_from_stsd(tmp_path):
    data = _build_mp4(
        _mvhd_v0(timescale=30, duration=60),
        _video_trak(frame_count=1800, codec_fourcc=b"hvc1"),
    )
    path = tmp_path / "20260715_133255_PF.mp4"
    path.write_bytes(data)

    info = read_mp4_info(path)

    assert info.codec == "hevc"


def test_read_mp4_info_codec_none_for_an_unrecognized_fourcc(tmp_path):
    # mp4v (MPEG-4 Part 2), seen on some older/lower-res dashcam
    # firmware, isn't in _CODEC_FOURCC_MAP - callers treat this the
    # same as "couldn't be determined", falling back to bv-export's
    # H.264 default rather than guessing.
    data = _build_mp4(
        _mvhd_v0(timescale=30, duration=60),
        _video_trak(frame_count=1800, codec_fourcc=b"mp4v"),
    )
    path = tmp_path / "20260715_133255_PF.mp4"
    path.write_bytes(data)

    info = read_mp4_info(path)

    assert info.codec is None


def test_read_mp4_info_codec_none_without_an_stsd_box(tmp_path):
    data = _build_mp4(
        _mvhd_v0(timescale=30, duration=60),
        _video_trak(frame_count=1800),
    )
    path = tmp_path / "20260715_133255_PF.mp4"
    path.write_bytes(data)

    info = read_mp4_info(path)

    assert info.codec is None
