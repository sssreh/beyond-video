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

from blackvue.web.archive_browser import ArchiveRecordingCache
from blackvue.web.archive_browser import filter_recordings
from blackvue.web.archive_browser import find_recording
from blackvue.web.archive_browser import first_valid_gps_fix
from blackvue.web.archive_browser import last_valid_gps_fix
from blackvue.web.archive_browser import group_by_day
from blackvue.web.archive_browser import kind_options
from blackvue.web.archive_browser import scan_archive
from blackvue.lexicaltimeparser import LexicalTimeParser


def _write(folder, filename, content=b"x"):
    folder.mkdir(parents=True, exist_ok=True)
    (folder / filename).write_bytes(content)


def test_scan_archive_returns_empty_list_for_missing_directory(tmp_path):
    assert scan_archive(tmp_path / "does_not_exist", "cam") == []


# ---------------------------------------------------------------------------
# adapter_id - scan_archive()/find_recording() routed through a camera's
# own CameraAdapter (docs/CAMERA_ADAPTERS.md) instead of always assuming
# BlackVue's flat layout. Confirms the "folder" adapter (a recursive
# folder of ordinary videos, e.g. a GoPro test archive) works through
# the exact same browsing functions bv-web's routes call.
# ---------------------------------------------------------------------------


def test_scan_archive_with_folder_adapter_finds_a_recursive_video(tmp_path):
    import os

    archive = tmp_path / "archive"
    clips = archive / "clips"
    clips.mkdir(parents=True)
    video = clips / "vacation.mp4"
    video.write_bytes(b"x" * 40)
    os.utime(video, (1700000000, 1700000000))

    recordings = scan_archive(archive, "gp", adapter_id="folder")

    assert len(recordings) == 1
    assert recordings[0].id.endswith("_V")
    assert recordings[0].camera_id == "gp"


def test_scan_archive_default_adapter_does_not_see_folder_shaped_files(
    tmp_path,
):
    archive = tmp_path / "archive"
    clips = archive / "clips"
    clips.mkdir(parents=True)
    (clips / "vacation.mp4").write_bytes(b"x")

    # Default "blackvue" adapter's flat scan never descends into
    # subfolders and requires the BlackVue filename convention, so a
    # recursive folder of arbitrarily-named videos yields nothing.
    recordings = scan_archive(archive, "gp")

    assert recordings == []


def test_find_recording_with_folder_adapter_resolves_the_scanned_id(
    tmp_path,
):
    import os

    archive = tmp_path / "archive"
    clips = archive / "clips"
    clips.mkdir(parents=True)
    video = clips / "vacation.mp4"
    video.write_bytes(b"x" * 40)
    os.utime(video, (1700000000, 1700000000))

    recordings = scan_archive(archive, "gp", adapter_id="folder")
    target_id = recordings[0].id

    found = find_recording(archive, "gp", target_id, adapter_id="folder")

    assert found is not None
    assert found.id == target_id


def test_find_recording_with_folder_adapter_returns_none_for_unknown_id(
    tmp_path,
):
    archive = tmp_path / "archive"
    _write(archive, "clip.mp4")

    found = find_recording(
        archive, "gp", "99991231_235959_V", adapter_id="folder"
    )

    assert found is None


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


def test_gps_path_resolves_when_present(tmp_path):
    archive = tmp_path / "archive"
    _write(archive, "20260715_140212_NF.mp4")
    _write(archive, "20260715_140212_N.gps")

    recording = scan_archive(archive, "kirby")[0]

    assert recording.gps_path == archive / "20260715_140212_N.gps"


def test_gps_path_is_none_without_a_gps_file(tmp_path):
    archive = tmp_path / "archive"
    _write(archive, "20260715_140212_NF.mp4")

    recording = scan_archive(archive, "kirby")[0]

    assert recording.gps_path is None


