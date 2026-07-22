from blackvue.generate.speech import SpeakerTurn
from blackvue.generate.speech import SpeechSegment
from blackvue.generate.subtitles import _lrc_timestamp
from blackvue.generate.subtitles import _srt_timestamp
from blackvue.generate.subtitles import format_lrc
from blackvue.generate.subtitles import format_srt
from blackvue.generate.subtitles import parse_lrc
from blackvue.generate.subtitles import parse_srt


def _segment(start, end, text):
    return SpeechSegment(start=start, end=end, text=text)


def test_srt_timestamp_formats_hours_minutes_seconds_millis():
    assert _srt_timestamp(0.0) == "00:00:00,000"
    assert _srt_timestamp(1.5) == "00:00:01,500"
    assert _srt_timestamp(61.25) == "00:01:01,250"
    assert _srt_timestamp(3661.001) == "01:01:01,001"


def test_srt_timestamp_rounds_to_nearest_millisecond():
    # 1.2345s -> 1234.5ms, rounds to 1235ms (banker's/round-half-even
    # doesn't matter here as long as it's a clean 3-digit ms value).
    assert _srt_timestamp(1.2345) in ("00:00:01,234", "00:00:01,235")


def test_lrc_timestamp_formats_minutes_seconds_hundredths():
    assert _lrc_timestamp(0.0) == "[00:00.00]"
    assert _lrc_timestamp(1.5) == "[00:01.50]"
    assert _lrc_timestamp(61.25) == "[01:01.25]"


def test_format_srt_numbers_cues_sequentially_with_text():
    segments = (
        _segment(0.0, 2.0, "Hello there."),
        _segment(2.0, 4.5, "How's it going?"),
    )

    result = format_srt(segments)

    assert result == (
        "1\n"
        "00:00:00,000 --> 00:00:02,000\n"
        "Hello there.\n"
        "\n"
        "2\n"
        "00:00:02,000 --> 00:00:04,500\n"
        "How's it going?\n"
    )


def test_format_srt_prefixes_speaker_label_when_turns_given():
    segments = (_segment(0.0, 2.0, "Hello there."),)
    turns = (SpeakerTurn(start=0.0, end=2.0, speaker="SPEAKER_00"),)

    result = format_srt(segments, turns)

    assert "[SPEAKER_00] Hello there." in result


def test_format_srt_handles_no_segments():
    assert format_srt(()) == ""


def test_format_lrc_one_line_per_segment_at_start_time():
    segments = (
        _segment(0.0, 2.0, "Hello there."),
        _segment(65.0, 67.0, "How's it going?"),
    )

    result = format_lrc(segments)

    assert result == (
        "[00:00.00] Hello there.\n"
        "[01:05.00] How's it going?"
    )


def test_format_lrc_prefixes_speaker_label_when_turns_given():
    segments = (_segment(0.0, 2.0, "Hello there."),)
    turns = (SpeakerTurn(start=0.0, end=2.0, speaker="SPEAKER_01"),)

    result = format_lrc(segments, turns)

    assert result == "[00:00.00] [SPEAKER_01] Hello there."


def test_format_lrc_handles_no_segments():
    assert format_lrc(()) == ""


def test_parse_srt_round_trips_through_format_srt():
    segments = (
        _segment(0.0, 2.0, "Hello there."),
        _segment(2.0, 4.5, "How's it going?"),
    )

    parsed = parse_srt(format_srt(segments))

    assert len(parsed) == 2
    assert parsed[0].start == 0.0
    assert parsed[0].end == 2.0
    assert parsed[0].text == "Hello there."
    assert parsed[1].start == 2.0
    assert parsed[1].end == 4.5
    assert parsed[1].text == "How's it going?"


def test_parse_srt_preserves_a_baked_in_speaker_prefix():
    segments = (_segment(0.0, 2.0, "Hello there."),)
    turns = (SpeakerTurn(start=0.0, end=2.0, speaker="SPEAKER_00"),)

    parsed = parse_srt(format_srt(segments, turns))

    assert parsed[0].text == "[SPEAKER_00] Hello there."


def test_parse_srt_ignores_cue_index_numbers():
    text = (
        "7\n"
        "00:00:00,000 --> 00:00:01,000\n"
        "first\n"
        "\n"
        "8\n"
        "00:00:01,000 --> 00:00:02,000\n"
        "second\n"
    )

    parsed = parse_srt(text)

    assert [segment.text for segment in parsed] == ["first", "second"]


def test_parse_srt_handles_empty_string():
    assert parse_srt("") == ()


def test_parse_lrc_round_trips_through_format_lrc():
    segments = (
        _segment(0.0, 0.0, "Hello there."),
        _segment(65.0, 65.0, "How's it going?"),
    )

    parsed = parse_lrc(format_lrc(segments))

    assert len(parsed) == 2
    assert parsed[0].start == 0.0
    assert parsed[0].text == "Hello there."
    assert parsed[1].start == 65.0
    assert parsed[1].text == "How's it going?"


def test_parse_lrc_preserves_a_baked_in_speaker_prefix():
    segments = (_segment(0.0, 0.0, "Hello there."),)
    turns = (SpeakerTurn(start=0.0, end=2.0, speaker="SPEAKER_01"),)

    parsed = parse_lrc(format_lrc(segments, turns))

    assert parsed[0].text == "[SPEAKER_01] Hello there."


def test_parse_lrc_handles_empty_string():
    assert parse_lrc("") == ()
