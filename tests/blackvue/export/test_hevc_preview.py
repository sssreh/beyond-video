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


# ---------------------------------------------------------------------------
# Codec-probe caching (2026-08-27): Christer reported "Slow to play
# vh264 videos ... via bv-web". Root cause: both
# load_or_transcode_hevc_preview() and open_hevc_preview_stream() called
# probe_video_codec() - a real ffprobe subprocess spawn - unconditionally
# on every single call, even for plain H.264 sources that immediately
# bail out unchanged. A browser issues many overlapping Range requests
# per video while buffering/seeking, so every one of those re-spawned
# ffprobe from scratch - and since open_hevc_preview_stream() is an
# async route handler while probe_video_codec() blocks via
# subprocess.run(), each probe stalled the whole event loop too. These
# tests cover the fix: _cached_probe_video_codec() memoizes the result
# per (resolved path, mtime_ns, size), and the async path hops the
# cache-miss probe onto a worker thread via asyncio.to_thread() so it
# can't block concurrent requests even on a cold cache.
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _clear_codec_probe_cache():
    """_CODEC_PROBE_CACHE is a module-level dict so the cache survives
    across calls within a real bv-web process (the whole point) - but
    that means it must be reset between tests, or a cache entry left
    behind by one test could mask a missing probe call in another."""

    hevc_preview_module._CODEC_PROBE_CACHE.clear()
    yield
    hevc_preview_module._CODEC_PROBE_CACHE.clear()


def test_cached_probe_video_codec_only_probes_once_for_repeated_calls(
    monkeypatch, tmp_path
):
    source = _make_source(tmp_path)

    calls = []

    def fake_probe(path):
        calls.append(path)
        return "h264"

    monkeypatch.setattr(hevc_preview_module, "probe_video_codec", fake_probe)

    first = hevc_preview_module._cached_probe_video_codec(source)
    second = hevc_preview_module._cached_probe_video_codec(source)

    assert first == "h264"
    assert second == "h264"
    assert len(calls) == 1  # second call was a cache hit, no subprocess spawn


def test_cached_probe_video_codec_reprobes_after_the_file_changes(
    monkeypatch, tmp_path
):
    source = _make_source(tmp_path, content=b"first version")

    calls = []

    def fake_probe(path):
        calls.append(path)
        return "h264"

    monkeypatch.setattr(hevc_preview_module, "probe_video_codec", fake_probe)

    hevc_preview_module._cached_probe_video_codec(source)

    # Same path, different mtime/size - a re-encoded file landing in
    # place must not serve a stale cached codec.
    source.write_bytes(b"a very different, longer second version")
    import os

    stat = source.stat()
    os.utime(source, ns=(stat.st_atime_ns, stat.st_mtime_ns + 1))

    hevc_preview_module._cached_probe_video_codec(source)

    assert len(calls) == 2


def test_cached_probe_video_codec_does_not_cache_a_probe_failure(
    monkeypatch, tmp_path
):
    """A MediaToolError (ffprobe missing/erroring) is a systemic
    problem, not a per-file fact - it must keep surfacing to the
    caller every time, not get memorized as a permanent false answer
    for one unlucky file that happened to be probed while ffprobe was
    briefly broken."""

    source = _make_source(tmp_path)

    calls = []

    def flaky_probe(path):
        calls.append(path)
        if len(calls) == 1:
            raise MediaToolError("ffprobe not found on PATH")
        return "h264"

    monkeypatch.setattr(hevc_preview_module, "probe_video_codec", flaky_probe)

    with pytest.raises(MediaToolError):
        hevc_preview_module._cached_probe_video_codec(source)

    result = hevc_preview_module._cached_probe_video_codec(source)

    assert result == "h264"
    assert len(calls) == 2  # the failed attempt was not cached


def test_load_or_transcode_hevc_preview_only_probes_once_across_calls(
    monkeypatch, tmp_path
):
    """Regression test for the real bug report: repeated calls for the
    same H.264 source (mirroring repeated HTTP requests for the same
    file) must only spawn ffprobe once."""

    source = _make_source(tmp_path)
    cache_dir = tmp_path / "cache"

    calls = []

    def fake_probe(path):
        calls.append(path)
        return "h264"

    monkeypatch.setattr(hevc_preview_module, "probe_video_codec", fake_probe)

    for _ in range(5):
        result = load_or_transcode_hevc_preview(source, cache_dir)
        assert result == source

    assert len(calls) == 1


