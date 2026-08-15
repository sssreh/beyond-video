import asyncio
import hashlib
from pathlib import Path

import pytest

from blackvue.export import hevc_preview as hevc_preview_module
from blackvue.export.hevc_preview import load_or_transcode_hevc_preview
from blackvue.export.hevc_preview import open_hevc_preview_stream
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


# ---------------------------------------------------------------------------
# open_hevc_preview_stream() / _stream_transcode() - the progressive
# ("start playing before the whole transcode finishes") path Christer
# asked for: "Can you convert the first 10 to 20%, start playing that
# and during that time convert the rest?" See hevc_preview.py's own
# "Progressive (streaming) preview transcode" section header for the
# full design story. Deliberately its own section, separate from
# load_or_transcode_hevc_preview() above (which is untouched by this
# feature and still fully tested by everything above this point).
# ---------------------------------------------------------------------------


class _FakeStream:
    """Stands in for asyncio.StreamReader - .read(n) pops pre-seeded
    chunks off a list, returning b"" (EOF) once exhausted, matching
    real asyncio stream-reading semantics closely enough for this
    module's own control-flow logic to be exercised."""

    def __init__(self, chunks):
        self._chunks = list(chunks)

    async def read(self, _n=-1):
        if self._chunks:
            return self._chunks.pop(0)
        return b""


class _FakeProcess:
    """Stands in for asyncio.subprocess.Process."""

    def __init__(self, stdout_chunks, returncode, stderr=b""):
        self.stdout = _FakeStream(stdout_chunks)
        self.stderr = _FakeStream([stderr] if stderr else [])
        self.returncode = returncode

    async def wait(self):
        return self.returncode


async def _collect(async_gen):
    return b"".join([chunk async for chunk in async_gen])


def test_open_hevc_preview_stream_returns_source_unchanged_when_not_hevc(
    monkeypatch, tmp_path
):
    source = _make_source(tmp_path)
    cache_dir = tmp_path / "cache"

    monkeypatch.setattr(hevc_preview_module, "probe_video_codec", lambda _path: "h264")

    result = asyncio.run(open_hevc_preview_stream(source, cache_dir))

    assert result == source
    assert not cache_dir.exists()


def test_open_hevc_preview_stream_returns_cached_path_on_a_cache_hit(
    monkeypatch, tmp_path
):
    source = _make_source(tmp_path)
    cache_dir = tmp_path / "cache"
    cache_dir.mkdir()
    expected_cache_path = _expected_cache_path(source, cache_dir)
    expected_cache_path.write_bytes(b"already transcoded")

    monkeypatch.setattr(hevc_preview_module, "probe_video_codec", lambda _path: "hevc")

    def fail_spawn(*_args, **_kwargs):
        raise AssertionError("should reuse the cached copy, not spawn ffmpeg")

    monkeypatch.setattr(hevc_preview_module, "_spawn_ffmpeg", fail_spawn)

    result = asyncio.run(open_hevc_preview_stream(source, cache_dir))

    assert result == expected_cache_path


def test_open_hevc_preview_stream_streams_and_caches_a_fresh_transcode(
    monkeypatch, tmp_path
):
    source = _make_source(tmp_path)
    cache_dir = tmp_path / "cache"

    monkeypatch.setattr(hevc_preview_module, "probe_video_codec", lambda _path: "hevc")
    monkeypatch.setattr(hevc_preview_module, "_nvdec_available", lambda: True)
    monkeypatch.setattr(hevc_preview_module, "_nvenc_available", lambda: True)

    spawn_calls = []

    async def fake_spawn(_source, _extra_codec_args, *, hw_decode, codec):
        spawn_calls.append((hw_decode, codec))
        return _FakeProcess([b"frag1", b"frag2", b"frag3"], returncode=0)

    monkeypatch.setattr(hevc_preview_module, "_spawn_ffmpeg", fake_spawn)

    async def scenario():
        result = await open_hevc_preview_stream(source, cache_dir)
        # Not a Path - a fresh transcode is needed, so the caller gets a
        # live async generator to stream, not a completed file.
        assert not isinstance(result, Path)
        return await _collect(result)

    collected = asyncio.run(scenario())

    assert collected == b"frag1frag2frag3"
    assert spawn_calls == [(True, "h264_nvenc")]  # preferred combo, first try

    expected_cache_path = _expected_cache_path(source, cache_dir)
    assert expected_cache_path.is_file()
    assert expected_cache_path.read_bytes() == b"frag1frag2frag3"
    # No stray .tmp file left behind once the rename lands.
    assert list(cache_dir.iterdir()) == [expected_cache_path]


