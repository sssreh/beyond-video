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

from blackvue.adapters.blackvue.adapter import BlackVueAdapter
from blackvue.archive.asset import Asset
from blackvue.archive.asset_file import AssetFile
from blackvue.archive.recording import Recording
from blackvue.archive.recording_id import RecordingId
from blackvue.web.archive_browser import ArchiveRecording
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


def test_thumbnail_direction_is_none_without_any_front_asset(tmp_path):
    # No thumbnail sidecar and no FRONT asset at all (rear-only) - the
    # video-frame fallback is front-only (mirrors the photo fallback),
    # so there's nothing left to fall back to.
    archive = tmp_path / "archive"
    _write(archive, "20260715_140212_NR.mp4")

    recording = scan_archive(archive, "kirby")[0]

    assert recording.thumbnail_direction is None


def test_thumbnail_direction_is_front_for_a_plain_video_with_no_thumbnail_sidecar(
    tmp_path,
):
    # No *_THUMBNAIL sidecar exists (e.g. a FolderAdapter/GoProAdapter
    # archive, which never writes one), but a FRONT video does - a
    # frame-grab can always be generated on demand (see
    # thumbnail_path()'s own video-fallback tier), so this must report
    # "front" rather than None or the grid never shows an <img> at all.
    archive = tmp_path / "archive"
    _write(archive, "20260715_140212_NF.mp4")

    recording = scan_archive(archive, "kirby")[0]

    assert recording.thumbnail_direction == "front"


def test_thumbnail_direction_is_front_for_a_photo_recording_with_no_sidecar(tmp_path):
    # The bug this docstring fix addresses: thumbnail_path()'s photo
    # fallback (task #947) existed with no matching branch here, so a
    # photo recording's thumbnail never actually rendered in the grid.
    photo_path = tmp_path / "IMG_0001.jpg"
    photo_path.write_bytes(b"jpeg bytes")

    recording = ArchiveRecording(
        camera_id="kirby",
        recording=Recording(
            id=RecordingId("20260715_140212_V"),
            assets={Asset.FRONT: AssetFile(Asset.FRONT, photo_path)},
        ),
    )

    assert recording.thumbnail_direction == "front"


def test_stats_timestamp_strips_the_trailing_kind_letter():
    # Christer, from the archive browser: "i would like a link to
    # bv-stats with a prefilled full timestamp and camera." bv-stats'
    # own --timestamp (and the web Stats dashboard's equivalent `?
    # timestamp=` param) rejects more than one '_' - a raw recording id
    # like "20260715_140212_N" has two (one before the time, one before
    # the kind letter), so the link needs the kind letter stripped
    # first, same as TimeInterval.__contains__ already does internally.
    recording = ArchiveRecording(
        camera_id="kirby",
        recording=Recording(id=RecordingId("20260715_140212_N")),
    )

    assert recording.stats_timestamp == "20260715_140212"


def test_stats_timestamp_round_trips_through_lexicaltimeparser():
    # The whole point of stats_timestamp: it must actually be a valid
    # --timestamp value that resolves back to this one recording, not
    # just "looks right." Confirms the full link the template builds
    # (?timestamp=<stats_timestamp>) would filter bv-stats down to
    # exactly this recording and no others nearby.
    recording = ArchiveRecording(
        camera_id="kirby",
        recording=Recording(id=RecordingId("20260715_140212_N")),
    )

    interval = LexicalTimeParser(timestamp=recording.stats_timestamp).parse()

    assert "20260715_140212_N" in interval
    assert "20260715_140213_N" not in interval
    assert "20260716_140212_N" not in interval


def test_thumbnail_path_resolves_the_right_file(tmp_path):
    archive = tmp_path / "archive"
    _write(archive, "20260715_140212_NF.thm")
    _write(archive, "20260715_140212_NR.thm")

    recording = scan_archive(archive, "kirby")[0]

    assert recording.thumbnail_path("front") == archive / "20260715_140212_NF.thm"
    assert recording.thumbnail_path("rear") == archive / "20260715_140212_NR.thm"
    assert recording.thumbnail_path("interior") is None
    assert recording.thumbnail_path("bogus") is None


def test_thumbnail_path_falls_back_to_the_photo_itself_for_a_photo_recording(
    tmp_path,
):
    # No *_THUMBNAIL sidecar exists for a photo recording (task
    # #940-949: "a picture is also a video, but 1 frame only") -
    # nothing generates one - so the photo's own FRONT file (already a
    # small real preview image) is served directly instead.
    photo_path = tmp_path / "IMG_0001.jpg"
    photo_path.write_bytes(b"jpeg bytes")

    recording = ArchiveRecording(
        camera_id="kirby",
        recording=Recording(
            id=RecordingId("20260715_140212_V"),
            assets={Asset.FRONT: AssetFile(Asset.FRONT, photo_path)},
        ),
    )

    assert recording.thumbnail_path("front") == photo_path
    assert recording.thumbnail_path("rear") is None


def test_thumbnail_path_prefers_a_real_thumbnail_sidecar_over_the_photo_itself(
    tmp_path,
):
    # Belt-and-suspenders: if a real *_THUMBNAIL sidecar somehow does
    # exist for a photo recording (e.g. a future adapter that
    # generates one), it wins over the raw-photo fallback.
    photo_path = tmp_path / "IMG_0001.jpg"
    thumbnail_path = tmp_path / "IMG_0001.thm"
    photo_path.write_bytes(b"jpeg bytes")
    thumbnail_path.write_bytes(b"thumbnail bytes")

    recording = ArchiveRecording(
        camera_id="kirby",
        recording=Recording(
            id=RecordingId("20260715_140212_V"),
            assets={
                Asset.FRONT: AssetFile(Asset.FRONT, photo_path),
                Asset.FRONT_THUMBNAIL: AssetFile(
                    Asset.FRONT_THUMBNAIL, thumbnail_path
                ),
            },
        ),
    )

    assert recording.thumbnail_path("front") == thumbnail_path


def test_thumbnail_path_returns_none_for_rear_even_on_a_photo_recording(
    tmp_path,
):
    # The raw-photo fallback is front-only - a photo recording never
    # has a rear counterpart to fall back to.
    photo_path = tmp_path / "IMG_0001.jpg"
    photo_path.write_bytes(b"jpeg bytes")

    recording = ArchiveRecording(
        camera_id="kirby",
        recording=Recording(
            id=RecordingId("20260715_140212_V"),
            assets={Asset.FRONT: AssetFile(Asset.FRONT, photo_path)},
        ),
    )

    assert recording.thumbnail_path("rear") is None


