from datetime import datetime
from pathlib import PurePosixPath

from blackvue.domain.recording import Recording
from blackvue.domain.vod_entry import VodEntry


def entry(path: str) -> VodEntry:
    return VodEntry(
        timestamp=datetime(2026, 7, 15, 13, 32, 55),
        path=PurePosixPath(path),
        fields={},
    )


def test_interior_returns_the_interior_entry():
    recording = Recording(
        id="20260715_133255_N",
        entries=[
            entry("20260715_133255_NF.mp4"),
            entry("20260715_133255_NR.mp4"),
            entry("20260715_133255_NI.mp4"),
        ],
    )

    assert recording.interior is not None
    assert recording.interior.is_interior


def test_interior_returns_none_when_absent():
    recording = Recording(
        id="20260715_133255_N",
        entries=[entry("20260715_133255_NF.mp4")],
    )

    assert recording.interior is None


def test_is_a_true_for_a_kind():
    """A - a recording kind observed alongside N/E/M/P, meaning
    unknown."""

    assert Recording(id="20260715_133255_A", entries=[]).is_a
    assert not Recording(id="20260715_133255_N", entries=[]).is_a


def test_kind_is_empty_string_for_an_id_with_no_underscore():
    # A generic (adapter-driven, e.g. GoPro) --media-imported recording's
    # id is just the file's own stem - GH010123.MP4 -> "GH010123", no
    # BlackVue-style _K suffix at all. Must not raise (an unguarded
    # id.rsplit("_", 1)[1] would IndexError here) - see
    # core/media_camera.py's own manifest-driven scan path.
    recording = Recording(id="GH010123", entries=[])

    assert recording.kind == ""


def test_kind_less_recording_matches_no_known_kind():
    recording = Recording(id="GH010123", entries=[])

    assert not recording.is_normal
    assert not recording.is_event
    assert not recording.is_manual
    assert not recording.is_parking
    assert not recording.is_a