def test_open_hevc_preview_stream_falls_back_when_preferred_combo_yields_nothing(
    monkeypatch, tmp_path
):
    """Nothing has been sent to the browser yet when the first chunk
    comes back empty, so it's safe to retry with the known-safe CPU
    decode + libx264 combination - the streaming counterpart to
    load_or_transcode_hevc_preview()'s own NVDEC-fails-falls-back-to-
    CPU behavior."""

    source = _make_source(tmp_path)
    cache_dir = tmp_path / "cache"

    monkeypatch.setattr(hevc_preview_module, "probe_video_codec", lambda _path: "hevc")
    monkeypatch.setattr(hevc_preview_module, "_nvdec_available", lambda: True)
    monkeypatch.setattr(hevc_preview_module, "_nvenc_available", lambda: True)

    spawn_calls = []

    async def fake_spawn(_source, _extra_codec_args, *, hw_decode, codec):
        spawn_calls.append((hw_decode, codec))
        if len(spawn_calls) == 1:
            return _FakeProcess([], returncode=1, stderr=b"nvdec init failed")
        return _FakeProcess([b"safe-bytes"], returncode=0)

    monkeypatch.setattr(hevc_preview_module, "_spawn_ffmpeg", fake_spawn)

    async def scenario():
        result = await open_hevc_preview_stream(source, cache_dir)
        return await _collect(result)

    collected = asyncio.run(scenario())

    assert collected == b"safe-bytes"
    assert spawn_calls == [
        (True, "h264_nvenc"),   # preferred combo - fails immediately
        (False, "libx264"),     # known-safe fallback - succeeds
    ]

    expected_cache_path = _expected_cache_path(source, cache_dir)
    assert expected_cache_path.read_bytes() == b"safe-bytes"


def test_open_hevc_preview_stream_yields_nothing_when_both_combinations_fail(
    monkeypatch, tmp_path
):
    source = _make_source(tmp_path)
    cache_dir = tmp_path / "cache"

    monkeypatch.setattr(hevc_preview_module, "probe_video_codec", lambda _path: "hevc")
    monkeypatch.setattr(hevc_preview_module, "_nvdec_available", lambda: True)
    monkeypatch.setattr(hevc_preview_module, "_nvenc_available", lambda: True)

    spawn_calls = []

    async def fake_spawn(_source, _extra_codec_args, *, hw_decode, codec):
        spawn_calls.append((hw_decode, codec))
        return _FakeProcess([], returncode=1, stderr=b"total failure")

    monkeypatch.setattr(hevc_preview_module, "_spawn_ffmpeg", fake_spawn)

    async def scenario():
        result = await open_hevc_preview_stream(source, cache_dir)
        return await _collect(result)

    collected = asyncio.run(scenario())

    assert collected == b""
    assert len(spawn_calls) == 2
    # No cache entry, and no stray .tmp file - nothing was ever
    # written since nothing was ever successfully produced. cache_dir
    # itself does exist by this point (open_hevc_preview_stream()
    # creates it before handing off to the streaming generator).
    assert list(cache_dir.iterdir()) == []


def test_open_hevc_preview_stream_does_not_retry_after_bytes_already_yielded(
    monkeypatch, tmp_path
):
    """Christer's own accepted trade-off: a failure *after* the first
    chunk has already been sent to the browser streams a broken/
    truncated preview for that one request rather than a clean
    fallback, since bytes already sent can't be un-sent. Confirms
    _spawn_ffmpeg() is only ever called once in this case (no silent
    second attempt after real playback bytes went out), and that the
    cache is left clean (no half-written entry) for the next request
    to retry cleanly."""

    source = _make_source(tmp_path)
    cache_dir = tmp_path / "cache"

    monkeypatch.setattr(hevc_preview_module, "probe_video_codec", lambda _path: "hevc")
    monkeypatch.setattr(hevc_preview_module, "_nvdec_available", lambda: False)
    monkeypatch.setattr(hevc_preview_module, "_nvenc_available", lambda: False)

    spawn_calls = []

    async def fake_spawn(_source, _extra_codec_args, *, hw_decode, codec):
        spawn_calls.append((hw_decode, codec))
        # Some real bytes are produced before the process dies mid-
        # encode.
        return _FakeProcess([b"partial-frame"], returncode=1, stderr=b"crashed")

    monkeypatch.setattr(hevc_preview_module, "_spawn_ffmpeg", fake_spawn)

    async def scenario():
        result = await open_hevc_preview_stream(source, cache_dir)
        return await _collect(result)

    collected = asyncio.run(scenario())

    assert collected == b"partial-frame"  # already streamed to the browser
    assert len(spawn_calls) == 1  # no retry once bytes were sent

    expected_cache_path = _expected_cache_path(source, cache_dir)
    assert not expected_cache_path.exists()
    assert list(cache_dir.iterdir()) == []  # no stray .tmp file either