# ---------------------------------------------------------------------------
# thumbnail_path()'s on-demand fallback tier - a generated frame-grab from
# a plain FRONT video with no *_THUMBNAIL sidecar, no Asset.THUMBNAIL
# generated asset yet, and no photo (the real FolderAdapter/GoProAdapter
# case that prompted this whole feature: Christer, "archive browser for
# folder would look so much better with a thumbnail"). Uses a real short
# ffmpeg-muxed video rather than a mock, matching this project's usual
# fixture convention. The permanent-asset design (Christer: "Number 1,
# but also be created by archive browser if not exists") means this now
# writes straight into the archive at <id>.thumb.jpg rather than a
# separate app-level cache dir - see generate/media.py's
# extract_video_thumbnail() and archive/asset.py's Asset.THUMBNAIL.
# ---------------------------------------------------------------------------


def _make_video(path, duration_seconds: float = 1.0) -> None:
    import subprocess

    subprocess.run(
        [
            "ffmpeg", "-y",
            "-f", "lavfi", "-i", "testsrc=size=64x64:rate=10",
            "-t", str(duration_seconds),
            str(path),
        ],
        capture_output=True,
        text=True,
        check=True,
    )


def test_thumbnail_path_generates_and_writes_a_permanent_video_frame_thumbnail(
    tmp_path,
):
    video_path = tmp_path / "GH010023.MP4"
    _make_video(video_path, duration_seconds=2.0)
    archive_root = tmp_path

    recording_id = RecordingId("20260715_140212_V")
    recording = ArchiveRecording(
        camera_id="gopro",
        recording=Recording(
            id=recording_id,
            assets={Asset.FRONT: AssetFile(Asset.FRONT, video_path)},
        ),
    )

    result = recording.thumbnail_path("front", archive_root=archive_root)

    assert result is not None
    assert result.is_file()
    # Written straight into the archive root as a normal, permanent
    # generated asset - the same <id>.thumb.jpg path bv-generate
    # --thumbnail and generated_assets_for() both expect.
    assert result == archive_root / f"{recording_id}.thumb.jpg"


def test_thumbnail_path_returns_none_for_a_plain_video_without_an_archive_root(
    tmp_path,
):
    # No archive_root given (e.g. an existing non-web caller) - the
    # video-frame fallback must not attempt anything, same as before
    # this fallback existed.
    video_path = tmp_path / "GH010023.MP4"
    _make_video(video_path, duration_seconds=2.0)

    recording = ArchiveRecording(
        camera_id="gopro",
        recording=Recording(
            id=RecordingId("20260715_140212_V"),
            assets={Asset.FRONT: AssetFile(Asset.FRONT, video_path)},
        ),
    )

    assert recording.thumbnail_path("front") is None


def test_thumbnail_path_swallows_a_generation_failure_and_returns_none(tmp_path):
    # A corrupt/unreadable source must not blow up the whole grid -
    # one recording's thumbnail failing to generate is swallowed, not
    # raised (see thumbnail_path()'s own docstring).
    video_path = tmp_path / "corrupt.mp4"
    video_path.write_bytes(b"not a real video")

    recording = ArchiveRecording(
        camera_id="gopro",
        recording=Recording(
            id=RecordingId("20260715_140212_V"),
            assets={Asset.FRONT: AssetFile(Asset.FRONT, video_path)},
        ),
    )

    assert recording.thumbnail_path("front", archive_root=tmp_path) is None


def test_thumbnail_path_prefers_an_existing_generated_thumbnail_asset(tmp_path):
    # If a THUMBNAIL asset already exists (e.g. bv-generate --thumbnail
    # ran ahead of time), thumbnail_path() must serve it directly
    # rather than generating a fresh one from the video - even when an
    # archive_root is given.
    generated_path = tmp_path / "existing.thumb.jpg"
    generated_path.write_bytes(b"jpeg bytes")
    video_path = tmp_path / "GH010023.MP4"
    video_path.write_bytes(b"pretend video bytes")

    recording = ArchiveRecording(
        camera_id="gopro",
        recording=Recording(
            id=RecordingId("20260715_140212_V"),
            assets={
                Asset.FRONT: AssetFile(Asset.FRONT, video_path),
                Asset.THUMBNAIL: AssetFile(Asset.THUMBNAIL, generated_path),
            },
        ),
    )

    assert recording.thumbnail_path("front", archive_root=tmp_path) == generated_path


# ---------------------------------------------------------------------------
# source_filename - the real, on-disk FRONT filename, shown in the grid
# for a FolderAdapter/GoProAdapter archive where it's genuinely
# different information from the synthesized recording id (task #931's
# bv-ls precedent, applied to the web grid).
# ---------------------------------------------------------------------------


def test_source_filename_is_none_for_an_id_derived_blackvue_filename(tmp_path):
    archive = tmp_path / "archive"
    _write(archive, "20260715_140212_NF.mp4")

    recording = scan_archive(archive, "kirby")[0]

    assert recording.source_filename is None


def test_source_filename_returns_the_real_name_for_a_non_id_derived_file(tmp_path):
    video_path = tmp_path / "GH010023.MP4"
    video_path.write_bytes(b"x")

    recording = ArchiveRecording(
        camera_id="gopro",
        recording=Recording(
            id=RecordingId("20260715_140212_V"),
            assets={Asset.FRONT: AssetFile(Asset.FRONT, video_path)},
        ),
    )

    assert recording.source_filename == "GH010023.MP4"


def test_source_filename_is_none_without_a_front_asset(tmp_path):
    video_path = tmp_path / "GH010023.MP4"
    video_path.write_bytes(b"x")

    recording = ArchiveRecording(
        camera_id="gopro",
        recording=Recording(
            id=RecordingId("20260715_140212_V"),
            assets={Asset.REAR: AssetFile(Asset.REAR, video_path)},
        ),
    )

    assert recording.source_filename is None


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
            ["At 0 seconds, shop/storefront sign: SOLNA♥DENTAL"],
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
    assert legible_reads == ["At 2 minutes, shop/storefront sign: MALL OF SCANDINAVIA"]


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
        "At 41 seconds, blue road sign with white text: 227 DALARÖ "
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
# scene_raw_text() - the exact raw .scene.txt/.scene-rear.txt content for
# one direction, used to prefill the "Edit" panel next to Read-aloud on
# archive_recording_detail.html. Christer: "it would be nice to have an
# edit option for the scene file, next to read aloud" - asked whether that
# should cover just the description paragraph or the whole raw file, and
# whether edits should persist to disk: "Full raw scene file" / "Overwrite
# the file on disk" (see WORKING_CONTEXT.md). Same label.lower() lookup
# sign_read_srt()/description_srt() below already do over scene_texts,
# but returning the exact original text rather than a derived view.
# ---------------------------------------------------------------------------


