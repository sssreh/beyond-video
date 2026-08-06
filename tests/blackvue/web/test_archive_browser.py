"""
Tests for bv-web's raw archive browser (web/archive_browser.py).

Builds small fake archives on disk using the real filename convention
ArchiveReader itself expects (YYYYMMDD_HHMMSS_K{F|R|I}.ext /
YYYYMMDD_HHMMSS_K.ext) rather than mocking blackvue.archive.Archive -
that reader is already tested on its own; what's under test here is
archive_browser.py's own wrapper/scan/group logic on top of it.

Copyright (C) 2026 Christer R. (sssreh)

SPDX-License-Identifier: GPL-3.0-or-later
"""

from __future__ import annotations

from datetime import date
from datetime import datetime

from blackvue.web.archive_browser import filter_recordings
from blackvue.web.archive_browser import find_recording
from blackvue.web.archive_browser import group_by_day
from blackvue.web.archive_browser import kind_options
from blackvue.web.archive_browser import scan_archive
from blackvue.lexicaltimeparser import LexicalTimeParser


def _write(folder, filename, content=b"x"):
    folder.mkdir(parents=True, exist_ok=True)
    (folder / filename).write_bytes(content)


def test_scan_archive_returns_empty_list_for_missing_directory(tmp_path):
    assert scan_archive(tmp_path / "does_not_exist", "cam") == []


def test_scan_archive_finds_a_normal_recording_with_front_and_rear(tmp_path):
    archive = tmp_path / "archive"
    _write(archive, "20260715_140212_NF.mp4")
    _write(archive, "20260715_140212_NR.mp4")

    recordings = scan_archive(archive, "kirby")

    assert len(recordings) == 1
    recording = recordings[0]
    assert recording.camera_id == "kirby"
    assert recording.id == "20260715_140212_N"
    assert recording.timestamp == datetime(2026, 7, 15, 14, 2, 12)
    assert recording.kind_label == "Normal"


def test_scan_archive_sorts_newest_first(tmp_path):
    archive = tmp_path / "archive"
    _write(archive, "20260701_000000_NF.mp4")
    _write(archive, "20260715_000000_NF.mp4")

    recordings = scan_archive(archive, "kirby")

    assert [r.id for r in recordings] == [
        "20260715_000000_N",
        "20260701_000000_N",
    ]


def test_scan_archive_maps_kind_letters_to_labels(tmp_path):
    archive = tmp_path / "archive"
    _write(archive, "20260715_100000_NF.mp4")
    _write(archive, "20260715_110000_EF.mp4")
    _write(archive, "20260715_120000_MF.mp4")
    _write(archive, "20260715_130000_PF.mp4")
    _write(archive, "20260715_140000_AF.mp4")

    recordings = scan_archive(archive, "kirby")
    labels = {r.id[-1]: r.kind_label for r in recordings}

    assert labels == {
        "N": "Normal",
        "E": "Event",
        "M": "Manual",
        "P": "Parking",
        "A": "Unknown",
    }


def test_video_directions_lists_only_directions_actually_present(tmp_path):
    archive = tmp_path / "archive"
    _write(archive, "20260715_140212_NF.mp4")
    _write(archive, "20260715_140212_NR.mp4")

    recording = scan_archive(archive, "kirby")[0]

    assert recording.videos == [
        ("Front", "20260715_140212_NF.mp4"),
        ("Rear", "20260715_140212_NR.mp4"),
    ]


def test_recording_with_no_video_has_empty_videos_list(tmp_path):
    archive = tmp_path / "archive"
    _write(archive, "20260715_140212_N.gps")

    recording = scan_archive(archive, "kirby")[0]

    assert recording.videos == []


def test_has_video_true_when_a_video_exists(tmp_path):
    archive = tmp_path / "archive"
    _write(archive, "20260715_140212_NF.mp4")

    recording = scan_archive(archive, "kirby")[0]

    assert recording.has_video is True


def test_has_video_false_with_only_a_thumbnail(tmp_path):
    # The exact case that prompted this property: a thumbnail can
    # exist without its video (they download separately) - the
    # archive-browser grid still shows the thumbnail, but overlays a
    # red cross using this flag rather than pretending the recording
    # is playable.
    archive = tmp_path / "archive"
    _write(archive, "20260715_140212_NF.thm")

    recording = scan_archive(archive, "kirby")[0]

    assert recording.has_video is False


def test_thumbnail_direction_prefers_front_then_rear_then_interior(tmp_path):
    archive = tmp_path / "archive"
    _write(archive, "20260715_140212_NR.thm")
    _write(archive, "20260715_140212_NI.thm")

    recording = scan_archive(archive, "kirby")[0]

    # Front is missing, so rear wins even though interior also exists.
    assert recording.thumbnail_direction == "rear"


