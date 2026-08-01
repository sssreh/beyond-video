from blackvue.archive import Archive
from blackvue.archive import Recording
from blackvue.archive import RecordingId
from blackvue.archive.configuration import write_record_time_snapshot


def _touch(path):
    path.write_bytes(b"x")


def test_archive_reads_no_configurations_when_none_exist(tmp_path):
    _touch(tmp_path / "20260101_000000_NF.mp4")

    archive = Archive(tmp_path)

    assert archive.configurations == []


def test_archive_reads_configuration_snapshots_sorted_by_recording(tmp_path):
    _touch(tmp_path / "20260101_000000_NF.mp4")

    # written out of order on purpose - _read_configurations() must
    # sort by recording_id itself, not rely on directory listing order.
    write_record_time_snapshot(tmp_path, "20260801_000000_N", 60)
    write_record_time_snapshot(tmp_path, "20260101_000000_N", 180)

    archive = Archive(tmp_path)

    assert [c.recording_id.value for c in archive.configurations] == [
        "20260101_000000_N",
        "20260801_000000_N",
    ]
    assert [c.record_time for c in archive.configurations] == [180, 60]


def test_archive_ignores_a_corrupt_configuration_snapshot(tmp_path):
    _touch(tmp_path / "20260101_000000_NF.mp4")

    write_record_time_snapshot(tmp_path, "20260101_000000_N", 180)
    (tmp_path / "20260601_000000_N.record_time.txt").write_text(
        "not-a-number\n", encoding="utf-8"
    )

    archive = Archive(tmp_path)

    assert [c.recording_id.value for c in archive.configurations] == [
        "20260101_000000_N",
    ]


def test_archive_reader_does_not_attach_record_time_files_to_recordings(tmp_path):
    """.record_time.txt is a camera-wide config snapshot, not a
    per-recording asset - ArchiveReader must never attach it to a
    Recording's own assets."""

    _touch(tmp_path / "20260101_000000_NF.mp4")
    write_record_time_snapshot(tmp_path, "20260101_000000_N", 180)

    archive = Archive(tmp_path)

    assert len(archive.recordings) == 1
    assert len(archive.recordings[0].assets) == 1


def test_configuration_lookup_uses_most_recent_snapshot_at_or_before(tmp_path):
    write_record_time_snapshot(tmp_path, "20260101_000000_N", 180)
    write_record_time_snapshot(tmp_path, "20260801_000000_N", 60)

    archive = Archive(tmp_path)

    before_any = Recording(RecordingId("20251201_000000_N"))
    between_eras = Recording(RecordingId("20260315_120000_N"))
    after_change = Recording(RecordingId("20260901_120000_N"))

    assert archive.configuration(between_eras).record_time == 180
    assert archive.configuration(after_change).record_time == 60
    # No snapshot applies yet - falls back rather than raising.
    assert archive.configuration(before_any).record_time == 300


def test_configuration_lookup_falls_back_when_archive_has_no_snapshots(
    tmp_path, capsys
):
    _touch(tmp_path / "20260101_000000_NF.mp4")
    archive = Archive(tmp_path)

    configuration = archive.configuration(archive.recordings[0])

    assert configuration.record_time == 300
    assert "fallback" in capsys.readouterr().out.lower()