def test_scene_raw_text_returns_exact_file_content_for_direction(tmp_path):
    archive = tmp_path / "archive"
    _write(archive, "20260715_140212_NF.mp4")
    _write(
        archive, "20260715_140212_N.scene.txt",
        content=_COMBINED_SCENE_TEXT.encode("utf-8"),
    )

    recording = scan_archive(archive, "kirby")[0]

    assert recording.scene_raw_text("front") == _COMBINED_SCENE_TEXT


def test_scene_raw_text_matches_direction_case_insensitively(tmp_path):
    archive = tmp_path / "archive"
    _write(archive, "20260715_140212_NF.mp4")
    _write(
        archive, "20260715_140212_N.rear.scene.txt",
        content=_OCR_ONLY_SCENE_TEXT.encode("utf-8"),
    )

    recording = scan_archive(archive, "kirby")[0]

    assert recording.scene_raw_text("Rear") == _OCR_ONLY_SCENE_TEXT
    assert recording.scene_raw_text("REAR") == _OCR_ONLY_SCENE_TEXT


def test_scene_raw_text_none_when_no_scene_file_for_direction(tmp_path):
    archive = tmp_path / "archive"
    _write(archive, "20260715_140212_NF.mp4")
    _write(
        archive, "20260715_140212_N.scene.txt",
        content=_COMBINED_SCENE_TEXT.encode("utf-8"),
    )

    recording = scan_archive(archive, "kirby")[0]

    # front-only scene file - rear has none.
    assert recording.scene_raw_text("rear") is None


# ---------------------------------------------------------------------------
# sign_read_srt() / build_sign_read_srt() - a downloadable .srt built from
# the same '## Zoomed sign reads' timestamps scene_summary's legible_reads
# already parses, for importing alongside the recording's own video in an
# editor. Christer, right after the ElevenLabs .srt feature: "Does the
# scene detection ever have the timestamps for the description, then i
# would like a scene.srt file to" (see WORKING_CONTEXT.md) - answered by
# this feature, scoped to the sign-reads data since the main free-text
# description has no internal timestamps at all.
# ---------------------------------------------------------------------------


def test_sign_read_srt_builds_cues_from_zoomed_sign_reads(tmp_path):
    archive = tmp_path / "archive"
    _write(archive, "20260715_140212_NF.mp4")
    _write(
        archive, "20260715_140212_N.scene.txt",
        content=_COMBINED_SCENE_TEXT.encode("utf-8"),
    )

    recording = scan_archive(archive, "kirby")[0]

    srt = recording.sign_read_srt("front")
    assert srt is not None
    assert "1\n00:00:00,000 --> 00:00:03,000\n" in srt
    assert "shop/storefront sign: SOLNA♥DENTAL" in srt
    # the "not legible" reads dropped by scene_summary must also be
    # absent here - same filtering, one shared parse.
    assert "not legible" not in srt


def test_sign_read_srt_matches_direction_case_insensitively(tmp_path):
    archive = tmp_path / "archive"
    _write(archive, "20260715_140212_NF.mp4")
    _write(
        archive, "20260715_140212_N.scene.txt",
        content=_COMBINED_SCENE_TEXT.encode("utf-8"),
    )

    recording = scan_archive(archive, "kirby")[0]

    assert recording.sign_read_srt("Front") == recording.sign_read_srt("front")


def test_sign_read_srt_none_when_direction_has_no_scene_text(tmp_path):
    archive = tmp_path / "archive"
    _write(archive, "20260715_140212_NF.mp4")
    _write(
        archive, "20260715_140212_N.scene.txt",
        content=_COMBINED_SCENE_TEXT.encode("utf-8"),
    )

    recording = scan_archive(archive, "kirby")[0]

    assert recording.sign_read_srt("rear") is None


def test_sign_read_srt_none_when_all_reads_not_legible(tmp_path):
    archive = tmp_path / "archive"
    _write(archive, "20260715_140212_NF.mp4")
    _write(
        archive, "20260715_140212_N.rear.scene.txt",
        content=_ALL_NOT_LEGIBLE_SCENE_TEXT.encode("utf-8"),
    )

    recording = scan_archive(archive, "kirby")[0]

    assert recording.sign_read_srt("rear") is None


def test_sign_read_srt_cues_never_overlap_even_with_out_of_order_timestamps(tmp_path):
    # zoom_into_signs() timestamps come from sequential frame sampling
    # in practice, but the cue-builder must stay safe even if two reads
    # share (or go backwards from) a previous cue's timestamp - each
    # cue's start is clamped to at least the previous cue's end.
    archive = tmp_path / "archive"
    _write(archive, "20260715_140212_NF.mp4")
    _write(
        archive, "20260715_140212_N.scene.txt",
        content=(
            "## Zoomed sign reads\n"
            "- [t=40.1s] first sign: STOP\n"
            "- [t=1.0s] second sign: YIELD\n"
        ).encode("utf-8"),
    )

    recording = scan_archive(archive, "kirby")[0]

    srt = recording.sign_read_srt("front")
    assert "00:00:40,100 --> 00:00:43,100" in srt
    assert "00:00:43,100 --> 00:00:46,100" in srt


def test_extract_legible_sign_reads_uses_natural_language_timestamp(tmp_path):
    # Christer: "instead of trying to say "[t=60.1s]" it would be much
    # better to say "At 60 seconds"  rounded of to closest second" -
    # this is the display/TTS-facing text scene_summary's legible_reads
    # returns, distinct from sign_read_srt()'s own cues (which don't
    # repeat the timestamp in words since the .srt's own timing already
    # conveys it).
    archive = tmp_path / "archive"
    _write(archive, "20260715_140212_NF.mp4")
    _write(
        archive, "20260715_140212_N.scene.txt",
        content=(
            "## Zoomed sign reads\n"
            "- [t=60.1s] sign: SPEED LIMIT 60\n"
            "- [t=1.4s] sign: STOP\n"
        ).encode("utf-8"),
    )

    recording = scan_archive(archive, "kirby")[0]

    [(_label, _description, legible_reads)] = recording.scene_summary
    assert legible_reads == [
        "At 1 minute, sign: SPEED LIMIT 60",
        "At 1 second, sign: STOP",
    ]