def test_open_hevc_preview_stream_only_probes_once_across_calls(
    monkeypatch, tmp_path
):
    """Async counterpart of the sync test above - the actual hot path
    for the reported bug (called on every browser Range request)."""

    source = _make_source(tmp_path)
    cache_dir = tmp_path / "cache"

    calls = []

    def fake_probe(path):
        calls.append(path)
        return "h264"

    monkeypatch.setattr(hevc_preview_module, "probe_video_codec", fake_probe)

    async def scenario():
        for _ in range(5):
            result = await open_hevc_preview_stream(source, cache_dir)
            assert result == source

    asyncio.run(scenario())

    assert len(calls) == 1


def test_open_hevc_preview_stream_offloads_cache_miss_probe_to_a_thread(
    monkeypatch, tmp_path
):
    """The cold-cache probe must run via asyncio.to_thread(), not a
    direct blocking call inside the coroutine - otherwise the very
    stall this fix exists to remove would just move from "every
    request" to "every request for a file no one has requested yet",
    still blocking the event loop for every other concurrent request
    each time it happens."""

    source = _make_source(tmp_path)
    cache_dir = tmp_path / "cache"

    monkeypatch.setattr(hevc_preview_module, "probe_video_codec", lambda _path: "h264")

    to_thread_calls = []
    real_to_thread = asyncio.to_thread

    async def spying_to_thread(func, *args, **kwargs):
        to_thread_calls.append(func)
        return await real_to_thread(func, *args, **kwargs)

    monkeypatch.setattr(hevc_preview_module.asyncio, "to_thread", spying_to_thread)

    result = asyncio.run(open_hevc_preview_stream(source, cache_dir))

    assert result == source
    assert to_thread_calls == [hevc_preview_module._cached_probe_video_codec]


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


# ---------------------------------------------------------------------------
# Cancelling stale transcodes on video switch: Christer, after the
# progressive-streaming feature above landed - "When i jump around
# videos and try to guess driver, i leave behind a lot of prieview
# caching that slows my next preview down. Are there any functionality
# with which you can cancel the preview if i go somewhere else."
# Confirmed via AskUserQuestion: opening a different video should
# cancel any other still-running transcode immediately. These tests
# cover _cancel_stale_transcodes() itself, _run_transcode_to_cache()'s
# own `except asyncio.CancelledError` handler (which has to explicitly
# kill the real ffmpeg child process - task cancellation alone never
# touches it), and the end-to-end path through
# open_hevc_preview_stream().
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _clear_in_progress_registry():
    """_IN_PROGRESS is a module-level dict, same rationale as
    _clear_codec_probe_cache above - a broadcast left behind by a test
    that cancels a task without letting it run to completion could
    otherwise leak into (and confuse) a later test."""

    hevc_preview_module._IN_PROGRESS.clear()
    yield
    hevc_preview_module._IN_PROGRESS.clear()


class _HangingFakeStream:
    """Like _FakeStream, but once its pre-seeded chunks run out, .read()
    genuinely never returns - it suspends forever awaiting an Event
    that's never set. Stands in for ffmpeg still mid-transcode, so a
    test can cancel the task while it's truly suspended (real
    asyncio.Task.cancel() only does anything meaningful while the task
    is actually awaiting something, not while it's synchronously
    running)."""

    def __init__(self, chunks_before_hang=()):
        self._chunks = list(chunks_before_hang)

    async def read(self, _n=-1):
        if self._chunks:
            return self._chunks.pop(0)
        await asyncio.Event().wait()
        return b""  # unreachable - the Event above is never set


