import subprocess

from blackvue.generate.mp4_repair import repair_parking_container


def _box(box_type: bytes, payload: bytes) -> bytes:
    return (8 + len(payload)).to_bytes(4, "big") + box_type + payload


def _hdlr(handler_type: bytes) -> bytes:
    payload = bytearray(12)
    payload[8:12] = handler_type
    return bytes(payload)


def _stsz(sample_count: int, sample_size: int = 0) -> bytes:
    payload = bytearray(12)
    payload[4:8] = sample_size.to_bytes(4, "big")
    payload[8:12] = sample_count.to_bytes(4, "big")
    return bytes(payload)


def _stsc(entries: list[tuple[int, int, int]]) -> bytes:
    payload = bytearray(8)
    payload[4:8] = len(entries).to_bytes(4, "big")
    for first_chunk, samples_per_chunk, sample_description_index in entries:
        payload += (
            first_chunk.to_bytes(4, "big")
            + samples_per_chunk.to_bytes(4, "big")
            + sample_description_index.to_bytes(4, "big")
        )
    return bytes(payload)


def _stco(offsets: list[int]) -> bytes:
    payload = bytearray(8)
    payload[4:8] = len(offsets).to_bytes(4, "big")
    for offset in offsets:
        payload += offset.to_bytes(4, "big")
    return bytes(payload)


def _stts(entries: list[tuple[int, int]]) -> bytes:
    payload = bytearray(8)
    payload[4:8] = len(entries).to_bytes(4, "big")
    for count, delta in entries:
        payload += count.to_bytes(4, "big") + delta.to_bytes(4, "big")
    return bytes(payload)


def _video_trak(sample_count: int) -> bytes:
    # A clean, fully self-consistent video track - the shape confirmed
    # against one of Christer's own real Parking recordings (647
    # samples agreeing across stsz/stco/stsc/stts).
    stbl = (
        _box(b"stsz", _stsz(sample_count, sample_size=100))
        + _box(b"stsc", _stsc([(1, 1, 1)]))
        + _box(b"stco", _stco(list(range(1000, 1000 + sample_count))))
        + _box(b"stts", _stts([(sample_count, 1)]))
    )
    minf = _box(b"minf", _box(b"stbl", stbl))
    mdia = _box(b"hdlr", _hdlr(b"vide")) + minf
    return _box(b"trak", _box(b"mdia", mdia))


def _empty_audio_trak() -> bytes:
    # The confirmed real-world shape (2026-08-08, via
    # scripts/dump_parking_container.py against one of Christer's own
    # Parking recordings): zero samples, zero chunks, but a stray stsc
    # entry pointing at chunk 0 anyway - exactly what trips ffmpeg's
    # "contradictionary STSC and STCO" check (reproduced directly
    # against the real ffprobe binary with these exact numbers - see
    # WORKING_CONTEXT.md).
    stbl = (
        _box(b"stsz", _stsz(0))
        + _box(b"stsc", _stsc([(0, 0, 1)]))
        + _box(b"stco", _stco([]))
        + _box(b"stts", _stts([(0, 0)]))
    )
    minf = _box(b"minf", _box(b"stbl", stbl))
    mdia = _box(b"hdlr", _hdlr(b"soun")) + minf
    return _box(b"trak", _box(b"mdia", mdia))


def _non_empty_broken_audio_trak() -> bytes:
    # A *different* kind of broken audio track: real samples/chunks
    # are present, but its stsc's last entry still points past stco's
    # chunk count. repair_parking_container() must leave this one
    # alone - it's outside the one specific, confirmed pattern this
    # module knows how to fix.
    stbl = (
        _box(b"stsz", _stsz(10, sample_size=50))
        + _box(b"stsc", _stsc([(1, 5, 1), (99, 5, 1)]))
        + _box(b"stco", _stco([5000, 6000]))
        + _box(b"stts", _stts([(10, 1)]))
    )
    minf = _box(b"minf", _box(b"stbl", stbl))
    mdia = _box(b"hdlr", _hdlr(b"soun")) + minf
    return _box(b"trak", _box(b"mdia", mdia))


def _mvhd() -> bytes:
    return bytes(bytearray(20))


def _build_mp4(*traks: bytes, moov_before_mdat: bool, mdat_size: int = 200) -> bytes:
    moov_payload = _box(b"mvhd", _mvhd()) + b"".join(traks)
    moov = _box(b"moov", moov_payload)
    ftyp = _box(b"ftyp", b"isom" + (0).to_bytes(4, "big") + b"isomiso2avc1mp41")
    # Real mdat bytes don't matter for any of this - repair_parking_
    # container() never reads or interprets them, only preserves their
    # position - but a distinctive, non-zero pattern makes "did the
    # bytes survive untouched" assertions meaningful rather than
    # trivially true against an all-zeros buffer.
    mdat = _box(b"mdat", bytes((i % 256 for i in range(mdat_size))))
    if moov_before_mdat:
        return ftyp + moov + mdat
    return ftyp + mdat + moov