# ---------------------------------------------------------------------------
# description_srt() / build_description_srt() / _chunk_description_text() -
# a second downloadable .srt, this one for the main '## Description' text,
# timed against the recording's own real video length rather than any TTS
# narration. Christer, right after the scene.srt (sign-reads) feature above:
# "Could i also get a srt file that is synced with the video of 3minutes"
# (see WORKING_CONTEXT.md).
#
# Originally describe_scene()'s main pass had no internal per-sentence
# timestamps at all, so this chunked the text and spaced it evenly across
# the recording's real duration - that fallback path (_chunk_description_
# text()/build_description_srt(), tested below) still exists and is what
# an older scene.txt (written before DESCRIBE_PROMPT started asking for
# timestamped events) or a still photo's description falls back to.
# Christer, on seeing the evenly-spaced version: "It would have been nice
# to both say and subtitle 'To the left, there's a red bus passing
# alongside the vehicle' at the same time you can see the red buss pass" -
# "yes, but please keep the old output" - answered by
# build_description_srt_from_events(), which ArchiveRecording.
# description_srt() now prefers whenever generate/scene.py's
# extract_description_events() finds real per-event timestamps to build
# cues from (see that function's own tests in test_scene.py).
# ---------------------------------------------------------------------------


def test_chunk_description_text_keeps_short_text_as_a_single_chunk():
    from blackvue.web.archive_browser import _chunk_description_text

    text = "A quiet residential street. Light traffic."
    chunks = _chunk_description_text(text)

    # Short enough to fit in one 90-char chunk - no premature split.
    assert chunks == [text]


def test_chunk_description_text_prefers_sentence_break_past_20_chars():
    from blackvue.web.archive_browser import _chunk_description_text

    # First sentence ends well past the 20-char minimum but before the
    # 90-char window closes - the chunk should end there rather than
    # spilling into the next sentence or cutting mid-word.
    text = (
        "A grey sedan is parked on the left side of the road near a driveway. "
        "Further ahead a cyclist is visible."
    )
    chunks = _chunk_description_text(text)

    assert chunks[0] == (
        "A grey sedan is parked on the left side of the road near a driveway."
    )
    assert chunks[1] == "Further ahead a cyclist is visible."


def test_chunk_description_text_falls_back_to_word_boundary():
    from blackvue.web.archive_browser import _chunk_description_text

    # No sentence-ending punctuation anywhere - must fall back to the
    # last space within the window rather than cutting mid-word.
    text = "one two three four five six seven eight nine ten " * 4
    chunks = _chunk_description_text(text)

    assert all(len(c) <= 90 for c in chunks)
    assert all(not c.endswith(" ") for c in chunks)
    # rejoining every chunk with a space must reproduce every original
    # word - no word lost or split across a chunk boundary.
    rejoined_words = " ".join(chunks).split()
    assert rejoined_words == text.split()


def test_chunk_description_text_empty_or_blank_returns_no_chunks():
    from blackvue.web.archive_browser import _chunk_description_text

    assert _chunk_description_text("") == []
    assert _chunk_description_text("   ") == []


def test_build_description_srt_spaces_chunks_evenly_across_duration():
    from blackvue.web.archive_browser import build_description_srt

    text = (
        "A grey sedan is parked on the left side of the road near a driveway. "
        "Further ahead a cyclist is visible."
    )
    srt = build_description_srt(text, 60.0)

    assert srt is not None
    assert "1\n00:00:00,000 --> 00:00:30,000\n" in srt
    assert "2\n00:00:30,000 --> 00:01:00,000\n" in srt


def test_build_description_srt_none_for_empty_description():
    from blackvue.web.archive_browser import build_description_srt

    assert build_description_srt("", 60.0) is None
    assert build_description_srt("   ", 60.0) is None


def test_build_description_srt_none_for_non_positive_duration():
    from blackvue.web.archive_browser import build_description_srt

    assert build_description_srt("Some description text.", 0) is None
    assert build_description_srt("Some description text.", -5) is None


def test_rescale_events_to_duration_spreads_compressed_timestamps_out():
    from blackvue.generate.scene import DescriptionEvent
    from blackvue.web.archive_browser import _rescale_events_to_duration

    # This is Christer's real-world report, trimmed: describe_scene()
    # only ever shows the model 16 frames spread across the whole
    # clip, so its "- [t=X.Ys]" values are the model's own narrative
    # pacing between those frames, not real elapsed video time - here
    # everything came back inside the first 6 seconds of an actual
    # 180-second (3-minute) recording. n=4, duration=180.0 ->
    # target_max = 180*4/5 = 144.0, scale = 144.0/6.0 = 24.0.
    events = [
        DescriptionEvent(0.0, "first"),
        DescriptionEvent(0.6, "second"),
        DescriptionEvent(1.7, "third"),
        DescriptionEvent(6.0, "fourth"),
    ]

    rescaled = _rescale_events_to_duration(events, 180.0)

    # round() to sidestep ordinary floating-point multiplication noise
    # (0.6 * 24.0 lands on 14.399999999999999, not 14.4) - not a
    # rounding rule the function itself applies.
    assert [round(event.timestamp_seconds, 6) for event in rescaled] == [
        0.0,
        14.4,
        40.8,
        144.0,
    ]
    assert [event.text for event in rescaled] == ["first", "second", "third", "fourth"]


def test_rescale_events_to_duration_leaves_a_single_zero_timestamp_alone():
    from blackvue.generate.scene import DescriptionEvent
    from blackvue.web.archive_browser import _rescale_events_to_duration

    # Nothing positive to anchor a proportion on - dividing by a
    # non-positive max would be meaningless, so this is a no-op. This
    # is exactly the DESCRIBE_PROMPT "nothing notable happened"
    # fallback case: a single bullet at t=0.0 for the whole clip.
    events = [DescriptionEvent(0.0, "Routine driving, nothing notable happened.")]

    rescaled = _rescale_events_to_duration(events, 180.0)

    assert rescaled == events


def test_rescale_events_to_duration_empty_list_is_a_noop():
    from blackvue.web.archive_browser import _rescale_events_to_duration

    assert _rescale_events_to_duration([], 60.0) == []


