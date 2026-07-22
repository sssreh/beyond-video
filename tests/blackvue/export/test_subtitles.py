from blackvue.archive.asset import Asset
from blackvue.archive.asset_file import AssetFile
from blackvue.archive.recording import Recording
from blackvue.archive.recording_id import RecordingId
from blackvue.export.subtitles import merge_lrc
from blackvue.export.subtitles import merge_srt
from blackvue.generate.speech import SpeechSegment
from blackvue.generate.subtitles import format_lrc
from blackvue.generate.subtitles import format_srt
from blackvue.trip.trip import Trip


def _segment(start, end, text):
    return SpeechSegment(start=start, end=end, text=text)


def test_merge_srt_rebases_each_recordings_timestamps_onto_the_trip(
    tmp_path,
):
    srt_a = tmp_path / "a.srt"
    srt_a.write_text(
        format_srt((_segment(0.0, 2.0, "first recording, first line"),))
    )
    srt_b = tmp_path / "b.srt"
    srt_b.write_text(
        format_srt((_segment(0.0, 1.0, "second recording, first line"),))
    )

    # Second recording starts 60s after the first.
    first = Recording(
        id=RecordingId("20260720_100000_N"),
        assets={Asset.SUBTITLES: AssetFile(Asset.SUBTITLES, srt_a)},
    )
    second = Recording(
        id=RecordingId("20260720_100100_N"),
        assets={Asset.SUBTITLES: AssetFile(Asset.SUBTITLES, srt_b)},
    )
    trip = Trip((first, second))

    result = merge_srt(trip)

    assert "00:00:00,000 --> 00:00:02,000" in result
    assert "first recording, first line" in result
    assert "00:01:00,000 --> 00:01:01,000" in result
    assert "second recording, first line" in result
    # Cues renumbered sequentially across the whole trip, not per
    # source file.
    assert result.startswith("1\n")
    assert "\n2\n" in result


def test_merge_srt_sorts_cues_by_start_time_across_recordings(tmp_path):
    # Even if a later recording's file happens to be processed after
    # an earlier one, cues should come out in chronological order.
    srt_a = tmp_path / "a.srt"
    srt_a.write_text(
        format_srt((_segment(5.0, 6.0, "from the later recording"),))
    )
    srt_b = tmp_path / "b.srt"
    srt_b.write_text(
        format_srt((_segment(0.0, 1.0, "from the earlier recording"),))
    )

    later = Recording(
        id=RecordingId("20260720_100100_N"),
        assets={Asset.SUBTITLES: AssetFile(Asset.SUBTITLES, srt_a)},
    )
    earlier = Recording(
        id=RecordingId("20260720_100000_N"),
        assets={Asset.SUBTITLES: AssetFile(Asset.SUBTITLES, srt_b)},
    )
    trip = Trip((earlier, later))

    result = merge_srt(trip)
    earlier_pos = result.index("from the earlier recording")
    later_pos = result.index("from the later recording")

    assert earlier_pos < later_pos


def test_merge_srt_returns_none_when_no_recording_has_subtitles():
    trip = Trip((Recording(id=RecordingId("20260720_100000_N")),))

    assert merge_srt(trip) is None


def test_merge_lrc_rebases_and_merges(tmp_path):
    lrc_a = tmp_path / "a.lrc"
    lrc_a.write_text(format_lrc((_segment(0.0, 0.0, "first"),)))
    lrc_b = tmp_path / "b.lrc"
    lrc_b.write_text(format_lrc((_segment(0.0, 0.0, "second"),)))

    first = Recording(
        id=RecordingId("20260720_100000_N"),
        assets={Asset.LYRICS: AssetFile(Asset.LYRICS, lrc_a)},
    )
    second = Recording(
        id=RecordingId("20260720_100030_N"),
        assets={Asset.LYRICS: AssetFile(Asset.LYRICS, lrc_b)},
    )
    trip = Trip((first, second))

    result = merge_lrc(trip)

    assert result == "[00:00.00] first\n[00:30.00] second"


def test_merge_lrc_returns_none_when_no_recording_has_lyrics():
    trip = Trip((Recording(id=RecordingId("20260720_100000_N")),))

    assert merge_lrc(trip) is None
