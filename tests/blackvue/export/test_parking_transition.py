import json
import subprocess

from PIL import Image

from blackvue.export.parking_transition import ParkingTransitionCache
from blackvue.export.parking_transition import PARKING_TRANSITION_DURATION_SECONDS
from blackvue.export.parking_transition import probe_audio_properties
from blackvue.export.parking_transition import probe_video_properties
from blackvue.export.parking_transition import render_parking_transition_image
from blackvue.export.parking_transition import render_parking_transition_silence
from blackvue.export.parking_transition import render_parking_transition_video


def _make_video(path, duration_seconds=0.5, size="64x48", rate=10) -> None:
    subprocess.run(
        [
            "ffmpeg", "-y",
            "-f", "lavfi",
            "-i", f"testsrc=size={size}:rate={rate}",
            "-t", str(duration_seconds),
            "-c:v", "libx264", "-pix_fmt", "yuv420p",
            str(path),
        ],
        capture_output=True,
        text=True,
        check=True,
    )


def _make_audio(path, duration_seconds=0.5, sample_rate=48000, channels=2) -> None:
    subprocess.run(
        [
            "ffmpeg", "-y",
            "-f", "lavfi",
            "-i", f"sine=frequency=440:duration={duration_seconds}",
            "-c:a", "aac", "-ar", str(sample_rate), "-ac", str(channels),
            str(path),
        ],
        capture_output=True,
        text=True,
        check=True,
    )


def _decoded_duration(path) -> float:
    """Real duration in seconds, decoded rather than read from the
    container's own metadata - see media.py's concatenate_media()
    docstring/test_trip_export.py's own note on why a raw AAC
    elementary stream's *reported* duration isn't trustworthy."""

    result = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "json", str(path)],
        capture_output=True, text=True, check=True,
    )
    return float(json.loads(result.stdout)["format"]["duration"])


def test_render_parking_transition_image_returns_the_requested_size():
    image = render_parking_transition_image(320, 180)

    assert image.size == (320, 180)
    assert image.mode == "RGB"


def test_render_parking_transition_image_is_not_blank():
    image = render_parking_transition_image(320, 180)

    # The icon/caption should paint something other than the flat
    # background color somewhere in the frame - a regression that
    # left the frame entirely blank (e.g. a font that failed to load
    # and drew nothing) would still pass a bare "returns an image"
    # check, so this confirms real content was actually drawn.
    colors = image.getcolors(maxcolors=320 * 180)
    assert colors is not None
    assert len(colors) > 1


def test_probe_video_properties_reads_width_height_frame_rate(tmp_path):
    video = tmp_path / "front.mp4"
    _make_video(video, size="64x48", rate=10)

    width, height, frame_rate = probe_video_properties(video)

    assert (width, height) == (64, 48)
    assert abs(frame_rate - 10.0) < 0.01


def test_probe_audio_properties_reads_codec_rate_channels(tmp_path):
    audio = tmp_path / "audio.aac"
    _make_audio(audio, sample_rate=44100, channels=1)

    properties = probe_audio_properties(audio)

    assert properties is not None
    codec_name, sample_rate, channels = properties
    assert codec_name == "aac"
    assert sample_rate == 44100
    assert channels == 1


def test_probe_audio_properties_returns_none_for_an_unreadable_file(tmp_path):
    not_audio = tmp_path / "not_audio.aac"
    not_audio.write_bytes(b"not a real audio file")

    assert probe_audio_properties(not_audio) is None


def test_render_parking_transition_video_matches_requested_dimensions(tmp_path):
    destination = tmp_path / "placeholder.mp4"

    render_parking_transition_video(
        destination, width=64, height=48, frame_rate=10.0, duration_seconds=1.0,
    )

    assert destination.exists()
    width, height, frame_rate = probe_video_properties(destination)
    assert (width, height) == (64, 48)
    assert abs(frame_rate - 10.0) < 0.5
    assert abs(_decoded_duration(destination) - 1.0) < 0.3


