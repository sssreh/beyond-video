"""
Tests for core/media_camera.py - MediaCamera, used by bv-download's
--media import mode (--sdcard is kept working as a deprecated alias
for the same flag - see cli/bv_download.py's own parse_args()).

Two recognizers: the default (`manifest=None`), which only recognizes
BlackVue's own on-camera filename convention; and the manifest-driven
one used for any other adapter (GoPro today), which matches by video
extension instead - see MediaCamera/_scan()'s own docstrings. Both
feed the same domain/Recording model bv-download's network path
already uses.
"""

import os
from datetime import datetime
from pathlib import Path

from blackvue.adapters.registry import load_adapter_manifest
from blackvue.core.media_camera import MediaCamera
from blackvue.core.media_camera import _matches_blackvue_filename
from blackvue.core.media_camera import _matches_generic_video


def _touch(path: Path, *, size: int = 10) -> Path:
    path.write_bytes(b"x" * size)
    return path


# ---------------------------------------------------------------------------
# _matches_blackvue_filename() - the strict recognizer.
# ---------------------------------------------------------------------------


def test_matches_real_video_filename():
    assert _matches_blackvue_filename("20260802_162130_NF.mp4")


def test_matches_real_gps_filename():
    assert _matches_blackvue_filename("20260802_162130_N.gps")


def test_matches_real_3gf_filename():
    assert _matches_blackvue_filename("20260802_162130_N.3gf")


def test_matches_real_thumbnail_filename():
    assert _matches_blackvue_filename("20260802_162130_NF.thm")


def test_rejects_video_without_a_direction_letter():
    assert not _matches_blackvue_filename("20260802_162130_N.mp4")


def test_rejects_gps_with_a_direction_letter():
    assert not _matches_blackvue_filename("20260802_162130_NF.gps")


def test_rejects_an_unknown_kind_letter():
    assert not _matches_blackvue_filename("20260802_162130_XF.mp4")


def test_rejects_arbitrary_camera_filenames():
    # Christer's own emulated test card (X:\SD_card, 2026-08-16) is
    # loaded with sample clips that don't follow BlackVue's real
    # naming convention - these must not be mistaken for real ones.
    assert not _matches_blackvue_filename("GOPR0001.MP4")
    assert not _matches_blackvue_filename("clip_001.mov")
    assert not _matches_blackvue_filename("video1.mp4")


def test_matching_is_case_insensitive_for_kind_and_extension():
    assert _matches_blackvue_filename("20260802_162130_nf.MP4")


# ---------------------------------------------------------------------------
# MediaCamera.recordings() / scan_summary() - the recursive scan.
# ---------------------------------------------------------------------------


def test_recordings_groups_files_by_recording_id(tmp_path):
    _touch(tmp_path / "20260802_162130_NF.mp4")
    _touch(tmp_path / "20260802_162130_NR.mp4")
    _touch(tmp_path / "20260802_162130_N.gps")

    camera = MediaCamera(tmp_path)
    recordings = camera.recordings()

    assert len(recordings) == 1
    assert recordings[0].id == "20260802_162130_N"
    assert len(recordings[0].entries) == 3


def test_recordings_recurses_into_subfolders(tmp_path):
    # The real on-disk layout of a mounted BlackVue SD card isn't
    # confirmed yet (see docs/CAMERA_ADAPTERS.md) - this works whether
    # files sit at the root or inside a Record/ subfolder.
    sub = tmp_path / "Record"
    sub.mkdir()
    _touch(tmp_path / "20260802_162130_NF.mp4")
    _touch(sub / "20260802_170000_EF.mp4")

    camera = MediaCamera(tmp_path)
    recordings = camera.recordings()

    assert len(recordings) == 2


def test_recordings_are_sorted_chronologically(tmp_path):
    _touch(tmp_path / "20260802_170000_EF.mp4")
    _touch(tmp_path / "20260802_162130_NF.mp4")

    camera = MediaCamera(tmp_path)
    ids = [r.id for r in camera.recordings()]

    assert ids == sorted(ids)


def test_unrecognized_files_are_silently_skipped(tmp_path):
    _touch(tmp_path / "20260802_162130_NF.mp4")
    _touch(tmp_path / "GOPR0001.MP4")
    _touch(tmp_path / "notes.txt")

    camera = MediaCamera(tmp_path)

    assert len(camera.recordings()) == 1


