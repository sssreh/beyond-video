from blackvue.archive.archive_reader import ArchiveReader
from blackvue.archive.asset import Asset


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


def test_archive_reader_detects_srt_and_lrc(tmp_path):
    (tmp_path / "20260715_133255_N.srt").write_text(
        "1\n00:00:00,000 --> 00:00:01,000\nhello\n"
    )
    (tmp_path / "20260715_133255_N.lrc").write_text("[00:00.00] hello")

    recordings = ArchiveReader(tmp_path).read()

    recording = recordings[0]

    assert recording.has(Asset.SUBTITLES)
    assert recording.has(Asset.LYRICS)