def test_build_description_srt_from_events_uses_real_per_event_timestamps():
    from blackvue.generate.scene import DescriptionEvent
    from blackvue.web.archive_browser import build_description_srt_from_events

    events = [
        DescriptionEvent(0.0, "Clear weather, light traffic."),
        DescriptionEvent(12.4, "A red bus passes on the left."),
        DescriptionEvent(25.0, "The vehicle continues through an intersection."),
    ]

    srt = build_description_srt_from_events(events, 40.0)

    # Real timestamps get rescaled (see _rescale_events_to_duration())
    # so the latest one lands at duration_seconds * n/(n+1) rather than
    # trusted verbatim - here n=3, duration=40.0, so 25.0 (the raw max)
    # maps to 30.0, and 12.4 scales by the same 1.2x factor to 14.88.
    # _apply_frame_sampling_lag() then adds a flat
    # duration/16*_FRAME_SAMPLING_LAG_MULTIPLIER offset -
    # _LAG_CORRECTION_CURVE's own position-dependent term is currently
    # flat/zero (reset 2026-08-19 - see that constant's own comment),
    # so it contributes nothing here. Each cue's *display* window is
    # then computed by _cue_display_window() from that real timestamp:
    # _CUE_LEAD_SECONDS (2.0s) before, at least _CUE_TRAIL_SECONDS
    # (2.0s) after, capped at the next real cue's start. These exact
    # numbers come from actually calling
    # build_description_srt_from_events() and reading its real output,
    # per this whole feature's own "never hand-derive SRT timestamps"
    # testing convention.
    assert srt is not None
    assert "1\n00:00:01,750 --> 00:00:05,750\nClear weather, light traffic.\n" in srt
    assert "2\n00:00:16,630 --> 00:00:20,630\nA red bus passes on the left.\n" in srt
    assert (
        "3\n00:00:28,000 --> 00:00:32,833\n"
        "The vehicle continues through an intersection.\n"
    ) in srt


def test_build_description_srt_from_events_rescales_an_overshoot_into_range():
    from blackvue.generate.scene import DescriptionEvent
    from blackvue.web.archive_browser import build_description_srt_from_events

    # Before _rescale_events_to_duration() existed, a timestamp past
    # the recording's real duration (a plausible model estimation
    # error) got clamped to duration_seconds and, if that clamp
    # collapsed its cue to zero length, silently dropped. Now every
    # event is rescaled onto the real timeline first (see that
    # function's docstring for why - raw model timestamps aren't real
    # elapsed video time to begin with), so an overshoot no longer
    # needs dropping - it lands inside the real duration instead and
    # keeps its text, same as every other event.
    events = [
        DescriptionEvent(0.0, "first"),
        DescriptionEvent(50.0, "second - past duration"),
    ]

    srt = build_description_srt_from_events(events, 40.0)

    assert srt is not None
    assert "second" in srt
    # n=2, duration=40.0 -> target_max = 40 * 2/3 = 26.6667, scale =
    # 26.6667/50 = 0.5333; second's rescaled start is 26.6667s, well
    # inside the clip. _apply_frame_sampling_lag()'s flat offset lands
    # the two cues here (the curve's own term is flat/zero); values
    # from actually calling build_description_srt_from_events(), not
    # hand-derived.
    assert "1\n00:00:01,750 --> 00:00:05,750\nfirst\n" in srt
    assert "2\n00:00:24,667 --> 00:00:28,667\nsecond - past duration\n" in srt


def test_build_description_srt_from_events_merges_a_collapsed_leading_cue_forward():
    from blackvue.generate.scene import DescriptionEvent
    from blackvue.web.archive_browser import build_description_srt_from_events

    # A negative leading timestamp (a plausible model estimation error for
    # content right at/before the clip's start, observed in real output)
    # clamps up to cursor=0.0. If the next event is also at 0.0, that
    # produces a zero-length "cue" for the first event. Unlike the
    # overshoot-past-duration case (nothing after it to merge into), there
    # IS a following cue here, so the first event's text must be carried
    # forward onto it rather than silently dropped.
    #
    # The raw timestamps below are more negative than the collapse itself
    # strictly requires, keeping both "first" and "second" clamped to the
    # same cursor=0.0 (and so still colliding into one merged cue) even
    # after _apply_frame_sampling_lag()'s flat offset is added on top of
    # the rescale. See build_description_srt_from_events()'s own
    # docstring for why a collapsed leading cue merges forward instead
    # of dropping.
    events = [
        DescriptionEvent(-1.5, "first"),
        DescriptionEvent(-1.4, "second"),
        DescriptionEvent(10.0, "third"),
    ]

    srt = build_description_srt_from_events(events, 30.0)

    # The collapse-and-merge-forward mechanic this test exists for still
    # fires. With _LAG_CORRECTION_CURVE flat/zero, both events collapse
    # at cursor=0.0 in their original list order, so the merged text is
    # "first second" (not reordered - contrast with this same scenario
    # under the old non-flat curve, which could and did reorder them).
    # Values below come from actually calling
    # build_description_srt_from_events(), not hand-derived.
    assert srt is not None
    assert "first" in srt
    assert "second" in srt
    assert "1\n00:00:00,000 --> 00:00:02,000\nfirst second\n" in srt
    assert "2\n00:00:20,500 --> 00:00:24,500\nthird\n" in srt


def test_build_description_srt_from_events_sorts_out_of_order_timestamps():
    from blackvue.generate.scene import DescriptionEvent
    from blackvue.web.archive_browser import build_description_srt_from_events

    events = [
        DescriptionEvent(10.0, "later thing, listed first"),
        DescriptionEvent(2.0, "earlier thing, listed second"),
    ]

    srt = build_description_srt_from_events(events, 30.0)

    # n=2, duration=30.0 -> target_max=20.0, scale=2.0: 2.0*2=4.0,
    # 10.0*2=20.0. _apply_frame_sampling_lag()'s flat offset, then
    # _cue_display_window()'s lead/trail, apply on top - values below
    # come from actually calling build_description_srt_from_events(),
    # not hand-derived.
    assert srt.index("earlier thing") < srt.index("later thing")
    assert "1\n00:00:04,812 --> 00:00:08,812\n" in srt
    assert "2\n00:00:18,000 --> 00:00:22,000\n" in srt