def test_scan_summary_reports_total_and_recognized_counts(tmp_path):
    _touch(tmp_path / "20260802_162130_NF.mp4")
    _touch(tmp_path / "20260802_162130_N.gps")
    _touch(tmp_path / "GOPR0001.MP4")

    summary = MediaCamera(tmp_path).scan_summary()

    assert summary.total_files_seen == 3
    assert summary.recognized_file_count == 2


def test_zero_name_standard_card_yields_zero_recordings(tmp_path):
    # Christer's real test scenario: X:\SD_card is loaded with sample
    # clips that don't follow BlackVue's naming convention - zero
    # recognized recordings is the correct, expected outcome here, not
    # an error.
    _touch(tmp_path / "GOPR0001.MP4")
    _touch(tmp_path / "clip_random.mov")

    camera = MediaCamera(tmp_path)

    assert camera.recordings() == []
    assert camera.scan_summary().total_files_seen == 2
    assert camera.scan_summary().recognized_file_count == 0


# ---------------------------------------------------------------------------
# probe_missing_sidecars() - always a no-op.
# ---------------------------------------------------------------------------


def test_probe_missing_sidecars_is_always_a_noop(tmp_path):
    _touch(tmp_path / "20260802_162130_NF.mp4")
    camera = MediaCamera(tmp_path)

    assert camera.probe_missing_sidecars(camera.recordings()[0]) == []


# ---------------------------------------------------------------------------
# download() - local copy, with select/on_bytes/resume semantics.
# ---------------------------------------------------------------------------


def test_download_copies_every_entry_by_default(tmp_path):
    _touch(tmp_path / "20260802_162130_NF.mp4", size=100)
    _touch(tmp_path / "20260802_162130_N.gps", size=5)

    camera = MediaCamera(tmp_path)
    dest = tmp_path / "dest"

    changed = camera.download(camera.recordings()[0], dest)

    assert changed is True
    assert (dest / "20260802_162130_NF.mp4").stat().st_size == 100
    assert (dest / "20260802_162130_N.gps").stat().st_size == 5


def test_download_respects_select(tmp_path):
    _touch(tmp_path / "20260802_162130_NF.mp4", size=100)
    _touch(tmp_path / "20260802_162130_N.gps", size=5)

    camera = MediaCamera(tmp_path)
    dest = tmp_path / "dest"

    camera.download(
        camera.recordings()[0], dest, select=lambda entry: not entry.is_video
    )

    assert not (dest / "20260802_162130_NF.mp4").exists()
    assert (dest / "20260802_162130_N.gps").exists()


def test_download_calls_on_bytes_for_copied_data(tmp_path):
    _touch(tmp_path / "20260802_162130_NF.mp4", size=200)
    camera = MediaCamera(tmp_path)

    reported = []
    camera.download(
        camera.recordings()[0], tmp_path / "dest", on_bytes=reported.append
    )

    assert sum(reported) == 200


def test_download_second_run_is_a_noop_when_file_already_matches(tmp_path):
    _touch(tmp_path / "20260802_162130_NF.mp4", size=100)
    camera = MediaCamera(tmp_path)
    dest = tmp_path / "dest"

    first = camera.download(camera.recordings()[0], dest)
    second = camera.download(camera.recordings()[0], dest)

    assert first is True
    assert second is False


def test_download_calls_on_entry_for_every_copied_file(tmp_path):
    _touch(tmp_path / "20260802_162130_NF.mp4", size=100)
    _touch(tmp_path / "20260802_162130_N.gps", size=5)
    camera = MediaCamera(tmp_path)

    reported = []
    camera.download(
        camera.recordings()[0], tmp_path / "dest",
        on_entry=lambda entry, elapsed, transferred: reported.append(
            (entry, elapsed, transferred)
        ),
    )

    assert {entry.path.name for entry, _elapsed, _transferred in reported} == {
        "20260802_162130_NF.mp4",
        "20260802_162130_N.gps",
    }
    assert all(elapsed >= 0 for _entry, elapsed, _transferred in reported)
    by_name = {entry.path.name: transferred for entry, _e, transferred in reported}
    assert by_name["20260802_162130_NF.mp4"] == 100
    assert by_name["20260802_162130_N.gps"] == 5


