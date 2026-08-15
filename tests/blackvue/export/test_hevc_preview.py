import hashlib

import pytest

from blackvue.export import hevc_preview as hevc_preview_module
from blackvue.export.hevc_preview import load_or_transcode_hevc_preview
from blackvue.generate.media import MediaToolError


def _make_source(tmp_path, name="20260715_140212_NF.mp4", content=b"video bytes"):
    source = tmp_path / name
    source.write_bytes(content)
    return source


def _expected_cache_path(source, cache_dir):
    stat = source.stat()
    digest = hashlib.sha256(str(source.resolve()).encode("utf-8")).hexdigest()[:16]
    return cache_dir / f"{digest}-{stat.st_mtime_ns}-{stat.st_size}.mp4"


def test_nvdec_available_checks_ffmpeg_hwaccels_output(monkeypatch):
    """Mirrors stitch.py's own test_nvdec_available_checks_ffmpeg_hwaccels_output()
    - this module keeps its own private probe rather than importing
    stitch.py's (see hevc_preview.py's own comment on why), so it gets
    its own copy of this test too."""

    monkeypatch.setattr(hevc_preview_module, "_NVDEC_AVAILABLE", None)

    captured = {}

    class FakeResult:
        stdout = "Hardware acceleration methods:\ncuda\nqsv\n"

    def fake_run(command, **kwargs):
        captured["command"] = command
        return FakeResult()

    monkeypatch.setattr(hevc_preview_module.subprocess, "run", fake_run)

    assert hevc_preview_module._nvdec_available() is True
    assert captured["command"] == ["ffmpeg", "-hide_banner", "-hwaccels"]

    # Cached after the first call - a second call shouldn't shell out
    # again.
    captured.clear()
    assert hevc_preview_module._nvdec_available() is True
    assert captured == {}


def test_returns_source_unchanged_when_not_hevc(monkeypatch, tmp_path):
    source = _make_source(tmp_path)
    cache_dir = tmp_path / "cache"

    monkeypatch.setattr(hevc_preview_module, "probe_video_codec", lambda _path: "h264")

    def fail_encode(*_args, **_kwargs):
        raise AssertionError("should not attempt to transcode a non-HEVC source")

    monkeypatch.setattr(hevc_preview_module, "encode_with_nvenc_fallback", fail_encode)

    result = load_or_transcode_hevc_preview(source, cache_dir)

    assert result == source
    assert not cache_dir.exists()


def test_returns_source_unchanged_when_no_video_stream(monkeypatch, tmp_path):
    source = _make_source(tmp_path)
    cache_dir = tmp_path / "cache"

    monkeypatch.setattr(hevc_preview_module, "probe_video_codec", lambda _path: None)

    result = load_or_transcode_hevc_preview(source, cache_dir)

    assert result == source
    assert not cache_dir.exists()


def test_returns_source_unchanged_when_codec_probe_fails(monkeypatch, tmp_path):
    source = _make_source(tmp_path)
    cache_dir = tmp_path / "cache"

    def fake_probe(_path):
        raise MediaToolError("ffprobe not found on PATH")

    monkeypatch.setattr(hevc_preview_module, "probe_video_codec", fake_probe)

    result = load_or_transcode_hevc_preview(source, cache_dir)

    assert result == source