def test_thumbnail_direction_is_none_without_any_thumbnail(tmp_path):
    archive = tmp_path / "archive"
    _write(archive, "20260715_140212_NF.mp4")

    recording = scan_archive(archive, "kirby")[0]

    assert recording.thumbnail_direction is None


def test_thumbnail_path_resolves_the_right_file(tmp_path):
    archive = tmp_path / "archive"
    _write(archive, "20260715_140212_NF.thm")
    _write(archive, "20260715_140212_NR.thm")

    recording = scan_archive(archive, "kirby")[0]

    assert recording.thumbnail_path("front") == archive / "20260715_140212_NF.thm"
    assert recording.thumbnail_path("rear") == archive / "20260715_140212_NR.thm"
    assert recording.thumbnail_path("interior") is None
    assert recording.thumbnail_path("bogus") is None


def test_sidecars_lists_gps_and_gsensor_when_present(tmp_path):
    archive = tmp_path / "archive"
    _write(archive, "20260715_140212_NF.mp4")
    _write(archive, "20260715_140212_N.gps")
    _write(archive, "20260715_140212_N.3gf")

    recording = scan_archive(archive, "kirby")[0]

    assert recording.sidecars == [
        ("GPS log", "20260715_140212_N.gps"),
        ("G-sensor log", "20260715_140212_N.3gf"),
    ]


def test_known_filenames_matches_what_actually_exists(tmp_path):
    archive = tmp_path / "archive"
    _write(archive, "20260715_140212_NF.mp4")
    _write(archive, "20260715_140212_N.gps")

    recording = scan_archive(archive, "kirby")[0]

    assert recording.known_filenames == frozenset(
        {"20260715_140212_NF.mp4", "20260715_140212_N.gps"}
    )
    assert "20260715_140212_NR.mp4" not in recording.known_filenames


def test_file_path_resolves_a_known_filename(tmp_path):
    archive = tmp_path / "archive"
    _write(archive, "20260715_140212_NF.mp4")

    recording = scan_archive(archive, "kirby")[0]

    assert (
        recording.file_path("20260715_140212_NF.mp4")
        == archive / "20260715_140212_NF.mp4"
    )
    assert recording.file_path("not_a_real_file.mp4") is None


def test_size_label_formats_bytes_human_readable(tmp_path):
    archive = tmp_path / "archive"
    _write(archive, "20260715_140212_NF.mp4", content=b"x" * 5 * 1024 * 1024)

    recording = scan_archive(archive, "kirby")[0]

    assert recording.size_label == "5.0M"


def test_find_recording_returns_the_matching_recording(tmp_path):
    archive = tmp_path / "archive"
    _write(archive, "20260715_140212_NF.mp4")
    _write(archive, "20260716_090000_EF.mp4")

    recording = find_recording(archive, "kirby", "20260716_090000_E")

    assert recording is not None
    assert recording.id == "20260716_090000_E"


def test_find_recording_returns_none_for_unknown_id(tmp_path):
    archive = tmp_path / "archive"
    _write(archive, "20260715_140212_NF.mp4")

    assert find_recording(archive, "kirby", "20260101_000000_N") is None


def test_find_recording_returns_none_for_missing_archive(tmp_path):
    assert find_recording(tmp_path / "does_not_exist", "kirby", "x") is None


def test_group_by_day_groups_consecutive_same_day_recordings(tmp_path):
    archive = tmp_path / "archive"
    _write(archive, "20260715_100000_NF.mp4")
    _write(archive, "20260715_150000_NF.mp4")
    _write(archive, "20260714_120000_NF.mp4")

    recordings = scan_archive(archive, "kirby")
    days = group_by_day(recordings)

    assert [day for day, _ in days] == [date(2026, 7, 15), date(2026, 7, 14)]
    assert [r.id for r in days[0][1]] == [
        "20260715_150000_N",
        "20260715_100000_N",
    ]
    assert [r.id for r in days[1][1]] == ["20260714_120000_N"]


def test_group_by_day_returns_empty_list_for_no_recordings():
    assert group_by_day([]) == []


def test_kind_options_returns_all_five_kinds_in_canonical_order():
    assert kind_options() == [
        ("N", "Normal"),
        ("E", "Event"),
        ("M", "Manual"),
        ("P", "Parking"),
        ("A", "Unknown"),
    ]


def test_filter_recordings_with_no_filters_returns_everything(tmp_path):
    archive = tmp_path / "archive"
    _write(archive, "20260715_100000_NF.mp4")
    _write(archive, "20260715_110000_EF.mp4")

    recordings = scan_archive(archive, "kirby")

    assert filter_recordings(recordings) == recordings


