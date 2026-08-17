"""
Tests for archive/photo.py - is_photo_path()/recording_is_photo() and
the shared PHOTO_EXTENSIONS/DEFAULT_PHOTO_DURATION_SECONDS constants.
"""

from pathlib import Path

from blackvue.archive.asset import Asset
from blackvue.archive.asset_file import AssetFile
from blackvue.archive.photo import DEFAULT_PHOTO_DURATION_SECONDS
from blackvue.archive.photo import PHOTO_EXTENSIONS
from blackvue.archive.photo import is_photo_path
from blackvue.archive.photo import recording_is_photo
from blackvue.archive.recording import Recording
from blackvue.archive.recording_id import RecordingId


def test_default_photo_duration_is_five_seconds():
    assert DEFAULT_PHOTO_DURATION_SECONDS == 5


def test_all_four_answered_extensions_are_recognized():
    # Christer's own answer when asked which extensions should count:
    # "All of them" - jpg/jpeg, png, heic, gpr.
    for suffix in (".jpg", ".jpeg", ".png", ".heic", ".gpr"):
        assert is_photo_path(Path(f"clip{suffix}")) is True


def test_extension_match_is_case_insensitive():
    assert is_photo_path(Path("IMG_0001.JPG")) is True
    assert is_photo_path(Path("photo.HEIC")) is True


def test_video_extension_is_not_a_photo():
    assert is_photo_path(Path("clip.mp4")) is False
    assert is_photo_path(Path("clip.MOV")) is False


def test_unrelated_extension_is_not_a_photo():
    assert is_photo_path(Path("notes.txt")) is False


def test_photo_extensions_constant_matches_is_photo_path():
    for suffix in PHOTO_EXTENSIONS:
        assert is_photo_path(Path(f"x{suffix}")) is True


def test_recording_is_photo_true_for_photo_front():
    recording = Recording(id=RecordingId("20260715_133255_V"))
    recording.assets[Asset.FRONT] = AssetFile(
        asset=Asset.FRONT, path=Path("/archive/GH010023.JPG")
    )

    assert recording_is_photo(recording) is True


def test_recording_is_photo_false_for_video_front():
    recording = Recording(id=RecordingId("20260715_133255_V"))
    recording.assets[Asset.FRONT] = AssetFile(
        asset=Asset.FRONT, path=Path("/archive/GH010023.MP4")
    )

    assert recording_is_photo(recording) is False


def test_recording_is_photo_false_without_any_front_asset():
    recording = Recording(id=RecordingId("20260715_133255_V"))

    assert recording_is_photo(recording) is False
