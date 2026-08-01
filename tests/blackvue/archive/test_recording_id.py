from blackvue.archive.recording_id import RecordingId


def test_kind_letters():
    assert RecordingId("20260715_133255_N").is_normal
    assert RecordingId("20260715_133255_E").is_event
    assert RecordingId("20260715_133255_M").is_manual
    assert RecordingId("20260715_133255_P").is_parking


def test_is_a_true_for_a_kind():
    """A - a recording kind observed on real hardware alongside
    N/E/M/P, meaning unknown (see the kind property's docstring)."""

    assert RecordingId("20260715_133255_A").is_a
    assert not RecordingId("20260715_133255_N").is_a


def test_kind_flags_are_mutually_exclusive_for_a():
    recording_id = RecordingId("20260715_133255_A")

    assert not recording_id.is_normal
    assert not recording_id.is_event
    assert not recording_id.is_manual
    assert not recording_id.is_parking