def test_build_description_srt_from_events_none_for_no_events_or_duration():
    from blackvue.generate.scene import DescriptionEvent
    from blackvue.web.archive_browser import build_description_srt_from_events

    events = [DescriptionEvent(0.0, "something")]
    assert build_description_srt_from_events([], 30.0) is None
    assert build_description_srt_from_events(events, 0) is None
    assert build_description_srt_from_events(events, -5) is None


# ---------------------------------------------------------------------------
# _cue_display_window() - replaces the earlier lead-time-shift +
# reading-time-trim design (2026-08-19). Christer, after using the
# frame-viewer to compare real frames against their shown cues: "I want
# the description to pop up a couple of seconds before the video then
# last a couple of seconds after, unless there is something more
# happening. And yes we also need to consider how long time each aloud
# takes." Pop up _CUE_LEAD_SECONDS before the real moment, stay at
# least _CUE_TRAIL_SECONDS after it, longer if the text needs more time
# to actually say - capped at the next real cue's own moment either way.
# ---------------------------------------------------------------------------


def test_cue_display_window_pops_up_early_and_holds_the_trail_floor():
    from blackvue.web.archive_browser import _cue_display_window

    # Short text ("Hi.") needs nowhere near the 2s trail floor to say,
    # so the trail floor (not the speaking-duration estimate) decides
    # the end: real_start=10.0 -> lead_start=8.0, natural_end=12.0.
    # Plenty of real span (real_end=30.0) and no previous cue
    # (prev_display_end=0.0) to interact with.
    assert _cue_display_window(10.0, 30.0, 0.0, "Hi.") == (8.0, 12.0)


def test_cue_display_window_floors_lead_start_at_zero():
    from blackvue.web.archive_browser import _cue_display_window

    # A cue right at the clip's start has nowhere earlier to lead into
    # - lead_start = max(0.0, 0.0 - 2.0) clamps to 0.0, not -2.0.
    assert _cue_display_window(0.0, 5.0, 0.0, "Hi.") == (0.0, 2.0)


def test_cue_display_window_extends_the_trail_for_longer_text():
    from blackvue.web.archive_browser import _cue_display_window

    # A long cue needs more than the 2s trail floor to say -
    # _CUE_SPEAKING_CHARS_PER_SECOND (12.0) estimates its speaking
    # duration and extends the trail to fit, but real_end=12.0 (a
    # tight real gap to whatever's next) still wins as the hard cap -
    # Christer's own "unless there is something more happening" caveat
    # is a floor being extended, not a license to overrun the next cue.
    text = "A much longer piece of text that needs real time to say aloud, quite a bit of it."
    assert _cue_display_window(10.0, 12.0, 0.0, text) == (8.0, 12.0)


def test_cue_display_window_never_overlaps_the_previous_cues_display():
    from blackvue.web.archive_browser import _cue_display_window

    # The previous cue's own display window ran until 9.0 - later than
    # this cue's lead-back-to-8.0 would otherwise start - so this cue's
    # display_start is pulled forward to 9.0 instead, guaranteeing the
    # two cues' display windows never visually overlap.
    assert _cue_display_window(10.0, 30.0, 9.0, "Hi.") == (9.0, 12.0)


# ---------------------------------------------------------------------------
# build_description_srt_from_events(signs=...) - Christer, same message as
# the trimming request above: "I would also like the signs be included
# both in the srt and the read aloud." Sign/plate reads carry their own
# real per-frame timestamps (see SignRead's docstring), so they merge into
# the sorted cue timeline as-is, without going through
# _rescale_events_to_duration() the way the (unreliable) description
# events do.
# ---------------------------------------------------------------------------


def test_build_description_srt_from_events_merges_signs_without_rescaling_them():
    from blackvue.generate.scene import DescriptionEvent
    from blackvue.web.archive_browser import SignRead, build_description_srt_from_events

    # These two description events force a real rescale (n=2,
    # duration=40.0 -> target_max=26.667, scale=0.5333): 50.0 lands at
    # 26.667, not verbatim. The sign read's own 5.0s timestamp must
    # come through completely untouched by that same scale factor -
    # landing between the two description events at its real position,
    # not at 5.0*0.5333=2.667.
    events = [
        DescriptionEvent(0.0, "first"),
        DescriptionEvent(50.0, "second - past duration"),
    ]
    signs = [SignRead(5.0, "STOP sign visible")]

    srt = build_description_srt_from_events(events, 40.0, signs=signs)

    # _apply_frame_sampling_lag() runs only on the description events,
    # after the rescale. With only one sign read, _apply_sign_lag()
    # leaves it unchanged (see that function's own docstring - a single
    # point has no meaningful "position" to interpolate from) - so the
    # sign's 5.0s timestamp only ever goes through _cue_display_window()'s
    # own lead/trail, applied per-cue to the whole merged timeline.
    assert srt is not None
    # Chronological order: first (0), the sign (5.0), then the rescaled
    # second event (26.667) - not the original list order. Values below
    # come from actually calling build_description_srt_from_events(),
    # not hand-derived.
    assert srt.index("first") < srt.index("STOP sign") < srt.index("second")
    assert "1\n00:00:01,750 --> 00:00:05,000\nfirst\n" in srt
    assert "2\n00:00:05,000 --> 00:00:07,417\nSTOP sign visible\n" in srt
    assert "3\n00:00:24,667 --> 00:00:28,667\nsecond - past duration\n" in srt


def test_build_description_srt_from_events_signs_only_with_no_description_events():
    from blackvue.web.archive_browser import SignRead, build_description_srt_from_events

    # A recording can have legible signs but no '## Description' events
    # at all (or an older scene.txt with only the old-format plain-
    # prose description, which build_description_srt_from_events()
    # never sees). This still produces a real, signs-only .srt - the
    # events-based path is tried whenever there's *either* events or
    # signs, not just events (see ArchiveRecording.description_srt()).
    signs = [SignRead(5.0, "YIELD sign visible")]

    srt = build_description_srt_from_events([], 30.0, signs=signs)

    assert srt is not None
    assert "YIELD sign visible" in srt


def test_build_description_srt_from_events_none_when_signs_and_events_both_empty():
    from blackvue.web.archive_browser import build_description_srt_from_events

    assert build_description_srt_from_events([], 30.0, signs=[]) is None
    assert build_description_srt_from_events([], 30.0, signs=None) is None