# ---------------------------------------------------------------------------
# scene_texts - added for the archive detail page's scene/OCR text panel
# (task #681). Mirrors blackvue/search.py's own TEXT_SEARCH_ASSETS["scene"]
# grouping: the two Asset types bv-generate --describe-scene / bv-scribe
# write, front then rear, skipping whichever isn't present.
# ---------------------------------------------------------------------------


def test_scene_texts_empty_when_neither_file_exists(tmp_path):
    archive = tmp_path / "archive"
    _write(archive, "20260715_140212_NF.mp4")

    recording = scan_archive(archive, "kirby")[0]

    assert recording.scene_texts == []


def test_scene_texts_includes_front_only(tmp_path):
    archive = tmp_path / "archive"
    _write(archive, "20260715_140212_NF.mp4")
    _write(archive, "20260715_140212_N.scene.txt", content=b"A quiet street.")

    recording = scan_archive(archive, "kirby")[0]

    assert recording.scene_texts == [("Front", "A quiet street.")]


def test_scene_texts_includes_front_and_rear_in_order(tmp_path):
    archive = tmp_path / "archive"
    _write(archive, "20260715_140212_NF.mp4")
    _write(archive, "20260715_140212_N.scene.txt", content=b"Front view text.")
    _write(archive, "20260715_140212_N.rear.scene.txt", content=b"Rear view text.")

    recording = scan_archive(archive, "kirby")[0]

    assert recording.scene_texts == [
        ("Front", "Front view text."),
        ("Rear", "Rear view text."),
    ]