def test_transcodes_hevc_source_and_caches_it(monkeypatch, tmp_path):
    source = _make_source(tmp_path)
    cache_dir = tmp_path / "cache"

    monkeypatch.setattr(hevc_preview_module, "probe_video_codec", lambda _path: "hevc")
    monkeypatch.setattr(hevc_preview_module, "_nvdec_available", lambda: False)

    calls = []

    def fake_encode(input_args, destination, extra_codec_args=None):
        calls.append((input_args, destination, extra_codec_args))
        destination.write_bytes(b"transcoded")

    monkeypatch.setattr(hevc_preview_module, "encode_with_nvenc_fallback", fake_encode)

    result = load_or_transcode_hevc_preview(source, cache_dir)

    expected_cache_path = _expected_cache_path(source, cache_dir)
    assert result == expected_cache_path
    assert result.is_file()
    assert result.read_bytes() == b"transcoded"
    assert len(calls) == 1
    input_args, destination, extra_codec_args = calls[0]
    # _nvdec_available() is mocked False above, so this is the plain
    # CPU-decode input args - NVDEC's own extra flags are covered by
    # test_transcodes_hevc_source_using_nvdec_decode_when_available()
    # and test_falls_back_to_cpu_decode_when_nvdec_decode_fails() below.
    assert input_args == ["-i", str(source)]
    # Encodes into a private temp file, not the final cache path
    # directly - only renamed into place once the encode finishes (see
    # the function's own docstring on why: avoiding a corrupted cache
    # entry from concurrent/interrupted transcodes).
    assert destination != expected_cache_path
    assert destination.parent == cache_dir
    assert destination.name.startswith(expected_cache_path.stem)
    assert destination.suffix == ".tmp"
    assert not destination.exists()  # renamed away by the time we check
    assert extra_codec_args == [
        "-preset", "fast",
        "-c:a", "copy",
        "-movflags", "+faststart",
        "-b:v", "8M",
        "-maxrate", "8M",
        "-bufsize", "8M",
        "-f", "mp4",
    ]


def test_transcodes_hevc_source_using_nvdec_decode_when_available(monkeypatch, tmp_path):
    source = _make_source(tmp_path)
    cache_dir = tmp_path / "cache"

    monkeypatch.setattr(hevc_preview_module, "probe_video_codec", lambda _path: "hevc")
    monkeypatch.setattr(hevc_preview_module, "_nvdec_available", lambda: True)

    calls = []

    def fake_encode(input_args, destination, extra_codec_args=None):
        calls.append(input_args)
        destination.write_bytes(b"transcoded")

    monkeypatch.setattr(hevc_preview_module, "encode_with_nvenc_fallback", fake_encode)

    load_or_transcode_hevc_preview(source, cache_dir)

    assert len(calls) == 1
    assert calls[0] == [
        "-init_hw_device", f"cuda={hevc_preview_module._HW_DEVICE_NAME}:0",
        "-hwaccel", "cuda",
        "-hwaccel_device", hevc_preview_module._HW_DEVICE_NAME,
        "-hwaccel_output_format", "cuda",
        "-i", str(source),
        "-vf", "hwdownload,format=nv12",
    ]


def test_falls_back_to_cpu_decode_when_nvdec_decode_fails(monkeypatch, tmp_path):
    """Christer's own machine is real hardware I can't test against
    directly - a bad NVDEC attempt (unsupported profile, driver
    hiccup, etc.) must degrade to the already-working plain CPU decode
    path rather than breaking the preview outright."""

    source = _make_source(tmp_path)
    cache_dir = tmp_path / "cache"

    monkeypatch.setattr(hevc_preview_module, "probe_video_codec", lambda _path: "hevc")
    monkeypatch.setattr(hevc_preview_module, "_nvdec_available", lambda: True)

    calls = []

    def fake_encode(input_args, destination, extra_codec_args=None):
        calls.append(input_args)
        if len(calls) == 1:
            raise MediaToolError("ffmpeg: nvdec decode failed")
        destination.write_bytes(b"transcoded")

    monkeypatch.setattr(hevc_preview_module, "encode_with_nvenc_fallback", fake_encode)

    result = load_or_transcode_hevc_preview(source, cache_dir)

    assert result.is_file()
    assert result.read_bytes() == b"transcoded"
    assert len(calls) == 2
    assert "-hwaccel" in calls[0]  # first attempt: NVDEC decode
    assert calls[1] == ["-i", str(source)]  # second attempt: plain CPU decode


def test_returns_source_unchanged_when_both_nvdec_and_cpu_decode_fail(monkeypatch, tmp_path):
    source = _make_source(tmp_path)
    cache_dir = tmp_path / "cache"

    monkeypatch.setattr(hevc_preview_module, "probe_video_codec", lambda _path: "hevc")
    monkeypatch.setattr(hevc_preview_module, "_nvdec_available", lambda: True)

    calls = []

    def fake_encode(input_args, destination, extra_codec_args=None):
        calls.append(input_args)
        raise MediaToolError("ffmpeg encode failed")

    monkeypatch.setattr(hevc_preview_module, "encode_with_nvenc_fallback", fake_encode)

    result = load_or_transcode_hevc_preview(source, cache_dir)

    assert result == source
    assert len(calls) == 2