def test_description_srt_prefers_real_event_timestamps_when_available(tmp_path):
    archive = tmp_path / "archive"
    _write(archive, "20260715_140212_NF.mp4")
    _write(
        archive, "20260715_140212_N.scene.txt",
        content=(
            "## Description\n"
            "- [t=0.0s] Clear weather, light traffic on a two-lane road.\n"
            "- [t=12.4s] A red bus passes on the left.\n"
            "- [t=25.0s] The vehicle continues through an intersection.\n"
        ).encode("utf-8"),
    )
    _write(archive, "20260715_140212_N.duration.txt", content=b"40")

    recording = scan_archive(archive, "kirby")[0]

    srt = recording.description_srt("front")
    assert srt is not None
    # End-to-end version of the per-event-timestamps unit test above:
    # rescale, _apply_frame_sampling_lag()'s flat offset, and
    # _cue_display_window()'s lead/trail all apply, same as there -
    # in original list order, since _LAG_CORRECTION_CURVE's own
    # position-dependent term is currently flat/zero (no reordering).
    # Value from actually calling description_srt(), not hand-derived.
    assert "2\n00:00:16,630 --> 00:00:20,630\nA red bus passes on the left.\n" in srt
    assert "red bus" in srt

    # scene_summary's description text stays clean prose, with no
    # bracket notation ever surfacing on the page/in TTS narration -
    # Christer: "please keep the old output".
    [(_label, description, _reads)] = recording.scene_summary
    assert "[t=" not in description
    assert description == (
        "Clear weather, light traffic on a two-lane road. "
        "A red bus passes on the left. "
        "The vehicle continues through an intersection."
    )


def test_description_srt_merges_sign_reads_from_a_real_scene_txt(tmp_path):
    # End-to-end version of the signs= unit tests above: a real
    # scene.txt with both a '## Description' section (needs rescaling)
    # and a '## Zoomed sign reads' section (real per-frame timestamps,
    # not rescaled) - Christer: "I would also like the signs be
    # included both in the srt and the read aloud." This exercises
    # ArchiveRecording.description_srt()'s own signs-gathering
    # (_parse_sign_reads()) rather than calling
    # build_description_srt_from_events() directly.
    archive = tmp_path / "archive"
    _write(archive, "20260715_140212_NF.mp4")
    _write(
        archive, "20260715_140212_N.scene.txt",
        content=(
            "## Description\n"
            "- [t=0.0s] Clear weather, light traffic.\n"
            "- [t=25.0s] The vehicle continues through an intersection.\n"
            "## Zoomed sign reads\n"
            "- [t=12.4s] STOP sign visible\n"
        ).encode("utf-8"),
    )
    _write(archive, "20260715_140212_N.duration.txt", content=b"40")

    recording = scan_archive(archive, "kirby")[0]

    srt = recording.description_srt("front")
    assert srt is not None
    # Chronological order in the merged .srt: the description event at
    # 0.0, then the sign read, then the description event at 25.0 -
    # the sign uses SignRead.text (no "At N seconds" prefix), since the
    # cue's own timestamp already conveys "when" for the .srt.
    # _apply_frame_sampling_lag()'s flat offset nudges the description
    # cues (the curve's own term is flat/zero); the sign read (only one
    # in this recording) is left alone by _apply_sign_lag() (a single
    # point has no "position" to interpolate from); each cue's own
    # _cue_display_window() lead/trail then applies across the whole
    # merged timeline, sign included. Values from actually calling
    # description_srt(), not hand-derived.
    assert srt.index("Clear weather") < srt.index("STOP sign") < srt.index("vehicle continues")
    assert "1\n00:00:01,750 --> 00:00:05,750\nClear weather, light traffic.\n" in srt
    assert "2\n00:00:10,400 --> 00:00:14,400\nSTOP sign visible\n" in srt
    assert "3\n00:00:24,667 --> 00:00:29,500\nThe vehicle continues through an intersection.\n" in srt

    # The on-page sign-reads list still uses the natural-language
    # display_text ("At 12 seconds, ...") since scene_summary has no
    # other visual timing cue - only the .srt's own cue text drops the
    # "At N seconds" prefix.
    [(_label, _description, legible_reads)] = recording.scene_summary
    assert legible_reads == ["At 12 seconds, STOP sign visible"]


def test_description_srt_builds_signs_only_srt_when_no_description_section(tmp_path):
    # A recording can have legible signs but no '## Description'
    # section at all - still produces a real, signs-only .srt rather
    # than None (see build_description_srt_from_events()'s "or signs"
    # guard).
    archive = tmp_path / "archive"
    _write(archive, "20260715_140212_NF.mp4")
    _write(
        archive, "20260715_140212_N.scene.txt",
        content=(
            "## Zoomed sign reads\n"
            "- [t=5.0s] YIELD sign visible\n"
        ).encode("utf-8"),
    )
    _write(archive, "20260715_140212_N.duration.txt", content=b"30")

    recording = scan_archive(archive, "kirby")[0]

    srt = recording.description_srt("front")
    assert srt is not None
    assert "YIELD sign visible" in srt


# ---------------------------------------------------------------------------
# _label_rear_view() / rear-camera "Rear view:" prefix - Christer, on the
# frame-viewer report above ("frame 6 in srt talks about the bus but in
# zoomed frames it talk about the license plate" / "I dont se any red
# bus"), in the same message as the curve-reset and display-window
# requests: "If its rear camera frames, it would be nice if the
# description sad 'behind is/are' 'rear view'." The recording detail page
# only ever shows one video (normally front), so rear-camera text needs
# to say it's rear text on its own.
# ---------------------------------------------------------------------------


def test_label_rear_view_prefixes_only_the_rear_direction():
    from blackvue.web.archive_browser import _label_rear_view

    assert _label_rear_view("A red bus passes.", "rear") == "Rear view: A red bus passes."
    assert _label_rear_view("A red bus passes.", "Rear") == "Rear view: A red bus passes."
    assert _label_rear_view("A red bus passes.", "front") == "A red bus passes."
    assert _label_rear_view("", "rear") == ""


def test_description_srt_prefixes_rear_camera_description_and_signs(tmp_path):
    archive = tmp_path / "archive"
    _write(archive, "20260715_140212_NF.mp4")
    _write(
        archive, "20260715_140212_N.rear.scene.txt",
        content=(
            "## Description\n"
            "- [t=0.0s] A red car follows at a distance.\n"
            "## Zoomed sign reads\n"
            "- [t=5.0s] STOP sign visible\n"
        ).encode("utf-8"),
    )
    _write(archive, "20260715_140212_N.duration.txt", content=b"30")

    recording = scan_archive(archive, "kirby")[0]

    srt = recording.description_srt("rear")
    assert srt is not None
    assert "Rear view: A red car follows at a distance." in srt
    assert "Rear view: STOP sign visible" in srt

    # scene_summary's on-page description is prefixed the same way;
    # the front direction (none exists here) would be unaffected.
    [(label, description, _reads)] = recording.scene_summary
    assert label == "Rear"
    assert description == "Rear view: A red car follows at a distance."