def test_download_skips_on_entry_for_a_file_already_up_to_date(tmp_path):
    _touch(tmp_path / "20260802_162130_NF.mp4", size=100)
    camera = MediaCamera(tmp_path)
    dest = tmp_path / "dest"

    camera.download(camera.recordings()[0], dest)  # first copy

    reported = []
    camera.download(
        camera.recordings()[0], dest,
        on_entry=lambda entry, elapsed, transferred: reported.append(
            (entry, elapsed, transferred)
        ),
    )

    assert reported == []  # nothing to copy the second time


def test_download_recopies_when_destination_size_differs(tmp_path):
    # A partial/stale file at the destination (different size) is
    # replaced wholesale - a local copy has no partial-transfer state
    # worth resuming into, unlike BlackVueClient.download()'s range
    # requests over the network.
    _touch(tmp_path / "20260802_162130_NF.mp4", size=100)
    camera = MediaCamera(tmp_path)
    dest = tmp_path / "dest"
    dest.mkdir()
    (dest / "20260802_162130_NF.mp4").write_bytes(b"y" * 10)

    changed = camera.download(camera.recordings()[0], dest)

    assert changed is True
    assert (dest / "20260802_162130_NF.mp4").stat().st_size == 100


# ---------------------------------------------------------------------------
# is_fully_downloaded() - the "would download() have nothing left to do"
# check bv-download's _run() uses to drop a recording from its
# "Matching recordings" listing/confirmation prompt entirely (Christer:
# "ignore files already fully downloaded").
# ---------------------------------------------------------------------------


def test_is_fully_downloaded_false_before_any_download(tmp_path):
    _touch(tmp_path / "20260802_162130_NF.mp4", size=100)
    camera = MediaCamera(tmp_path)
    dest = tmp_path / "dest"

    assert camera.is_fully_downloaded(camera.recordings()[0], dest) is False


def test_is_fully_downloaded_true_after_a_real_download(tmp_path):
    _touch(tmp_path / "20260802_162130_NF.mp4", size=100)
    _touch(tmp_path / "20260802_162130_N.gps", size=5)
    camera = MediaCamera(tmp_path)
    dest = tmp_path / "dest"

    camera.download(camera.recordings()[0], dest)

    assert camera.is_fully_downloaded(camera.recordings()[0], dest) is True


def test_is_fully_downloaded_false_when_one_entry_is_missing(tmp_path):
    _touch(tmp_path / "20260802_162130_NF.mp4", size=100)
    _touch(tmp_path / "20260802_162130_N.gps", size=5)
    camera = MediaCamera(tmp_path)
    dest = tmp_path / "dest"
    dest.mkdir()
    # Only the video made it across on some earlier, interrupted run -
    # the .gps sidecar is still missing.
    (dest / "20260802_162130_NF.mp4").write_bytes(b"x" * 100)

    assert camera.is_fully_downloaded(camera.recordings()[0], dest) is False


def test_is_fully_downloaded_false_when_size_differs(tmp_path):
    _touch(tmp_path / "20260802_162130_NF.mp4", size=100)
    camera = MediaCamera(tmp_path)
    dest = tmp_path / "dest"
    dest.mkdir()
    (dest / "20260802_162130_NF.mp4").write_bytes(b"y" * 10)

    assert camera.is_fully_downloaded(camera.recordings()[0], dest) is False


# ---------------------------------------------------------------------------
# read_config_text() - best-effort local config.ini read.
# ---------------------------------------------------------------------------


def test_read_config_text_returns_none_when_absent(tmp_path):
    assert MediaCamera(tmp_path).read_config_text() is None


def test_read_config_text_finds_config_subfolder(tmp_path):
    (tmp_path / "Config").mkdir()
    (tmp_path / "Config" / "config.ini").write_text("[Tab1]\nRecordTime=1\n")

    text = MediaCamera(tmp_path).read_config_text()

    assert text is not None
    assert "RecordTime" in text


def test_read_config_text_falls_back_to_card_root(tmp_path):
    (tmp_path / "config.ini").write_text("[Tab1]\nRecordTime=1\n")

    text = MediaCamera(tmp_path).read_config_text()

    assert text is not None
    assert "RecordTime" in text


