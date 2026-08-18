"""
Tests for adapters/telemetry_bridge.py's recording_gps_available() -
the shared yes/no GPS probe cli/bv_ls.py's GPS column and bv-web's
archive detail page "Show start and stop location" link both now use
(see the function's own docstring in telemetry_bridge.py for the
history: cli/bv_ls.py had a private, more-thorough copy of this check
- real telemetry, falling through to a photo's EXIF GPS tag or a
video's own ISO 6709 container `location` tag whenever real telemetry
comes up with zero valid fixes, not just when there's no GPS source at
all - while bv-web's archive detail page link used a narrower
recording_has_gps() check that never fell through. Both callers now
share this one function instead of drifting apart again.

These tests exercise recording_gps_available() directly (not via
bv-ls's or bv-web's own CLI/route layers, which already have their own
coverage - cli/bv_ls.py's test_bv_ls_gps_column_* tests cover the same
fallback behavior end-to-end through the CLI) using real
FolderAdapter/GoProAdapter instances and real Recording objects from
open_archive(), the same fixture style test_folder_adapter.py and
test_gopro_adapter.py already use for real ffprobe/Pillow-backed
checks.
"""

import subprocess
from pathlib import Path

from PIL import Image

from blackvue.adapters.folder.adapter import FolderAdapter
from blackvue.adapters.gopro.adapter import GoProAdapter
from blackvue.adapters.telemetry_bridge import recording_gps_available

# EXIF GPS sub-IFD tag id - matches archive/exif.py's own private
# constant (see test_exif.py and test_bv_ls.py, which duplicate it the
# same way for the same reason: these tests would actually notice if
# the module started reading the wrong tag id).
_TAG_GPS_IFD = 34853


def _make_photo_with_gps(path: Path) -> None:
    image = Image.new("RGB", (100, 60), (200, 100, 50))
    exif = image.getexif()
    exif[_TAG_GPS_IFD] = {
        1: "N",
        2: (59.0, 17.0, 34.0),
        3: "E",
        4: (18.0, 5.0, 17.0),
    }
    image.save(path, exif=exif)


def _make_video_with_location(path: Path, location: str = "+05.0448-073.7965/") -> None:
    subprocess.run(
        [
            "ffmpeg", "-y",
            "-f", "lavfi", "-i", "testsrc=size=64x64:rate=10",
            "-t", "1",
            "-c:v", "libx264",
            "-metadata", f"location={location}",
            str(path),
        ],
        capture_output=True,
        text=True,
        check=True,
    )


def _make_plain_video(path: Path) -> None:
    subprocess.run(
        [
            "ffmpeg", "-y",
            "-f", "lavfi", "-i", "testsrc=size=64x64:rate=10",
            "-t", "1",
            "-c:v", "libx264",
            str(path),
        ],
        capture_output=True,
        text=True,
        check=True,
    )


def test_true_for_a_photo_with_real_exif_gps(tmp_path):
    # FolderAdapter never declares gps support at all (manifest.json's
    # capabilities.gps is False) - recording_has_gps() alone would say
    # False here, but the photo's own EXIF GPS tag is a real, usable
    # fix. This is exactly the case bv-web's archive detail page link
    # used to miss before it switched from recording_has_gps() to this
    # function (task #999).
    photo = tmp_path / "beach.jpg"
    _make_photo_with_gps(photo)

    adapter = FolderAdapter()
    recording = adapter.open_archive(tmp_path).recordings[0]

    assert recording_gps_available(adapter, recording) is True


def test_true_for_a_video_with_a_container_location_tag(tmp_path):
    # Same folder-adapter "no GPS source at all" case, but the real
    # report that motivated this fallback in the first place: a
    # stock/downloaded clip mixed into an archive with an ISO 6709
    # `location` container tag and no sidecar of any kind.
    video = tmp_path / "clip.mp4"
    _make_video_with_location(video)

    adapter = FolderAdapter()
    recording = adapter.open_archive(tmp_path).recordings[0]

    assert recording_gps_available(adapter, recording) is True


def test_true_for_a_gopro_clip_with_no_real_gpmf_track(tmp_path):
    # GoProAdapter's manifest declares real gps support
    # (gps_source_asset="FRONT"), so recording_has_gps() is True for
    # any recording with a FRONT file - but a stock/downloaded clip
    # mixed into a GoPro archive (Christer's real case, see
    # container_gps.py's own module docstring) has no real GPMF
    # stream: adapter.read_gps() raises MediaToolError, caught by
    # read_recording_gps() as "no fixes". recording_gps_available()
    # must still fall through to the container-tag fallback here
    # rather than stopping at "a GPS source exists" - the exact gap
    # both cli/bv_ls.py's GPS column (task #974-977) and this route
    # (task #998-999) needed fixed.
    video = tmp_path / "clip.mp4"
    _make_video_with_location(video)

    adapter = GoProAdapter()
    recording = adapter.open_archive(tmp_path).recordings[0]

    assert recording_gps_available(adapter, recording) is True


def test_false_when_no_gps_data_exists_anywhere(tmp_path):
    # A plain video with no EXIF, no container location tag, and no
    # real telemetry source - the ordinary case for most recordings.
    video = tmp_path / "clip.mp4"
    _make_plain_video(video)

    adapter = FolderAdapter()
    recording = adapter.open_archive(tmp_path).recordings[0]

    assert recording_gps_available(adapter, recording) is False


def test_false_for_a_plain_photo_with_no_exif_gps(tmp_path):
    # A photo can have EXIF data but no GPS sub-IFD at all - must not
    # be confused with "has GPS".
    photo = tmp_path / "no_gps.jpg"
    Image.new("RGB", (100, 60), (10, 20, 30)).save(photo)

    adapter = FolderAdapter()
    recording = adapter.open_archive(tmp_path).recordings[0]

    assert recording_gps_available(adapter, recording) is False