def test_open_hevc_preview_stream_uses_plain_cpu_when_no_acceleration_available(
    monkeypatch, tmp_path
):
    source = _make_source(tmp_path)
    cache_dir = tmp_path / "cache"

    monkeypatch.setattr(hevc_preview_module, "probe_video_codec", lambda _path: "hevc")
    monkeypatch.setattr(hevc_preview_module, "_nvdec_available", lambda: False)
    monkeypatch.setattr(hevc_preview_module, "_nvenc_available", lambda: False)

    spawn_calls = []

    async def fake_spawn(_source, _extra_codec_args, *, hw_decode, codec):
        spawn_calls.append((hw_decode, codec))
        return _FakeProcess([b"cpu-bytes"], returncode=0)

    monkeypatch.setattr(hevc_preview_module, "_spawn_ffmpeg", fake_spawn)

    async def scenario():
        result = await open_hevc_preview_stream(source, cache_dir)
        return await _collect(result)

    collected = asyncio.run(scenario())

    assert collected == b"cpu-bytes"
    assert spawn_calls == [(False, "libx264")]


# ---------------------------------------------------------------------------
# Second iteration: Christer reported a real bug in the first streaming
# design above - "I looks lile every time a look at the video, it does
# the same and not playing the cached file." Root cause: the old
# _stream_transcode() tied the entire ffmpeg-reading/cache-write loop
# directly to the HTTP response's own async generator, so a browser
# that disconnected before draining the whole response (pausing,
# seeking, navigating away, or simply not finishing the clip - all
# completely normal for real video playback) meant Starlette's
# GeneratorExit on close skipped the rename-into-cache step entirely.
# These two tests cover the fix: the background transcode now runs as
# an independent asyncio.Task (_run_transcode_to_cache(), tracked in
# _IN_PROGRESS), decoupled from any one request's own generator.
# ---------------------------------------------------------------------------


def test_background_transcode_finishes_and_caches_even_if_consumer_closes_early(
    monkeypatch, tmp_path
):
    """The actual regression test for Christer's bug report. A consumer
    that reads only the first chunk and then closes early (simulating
    Starlette tearing down the response generator on a client
    disconnect) must not prevent the shared background transcode from
    running to completion and populating the cache - the whole point of
    decoupling _run_transcode_to_cache() from any one request's own
    async generator."""

    source = _make_source(tmp_path)
    cache_dir = tmp_path / "cache"

    monkeypatch.setattr(hevc_preview_module, "probe_video_codec", lambda _path: "hevc")
    monkeypatch.setattr(hevc_preview_module, "_nvdec_available", lambda: False)
    monkeypatch.setattr(hevc_preview_module, "_nvenc_available", lambda: False)

    async def fake_spawn(_source, _extra_codec_args, *, hw_decode, codec):
        return _FakeProcess([b"frag1", b"frag2", b"frag3"], returncode=0)

    monkeypatch.setattr(hevc_preview_module, "_spawn_ffmpeg", fake_spawn)

    expected_cache_path = _expected_cache_path(source, cache_dir)

    async def scenario():
        result = await open_hevc_preview_stream(source, cache_dir)
        assert not isinstance(result, Path)

        # Grab a handle to the background task before consuming
        # anything - _IN_PROGRESS pops its entry once the transcode
        # finishes, so this reference has to be taken up front.
        broadcast = hevc_preview_module._IN_PROGRESS.get(expected_cache_path)

        first_chunk = await result.__anext__()
        assert first_chunk == b"frag1"

        # Simulate the browser disconnecting after only the first
        # chunk: Starlette closes this one request's generator early.
        await result.aclose()

        # The background transcode is its own asyncio.Task, independent
        # of the consumer generator just closed above - wait for it
        # directly, exactly as it would keep running on the event loop
        # in real usage regardless of this one request's fate.
        if broadcast is not None and broadcast.task is not None:
            await broadcast.task

    asyncio.run(scenario())

    assert expected_cache_path.is_file()
    assert expected_cache_path.read_bytes() == b"frag1frag2frag3"
    assert list(cache_dir.iterdir()) == [expected_cache_path]  # no stray .tmp

    # A fresh request for the same source now lands on the fast
    # cache-hit path - no new ffmpeg process, confirming the cache
    # really did get finalized rather than just written-then-discarded.
    def fail_spawn(*_args, **_kwargs):
        raise AssertionError("should reuse the cached copy, not transcode again")

    monkeypatch.setattr(hevc_preview_module, "_spawn_ffmpeg", fail_spawn)

    second_result = asyncio.run(open_hevc_preview_stream(source, cache_dir))
    assert second_result == expected_cache_path