class _KillableFakeProcess:
    """Like _SlowFakeProcess, but its stdout hangs (via
    _HangingFakeStream) after any pre-seeded chunks, and it tracks
    whether .kill() was called - exercising _run_transcode_to_cache()'s
    own cancellation handler for real, not just by inspecting code."""

    def __init__(self, chunks_before_hang=(), returncode=0):
        self.stdout = _HangingFakeStream(chunks_before_hang)
        self.stderr = _FakeStream([])
        self.returncode = None
        self._final_returncode = returncode
        self.killed = False

    def kill(self):
        # Real asyncio.subprocess.Process.kill() is a plain sync call.
        self.killed = True
        self.returncode = self._final_returncode

    async def wait(self):
        return self.returncode


def test_cancel_stale_transcodes_cancels_other_sources_but_leaves_finished_ones_alone(
    tmp_path,
):
    """Direct unit test of _cancel_stale_transcodes(): a still-running
    broadcast for a different source gets cancelled; a broadcast whose
    task has already finished is left untouched (nothing to cancel, and
    calling .cancel() on a done task is harmless but pointless)."""

    async def scenario():
        source_a = tmp_path / "a.mp4"
        source_b = tmp_path / "b.mp4"
        source_c = tmp_path / "c.mp4"  # the video being opened now

        async def hang_forever():
            await asyncio.Event().wait()

        async def already_done():
            return None

        broadcast_a = hevc_preview_module._TranscodeBroadcast(
            source_a, tmp_path / "a-cache.mp4", tmp_path / "a.tmp"
        )
        broadcast_a.task = asyncio.create_task(hang_forever())

        broadcast_b = hevc_preview_module._TranscodeBroadcast(
            source_b, tmp_path / "b-cache.mp4", tmp_path / "b.tmp"
        )
        broadcast_b.task = asyncio.create_task(already_done())
        # Let both tasks actually run: a's suspends on hang_forever(),
        # b's runs to completion.
        await broadcast_b.task

        hevc_preview_module._IN_PROGRESS[broadcast_a.cache_path] = broadcast_a
        hevc_preview_module._IN_PROGRESS[broadcast_b.cache_path] = broadcast_b

        hevc_preview_module._cancel_stale_transcodes(except_source=source_c)
        await asyncio.sleep(0)  # let the cancellation actually land

        assert broadcast_a.task.cancelled()
        assert not broadcast_b.task.cancelled()  # already done - never touched

    asyncio.run(scenario())


def test_cancel_stale_transcodes_does_not_cancel_the_video_just_opened(tmp_path):
    """The one exception: a broadcast whose own source matches
    except_source (the video just opened) is left running - joining an
    already in-progress transcode of the very thing being watched now
    is the normal dedup path (see the "late joiner" test above), not
    something to cancel."""

    async def scenario():
        source_a = tmp_path / "a.mp4"

        async def hang_forever():
            await asyncio.Event().wait()

        broadcast_a = hevc_preview_module._TranscodeBroadcast(
            source_a, tmp_path / "a-cache.mp4", tmp_path / "a.tmp"
        )
        broadcast_a.task = asyncio.create_task(hang_forever())
        hevc_preview_module._IN_PROGRESS[broadcast_a.cache_path] = broadcast_a
        await asyncio.sleep(0)  # let it reach its suspend point

        hevc_preview_module._cancel_stale_transcodes(except_source=source_a)
        await asyncio.sleep(0)

        assert not broadcast_a.task.cancelled()
        assert not broadcast_a.task.done()

        broadcast_a.task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await broadcast_a.task

    asyncio.run(scenario())


