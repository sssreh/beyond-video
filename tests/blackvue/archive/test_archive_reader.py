from blackvue.archive.archive_reader import ArchiveReader
from blackvue.archive.asset import Asset
from blackvue.archive.recording_id import RecordingId


def test_archive_reader_detects_generated_assets(tmp_path):
    (tmp_path / "20260715_133255_NF.mp4").write_bytes(b"x")
    (tmp_path / "20260715_133255_N.aac").write_bytes(b"x")
    (tmp_path / "20260715_133255_N.duration.txt").write_text("300")
    (tmp_path / "20260715_133255_N.transcript.txt").write_text("hello")
    (tmp_path / "20260715_133255_N.translation.txt").write_text("hola")

    recordings = ArchiveReader(tmp_path).read()

    assert len(recordings) == 1

    recording = recordings[0]

    assert recording.has(Asset.FRONT)
    assert recording.has(Asset.AUDIO)
    assert recording.has(Asset.DURATION)
    assert recording.has(Asset.TRANSCRIPT)
    assert recording.has(Asset.TRANSLATION)


def test_archive_reader_transcript_and_translation_do_not_collide(tmp_path):
    (tmp_path / "20260715_133255_N.transcript.txt").write_text("hello")

    recordings = ArchiveReader(tmp_path).read()

    recording = recordings[0]

    assert recording.has(Asset.TRANSCRIPT)
    assert not recording.has(Asset.TRANSLATION)


def test_archive_reader_detects_language_suffixed_generated_files(tmp_path):
    (tmp_path / "20260715_133255_N_swe.transcript.txt").write_text("hej")
    (tmp_path / "20260715_133255_N_tha.translation.txt").write_text("x")

    recordings = ArchiveReader(tmp_path).read()

    recording = recordings[0]

    assert recording.id.value == "20260715_133255_N"
    assert recording.has(Asset.TRANSCRIPT)
    assert recording.has(Asset.TRANSLATION)


def test_archive_reader_tracks_diarized_transcript_separately(tmp_path):
    (tmp_path / "20260715_133255_N.transcript.txt").write_text("plain")
    (tmp_path / "20260715_133255_N.diarized.transcript.txt").write_text(
        "[SPEAKER_00] plain"
    )

    recordings = ArchiveReader(tmp_path).read()

    recording = recordings[0]

    assert recording.has(Asset.TRANSCRIPT)
    assert recording.has(Asset.TRANSCRIPT_DIARIZED)
    assert recording.file(Asset.TRANSCRIPT).path.read_text() == "plain"
    assert recording.file(
        Asset.TRANSCRIPT_DIARIZED
    ).path.read_text() == "[SPEAKER_00] plain"


def test_archive_reader_diarized_only_does_not_count_as_plain_transcript(
    tmp_path,
):
    (tmp_path / "20260715_133255_N.diarized.transcript.txt").write_text(
        "[SPEAKER_00] hi"
    )

    recordings = ArchiveReader(tmp_path).read()

    recording = recordings[0]

    assert not recording.has(Asset.TRANSCRIPT)
    assert recording.has(Asset.TRANSCRIPT_DIARIZED)


def test_archive_reader_diarized_translation_tracked_separately(tmp_path):
    (tmp_path / "20260715_133255_N_swe.translation.txt").write_text("hej")
    (
        tmp_path / "20260715_133255_N_swe.diarized.translation.txt"
    ).write_text("[SPEAKER_00] hej")

    recordings = ArchiveReader(tmp_path).read()

    recording = recordings[0]

    assert recording.has(Asset.TRANSLATION)
    assert recording.has(Asset.TRANSLATION_DIARIZED)


def test_archive_reader_detects_interior_camera_video_and_thumbnail(tmp_path):
    (tmp_path / "20260715_133255_NF.mp4").write_bytes(b"x")
    (tmp_path / "20260715_133255_NI.mp4").write_bytes(b"x")
    (tmp_path / "20260715_133255_NI.thm").write_bytes(b"x")

    recordings = ArchiveReader(tmp_path).read()

    assert len(recordings) == 1

    recording = recordings[0]

    assert recording.has(Asset.FRONT)
    assert recording.has(Asset.INTERIOR)
    assert recording.has(Asset.INTERIOR_THUMBNAIL)


def test_archive_reader_detects_srt(tmp_path):
    (tmp_path / "20260715_133255_N.srt").write_text(
        "1\n00:00:00,000 --> 00:00:01,000\nhello\n"
    )

    recordings = ArchiveReader(tmp_path).read()

    recording = recordings[0]

    assert recording.has(Asset.SUBTITLES)