def test_reuses_cached_copy_without_transcoding_again(monkeypatch, tmp_path):
    source = _make_source(tmp_path)
    cache_dir = tmp_path / "cache"
    cache_dir.mkdir()

    expected_cache_path = _expected_cache_path(source, cache_dir)
    expected_cache_path.write_bytes(b"already transcoded")

    monkeypatch.setattr(hevc_preview_module, "probe_video_codec", lambda _path: "hevc")

    def fail_encode(*_args, **_kwargs):
        raise AssertionError("should reuse the cached copy, not transcode again")

    monkeypatch.setattr(hevc_preview_module, "encode_with_nvenc_fallback", fail_encode)

    result = load_or_transcode_hevc_preview(source, cache_dir)

    assert result == expected_cache_path


def test_returns_source_unchanged_when_encode_fails(monkeypatch, tmp_path):
    source = _make_source(tmp_path)
    cache_dir = tmp_path / "cache"

    monkeypatch.setattr(hevc_preview_module, "probe_video_codec", lambda _path: "hevc")
    monkeypatch.setattr(hevc_preview_module, "_nvdec_available", lambda: False)

    def fake_encode(*_args, **_kwargs):
        raise MediaToolError("ffmpeg encode failed")

    monkeypatch.setattr(hevc_preview_module, "encode_with_nvenc_fallback", fake_encode)

    result = load_or_transcode_hevc_preview(source, cache_dir)

    assert result == source


def test_encode_failure_leaves_no_stray_temp_file_behind(monkeypatch, tmp_path):
    """Christer hit exactly the corruption this guards against: a
    transcode that got interrupted (or raced by an overlapping browser
    request) left a broken file sitting at the cache path, and it just
    kept getting served - audio-only-again - until he noticed and
    deleted it by hand. A failed/partial encode should leave the cache
    directory clean, not a half-written .tmp file that could later be
    mistaken for something worth keeping."""

    source = _make_source(tmp_path)
    cache_dir = tmp_path / "cache"

    monkeypatch.setattr(hevc_preview_module, "probe_video_codec", lambda _path: "hevc")
    monkeypatch.setattr(hevc_preview_module, "_nvdec_available", lambda: False)

    def fake_encode(_input_args, destination, extra_codec_args=None):
        # Mimic ffmpeg's real behavior: the output file gets created/
        # truncated before the command fails.
        destination.write_bytes(b"")
        raise MediaToolError("ffmpeg encode failed")

    monkeypatch.setattr(hevc_preview_module, "encode_with_nvenc_fallback", fake_encode)

    result = load_or_transcode_hevc_preview(source, cache_dir)

    assert result == source
    assert list(cache_dir.iterdir()) == []


def test_enforces_the_cache_size_cap_after_a_successful_transcode(monkeypatch, tmp_path):
    """Christer, after the bitrate cap already shrank individual
    previews to ~10% of their prior size: the cache directory itself
    still "needed to be purged after a while." Confirms
    load_or_transcode_hevc_preview() actually calls the shared
    eviction helper (see cache_utils.enforce_cache_size_cap()) with
    this cache's own directory - the eviction *policy* itself is
    covered by test_cache_utils.py, not re-tested here."""

    source = _make_source(tmp_path)
    cache_dir = tmp_path / "cache"

    monkeypatch.setattr(hevc_preview_module, "probe_video_codec", lambda _path: "hevc")
    monkeypatch.setattr(hevc_preview_module, "_nvdec_available", lambda: False)

    def fake_encode(_input_args, destination, extra_codec_args=None):
        destination.write_bytes(b"transcoded")

    monkeypatch.setattr(hevc_preview_module, "encode_with_nvenc_fallback", fake_encode)

    calls = []
    monkeypatch.setattr(
        hevc_preview_module,
        "enforce_cache_size_cap",
        lambda cache_dir_arg, max_bytes: calls.append((cache_dir_arg, max_bytes)),
    )

    load_or_transcode_hevc_preview(source, cache_dir)

    assert calls == [(cache_dir, hevc_preview_module._MAX_CACHE_BYTES)]