def _ffprobe_can_open(path) -> bool:
    result = subprocess.run(
        ["ffprobe", "-v", "error", str(path)],
        capture_output=True,
    )
    return result.returncode == 0


def _mdat_slice(data: bytes) -> tuple[int, bytes]:
    """(offset, bytes) of just the 'mdat' box itself within data - not
    "from 'mdat' to end of file", since whatever comes after 'mdat'
    (here, always 'moov') can legitimately change size across a
    repair without that meaning 'mdat' itself was touched."""

    idx = data.find(b"mdat") - 4
    size = int.from_bytes(data[idx:idx + 4], "big")
    return idx, data[idx:idx + size]


def test_repair_drops_empty_audio_track_when_moov_precedes_mdat(tmp_path):
    # moov-before-mdat is the harder case: repair must pad rather than
    # shrink, since mdat's absolute file offset would otherwise move
    # and silently invalidate the video track's own stco entries.
    original = _build_mp4(
        _video_trak(647), _empty_audio_trak(), moov_before_mdat=True
    )
    source = tmp_path / "20260726_144116_PF.mp4"
    source.write_bytes(original)
    assert not _ffprobe_can_open(source)  # sanity: reproduces the real bug

    destination = tmp_path / "repaired.mp4"
    result = repair_parking_container(source, destination)

    assert result is True
    assert destination.exists()
    # Padding, not shrinking - total file size is unchanged.
    assert destination.stat().st_size == source.stat().st_size

    repaired = destination.read_bytes()
    orig_offset, orig_mdat = _mdat_slice(original)
    new_offset, new_mdat = _mdat_slice(repaired)
    assert new_offset == orig_offset
    assert new_mdat == orig_mdat

    assert _ffprobe_can_open(destination)


def test_repair_drops_empty_audio_track_when_moov_follows_mdat(tmp_path):
    # moov-after-mdat (what a finalized BlackVue recording actually
    # looks like per scripts/check_mp4_recoverability.py's own
    # docstring) is the easy case: repair can just shrink moov, since
    # nothing after it needs its absolute position preserved.
    original = _build_mp4(
        _video_trak(647), _empty_audio_trak(), moov_before_mdat=False
    )
    source = tmp_path / "20260726_144116_PF.mp4"
    source.write_bytes(original)
    assert not _ffprobe_can_open(source)

    destination = tmp_path / "repaired.mp4"
    result = repair_parking_container(source, destination)

    assert result is True
    assert destination.exists()
    assert destination.stat().st_size < source.stat().st_size

    repaired = destination.read_bytes()
    orig_offset, orig_mdat = _mdat_slice(original)
    new_offset, new_mdat = _mdat_slice(repaired)
    assert new_offset == orig_offset  # mdat comes before moov either way
    assert new_mdat == orig_mdat

    assert _ffprobe_can_open(destination)


def test_repair_reports_a_single_video_stream_after_dropping_audio(tmp_path):
    original = _build_mp4(
        _video_trak(647), _empty_audio_trak(), moov_before_mdat=True
    )
    source = tmp_path / "20260726_144116_PF.mp4"
    source.write_bytes(original)
    destination = tmp_path / "repaired.mp4"
    repair_parking_container(source, destination)

    result = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "stream=codec_type", "-of", "csv=p=0", str(destination)],
        capture_output=True,
        text=True,
    )
    streams = [line for line in result.stdout.splitlines() if line.strip()]
    assert streams == ["video"]


def test_repair_returns_false_and_writes_nothing_without_an_empty_audio_track(tmp_path):
    # No audio track at all - nothing for this narrow repair to do.
    original = _build_mp4(_video_trak(647), moov_before_mdat=True)
    source = tmp_path / "20260726_144116_NF.mp4"
    source.write_bytes(original)

    destination = tmp_path / "repaired.mp4"
    result = repair_parking_container(source, destination)

    assert result is False
    assert not destination.exists()


def test_repair_leaves_a_non_empty_broken_audio_track_alone(tmp_path):
    # A real (non-zero chunk count) but still internally broken audio
    # track is a different problem than the one this module fixes -
    # confirm it's left completely untouched rather than guessed at.
    original = _build_mp4(
        _video_trak(647), _non_empty_broken_audio_trak(), moov_before_mdat=True
    )
    source = tmp_path / "20260726_144116_PF.mp4"
    source.write_bytes(original)
    assert not _ffprobe_can_open(source)  # confirms this case is broken too

    destination = tmp_path / "repaired.mp4"
    result = repair_parking_container(source, destination)

    assert result is False
    assert not destination.exists()


def test_repair_returns_false_for_a_file_with_no_moov_box(tmp_path):
    source = tmp_path / "not_really_an_mp4.mp4"
    source.write_bytes(_box(b"ftyp", b"isom") + _box(b"mdat", b"\x00" * 16))

    destination = tmp_path / "repaired.mp4"
    result = repair_parking_container(source, destination)

    assert result is False
    assert not destination.exists()