# ---------------------------------------------------------------------------
# read_recording() - targeted single-recording lookup, added for bv-web's
# archive browser (web/archive_browser.py's find_recording()), which needs
# to resolve one recording per thumbnail image request and per video-player
# range request without paying read()'s full-archive scan cost every time.
# ---------------------------------------------------------------------------


def test_read_recording_finds_a_matching_recording(tmp_path):
    (tmp_path / "20260715_140212_NF.mp4").write_bytes(b"x")
    (tmp_path / "20260715_140212_NR.mp4").write_bytes(b"x")

    recording_id = RecordingId.parse("20260715_140212_N")
    recording = ArchiveReader(tmp_path).read_recording(recording_id)

    assert recording is not None
    assert recording.id == recording_id
    assert recording.has(Asset.FRONT)
    assert recording.has(Asset.REAR)


def test_read_recording_returns_none_for_unknown_id(tmp_path):
    (tmp_path / "20260715_140212_NF.mp4").write_bytes(b"x")

    recording_id = RecordingId.parse("20260101_000000_N")
    assert ArchiveReader(tmp_path).read_recording(recording_id) is None


def test_read_recording_returns_none_for_missing_directory(tmp_path):
    recording_id = RecordingId.parse("20260715_140212_N")
    reader = ArchiveReader(tmp_path / "does_not_exist")

    assert reader.read_recording(recording_id) is None


def test_read_recording_ignores_other_recordings(tmp_path):
    (tmp_path / "20260715_140212_NF.mp4").write_bytes(b"x")
    (tmp_path / "20260716_090000_EF.mp4").write_bytes(b"x")
    (tmp_path / "20260716_090000_ER.mp4").write_bytes(b"x")

    recording_id = RecordingId.parse("20260715_140212_N")
    recording = ArchiveReader(tmp_path).read_recording(recording_id)

    assert recording is not None
    assert len(recording.assets) == 1
    assert recording.has(Asset.FRONT)


def test_read_recording_matches_read_for_the_same_recording(tmp_path):
    (tmp_path / "20260715_140212_NF.mp4").write_bytes(b"x")
    (tmp_path / "20260715_140212_NR.mp4").write_bytes(b"x")
    (tmp_path / "20260715_140212_N.gps").write_bytes(b"x")
    (tmp_path / "20260716_090000_EF.mp4").write_bytes(b"x")

    reader = ArchiveReader(tmp_path)
    all_recordings = {r.id: r for r in reader.read()}

    target_id = RecordingId.parse("20260715_140212_N")
    targeted = reader.read_recording(target_id)

    full = all_recordings[target_id]
    assert targeted is not None
    assert targeted.size == full.size
    assert set(targeted.assets) == set(full.assets)
    assert {f.name for f in targeted.assets.values()} == {
        f.name for f in full.assets.values()
    }


def test_read_recording_computes_correct_total_size(tmp_path):
    (tmp_path / "20260715_140212_NF.mp4").write_bytes(b"x" * 100)
    (tmp_path / "20260715_140212_NR.mp4").write_bytes(b"x" * 50)
    (tmp_path / "20260716_090000_EF.mp4").write_bytes(b"x" * 999)

    recording_id = RecordingId.parse("20260715_140212_N")
    recording = ArchiveReader(tmp_path).read_recording(recording_id)

    assert recording is not None
    assert recording.size == 150


def test_read_recording_never_lists_the_directory(tmp_path, monkeypatch):
    # The whole point of read_recording() (see its own docstring on
    # the glob()-based version this replaced, which read as targeted
    # but still listed every entry in the directory under the hood):
    # probing the known exact filenames must never require the OS to
    # enumerate the directory, since that's exactly the O(archive
    # size) cost this method exists to avoid. Monkeypatch every
    # directory-listing primitive to blow up if called at all - a
    # passing test proves read_recording() really doesn't take that
    # path, not just that its return value happens to look right.
    import os
    from pathlib import Path as PathClass

    (tmp_path / "20260715_140212_NF.mp4").write_bytes(b"x")

    def _boom(*args, **kwargs):
        raise AssertionError("read_recording() must not list the directory")

    monkeypatch.setattr(os, "scandir", _boom)
    monkeypatch.setattr(PathClass, "glob", _boom)
    monkeypatch.setattr(PathClass, "iterdir", _boom)

    recording_id = RecordingId.parse("20260715_140212_N")
    recording = ArchiveReader(tmp_path).read_recording(recording_id)

    assert recording is not None
    assert recording.has(Asset.FRONT)