def test_description_srt_builds_from_scene_text_and_cached_duration(tmp_path):
    # Old-format plain-prose '## Description' (no bulleted timestamps)
    # and no '## Zoomed sign reads' section at all - this exercises the
    # evenly-spaced fallback path, still exactly as before this feature
    # existed. (Deliberately NOT using _COMBINED_SCENE_TEXT here since
    # that fixture also has legible sign reads, which would now route
    # through the signs-only build_description_srt_from_events() path
    # instead of the fallback this test means to cover.)
    archive = tmp_path / "archive"
    _write(archive, "20260715_140212_NF.mp4")
    _write(
        archive, "20260715_140212_N.scene.txt",
        content=(
            "## Description\n"
            "A quiet residential street, clear weather, light traffic.\n"
        ).encode("utf-8"),
    )
    # Pre-seed the self-healing .duration.txt cache so this test stays
    # ffmpeg-free - load_or_compute_duration() reads this straight off
    # disk rather than probing the (fake, 1-byte) video file.
    _write(archive, "20260715_140212_N.duration.txt", content=b"180")

    recording = scan_archive(archive, "kirby")[0]

    srt = recording.description_srt("front")
    assert srt is not None
    assert "A quiet residential street, clear weather, light traffic." in srt
    assert "00:00:00,000 -->" in srt


def test_description_srt_matches_direction_case_insensitively(tmp_path):
    archive = tmp_path / "archive"
    _write(archive, "20260715_140212_NF.mp4")
    _write(
        archive, "20260715_140212_N.scene.txt",
        content=_COMBINED_SCENE_TEXT.encode("utf-8"),
    )
    _write(archive, "20260715_140212_N.duration.txt", content=b"180")

    recording = scan_archive(archive, "kirby")[0]

    assert recording.description_srt("Front") == recording.description_srt("front")


def test_description_srt_returns_a_signs_only_srt_when_direction_has_no_description(tmp_path):
    # _OCR_ONLY_SCENE_TEXT has legible sign reads but no '## Description'
    # section - since signs now merge into description.srt too (see
    # build_description_srt_from_events()'s signs= param), this must no
    # longer be None: it's a real signs-only .srt built from the one
    # legible read ("not legible" reads are still dropped as always).
    archive = tmp_path / "archive"
    _write(archive, "20260715_140212_NF.mp4")
    _write(
        archive, "20260715_140212_N.rear.scene.txt",
        content=_OCR_ONLY_SCENE_TEXT.encode("utf-8"),
    )
    _write(archive, "20260715_140212_N.duration.txt", content=b"180")

    recording = scan_archive(archive, "kirby")[0]

    srt = recording.description_srt("rear")
    assert srt is not None
    assert "MALL OF SCANDINAVIA" in srt
    assert "not legible" not in srt


def test_description_srt_none_when_direction_has_no_description_or_signs(tmp_path):
    # _ALL_NOT_LEGIBLE_SCENE_TEXT has neither a description section nor
    # any legible sign read - genuinely nothing to build a cue from.
    archive = tmp_path / "archive"
    _write(archive, "20260715_140212_NF.mp4")
    _write(
        archive, "20260715_140212_N.rear.scene.txt",
        content=_ALL_NOT_LEGIBLE_SCENE_TEXT.encode("utf-8"),
    )
    _write(archive, "20260715_140212_N.duration.txt", content=b"180")

    recording = scan_archive(archive, "kirby")[0]

    assert recording.description_srt("rear") is None


def test_description_srt_none_when_no_duration_available(tmp_path):
    archive = tmp_path / "archive"
    _write(archive, "20260715_140212_NF.mp4")
    _write(
        archive, "20260715_140212_N.scene.txt",
        content=_COMBINED_SCENE_TEXT.encode("utf-8"),
    )
    # No .duration.txt cache and the "video" is a fake 1-byte file, so
    # get_span()'s ffprobe/box-reader fallback will fail too - this must
    # come back None (turned into a 404 by app.py), not raise.

    recording = scan_archive(archive, "kirby")[0]

    assert recording.description_srt("front") is None


# ---------------------------------------------------------------------------
# first_valid_gps_fix()/last_valid_gps_fix() - added for the archive detail
# page's "Show Start and stop location" link (see app.py's
# archive_recording_location route). Fixture NMEA text mirrors
# tests/blackvue/telemetry/test_gps_reader.py's own - real read_gps()
# parsing is exercised end-to-end here, not mocked.
#
# Both functions now take (adapter, recording) rather than a bare .gps
# path (see adapters/telemetry_bridge.py's read_recording_gps() - this
# rewire threads every GPS/g-sensor read through the CameraAdapter
# abstraction, task #914) - each fixture below wraps its NMEA file in a
# real Recording/AssetFile and reads it via a real BlackVueAdapter(),
# whose read_gps() delegates unchanged to telemetry.gps_reader.read_gps().
# ---------------------------------------------------------------------------


def _gps_recording(path) -> Recording:
    return Recording(
        id=RecordingId("20260715_120000_N"),
        assets={Asset.GPS: AssetFile(Asset.GPS, path)},
    )


def test_first_valid_gps_fix_skips_leading_no_fix_sentences(tmp_path):
    path = tmp_path / "sample.gps"
    path.write_text(
        # Cold start: no fix yet (mode N).
        "[1700000000000]$GPRMC,120000.00,V,,,,,,,010124,,,N*7F\n"
        # Then a real position (mode A).
        "[1700000001000]$GPRMC,120001.00,A,4807.038,N,01131.000,E,"
        "10.00,45.00,010124,,,A*6D\n"
    )

    fix = first_valid_gps_fix(BlackVueAdapter(), _gps_recording(path))

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

    assert first_valid_gps_fix(BlackVueAdapter(), _gps_recording(path)) is None


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

    fix = last_valid_gps_fix(BlackVueAdapter(), _gps_recording(path))

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

    assert last_valid_gps_fix(BlackVueAdapter(), _gps_recording(path)) is None


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
