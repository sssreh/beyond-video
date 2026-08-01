from datetime import datetime
from pathlib import PurePosixPath

from blackvue.domain.vod_entry import VodEntry


def entry(path: str) -> VodEntry:
    return VodEntry(
        timestamp=datetime(2026, 7, 15, 13, 32, 55),
        path=PurePosixPath(path),
        fields={},
    )


def test_is_front_true_for_f_suffix():
    assert entry("20260715_133255_NF.mp4").is_front


def test_is_rear_true_for_r_suffix():
    assert entry("20260715_133255_NR.mp4").is_rear


def test_is_interior_true_for_i_suffix():
    """I - interior (cabin-facing) camera, seen on some BlackVue
    models alongside front/rear. Recognition only for now."""

    assert entry("20260715_133255_NI.mp4").is_interior
    assert not entry("20260715_133255_NF.mp4").is_interior
    assert not entry("20260715_133255_NR.mp4").is_interior


def test_recording_strips_front_rear_and_interior_suffixes():
    """All three direction suffixes must resolve to the same
    recording identifier so front/rear/interior entries for one
    physical recording get grouped together, not split into separate
    (and for interior, previously unrecognized/mis-grouped) entries."""

    front = entry("20260715_133255_NF.mp4")
    rear = entry("20260715_133255_NR.mp4")
    interior = entry("20260715_133255_NI.mp4")

    assert front.recording == "20260715_133255_N"
    assert rear.recording == front.recording
    assert interior.recording == front.recording


def test_recording_leaves_non_direction_suffixes_untouched():
    """A metadata file's stem has no trailing direction letter (e.g.
    a .gps file's stem is just the recording id), so it shouldn't
    have its last character stripped - and it groups under the same
    recording id as the video entries for the same recording."""

    gps = entry("20260715_133255_N.gps")

    assert gps.recording == "20260715_133255_N"
    assert gps.recording == entry("20260715_133255_NF.mp4").recording
