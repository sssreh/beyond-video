"""
Tests for archive/photo.py - is_photo_path()/recording_is_photo() and
the shared PHOTO_EXTENSIONS/GIF_EXTENSIONS/DEFAULT_PHOTO_DURATION_SECONDS
constants.

The GIF tests below (task #950-959: Christer, "how do you define a gif
file, a picture or a silent video?") use real, ffmpeg-readable GIF
fixtures rather than junk bytes - count_gif_frames() shells out to
ffprobe, so a fixture needs real GIF-encoded pixel data for the
animated-vs-static distinction to mean anything.
"""

from pathlib import Path

from PIL import Image

from blackvue.archive.asset import Asset
from blackvue.archive.asset_file import AssetFile
from blackvue.archive.photo import DEFAULT_PHOTO_DURATION_SECONDS
from blackvue.archive.photo import GIF_EXTENSIONS
from blackvue.archive.photo import PHOTO_EXTENSIONS
from blackvue.archive.photo import count_gif_frames
from blackvue.archive.photo import is_gif_path
from blackvue.archive.photo import is_photo_path
from blackvue.archive.photo import recording_is_photo
from blackvue.archive.recording import Recording
from blackvue.archive.recording_id import RecordingId


def _make_static_gif(path: Path) -> None:
    Image.new("RGB", (64, 48), (10, 20, 30)).save(path)


def _make_animated_gif(path: Path, frame_count: int = 3) -> None:
    frames = [
        Image.new("RGB", (64, 48), (i * 50, 0, 0)) for i in range(frame_count)
    ]
    frames[0].save(
        path, save_all=True, append_images=frames[1:], duration=100, loop=0
    )


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


# ---------------------------------------------------------------------------
# GIF classification - Christer: "how do you define a gif file, a picture or
# a silent video?" Answer: it depends on the gif - a static, single-frame
# GIF is a photo; an animated one is really a silent video (it already has
# its own real per-frame timing). .gif is deliberately outside
# PHOTO_EXTENSIONS (extension alone can't tell the two apart) and gets its
# own GIF_EXTENSIONS set plus a frame-count check in recording_is_photo().
# ---------------------------------------------------------------------------


def test_gif_extension_is_not_in_photo_extensions():
    assert ".gif" not in PHOTO_EXTENSIONS
    assert is_photo_path(Path("clip.gif")) is False


def test_gif_extension_is_recognized_by_is_gif_path():
    assert is_gif_path(Path("clip.gif")) is True
    assert is_gif_path(Path("clip.GIF")) is True
    assert is_gif_path(Path("clip.jpg")) is False


def test_gif_extensions_constant_contains_only_gif():
    assert GIF_EXTENSIONS == frozenset({".gif"})


def test_count_gif_frames_returns_one_for_a_static_gif(tmp_path):
    path = tmp_path / "static.gif"
    _make_static_gif(path)

    assert count_gif_frames(path) == 1


def test_count_gif_frames_returns_more_than_one_for_an_animated_gif(tmp_path):
    path = tmp_path / "animated.gif"
    _make_animated_gif(path, frame_count=3)

    assert count_gif_frames(path) == 3


def test_count_gif_frames_returns_none_for_an_unreadable_file(tmp_path):
    path = tmp_path / "not_really_a_gif.gif"
    path.write_bytes(b"this is not a gif")

    assert count_gif_frames(path) is None


def test_recording_is_photo_true_for_a_static_gif(tmp_path):
    path = tmp_path / "static.gif"
    _make_static_gif(path)

    recording = Recording(id=RecordingId("20260715_133255_V"))
    recording.assets[Asset.FRONT] = AssetFile(asset=Asset.FRONT, path=path)

    assert recording_is_photo(recording) is True


def test_recording_is_photo_false_for_an_animated_gif(tmp_path):
    # An animated GIF is treated as an ordinary silent video, not a
    # photo - it already has real per-frame timing, nothing to hold
    # for a fixed --photo-duration.
    path = tmp_path / "animated.gif"
    _make_animated_gif(path, frame_count=3)

    recording = Recording(id=RecordingId("20260715_133255_V"))
    recording.assets[Asset.FRONT] = AssetFile(asset=Asset.FRONT, path=path)

    assert recording_is_photo(recording) is False


def test_recording_is_photo_false_for_an_unreadable_gif(tmp_path):
    # An unreadable/corrupt .gif can't be confirmed as a static single
    # frame, so it's left to flow through the ordinary video pipeline
    # (which already degrades gracefully on a genuinely bad source)
    # rather than risk mis-classifying it as a photo.
    path = tmp_path / "corrupt.gif"
    path.write_bytes(b"not a gif at all")

    recording = Recording(id=RecordingId("20260715_133255_V"))
    recording.assets[Asset.FRONT] = AssetFile(asset=Asset.FRONT, path=path)

    assert recording_is_photo(recording) is False
