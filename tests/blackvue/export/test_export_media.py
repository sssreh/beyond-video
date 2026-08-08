import json
import subprocess

import pytest
from PIL import Image

from blackvue.export import media as media_module
from blackvue.export.media import change_playback_speed
from blackvue.export.media import check_readable
from blackvue.export.media import concatenate_media
from blackvue.export.media import encode_frame_sequence
from blackvue.export.media import encode_with_nvenc_fallback
from blackvue.export.media import generate_silence
from blackvue.export.media import mux_audio_track
from blackvue.export.media import trim_media
from blackvue.export.media import trim_media_head
from blackvue.generate.media import MediaToolError
from blackvue.generate.media import probe_audio_codec
from blackvue.generate.media import probe_audio_format


def _make_silent_audio(path, duration_seconds: float) -> None:
    subprocess.run(
        [
            "ffmpeg", "-y",
            "-f", "lavfi", "-i", f"anullsrc=r=8000:cl=mono",
            "-t", str(duration_seconds),
            str(path),
        ],
        capture_output=True,
        text=True,
        check=True,
    )


def _audio_duration_seconds(path) -> float:
    # generate.media.probe() assumes a video stream (-select_streams
    # v:0), which these audio-only fixtures don't have - query the
    # container duration directly instead.
    result = subprocess.run(
        [
            "ffprobe", "-v", "error",
            "-show_entries", "format=duration",
            "-of", "json",
            str(path),
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    return float(json.loads(result.stdout)["format"]["duration"])


def test_concatenate_media_joins_two_files_end_to_end(tmp_path):
    first = tmp_path / "first.aac"
    second = tmp_path / "second.aac"
    _make_silent_audio(first, 1.0)
    _make_silent_audio(second, 2.0)

    destination = tmp_path / "combined.aac"
    concatenate_media([first, second], destination)

    assert destination.exists()
    assert round(_audio_duration_seconds(destination)) == 3


def test_concatenate_media_does_nothing_for_empty_sources(tmp_path):
    destination = tmp_path / "combined.aac"

    concatenate_media([], destination)

    assert not destination.exists()


def test_concatenate_media_handles_a_single_source(tmp_path):
    first = tmp_path / "only.aac"
    _make_silent_audio(first, 1.0)

    destination = tmp_path / "combined.aac"
    concatenate_media([first], destination)

    assert destination.exists()
    assert round(_audio_duration_seconds(destination)) == 1


def test_concatenate_media_handles_paths_with_single_quotes(tmp_path):
    weird_dir = tmp_path / "trip's audio"
    weird_dir.mkdir()
    first = weird_dir / "clip.aac"
    _make_silent_audio(first, 1.0)

    destination = tmp_path / "combined.aac"
    concatenate_media([first], destination)

    assert destination.exists()


def test_check_readable_accepts_a_valid_video_file(tmp_path):
    video = tmp_path / "clip.mp4"
    subprocess.run(
        [
            "ffmpeg", "-y",
            "-f", "lavfi", "-i", "color=c=black:s=32x32:d=1",
            str(video),
        ],
        capture_output=True,
        text=True,
        check=True,
    )

    check_readable(video)  # should not raise


def test_check_readable_accepts_a_valid_audio_only_file(tmp_path):
    # This is the exact case generate.media.probe() gets wrong for -
    # it selects -select_streams v:0, which an audio-only file has
    # none of. check_readable() has to work for audio.aac sources too
    # (_concatenate_asset() uses it for all three of front/rear/audio).
    audio = tmp_path / "clip.aac"
    _make_silent_audio(audio, 1.0)

    check_readable(audio)  # should not raise


def test_check_readable_raises_for_a_truncated_moov_atom_file(tmp_path):
    # A real MP4 with its trailing moov atom cut off, reproducing the
    # "moov atom not found" failure Christer hit on a real export
    # (camera power loss mid-recording is the usual real-world cause).
    video = tmp_path / "clip.mp4"
    subprocess.run(
        [
            "ffmpeg", "-y",
            "-f", "lavfi", "-i", "color=c=black:s=32x32:d=1",
            str(video),
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    truncated = tmp_path / "truncated.mp4"
    truncated.write_bytes(video.read_bytes()[:2000])

    with pytest.raises(MediaToolError):
        check_readable(truncated)


def test_check_readable_raises_when_the_file_does_not_exist(tmp_path):
    with pytest.raises(MediaToolError):
        check_readable(tmp_path / "does_not_exist.mp4")


def _make_frames(frame_dir, count=2):
    frame_dir.mkdir(parents=True, exist_ok=True)
    for i in range(count):
        Image.new("RGB", (32, 32), (i * 40, 0, 0)).save(
            frame_dir / f"frame_{i:06d}.png"
        )


def test_nvenc_available_detects_h264_nvenc_in_the_encoder_list(monkeypatch):
    monkeypatch.setattr(media_module, "_NVENC_AVAILABLE", None)
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *_a, **_k: subprocess.CompletedProcess(
            [], 0, stdout="... h264_nvenc ...", stderr=""
        ),
    )

    assert media_module._nvenc_available() is True


def test_nvenc_available_returns_false_when_not_listed(monkeypatch):
    monkeypatch.setattr(media_module, "_NVENC_AVAILABLE", None)
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *_a, **_k: subprocess.CompletedProcess(
            [], 0, stdout="... libx264 ...", stderr=""
        ),
    )

    assert media_module._nvenc_available() is False


def test_nvenc_available_is_cached_after_the_first_call(monkeypatch):
    monkeypatch.setattr(media_module, "_NVENC_AVAILABLE", None)
    calls = []

    def fake_run(*args, **kwargs):
        calls.append(args)
        return subprocess.CompletedProcess([], 0, stdout="h264_nvenc", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)

    assert media_module._nvenc_available() is True
    assert media_module._nvenc_available() is True
    assert len(calls) == 1


def test_encode_frame_sequence_uses_libx264_directly_when_nvenc_unavailable(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(media_module, "_NVENC_AVAILABLE", False)
    captured = []

    def fake_encode(codec_args, input_args, destination):
        captured.append(codec_args)

    monkeypatch.setattr(media_module, "_run_ffmpeg_encode", fake_encode)

    encode_frame_sequence(tmp_path, tmp_path / "out.mp4", fps=5)

    # No bitrate was requested, so the default quality target
    # (_DEFAULT_LIBX264_QUALITY_ARGS) is applied instead of leaving it
    # to libx264's own internal default - see
    # encode_with_nvenc_fallback()'s own docstring for why.
    assert captured == [
        ["-c:v", "libx264", "-pix_fmt", "yuv420p", "-crf", "19"]
    ]


def test_encode_frame_sequence_tries_nvenc_first_when_available(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(media_module, "_NVENC_AVAILABLE", True)
    captured = []

    def fake_encode(codec_args, input_args, destination):
        captured.append(codec_args)

    monkeypatch.setattr(media_module, "_run_ffmpeg_encode", fake_encode)

    encode_frame_sequence(tmp_path, tmp_path / "out.mp4", fps=5)

    # Only the (successful) NVENC attempt - no CPU fallback needed. No
    # bitrate was requested, so the default quality target
    # (_DEFAULT_NVENC_QUALITY_ARGS) is applied instead of leaving it to
    # nvenc's own internal default - see encode_with_nvenc_fallback()'s
    # own docstring for why.
    assert captured == [
        [
            "-c:v", "h264_nvenc", "-pix_fmt", "yuv420p",
            "-rc", "vbr", "-cq", "19", "-b:v", "0",
        ]
    ]


def test_encode_frame_sequence_falls_back_to_libx264_when_nvenc_fails_for_real(
    tmp_path, monkeypatch
):
    # Force "NVENC is available" (this sandbox's ffmpeg build may or
    # may not actually list it) but let the real ffmpeg attempt run -
    # with no real NVIDIA GPU/driver here, the h264_nvenc attempt
    # genuinely fails, proving the fallback to libx264 isn't just
    # mocked but actually produces a working video.
    monkeypatch.setattr(media_module, "_NVENC_AVAILABLE", True)

    frame_dir = tmp_path / "frames"
    _make_frames(frame_dir)
    destination = tmp_path / "out.mp4"

    encode_frame_sequence(frame_dir, destination, fps=5)

    assert destination.exists()


def test_encode_frame_sequence_raises_when_the_cpu_encoder_also_fails(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(media_module, "_NVENC_AVAILABLE", False)

    # An empty frame_dir has no frame_%06d.png files for ffmpeg to
    # read, so even the libx264 fallback genuinely fails.
    empty_frame_dir = tmp_path / "empty"
    empty_frame_dir.mkdir()

    with pytest.raises(MediaToolError):
        encode_frame_sequence(empty_frame_dir, tmp_path / "out.mp4", fps=5)


def test_encode_with_nvenc_fallback_applies_default_quality_when_unspecified(
    tmp_path, monkeypatch
):
    # Regression test for a real problem Christer found on his own
    # archive: with no --stitch-bitrate given, nvenc's own unset
    # -b:v default landed at a visibly grainy ~1.9Mbps for a real
    # stitch.mp4 (vs. ~23Mbps for an earlier, differently-composited
    # stitch also run with no bitrate given) - not something safe to
    # leave to the encoder's own internal heuristic. No extra_codec_args
    # at all here (the "nothing requested" case every caller besides
    # stitch.py's --stitch-bitrate path is in).
    monkeypatch.setattr(media_module, "_NVENC_AVAILABLE", True)
    captured = []

    def fake_encode(codec_args, input_args, destination):
        captured.append(codec_args)

    monkeypatch.setattr(media_module, "_run_ffmpeg_encode", fake_encode)

    encode_with_nvenc_fallback(["-i", "in.mp4"], tmp_path / "out.mp4")

    assert captured == [
        [
            "-c:v", "h264_nvenc", "-pix_fmt", "yuv420p",
            "-rc", "vbr", "-cq", "19", "-b:v", "0",
        ]
    ]


def test_encode_with_nvenc_fallback_applies_default_quality_to_libx264_too(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(media_module, "_NVENC_AVAILABLE", False)
    captured = []

    def fake_encode(codec_args, input_args, destination):
        captured.append(codec_args)

    monkeypatch.setattr(media_module, "_run_ffmpeg_encode", fake_encode)

    encode_with_nvenc_fallback(["-i", "in.mp4"], tmp_path / "out.mp4")

    assert captured == [
        ["-c:v", "libx264", "-pix_fmt", "yuv420p", "-crf", "19"]
    ]


def test_encode_with_nvenc_fallback_skips_default_quality_when_caller_sets_bitrate(
    tmp_path, monkeypatch
):
    # An explicit --stitch-bitrate (via stitch.py's own _bitrate_args())
    # arrives here as "-b:v", "256k", "-maxrate", "256k", "-bufsize",
    # "256k" - the caller's own explicit rate control must win outright,
    # not get a competing -cq/-crf target stacked on top of it.
    monkeypatch.setattr(media_module, "_NVENC_AVAILABLE", True)
    captured = []

    def fake_encode(codec_args, input_args, destination):
        captured.append(codec_args)

    monkeypatch.setattr(media_module, "_run_ffmpeg_encode", fake_encode)

    encode_with_nvenc_fallback(
        ["-i", "in.mp4"],
        tmp_path / "out.mp4",
        extra_codec_args=["-b:v", "256k", "-maxrate", "256k", "-bufsize", "256k"],
    )

    assert captured == [
        [
            "-c:v", "h264_nvenc", "-pix_fmt", "yuv420p",
            "-b:v", "256k", "-maxrate", "256k", "-bufsize", "256k",
        ]
    ]


def test_encode_with_nvenc_fallback_default_quality_survives_a_real_encode(
    tmp_path, monkeypatch
):
    # Not mocked - lets the real ffmpeg/libx264 (this sandbox has no
    # NVIDIA GPU, so the nvenc attempt genuinely fails and falls
    # through) actually run with the new default -crf 19, confirming
    # it's a flag ffmpeg accepts and produces a real, playable file
    # from, not just a string this project's own code expects.
    monkeypatch.setattr(media_module, "_NVENC_AVAILABLE", False)

    frame_dir = tmp_path / "frames"
    _make_frames(frame_dir)
    destination = tmp_path / "out.mp4"

    encode_frame_sequence(frame_dir, destination, fps=5)

    assert destination.exists()
    assert destination.stat().st_size > 0


def _make_silent_video(path, duration_seconds: float) -> None:
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


def test_trim_media_shortens_a_real_video_via_stream_copy(tmp_path):
    source = tmp_path / "source.mp4"
    _make_silent_video(source, 5.0)

    destination = tmp_path / "trimmed.mp4"
    trim_media(source, destination, 2.0)

    assert destination.exists()
    # A plain stream copy can only cut on keyframe boundaries, so this
    # won't be exactly 2.0s - it should be noticeably shorter than the
    # 5.0s source and not wildly longer than what was asked for.
    trimmed_duration = _audio_duration_seconds(destination)
    assert trimmed_duration < 5.0
    assert trimmed_duration < 3.0


def test_trim_media_raises_when_the_source_does_not_exist(tmp_path):
    with pytest.raises(MediaToolError):
        trim_media(tmp_path / "missing.mp4", tmp_path / "out.mp4", 2.0)


def _make_video_with_frequent_keyframes(path, duration_seconds: float) -> None:
    """Like _make_silent_video(), but with a keyframe forced every
    real second instead of libx264's own default GOP (large enough
    that a short lavfi testsrc clip this length only ever gets one
    keyframe, at the very start - confirmed empirically). A stream-
    copy input-side seek (trim_media_head()'s own -ss before -i) can
    only land on a real keyframe, so a source with just one at t=0
    can never demonstrate an actual head trim - any -ss short of the
    clip's own end still seeks right back to frame 0."""

    subprocess.run(
        [
            "ffmpeg", "-y",
            "-f", "lavfi", "-i", "testsrc=size=64x64:rate=10",
            "-t", str(duration_seconds),
            "-g", "10",
            "-force_key_frames", "expr:gte(t,n_forced*1)",
            str(path),
        ],
        capture_output=True,
        text=True,
        check=True,
    )


def test_trim_media_head_shortens_a_real_video_via_stream_copy(tmp_path):
    source = tmp_path / "source.mp4"
    _make_video_with_frequent_keyframes(source, 5.0)

    destination = tmp_path / "trimmed.mp4"
    trim_media_head(source, destination, 2.0)

    assert destination.exists()
    # A stream-copy input-side seek snaps to the nearest keyframe at
    # or before the requested offset, so with a keyframe roughly every
    # 1s this won't be exactly 3.0s (5.0s source minus a 2.0s head
    # trim) - just noticeably shorter than the source and not longer
    # than the requested cut point would allow.
    trimmed_duration = _audio_duration_seconds(destination)
    assert trimmed_duration < 5.0
    assert trimmed_duration <= 3.5


def test_trim_media_head_raises_when_the_source_does_not_exist(tmp_path):
    with pytest.raises(MediaToolError):
        trim_media_head(tmp_path / "missing.mp4", tmp_path / "out.mp4", 2.0)


def test_change_playback_speed_shortens_video_when_sped_up(tmp_path):
    source = tmp_path / "source.mp4"
    _make_silent_video(source, 4.0)

    destination = tmp_path / "fast.mp4"
    change_playback_speed(source, destination, 2.0)

    assert destination.exists()
    sped_up_duration = _audio_duration_seconds(destination)
    # Real encode, not a stream copy, so this won't be exactly 2.0s -
    # just close to it and clearly shorter than the 4.0s source.
    assert 1.5 < sped_up_duration < 2.5


def test_change_playback_speed_lengthens_video_when_slowed_down(tmp_path):
    source = tmp_path / "source.mp4"
    _make_silent_video(source, 2.0)

    destination = tmp_path / "slow.mp4"
    change_playback_speed(source, destination, 0.5)

    assert destination.exists()
    slowed_duration = _audio_duration_seconds(destination)
    assert 3.5 < slowed_duration < 4.5


def test_change_playback_speed_drops_any_audio_track(tmp_path):
    source = tmp_path / "source.mp4"
    _make_video_with_audio(source, 2.0)

    destination = tmp_path / "fast.mp4"
    change_playback_speed(source, destination, 2.0)

    result = subprocess.run(
        [
            "ffprobe", "-v", "error",
            "-select_streams", "a",
            "-show_entries", "stream=index",
            "-of", "json",
            str(destination),
        ],
        capture_output=True, text=True, check=True,
    )
    assert json.loads(result.stdout)["streams"] == []


def test_change_playback_speed_raises_for_zero_speed(tmp_path):
    source = tmp_path / "source.mp4"
    _make_silent_video(source, 1.0)

    with pytest.raises(ValueError):
        change_playback_speed(source, tmp_path / "out.mp4", 0.0)


def test_change_playback_speed_raises_for_negative_speed(tmp_path):
    source = tmp_path / "source.mp4"
    _make_silent_video(source, 1.0)

    with pytest.raises(ValueError):
        change_playback_speed(source, tmp_path / "out.mp4", -1.0)


def test_change_playback_speed_raises_when_the_source_does_not_exist(tmp_path):
    with pytest.raises(MediaToolError):
        change_playback_speed(tmp_path / "missing.mp4", tmp_path / "out.mp4", 2.0)


def _make_video_with_audio(path, duration_seconds: float) -> None:
    """A real video+audio file - the shape a normal (non-Parking)
    BlackVue FRONT recording actually has (see concatenate_media()'s
    own docstring for why that matters: a repaired Parking FRONT
    recording, unlike this, is video-only)."""

    subprocess.run(
        [
            "ffmpeg", "-y",
            "-f", "lavfi", "-i", "testsrc=size=64x64:rate=10",
            "-f", "lavfi", "-i", "anullsrc=r=8000:cl=mono",
            "-t", str(duration_seconds),
            "-shortest",
            str(path),
        ],
        capture_output=True,
        text=True,
        check=True,
    )


def test_concatenate_media_strips_audio_when_video_only(tmp_path):
    # The fix for the real bug this whole feature exists for: mixing a
    # video-only source (a repaired Parking recording - see
    # concatenate_media()'s own docstring) in among video+audio
    # sources corrupts the concatenated output's own duration
    # metadata. video_only=True sidesteps it by never letting any
    # source's audio - present or not - reach the concat demuxer.
    first = tmp_path / "first.mp4"
    second = tmp_path / "second.mp4"
    _make_video_with_audio(first, 1.0)
    _make_video_with_audio(second, 1.0)

    destination = tmp_path / "combined.mp4"
    concatenate_media([first, second], destination, video_only=True)

    assert destination.exists()
    assert probe_audio_codec(destination) is None


def test_concatenate_media_video_only_normalizes_each_source_before_concat(
    tmp_path,
):
    # Regression test for the second, deeper fix: the first attempt at
    # video_only just appended -an to the final concat command, which
    # left every source's own file untouched (still 2 streams for an
    # ordinary video+audio source) - only the *output selection* was
    # restricted. On a real trip that didn't fix anything: front.mp4
    # came back with the exact same corrupted duration as before the
    # -an was ever added (see concatenate_media()'s own docstring for
    # the real numbers). video_only=True now strips audio from each
    # source individually first, so every file the concat demuxer ever
    # sees already has an identical, single-stream layout - the same
    # shape a video-only source (like a repaired Parking recording)
    # already has on its own. This can't reproduce the exact real
    # -world corruption (this sandbox's ffmpeg doesn't exhibit it even
    # against the pre-fix code), but it does confirm the mixed-layout
    # case - one video-only source sandwiched between two video+audio
    # ones - still concatenates to the correct total duration and
    # frame layout, not just "no audio in the output".
    first = tmp_path / "first.mp4"
    middle = tmp_path / "middle.mp4"
    last = tmp_path / "last.mp4"
    _make_video_with_audio(first, 1.0)
    _make_silent_video(middle, 1.0)
    _make_video_with_audio(last, 1.0)

    destination = tmp_path / "combined.mp4"
    concatenate_media([first, middle, last], destination, video_only=True)

    assert destination.exists()
    assert probe_audio_codec(destination) is None
    combined_duration = _audio_duration_seconds(destination)
    assert 2.5 < combined_duration < 3.5


def test_concatenate_media_keeps_audio_by_default(tmp_path):
    # Confirms video_only's default (False) preserves this project's
    # existing behavior - every other concatenate_media() caller
    # (REAR, AUDIO, and FRONT before this fix) still gets whatever
    # audio its sources carry.
    first = tmp_path / "first.mp4"
    _make_video_with_audio(first, 1.0)

    destination = tmp_path / "combined.mp4"
    concatenate_media([first], destination)

    assert destination.exists()
    assert probe_audio_codec(destination) is not None


def _video_packet_pts_times(path) -> list[float]:
    result = subprocess.run(
        [
            "ffprobe", "-v", "error",
            "-select_streams", "v:0",
            "-show_packets",
            "-show_entries", "packet=pts_time",
            "-of", "csv=p=0",
            str(path),
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    return sorted(
        float(line) for line in result.stdout.strip().splitlines()
        if line and line != "N/A"
    )


def _speed_change_freeze_regression_sources(tmp_path):
    # Shared setup for the two tests below - task #535, Christer
    # running --parking-speed 0.1 for real: "map, gps and sound good,
    # but video freeze after parking". Root cause: change_playback_
    # speed()'s re-encode can land on a different internal MP4
    # timescale than a source that was never re-encoded (libx264/
    # NVENC pick one based on the encoded stream's own effective frame
    # rate) - concatenating that against an unrelated-timescale source
    # via the concat demuxer's stream-copy path collapsed the *next*
    # segment's own real frames into a fractions-of-a-millisecond
    # sliver right at the transition, which looks exactly like the
    # video freezing (while audio/map/gsensor - built independently of
    # front.mp4/rear.mp4 - keep advancing normally, matching what
    # Christer saw). Reproducing this reliably needs: (1) the Parking
    # source's own *native* frame rate to differ from its neighbors'
    # - realistic, since BlackVue Parking mode commonly records at a
    # reduced timelapse rate unrelated to a normal recording's own fps
    # - and (2) a *third* segment after the sped one, matching the
    # real front.mp4/rear.mp4 shape (drive, park, drive) - a bare
    # two-segment concat didn't reproduce the corruption in this
    # sandbox's ffmpeg build, only the three-segment case did.
    before = tmp_path / "before.mp4"
    subprocess.run(
        [
            "ffmpeg", "-y",
            "-f", "lavfi", "-i", "testsrc=size=64x64:rate=30",
            "-t", "1",
            str(before),
        ],
        capture_output=True, text=True, check=True,
    )

    sped_source = tmp_path / "parking.mp4"
    subprocess.run(
        [
            "ffmpeg", "-y",
            "-f", "lavfi", "-i", "testsrc=size=64x64:rate=2",
            "-t", "6",
            str(sped_source),
        ],
        capture_output=True, text=True, check=True,
    )
    sped = tmp_path / "parking_sped.mp4"
    change_playback_speed(sped_source, sped, 0.1)

    after = tmp_path / "after.mp4"
    subprocess.run(
        [
            "ffmpeg", "-y",
            "-f", "lavfi", "-i", "testsrc=size=64x64:rate=30",
            "-t", "1",
            str(after),
        ],
        capture_output=True, text=True, check=True,
    )

    return before, sped, after


def _assert_no_collapsed_packets(destination) -> None:
    pts = _video_packet_pts_times(destination)
    diffs = [round(b - a, 6) for a, b in zip(pts, pts[1:])]
    # A real 30fps second of footage spread out normally has gaps
    # around 0.033s apart, never anywhere near zero - collapsed frames
    # (the freeze bug) show up as a long run of near-simultaneous
    # timestamps instead.
    assert not any(diff < 0.01 for diff in diffs), (
        f"found collapsed (near-simultaneous) packet timestamps: {diffs}"
    )


def test_concatenate_media_keeps_the_segment_after_a_speed_change_playable_video_only(
    tmp_path,
):
    # FRONT's own path (video_only=True).
    before, sped, after = _speed_change_freeze_regression_sources(tmp_path)
    destination = tmp_path / "combined.mp4"
    concatenate_media([before, sped, after], destination, video_only=True)
    _assert_no_collapsed_packets(destination)


def test_concatenate_media_keeps_the_segment_after_a_speed_change_playable_rear(
    tmp_path,
):
    # REAR's own path (video_only=False, the default) - confirms the
    # timescale-normalization fix isn't FRONT-only.
    before, sped, after = _speed_change_freeze_regression_sources(tmp_path)
    destination = tmp_path / "combined.mp4"
    concatenate_media([before, sped, after], destination)
    _assert_no_collapsed_packets(destination)


def _probe_duration_seconds(path) -> float:
    result = subprocess.run(
        [
            "ffprobe", "-v", "error",
            "-show_entries", "format=duration",
            "-of", "csv=p=0",
            str(path),
        ],
        capture_output=True, text=True, check=True,
    )
    return float(result.stdout.strip())


def _extract_frame(path, timestamp_seconds: float, destination) -> Image.Image:
    subprocess.run(
        [
            "ffmpeg", "-y",
            "-ss", str(timestamp_seconds),
            "-i", str(path),
            "-frames:v", "1",
            str(destination),
        ],
        capture_output=True, text=True, check=True,
    )
    return Image.open(destination).convert("RGB")


def test_concatenate_media_force_reencode_survives_mismatched_provenance(tmp_path):
    # task #536 - the task #535 timescale fix (see
    # _speed_change_freeze_regression_sources() above) turned out to
    # only cover the specific symptom it was diagnosed from. Christer,
    # after that fix shipped: "both --parking-speed 3 and
    # --parking-speed 0.1 freezes, not on first frame, but a few
    # frames in second video" - on front.mp4, rear.mp4, *and*
    # stitch.mp4. The real cause (see _concatenate_asset()'s own
    # docstring, task #234's original diagnosis) is that a stream-copy
    # concat needs every source's SPS/PPS/GOP/profile to already agree
    # - true for any two recordings straight off the same camera, not
    # true once one segment was re-encoded by change_playback_speed()
    # under a completely different encoder session. force_reencode=True
    # sidesteps the whole class of mismatch by decoding everything and
    # re-encoding as one continuous stream instead. Sources here are
    # built with deliberately incompatible profiles/B-frame settings
    # (baseline+no-B-frames vs. change_playback_speed()'s own libx264
    # defaults, which use High profile + B-frames) to exercise exactly
    # that mismatch - this sandbox's ffmpeg tolerates it even via plain
    # stream copy (see this module's own git history for the synthetic
    # repro that didn't reproduce the corruption), so this test can't
    # prove force_reencode=True fixes Christer's exact unreproducible
    # -here failure, only that the new code path itself produces a
    # correct, fully continuous, non-frozen result.
    before = tmp_path / "before.mp4"
    subprocess.run(
        [
            "ffmpeg", "-y",
            "-f", "lavfi", "-i", "testsrc=size=64x64:rate=30",
            "-t", "1",
            "-c:v", "libx264", "-profile:v", "baseline", "-bf", "0",
            "-pix_fmt", "yuv420p",
            str(before),
        ],
        capture_output=True, text=True, check=True,
    )

    sped_source = tmp_path / "parking.mp4"
    subprocess.run(
        [
            "ffmpeg", "-y",
            "-f", "lavfi", "-i", "testsrc2=size=64x64:rate=2",
            "-t", "1",
            "-c:v", "libx264", "-profile:v", "baseline", "-bf", "0",
            "-pix_fmt", "yuv420p",
            str(sped_source),
        ],
        capture_output=True, text=True, check=True,
    )
    # change_playback_speed() re-encodes via its own libx264/NVENC
    # defaults - High profile, B-frames on - deliberately different
    # from before/after's baseline/no-B-frames sources above.
    sped = tmp_path / "parking_sped.mp4"
    change_playback_speed(sped_source, sped, 0.2)

    after = tmp_path / "after.mp4"
    subprocess.run(
        [
            "ffmpeg", "-y",
            "-f", "lavfi", "-i", "mandelbrot=size=64x64:rate=30",
            "-t", "1",
            "-c:v", "libx264", "-profile:v", "baseline", "-bf", "0",
            "-pix_fmt", "yuv420p",
            str(after),
        ],
        capture_output=True, text=True, check=True,
    )

    # sped's own real duration, measured rather than assumed - a
    # source this short (1s at 2fps, only 2 real input frames) leaves
    # the trailing frame's own displayed length up to the encoder's
    # own guess, so "1s at 0.2x = 5s" doesn't land exactly.
    expected_total = (
        _probe_duration_seconds(before)
        + _probe_duration_seconds(sped)
        + _probe_duration_seconds(after)
    )

    destination = tmp_path / "combined.mp4"
    concatenate_media([before, sped, after], destination, force_reencode=True)

    # Total duration survives intact - not collapsed the way the
    # pre-task-#535 bug (or an unreconciled provenance mismatch) would
    # leave it.
    assert abs(_probe_duration_seconds(destination) - expected_total) < 1.0

    # No long collapsed-packet run.
    _assert_no_collapsed_packets(destination)

    # The tail (the "after" segment, the one Christer's report says
    # freezes) actually keeps changing frame-to-frame instead of being
    # stuck on a single held frame - sample 3 points in its last second
    # and confirm they're not all identical.
    tail_start = expected_total - 1.0
    frame_a = _extract_frame(destination, tail_start + 0.1, tmp_path / "frame_a.png")
    frame_b = _extract_frame(destination, tail_start + 0.5, tmp_path / "frame_b.png")
    frame_c = _extract_frame(destination, tail_start + 0.9, tmp_path / "frame_c.png")
    assert frame_a.tobytes() != frame_b.tobytes() or frame_b.tobytes() != frame_c.tobytes(), (
        "the 'after' segment's tail looks frozen - consecutive sampled "
        "frames are byte-identical"
    )


def test_mux_audio_track_combines_a_video_only_file_with_a_separate_audio_file(
    tmp_path,
):
    video = tmp_path / "video_only.mp4"
    _make_silent_video(video, 2.0)
    audio = tmp_path / "standalone.aac"
    _make_silent_audio(audio, 2.0)

    destination = tmp_path / "muxed.mp4"
    mux_audio_track(video, audio, destination)

    assert destination.exists()
    assert probe_audio_codec(destination) is not None


def test_mux_audio_track_raises_when_the_video_source_does_not_exist(tmp_path):
    audio = tmp_path / "standalone.aac"
    _make_silent_audio(audio, 1.0)

    with pytest.raises(MediaToolError):
        mux_audio_track(tmp_path / "missing.mp4", audio, tmp_path / "out.mp4")


def test_mux_audio_track_raises_when_the_audio_source_does_not_exist(tmp_path):
    video = tmp_path / "video_only.mp4"
    _make_silent_video(video, 1.0)

    with pytest.raises(MediaToolError):
        mux_audio_track(video, tmp_path / "missing.aac", tmp_path / "out.mp4")


def test_generate_silence_produces_a_clip_of_the_exact_requested_duration(
    tmp_path,
):
    destination = tmp_path / "silence.aac"

    generate_silence(destination, 3.0, sample_rate=8000, channels=1)

    assert destination.exists()
    assert probe_audio_codec(destination) == "aac"
    duration = _audio_duration_seconds(destination)
    # ffmpeg's own encoder framing means this won't be exact to the
    # millisecond - same tolerance trim_media()'s own duration test
    # already uses for the same reason.
    assert 2.9 < duration < 3.2


def test_generate_silence_matches_a_given_sample_rate_and_channel_count(
    tmp_path,
):
    destination = tmp_path / "silence.aac"

    generate_silence(destination, 1.0, sample_rate=16000, channels=2)

    assert probe_audio_format(destination) == (16000, 2)


def test_generate_silence_defaults_to_stereo_for_any_channel_count_other_than_one(
    tmp_path,
):
    # channels=1 -> mono is the only special case; anything else
    # (including an unexpected value) falls back to stereo rather than
    # erroring - see generate_silence()'s own docstring.
    destination = tmp_path / "silence.aac"

    generate_silence(destination, 1.0, sample_rate=8000, channels=3)

    assert probe_audio_format(destination) == (8000, 2)


def test_generate_silence_raises_when_ffmpeg_itself_is_missing(tmp_path, monkeypatch):
    def fake_run(*args, **kwargs):
        raise FileNotFoundError("no ffmpeg")

    monkeypatch.setattr(media_module.subprocess, "run", fake_run)

    with pytest.raises(MediaToolError):
        generate_silence(tmp_path / "silence.aac", 1.0)