def test_filter_recordings_by_single_mode(tmp_path):
    archive = tmp_path / "archive"
    _write(archive, "20260715_100000_NF.mp4")
    _write(archive, "20260715_110000_EF.mp4")
    _write(archive, "20260715_120000_PF.mp4")

    recordings = scan_archive(archive, "kirby")
    filtered = filter_recordings(recordings, modes={"E"})

    assert [r.id for r in filtered] == ["20260715_110000_E"]


def test_filter_recordings_by_multiple_modes(tmp_path):
    archive = tmp_path / "archive"
    _write(archive, "20260715_100000_NF.mp4")
    _write(archive, "20260715_110000_EF.mp4")
    _write(archive, "20260715_120000_PF.mp4")

    recordings = scan_archive(archive, "kirby")
    filtered = filter_recordings(recordings, modes={"E", "P"})

    assert {r.id[-1] for r in filtered} == {"E", "P"}
    assert len(filtered) == 2


def test_filter_recordings_by_empty_mode_set_returns_nothing(tmp_path):
    # An empty *set* (as opposed to None) is a real "match no kind"
    # filter - the None-vs-empty-set distinction is what app.py's
    # route uses to turn "no checkboxes ticked" into "no mode filter"
    # (passing None), so this only matters if filter_recordings()
    # itself is called with an explicit empty set some other way.
    archive = tmp_path / "archive"
    _write(archive, "20260715_100000_NF.mp4")

    recordings = scan_archive(archive, "kirby")

    assert filter_recordings(recordings, modes=set()) == []


def test_filter_recordings_by_lexical_time_interval(tmp_path):
    archive = tmp_path / "archive"
    _write(archive, "20260701_000000_NF.mp4")
    _write(archive, "20260715_000000_NF.mp4")
    _write(archive, "20260731_000000_NF.mp4")

    recordings = scan_archive(archive, "kirby")
    interval = LexicalTimeParser(from_="20260710", until="20260720").parse()
    filtered = filter_recordings(recordings, time_interval=interval)

    assert [r.id for r in filtered] == ["20260715_000000_N"]


def test_filter_recordings_by_exact_timestamp_prefix(tmp_path):
    archive = tmp_path / "archive"
    _write(archive, "20260715_100000_NF.mp4")
    _write(archive, "20260716_100000_NF.mp4")

    recordings = scan_archive(archive, "kirby")
    interval = LexicalTimeParser(timestamp="20260715").parse()
    filtered = filter_recordings(recordings, time_interval=interval)

    assert [r.id for r in filtered] == ["20260715_100000_N"]


def test_filter_recordings_videos_only_excludes_thumbnail_only_recordings(tmp_path):
    archive = tmp_path / "archive"
    _write(archive, "20260715_100000_NF.mp4")
    _write(archive, "20260715_110000_NF.thm")  # thumbnail, no video

    recordings = scan_archive(archive, "kirby")
    filtered = filter_recordings(recordings, videos_only=True)

    assert [r.id for r in filtered] == ["20260715_100000_N"]


def test_filter_recordings_videos_only_false_keeps_everything(tmp_path):
    archive = tmp_path / "archive"
    _write(archive, "20260715_100000_NF.mp4")
    _write(archive, "20260715_110000_NF.thm")

    recordings = scan_archive(archive, "kirby")

    assert filter_recordings(recordings, videos_only=False) == recordings


def test_filter_recordings_combines_videos_only_with_mode_and_time_filters(tmp_path):
    archive = tmp_path / "archive"
    _write(archive, "20260715_100000_EF.mp4")
    _write(archive, "20260715_110000_EF.thm")  # same day/mode, no video
    _write(archive, "20260716_120000_EF.mp4")  # wrong day

    recordings = scan_archive(archive, "kirby")
    interval = LexicalTimeParser(timestamp="20260715").parse()
    filtered = filter_recordings(
        recordings, modes={"E"}, time_interval=interval, videos_only=True
    )

    assert [r.id for r in filtered] == ["20260715_100000_E"]


def test_filter_recordings_combines_mode_and_time_filters(tmp_path):
    archive = tmp_path / "archive"
    _write(archive, "20260715_100000_NF.mp4")
    _write(archive, "20260715_110000_EF.mp4")
    _write(archive, "20260716_120000_EF.mp4")

    recordings = scan_archive(archive, "kirby")
    interval = LexicalTimeParser(timestamp="20260715").parse()
    filtered = filter_recordings(recordings, modes={"E"}, time_interval=interval)

    assert [r.id for r in filtered] == ["20260715_110000_E"]
