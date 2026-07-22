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


def _trip_with_one_srt(tmp_path, segment):
    srt_path = tmp_path / "a.srt"
    srt_path.write_text(format_srt((segment,)))
    recording = Recording(
        id=RecordingId("20260720_100000_N"),
        assets={Asset.SUBTITLES: AssetFile(Asset.SUBTITLES, srt_path)},
    )
    return Trip((recording,))


def _trip_with_one_lrc(tmp_path, segment):
    lrc_path = tmp_path / "a.lrc"
    lrc_path.write_text(format_lrc((segment,)))
    recording = Recording(
        id=RecordingId("20260720_100000_N"),
        assets={Asset.LYRICS: AssetFile(Asset.LYRICS, lrc_path)},
    )
    return Trip((recording,))


def test_merge_srt_pads_an_empty_trailing_cue_to_match_video_length(
    tmp_path,
):
    trip = _trip_with_one_srt(tmp_path, _segment(0.0, 2.0, "hello"))

    result = merge_srt(trip, total_duration_seconds=120.0)

    assert "00:00:00,000 --> 00:00:02,000" in result
    assert "hello" in result
    # Padding cue ends exactly at the video's length, starting within
    # the final second (not at 2.0s, where the real content ended).
    assert "00:01:59,000 --> 00:02:00,000" in result
    assert result.startswith("1\n")
    assert "\n2\n" in result  # a second, numbered cue was appended


def test_merge_srt_does_not_pad_when_content_already_reaches_the_end(
    tmp_path,
):
    trip = _trip_with_one_srt(tmp_path, _segment(0.0, 120.0, "hello"))

    result = merge_srt(trip, total_duration_seconds=120.0)

    # No extra cue appended - real content already covers the length.
    assert result == format_srt((_segment(0.0, 120.0, "hello"),))


def test_merge_srt_ignores_padding_when_no_duration_given(tmp_path):
    trip = _trip_with_one_srt(tmp_path, _segment(0.0, 2.0, "hello"))

    result = merge_srt(trip)

    assert result == format_srt((_segment(0.0, 2.0, "hello"),))


def test_merge_lrc_pads_an_empty_trailing_line_near_the_end(tmp_path):
    trip = _trip_with_one_lrc(tmp_path, _segment(0.0, 0.0, "hello"))

    result = merge_lrc(trip, total_duration_seconds=90.0)

    # Padding line's timestamp lands within the final second before
    # 90s (89.0s = [01:29.00]), with empty text - trailing space comes
    # from format_lrc's f"{ts} {text}" always inserting the separator.
    assert result == "[00:00.00] hello\n[01:29.00] "


def test_merge_lrc_does_not_pad_when_no_duration_given(tmp_path):
    trip = _trip_with_one_lrc(tmp_path, _segment(0.0, 0.0, "hello"))

    result = merge_lrc(trip)

    assert result == "[00:00.00] hello"