def test_render_parking_transition_video_uses_a_custom_image_when_given(tmp_path):
    custom_image = tmp_path / "custom.png"
    Image.new("RGB", (10, 10), (0, 0, 255)).save(custom_image)
    destination = tmp_path / "placeholder.mp4"

    render_parking_transition_video(
        destination, width=64, height=48, frame_rate=10.0,
        duration_seconds=1.0, image_path=custom_image,
    )

    assert destination.exists()
    width, height, _ = probe_video_properties(destination)
    # Fitted/padded to the requested size, not left at the source
    # image's own (much smaller) 10x10.
    assert (width, height) == (64, 48)


def test_render_parking_transition_video_uses_a_custom_clip_when_given(tmp_path):
    # A source clip with its own embedded audio track and a different
    # resolution/frame rate than the requested output - both should
    # get normalized away (audio stripped, video scaled/padded/re-
    # timed) rather than carried through.
    clip = tmp_path / "custom_clip.mp4"
    subprocess.run(
        [
            "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
            "-f", "lavfi", "-i", "testsrc=size=160x90:rate=24",
            "-f", "lavfi", "-i", "sine=frequency=440:duration=1",
            "-t", "1.0", "-shortest",
            "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac",
            str(clip),
        ],
        capture_output=True, text=True, check=True,
    )
    destination = tmp_path / "placeholder.mp4"

    render_parking_transition_video(
        destination, width=64, height=48, frame_rate=10.0,
        duration_seconds=1.0, clip_path=clip,
    )

    assert destination.exists()
    width, height, frame_rate = probe_video_properties(destination)
    assert (width, height) == (64, 48)
    assert abs(frame_rate - 10.0) < 0.5
    assert probe_audio_properties(destination) is None


def test_render_parking_transition_video_loops_a_clip_shorter_than_the_duration(
    tmp_path,
):
    short_clip = tmp_path / "short_clip.mp4"
    _make_video(short_clip, duration_seconds=0.5, size="64x48", rate=10)
    destination = tmp_path / "placeholder.mp4"

    render_parking_transition_video(
        destination, width=64, height=48, frame_rate=10.0,
        duration_seconds=2.0, clip_path=short_clip,
    )

    assert abs(_decoded_duration(destination) - 2.0) < 0.3


def test_render_parking_transition_video_trims_a_clip_longer_than_the_duration(
    tmp_path,
):
    long_clip = tmp_path / "long_clip.mp4"
    _make_video(long_clip, duration_seconds=3.0, size="64x48", rate=10)
    destination = tmp_path / "placeholder.mp4"

    render_parking_transition_video(
        destination, width=64, height=48, frame_rate=10.0,
        duration_seconds=1.0, clip_path=long_clip,
    )

    assert abs(_decoded_duration(destination) - 1.0) < 0.3


def test_render_parking_transition_video_clip_takes_priority_over_image(tmp_path):
    clip = tmp_path / "clip.mp4"
    _make_video(clip, duration_seconds=1.0, size="64x48", rate=10)
    image = tmp_path / "image.png"
    Image.new("RGB", (10, 10), (0, 255, 0)).save(image)
    destination = tmp_path / "placeholder.mp4"

    # Both given - clip_path should win, not raise, not silently use
    # the image instead (see this module's own docstring).
    render_parking_transition_video(
        destination, width=64, height=48, frame_rate=10.0,
        duration_seconds=1.0, image_path=image, clip_path=clip,
    )

    assert destination.exists()
    assert probe_video_properties(destination)[:2] == (64, 48)


def test_render_parking_transition_silence_matches_requested_properties(tmp_path):
    destination = tmp_path / "silence.aac"

    render_parking_transition_silence(
        destination, sample_rate=44100, channels=1, duration_seconds=1.0,
    )

    assert destination.exists()
    properties = probe_audio_properties(destination)
    assert properties is not None
    codec_name, sample_rate, channels = properties
    assert codec_name == "aac"
    assert sample_rate == 44100
    assert channels == 1
    assert abs(_decoded_duration(destination) - 1.0) < 0.3


def test_parking_transition_cache_reuses_a_video_for_matching_properties(tmp_path):
    work_dir = tmp_path / "work"
    work_dir.mkdir()
    cache = ParkingTransitionCache(work_dir=work_dir)

    source_a = tmp_path / "front_a.mp4"
    source_b = tmp_path / "front_b.mp4"
    _make_video(source_a, size="64x48", rate=10)
    _make_video(source_b, size="64x48", rate=10)

    placeholder_a = cache.video_for(source_a)
    placeholder_b = cache.video_for(source_b)

    assert placeholder_a == placeholder_b
    assert placeholder_a.exists()