def test_scene_texts_falls_back_to_placeholder_on_read_error(tmp_path, monkeypatch):
    archive = tmp_path / "archive"
    _write(archive, "20260715_140212_NF.mp4")
    _write(archive, "20260715_140212_N.scene.txt", content=b"Front view text.")

    recording = scan_archive(archive, "kirby")[0]

    from pathlib import Path

    real_read_text = Path.read_text

    def _boom(self, *args, **kwargs):
        if self.name.endswith(".scene.txt"):
            raise OSError("permission denied")
        return real_read_text(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", _boom)

    [(label, text)] = recording.scene_texts
    assert label == "Front"
    assert "could not read" in text
    assert "20260715_140212_N.scene.txt" in text


# ---------------------------------------------------------------------------
# scene_summary - a cleaner "description + legible sign reads only"
# view derived live from the same files scene_texts reads (no new file,
# no model call). Christer, after seeing how much of a real scene.txt
# is "not legible" noise: "maybe i just want a report on the scene
# files for human reading" -> "like a trip-summary but per recording,
# could be shown when you look at a video... only freshly generated
# and not a new file" (see WORKING_CONTEXT.md).
# ---------------------------------------------------------------------------

_COMBINED_SCENE_TEXT = (
    "## Description\n"
    "A quiet residential street, clear weather, light traffic.\n\n"
    "## On-screen text\n"
    "Speed 42 km/h, timestamp overlay visible.\n\n"
    "## Zoomed sign reads\n"
    "- [t=0.0s] road sign: not legible\n"
    "- [t=0.0s] shop/storefront sign: SOLNA♥DENTAL\n"
    "- [t=59.8s] vehicle license plate: not legible\n\n"
    "---\n"
    "Note: the reads above ... Treat every read here as unverified "
    "until checked against the source video."
)

# What bv-scribe/bv-generate's --camera both rear pass actually writes
# (task forced to "ocr" - see WORKING_CONTEXT.md's "cleaner description
# + legible signs only" note and the earlier "What type is this?"
# exchange): no "## Description" section at all.
_OCR_ONLY_SCENE_TEXT = (
    "SOLNA DENTAL\nMALL OF SCANDINAVIA\n\n"
    "## Zoomed sign reads\n"
    "- [t=0.0s] road sign: not legible\n"
    "- [t=119.5s] shop/storefront sign: MALL OF SCANDINAVIA\n\n"
    "---\n"
    "Note: unverified disclaimer text."
)

_ALL_NOT_LEGIBLE_SCENE_TEXT = (
    "## Zoomed sign reads\n"
    "- [t=0.0s] road sign: not legible\n"
    "- [t=59.8s] vehicle license plate: not legible\n\n"
    "---\n"
    "Note: unverified disclaimer text."
)


def test_scene_summary_empty_when_neither_file_exists(tmp_path):
    archive = tmp_path / "archive"
    _write(archive, "20260715_140212_NF.mp4")

    recording = scan_archive(archive, "kirby")[0]

    assert recording.scene_summary == []


def test_scene_summary_extracts_description_and_drops_not_legible_reads(tmp_path):
    archive = tmp_path / "archive"
    _write(archive, "20260715_140212_NF.mp4")
    _write(
        archive, "20260715_140212_N.scene.txt",
        content=_COMBINED_SCENE_TEXT.encode("utf-8"),
    )

    recording = scan_archive(archive, "kirby")[0]

    assert recording.scene_summary == [
        (
            "Front",
            "A quiet residential street, clear weather, light traffic.",
            ["[t=0.0s] shop/storefront sign: SOLNA♥DENTAL"],
        )
    ]


def test_scene_summary_front_and_rear_in_order(tmp_path):
    archive = tmp_path / "archive"
    _write(archive, "20260715_140212_NF.mp4")
    _write(
        archive, "20260715_140212_N.scene.txt",
        content=_COMBINED_SCENE_TEXT.encode("utf-8"),
    )
    _write(
        archive, "20260715_140212_N.rear.scene.txt",
        content=_OCR_ONLY_SCENE_TEXT.encode("utf-8"),
    )

    recording = scan_archive(archive, "kirby")[0]

    labels = [label for label, _description, _reads in recording.scene_summary]
    assert labels == ["Front", "Rear"]


def test_scene_summary_ocr_only_pass_has_no_description_but_keeps_legible_reads(tmp_path):
    archive = tmp_path / "archive"
    _write(archive, "20260715_140212_NF.mp4")
    _write(
        archive, "20260715_140212_N.rear.scene.txt",
        content=_OCR_ONLY_SCENE_TEXT.encode("utf-8"),
    )

    recording = scan_archive(archive, "kirby")[0]

    [(label, description, legible_reads)] = recording.scene_summary
    assert label == "Rear"
    assert description == ""
    assert legible_reads == ["[t=119.5s] shop/storefront sign: MALL OF SCANDINAVIA"]


def test_scene_summary_keeps_a_multi_line_sign_read_intact(tmp_path):
    # Christer, from a real scene.txt: a sign whose OCR read itself
    # spans several lines (a stacked destination board) had everything
    # after the first line silently dropped - "but i only got" a
    # summary missing "259 HUDDINGE" / "JORDBRO" / "500" (see
    # WORKING_CONTEXT.md). The continuation lines below a "- [t=...]"
    # bullet must be folded into that same read, not discarded.
    archive = tmp_path / "archive"
    _write(archive, "20260715_140212_NF.mp4")
    _write(
        archive, "20260715_140212_N.scene.txt",
        content=(
            "## On-Screen Text\n"
            "327 BERGEN\n359 JORDBRØ\n600\n\n"
            "## Zoomed sign reads\n"
            "- [t=0.0s] vehicle license plate: not legible\n"
            "- [t=40.6s] blue road sign with white text: 227 DALARÖ\n"
            "259 HUDDINGE\nJORDBRÖ\n500\n"
            "- [t=40.6s] green road sign with white text: not legible\n"
            "- [t=40.6s] vehicle license plate: not legible\n"
        ).encode("utf-8"),
    )

    recording = scan_archive(archive, "kirby")[0]

    [(label, description, legible_reads)] = recording.scene_summary
    assert label == "Front"
    assert description == ""
    assert legible_reads == [
        "[t=40.6s] blue road sign with white text: 227 DALARÖ "
        "259 HUDDINGE JORDBRÖ 500"
    ]


def test_scene_summary_skips_direction_with_nothing_legible_and_no_description(tmp_path):
    archive = tmp_path / "archive"
    _write(archive, "20260715_140212_NF.mp4")
    _write(
        archive, "20260715_140212_N.rear.scene.txt",
        content=_ALL_NOT_LEGIBLE_SCENE_TEXT.encode("utf-8"),
    )

    recording = scan_archive(archive, "kirby")[0]

    assert recording.scene_summary == []


def test_scene_summary_skips_direction_on_read_error_placeholder(tmp_path, monkeypatch):
    archive = tmp_path / "archive"
    _write(archive, "20260715_140212_NF.mp4")
    _write(
        archive, "20260715_140212_N.scene.txt",
        content=_COMBINED_SCENE_TEXT.encode("utf-8"),
    )

    recording = scan_archive(archive, "kirby")[0]

    from pathlib import Path

    real_read_text = Path.read_text

    def _boom(self, *args, **kwargs):
        if self.name.endswith(".scene.txt"):
            raise OSError("permission denied")
        return real_read_text(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", _boom)

    # scene_texts still surfaces the bracketed error message in full
    # (unaffected by this feature); scene_summary just finds neither a
    # "## Description" heading nor a legible sign read in that
    # placeholder text, so it drops the direction rather than showing
    # a broken/empty entry.
    assert "could not read" in recording.scene_texts[0][1]
    assert recording.scene_summary == []


# ---------------------------------------------------------------------------
# first_valid_gps_fix()/last_valid_gps_fix() - added for the archive detail
# page's "Show Start and stop location" link (see app.py's
# archive_recording_location route). Fixture NMEA text mirrors
# tests/blackvue/telemetry/test_gps_reader.py's own - real read_gps()
# parsing is exercised end-to-end here, not mocked.
# ---------------------------------------------------------------------------


def test_first_valid_gps_fix_skips_leading_no_fix_sentences(tmp_path):
    path = tmp_path / "sample.gps"
    path.write_text(
        # Cold start: no fix yet (mode N).
        "[1700000000000]$GPRMC,120000.00,V,,,,,,,010124,,,N*7F\n"
        # Then a real position (mode A).
        "[1700000001000]$GPRMC,120001.00,A,4807.038,N,01131.000,E,"
        "10.00,45.00,010124,,,A*6D\n"
    )

    fix = first_valid_gps_fix(path)

    assert fix is not None
    assert fix.valid is True
    assert fix.latitude == 48 + 7.038 / 60
    assert fix.longitude == 11 + 31 / 60


def test_first_valid_gps_fix_returns_none_when_no_fix_ever_has_a_position(
    tmp_path,
):
    path = tmp_path / "sample.gps"
    path.write_text(
        "[1700000000000]$GPRMC,120000.00,V,,,,,,,010124,,,N*7F\n"
        "[1700000001000]$GPRMC,120001.00,V,,,,,,,010124,,,N*7F\n"
    )

    assert first_valid_gps_fix(path) is None


def test_last_valid_gps_fix_skips_trailing_no_fix_sentences(tmp_path):
    path = tmp_path / "sample.gps"
    path.write_text(
        # A real position first (mode A).
        "[1700000000000]$GPRMC,120000.00,A,4807.038,N,01131.000,E,"
        "10.00,45.00,010124,,,A*6D\n"
        # Then signal lost again right before the clip ends (mode N).
        "[1700000001000]$GPRMC,120001.00,V,,,,,,,010124,,,N*7F\n"
        # A later real position - this is the one last_valid_gps_fix()
        # should return.
        "[1700000002000]$GPRMC,120002.00,A,4900.000,N,01200.000,E,"
        "10.00,45.00,010124,,,A*6D\n"
        "[1700000003000]$GPRMC,120003.00,V,,,,,,,010124,,,N*7F\n"
    )

    fix = last_valid_gps_fix(path)

    assert fix is not None
    assert fix.valid is True
    assert fix.latitude == 49
    assert fix.longitude == 12


def test_last_valid_gps_fix_returns_none_when_no_fix_ever_has_a_position(
    tmp_path,
):
    path = tmp_path / "sample.gps"
    path.write_text(
        "[1700000000000]$GPRMC,120000.00,V,,,,,,,010124,,,N*7F\n"
        "[1700000001000]$GPRMC,120001.00,V,,,,,,,010124,,,N*7F\n"
    )

    assert last_valid_gps_fix(path) is None


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


# ---------------------------------------------------------------------------
# ArchiveRecordingCache - mirrors trips.py's TripCache (see its own
# docstring). Added because a recording's detail page, thumbnail, and every
# HTTP range request while its video plays each re-resolve the same
# recording via find_recording() - cheap in isolation, but repeated on a LAN
# where bv-web's Docker host is a different machine than the one playing the
# video, that adds up to felt lag. time.monotonic() is monkeypatched here
# (rather than a real time.sleep()) to control TTL expiry deterministically
# and instantly.
# ---------------------------------------------------------------------------


class _FakeClock:
    def __init__(self, start=0.0):
        self.value = start

    def __call__(self):
        return self.value


def test_archive_recording_cache_reuses_result_within_ttl(tmp_path, monkeypatch):
    import blackvue.web.archive_browser as archive_browser_module

    clock = _FakeClock()
    monkeypatch.setattr(archive_browser_module.time, "monotonic", clock)

    archive = tmp_path / "archive"
    _write(archive, "20260715_140212_NF.mp4")

    cache = ArchiveRecordingCache(ttl_seconds=2.0)
    first = cache.get(archive, "kirby", "20260715_140212_N")

    # A rear video appears after the first (real) lookup - a second get()
    # still within the TTL should return the exact same cached
    # ArchiveRecording, not notice the new file yet.
    _write(archive, "20260715_140212_NR.mp4")
    clock.value += 1.0
    second = cache.get(archive, "kirby", "20260715_140212_N")

    assert second is first
    assert second.videos == [("Front", "20260715_140212_NF.mp4")]


def test_archive_recording_cache_rescans_once_ttl_expires(tmp_path, monkeypatch):
    import blackvue.web.archive_browser as archive_browser_module

    clock = _FakeClock()
    monkeypatch.setattr(archive_browser_module.time, "monotonic", clock)

    archive = tmp_path / "archive"
    _write(archive, "20260715_140212_NF.mp4")

    cache = ArchiveRecordingCache(ttl_seconds=2.0)
    first = cache.get(archive, "kirby", "20260715_140212_N")

    _write(archive, "20260715_140212_NR.mp4")
    clock.value += 2.1
    second = cache.get(archive, "kirby", "20260715_140212_N")

    assert second is not first
    assert second.videos == [
        ("Front", "20260715_140212_NF.mp4"),
        ("Rear", "20260715_140212_NR.mp4"),
    ]


def test_archive_recording_cache_does_not_cache_a_miss(tmp_path, monkeypatch):
    import blackvue.web.archive_browser as archive_browser_module

    clock = _FakeClock()
    monkeypatch.setattr(archive_browser_module.time, "monotonic", clock)

    archive = tmp_path / "archive"
    archive.mkdir()

    cache = ArchiveRecordingCache(ttl_seconds=2.0)
    assert cache.get(archive, "kirby", "20260715_140212_N") is None

    # No time has passed at all - if the miss had been cached, this would
    # still return None even though the recording now genuinely exists.
    _write(archive, "20260715_140212_NF.mp4")
    assert cache.get(archive, "kirby", "20260715_140212_N") is not None