class _SlowFakeStream:
    """Like _FakeStream, but each read() genuinely yields control back
    to the event loop (a real `await asyncio.sleep(0)` suspension,
    not just a synchronous coroutine call) - needed so a test can
    deterministically interleave a second, late-joining request partway
    through an in-progress transcode, the way two overlapping browser
    requests for the same not-yet-cached source would in real usage."""

    def __init__(self, chunks):
        self._chunks = list(chunks)

    async def read(self, _n=-1):
        await asyncio.sleep(0)
        if self._chunks:
            return self._chunks.pop(0)
        return b""


class _SlowFakeProcess:
    def __init__(self, stdout_chunks, returncode):
        self.stdout = _SlowFakeStream(stdout_chunks)
        self.stderr = _FakeStream([])
        self.returncode = returncode

    async def wait(self):
        return self.returncode


def test_open_hevc_preview_stream_dedupes_and_replays_history_for_a_late_joiner(
    monkeypatch, tmp_path
):
    """A free side-effect of the _IN_PROGRESS/_TranscodeBroadcast design:
    two overlapping requests for the same not-yet-cached source join a
    single in-flight transcode instead of each spawning a redundant
    ffmpeg process. The second request here deliberately joins only
    after the first chunk has already been produced, confirming the
    late joiner still gets the *entire* stream from the very
    beginning - via _TranscodeBroadcast.subscribe()'s history replay -
    not just whatever's produced from the moment it joined."""

    source = _make_source(tmp_path)
    cache_dir = tmp_path / "cache"

    monkeypatch.setattr(hevc_preview_module, "probe_video_codec", lambda _path: "hevc")
    monkeypatch.setattr(hevc_preview_module, "_nvdec_available", lambda: False)
    monkeypatch.setattr(hevc_preview_module, "_nvenc_available", lambda: False)

    spawn_calls = []

    async def fake_spawn(_source, _extra_codec_args, *, hw_decode, codec):
        spawn_calls.append((hw_decode, codec))
        return _SlowFakeProcess([b"frag1", b"frag2", b"frag3"], returncode=0)

    monkeypatch.setattr(hevc_preview_module, "_spawn_ffmpeg", fake_spawn)

    async def scenario():
        first_result = await open_hevc_preview_stream(source, cache_dir)
        assert not isinstance(first_result, Path)

        # Pull one chunk so the background transcode is genuinely
        # underway before the second request joins.
        first_chunk = await first_result.__anext__()
        assert first_chunk == b"frag1"

        second_result = await open_hevc_preview_stream(source, cache_dir)
        assert not isinstance(second_result, Path)

        rest_of_first = b"".join([chunk async for chunk in first_result])
        all_of_second = await _collect(second_result)
        return first_chunk + rest_of_first, all_of_second

    first_total, second_total = asyncio.run(scenario())

    assert first_total == b"frag1frag2frag3"
    assert second_total == b"frag1frag2frag3"  # late joiner still got everything
    assert len(spawn_calls) == 1  # dedup: only one real ffmpeg process


def test_nvenc_available_checks_ffmpeg_encoders_output(monkeypatch):
    """Mirrors media.py's own _nvenc_available() test - this module
    keeps its own private copy of the probe (see this module's own
    comment on why), so it gets its own copy of this test too."""

    monkeypatch.setattr(hevc_preview_module, "_NVENC_AVAILABLE", None)

    captured = {}

    class FakeResult:
        stdout = "... h264_nvenc ...\n"

    def fake_run(command, **kwargs):
        captured["command"] = command
        return FakeResult()

    monkeypatch.setattr(hevc_preview_module.subprocess, "run", fake_run)

    assert hevc_preview_module._nvenc_available() is True
    assert captured["command"] == ["ffmpeg", "-hide_banner", "-encoders"]

    captured.clear()
    assert hevc_preview_module._nvenc_available() is True
    assert captured == {}