def test_run_transcode_to_cache_kills_ffmpeg_and_cleans_up_on_cancellation(
    monkeypatch, tmp_path
):
    """Christer: "are there any functionality with which you can cancel
    the preview if i go somewhere else." asyncio.Task.cancel() alone
    only unwinds the awaiting coroutine - it does NOT touch the spawned
    ffmpeg child process. Confirms _run_transcode_to_cache()'s own
    `except asyncio.CancelledError` handler kills the real subprocess,
    leaves no stray .tmp file behind, and still runs broadcast.finish()
    and pops the _IN_PROGRESS entry so nothing hangs or blocks the next
    request for the same source."""

    source = _make_source(tmp_path)
    cache_dir = tmp_path / "cache"
    cache_dir.mkdir()
    cache_path = _expected_cache_path(source, cache_dir)
    tmp_path_arg = cache_path.with_name(f"{cache_path.stem}.abcd1234.tmp")

    fake_proc = _KillableFakeProcess(chunks_before_hang=[b"frag1"])

    async def fake_spawn(_source, _extra_codec_args, *, hw_decode, codec):
        return fake_proc

    monkeypatch.setattr(hevc_preview_module, "_spawn_ffmpeg", fake_spawn)
    monkeypatch.setattr(hevc_preview_module, "_nvdec_available", lambda: False)
    monkeypatch.setattr(hevc_preview_module, "_nvenc_available", lambda: False)

    broadcast = hevc_preview_module._TranscodeBroadcast(source, cache_path, tmp_path_arg)
    hevc_preview_module._IN_PROGRESS[cache_path] = broadcast

    async def scenario():
        task = asyncio.create_task(hevc_preview_module._run_transcode_to_cache(broadcast))
        broadcast.task = task

        # Let the task actually run: spawn ffmpeg, publish the first
        # chunk, and suspend on the (hanging) second read - proven by
        # broadcast.history holding that first chunk.
        for _ in range(20):
            await asyncio.sleep(0)
            if broadcast.history:
                break

        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(scenario())

    assert fake_proc.killed
    assert not tmp_path_arg.exists()
    assert cache_path not in hevc_preview_module._IN_PROGRESS
    assert broadcast.done  # finish() still ran despite the cancellation
    assert not cache_path.exists()  # cancelled mid-transcode - nothing cached


def test_open_hevc_preview_stream_cancels_a_stale_transcode_for_a_different_video(
    monkeypatch, tmp_path
):
    """End-to-end version of the two _cancel_stale_transcodes() unit
    tests above, going through the real open_hevc_preview_stream()
    entry point: opening video B while video A is still transcoding
    cancels A's background task (and kills its ffmpeg process) and lets
    B's own transcode proceed and complete normally."""

    source_a = _make_source(tmp_path, name="a.mp4")
    source_b = _make_source(tmp_path, name="b.mp4")
    cache_dir = tmp_path / "cache"

    monkeypatch.setattr(hevc_preview_module, "probe_video_codec", lambda _path: "hevc")
    monkeypatch.setattr(hevc_preview_module, "_nvdec_available", lambda: False)
    monkeypatch.setattr(hevc_preview_module, "_nvenc_available", lambda: False)

    proc_a = _KillableFakeProcess(chunks_before_hang=[b"a-frag"])
    proc_b = _FakeProcess([b"b-frag"], returncode=0)

    async def fake_spawn(source, _extra_codec_args, *, hw_decode, codec):
        return proc_a if source == source_a else proc_b

    monkeypatch.setattr(hevc_preview_module, "_spawn_ffmpeg", fake_spawn)

    async def scenario():
        result_a = await open_hevc_preview_stream(source_a, cache_dir)
        assert not isinstance(result_a, Path)

        cache_path_a = _expected_cache_path(source_a, cache_dir)
        broadcast_a = hevc_preview_module._IN_PROGRESS[cache_path_a]

        first_chunk_a = await result_a.__anext__()
        assert first_chunk_a == b"a-frag"  # a's transcode is now hung mid-read

        # Opening a different video now must cancel a's still-running
        # background task.
        result_b = await open_hevc_preview_stream(source_b, cache_dir)
        assert not isinstance(result_b, Path)

        with pytest.raises(asyncio.CancelledError):
            await broadcast_a.task

        return await _collect(result_b)

    collected_b = asyncio.run(scenario())

    assert collected_b == b"b-frag"
    assert proc_a.killed
    assert not hasattr(proc_b, "killed") or not proc_b.killed  # b ran to completion

    cache_path_a = _expected_cache_path(source_a, cache_dir)
    assert cache_path_a not in hevc_preview_module._IN_PROGRESS
    assert not cache_path_a.is_file()  # a's transcode was cancelled, not cached

    cache_path_b = _expected_cache_path(source_b, cache_dir)
    assert cache_path_b.is_file()
    assert cache_path_b.read_bytes() == b"b-frag"
