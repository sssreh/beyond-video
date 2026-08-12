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

    calls = []

    def fake_encode(input_args, destination, extra_codec_args=None):
        calls.append((input_args, destination, extra_codec_args))
        destination.write_bytes(b"transcoded")

    monkeypatch.setattr(hevc_preview_module, "encode_with_nvenc_fallback", fake_encode)

    result = load_or_transcode_hevc_preview(source, cache_dir)

    expected_cache_path = _expected_cache_path(source, cache_dir)
    assert result == expected_cache_path
    assert result.is_file()
    assert len(calls) == 1
    input_args, destination, extra_codec_args = calls[0]
    assert input_args == ["-i", str(source)]
    assert destination == expected_cache_path
    assert extra_codec_args == [
        "-c:a", "copy",
        "-movflags", "+faststart",
        "-b:v", "8M",
        "-maxrate", "8M",
        "-bufsize", "8M",
    ]


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

    def fake_encode(*_args, **_kwargs):
        raise MediaToolError("ffmpeg encode failed")

    monkeypatch.setattr(hevc_preview_module, "encode_with_nvenc_fallback", fake_encode)

    result = load_or_transcode_hevc_preview(source, cache_dir)

    assert result == source


def test_h265_codec_name_is_also_treated_as_hevc(monkeypatch, tmp_path):
    source = _make_source(tmp_path)
    cache_dir = tmp_path / "cache"

    monkeypatch.setattr(hevc_preview_module, "probe_video_codec", lambda _path: "h265")

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