def test_does_not_enforce_the_cache_size_cap_on_a_cache_hit(monkeypatch, tmp_path):
    """A cache hit does no new I/O at all - re-sweeping the whole
    directory on every single playback request would be wasteful, and
    unnecessary: the cap is already enforced, since the only way an
    entry could exist is a prior write that already ran the sweep."""

    source = _make_source(tmp_path)
    cache_dir = tmp_path / "cache"
    cache_dir.mkdir()
    expected_cache_path = _expected_cache_path(source, cache_dir)
    expected_cache_path.write_bytes(b"already transcoded")

    monkeypatch.setattr(hevc_preview_module, "probe_video_codec", lambda _path: "hevc")

    calls = []
    monkeypatch.setattr(
        hevc_preview_module,
        "enforce_cache_size_cap",
        lambda cache_dir_arg, max_bytes: calls.append((cache_dir_arg, max_bytes)),
    )

    load_or_transcode_hevc_preview(source, cache_dir)

    assert calls == []


def test_does_not_enforce_the_cache_size_cap_on_a_failed_transcode(monkeypatch, tmp_path):
    source = _make_source(tmp_path)
    cache_dir = tmp_path / "cache"

    monkeypatch.setattr(hevc_preview_module, "probe_video_codec", lambda _path: "hevc")
    monkeypatch.setattr(hevc_preview_module, "_nvdec_available", lambda: False)

    def fake_encode(*_args, **_kwargs):
        raise MediaToolError("ffmpeg encode failed")

    monkeypatch.setattr(hevc_preview_module, "encode_with_nvenc_fallback", fake_encode)

    calls = []
    monkeypatch.setattr(
        hevc_preview_module,
        "enforce_cache_size_cap",
        lambda cache_dir_arg, max_bytes: calls.append((cache_dir_arg, max_bytes)),
    )

    load_or_transcode_hevc_preview(source, cache_dir)

    assert calls == []


def test_h265_codec_name_is_also_treated_as_hevc(monkeypatch, tmp_path):
    source = _make_source(tmp_path)
    cache_dir = tmp_path / "cache"

    monkeypatch.setattr(hevc_preview_module, "probe_video_codec", lambda _path: "h265")
    monkeypatch.setattr(hevc_preview_module, "_nvdec_available", lambda: False)

    def fake_encode(input_args, destination, extra_codec_args=None):
        destination.write_bytes(b"transcoded")

    monkeypatch.setattr(hevc_preview_module, "encode_with_nvenc_fallback", fake_encode)

    result = load_or_transcode_hevc_preview(source, cache_dir)

    assert result != source
    assert result.is_file()


def test_different_source_bytes_produce_a_different_cache_entry(monkeypatch, tmp_path):
    """Re-encoded/re-downloaded source (same filename, different mtime/
    size) must never serve a stale cached preview - same guarantee
    load_or_repair_parking_video() gives via its own digest+mtime+size
    cache key."""

    cache_dir = tmp_path / "cache"
    monkeypatch.setattr(hevc_preview_module, "probe_video_codec", lambda _path: "hevc")
    monkeypatch.setattr(hevc_preview_module, "_nvdec_available", lambda: False)

    calls = []

    def fake_encode(input_args, destination, extra_codec_args=None):
        calls.append(destination)
        destination.write_bytes(b"transcoded-%d" % len(calls))

    monkeypatch.setattr(hevc_preview_module, "encode_with_nvenc_fallback", fake_encode)

    source = _make_source(tmp_path, content=b"first version")
    first_result = load_or_transcode_hevc_preview(source, cache_dir)

    # Simulate a re-encoded file landing at the same path with different
    # bytes/mtime - bump mtime explicitly since some filesystems have
    # coarse mtime resolution.
    source.write_bytes(b"a very different, longer second version")
    stat = source.stat()
    import os

    os.utime(source, ns=(stat.st_atime_ns, stat.st_mtime_ns + 1))

    second_result = load_or_transcode_hevc_preview(source, cache_dir)

    assert first_result != second_result
    assert len(calls) == 2