def test_read_config_text_prefers_config_subfolder_over_root(tmp_path):
    (tmp_path / "Config").mkdir()
    (tmp_path / "Config" / "config.ini").write_text("[Tab1]\nRecordTime=1\n")
    (tmp_path / "config.ini").write_text("[Tab1]\nRecordTime=99\n")

    text = MediaCamera(tmp_path).read_config_text()

    assert "RecordTime=1" in text


# ---------------------------------------------------------------------------
# _matches_generic_video() - the manifest-driven recognizer.
# ---------------------------------------------------------------------------


def test_matches_generic_video_recognizes_gopro_style_filenames():
    # Real GoPro on-camera names carry a chapter+file counter, not a
    # timestamp - GH010123.MP4/GX010123.MP4, no BlackVue-style
    # convention to match against, extension only.
    assert _matches_generic_video("GH010123.MP4", frozenset({".mp4"}))
    assert _matches_generic_video("gx010123.mp4", frozenset({".mp4"}))


def test_matches_generic_video_rejects_wrong_extension():
    assert not _matches_generic_video("GH010123.LRV", frozenset({".mp4"}))
    assert not _matches_generic_video("notes.txt", frozenset({".mp4"}))


def test_matches_generic_video_rejects_hidden_appledouble_files():
    # macOS leaves "._GH010123.MP4" shadow copies behind after a
    # Finder-mediated card copy - never written by the camera itself,
    # must not be mistaken for a real clip even though the extension
    # matches.
    assert not _matches_generic_video("._GH010123.MP4", frozenset({".mp4"}))


# ---------------------------------------------------------------------------
# MediaCamera(manifest=...) - the manifest-driven scan path end to end.
# ---------------------------------------------------------------------------


def _gopro_manifest():
    return load_adapter_manifest("gopro")


def test_manifest_scan_recognizes_gopro_filenames_the_blackvue_scan_rejects(
    tmp_path,
):
    _touch(tmp_path / "GH010123.MP4")

    camera = MediaCamera(tmp_path, manifest=_gopro_manifest())

    assert len(camera.recordings()) == 1
    assert camera.recordings()[0].id == "GH010123"


def test_manifest_scan_default_stays_blackvue_only(tmp_path):
    # No manifest given (the default) - unchanged strict behavior, the
    # exact same scenario task #901's zero-match test already covers.
    _touch(tmp_path / "GH010123.MP4")

    camera = MediaCamera(tmp_path)

    assert camera.recordings() == []


def test_manifest_scan_each_file_is_its_own_recording(tmp_path):
    # No BlackVue-style trailing F/R/I letter to strip and no chapter
    # stitching - each matched file is its own recording (see
    # gopro/manifest.json's own unsupported_notes on chaptered clips).
    _touch(tmp_path / "GH010123.MP4")
    _touch(tmp_path / "GH020123.MP4")

    camera = MediaCamera(tmp_path, manifest=_gopro_manifest())
    ids = sorted(r.id for r in camera.recordings())

    assert ids == ["GH010123", "GH020123"]


def test_manifest_scan_timestamp_comes_from_mtime(tmp_path):
    path = _touch(tmp_path / "GH010123.MP4")
    mtime = datetime(2026, 3, 1, 9, 0, 0).timestamp()
    os.utime(path, (mtime, mtime))

    camera = MediaCamera(tmp_path, manifest=_gopro_manifest())
    entry = camera.recordings()[0].entries[0]

    assert entry.timestamp == datetime(2026, 3, 1, 9, 0, 0)


def test_manifest_scan_ignores_non_video_files(tmp_path):
    _touch(tmp_path / "GH010123.MP4")
    _touch(tmp_path / "GOPR0001.JPG")
    _touch(tmp_path / "notes.txt")

    camera = MediaCamera(tmp_path, manifest=_gopro_manifest())
    summary = camera.scan_summary()

    assert len(camera.recordings()) == 1
    assert summary.total_files_seen == 3
    assert summary.recognized_file_count == 1


def test_manifest_scan_downloads_the_same_way_as_the_default_scan(tmp_path):
    _touch(tmp_path / "GH010123.MP4", size=100)

    camera = MediaCamera(tmp_path, manifest=_gopro_manifest())
    dest = tmp_path / "dest"

    changed = camera.download(camera.recordings()[0], dest)

    assert changed is True
    assert (dest / "GH010123.MP4").stat().st_size == 100