def test_parking_transition_cache_renders_a_new_video_for_different_properties(
    tmp_path,
):
    work_dir = tmp_path / "work"
    work_dir.mkdir()
    cache = ParkingTransitionCache(work_dir=work_dir)

    small = tmp_path / "front_small.mp4"
    large = tmp_path / "front_large.mp4"
    _make_video(small, size="64x48", rate=10)
    _make_video(large, size="96x64", rate=10)

    placeholder_small = cache.video_for(small)
    placeholder_large = cache.video_for(large)

    assert placeholder_small != placeholder_large
    assert probe_video_properties(placeholder_small)[:2] == (64, 48)
    assert probe_video_properties(placeholder_large)[:2] == (96, 64)


def test_parking_transition_cache_reuses_silence_for_matching_properties(tmp_path):
    work_dir = tmp_path / "work"
    work_dir.mkdir()
    cache = ParkingTransitionCache(work_dir=work_dir)

    audio_a = tmp_path / "audio_a.aac"
    audio_b = tmp_path / "audio_b.aac"
    _make_audio(audio_a, sample_rate=48000, channels=2)
    _make_audio(audio_b, sample_rate=48000, channels=2)

    placeholder_a = cache.silence_for(audio_a)
    placeholder_b = cache.silence_for(audio_b)

    assert placeholder_a is not None
    assert placeholder_a == placeholder_b


def test_parking_transition_cache_silence_for_returns_none_for_bad_source(tmp_path):
    work_dir = tmp_path / "work"
    work_dir.mkdir()
    cache = ParkingTransitionCache(work_dir=work_dir)

    not_audio = tmp_path / "not_audio.aac"
    not_audio.write_bytes(b"not a real audio file")

    assert cache.silence_for(not_audio) is None


def test_parking_transition_cache_uses_a_custom_image_for_every_render(tmp_path):
    work_dir = tmp_path / "work"
    work_dir.mkdir()
    custom_image = tmp_path / "custom.png"
    Image.new("RGB", (10, 10), (0, 255, 0)).save(custom_image)
    cache = ParkingTransitionCache(work_dir=work_dir, image_path=custom_image)

    source = tmp_path / "front.mp4"
    _make_video(source, size="64x48", rate=10)

    placeholder = cache.video_for(source)

    assert placeholder.exists()
    assert probe_video_properties(placeholder)[:2] == (64, 48)


def test_parking_transition_cache_uses_a_custom_clip_for_every_render(tmp_path):
    work_dir = tmp_path / "work"
    work_dir.mkdir()
    custom_clip = tmp_path / "custom_clip.mp4"
    _make_video(custom_clip, duration_seconds=1.0, size="32x32", rate=10)
    cache = ParkingTransitionCache(work_dir=work_dir, clip_path=custom_clip)

    source = tmp_path / "front.mp4"
    _make_video(source, size="64x48", rate=10)

    placeholder = cache.video_for(source)

    assert placeholder.exists()
    assert probe_video_properties(placeholder)[:2] == (64, 48)
    assert probe_audio_properties(placeholder) is None


def test_parking_transition_cache_clip_path_takes_priority_over_image_path(tmp_path):
    work_dir = tmp_path / "work"
    work_dir.mkdir()
    custom_image = tmp_path / "image.png"
    Image.new("RGB", (10, 10), (255, 0, 0)).save(custom_image)
    custom_clip = tmp_path / "clip.mp4"
    _make_video(custom_clip, duration_seconds=1.0, size="32x32", rate=10)
    cache = ParkingTransitionCache(
        work_dir=work_dir, image_path=custom_image, clip_path=custom_clip
    )

    source = tmp_path / "front.mp4"
    _make_video(source, size="64x48", rate=10)

    placeholder = cache.video_for(source)

    # Doesn't raise, doesn't silently fall back to the image - both
    # given means the clip wins (see render_parking_transition_video()).
    assert placeholder.exists()


def test_parking_transition_duration_constant_is_three_seconds():
    # Christer: "a 3 second video" - pinned here so a future change to
    # the constant is a deliberate, visible edit to this test too.
    assert PARKING_TRANSITION_DURATION_SECONDS == 3.0
