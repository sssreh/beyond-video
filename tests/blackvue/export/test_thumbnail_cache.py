import hashlib

from blackvue.export import thumbnail_cache as thumbnail_cache_module
from blackvue.export.thumbnail_cache import load_or_generate_thumbnail


def _make_source(tmp_path, name="20260715_140212_NF.mp4", content=b"video bytes"):
    source = tmp_path / name
    source.write_bytes(content)
    return source


def _expected_cache_path(source, cache_dir):
    stat = source.stat()
    digest = hashlib.sha256(str(source.resolve()).encode("utf-8")).hexdigest()[:16]
    return cache_dir / f"{digest}-{stat.st_mtime_ns}-{stat.st_size}.jpg"


def test_generates_and_caches_a_thumbnail_on_first_use(monkeypatch, tmp_path):
    source = _make_source(tmp_path)
    cache_dir = tmp_path / "cache"

    def fake_extract(_source, destination, **_kwargs):
        destination.write_bytes(b"a jpeg frame")

    monkeypatch.setattr(thumbnail_cache_module, "extract_video_thumbnail", fake_extract)

    result = load_or_generate_thumbnail(source, cache_dir)

    assert result == _expected_cache_path(source, cache_dir)
    assert result.read_bytes() == b"a jpeg frame"
    # The atomic-rename temp file must not survive a successful generate.
    assert list(cache_dir.iterdir()) == [result]


def test_reuses_a_cached_thumbnail_on_a_second_call(monkeypatch, tmp_path):
    source = _make_source(tmp_path)
    cache_dir = tmp_path / "cache"
    cache_dir.mkdir()
    expected_cache_path = _expected_cache_path(source, cache_dir)
    expected_cache_path.write_bytes(b"already generated")

    def fail_extract(*_args, **_kwargs):
        raise AssertionError("a cache hit must not re-generate the thumbnail")

    monkeypatch.setattr(thumbnail_cache_module, "extract_video_thumbnail", fail_extract)

    result = load_or_generate_thumbnail(source, cache_dir)

    assert result == expected_cache_path
    assert result.read_bytes() == b"already generated"


def test_enforces_the_cache_size_cap_after_generating(monkeypatch, tmp_path):
    source = _make_source(tmp_path)
    cache_dir = tmp_path / "cache"

    def fake_extract(_source, destination, **_kwargs):
        destination.write_bytes(b"a jpeg frame")

    monkeypatch.setattr(thumbnail_cache_module, "extract_video_thumbnail", fake_extract)

    calls = []
    monkeypatch.setattr(
        thumbnail_cache_module,
        "enforce_cache_size_cap",
        lambda cache_dir_arg, max_bytes: calls.append((cache_dir_arg, max_bytes)),
    )

    load_or_generate_thumbnail(source, cache_dir)

    assert calls == [(cache_dir, thumbnail_cache_module._MAX_CACHE_BYTES)]


def test_does_not_enforce_the_cache_size_cap_on_a_cache_hit(monkeypatch, tmp_path):
    source = _make_source(tmp_path)
    cache_dir = tmp_path / "cache"
    cache_dir.mkdir()
    _expected_cache_path(source, cache_dir).write_bytes(b"already generated")

    calls = []
    monkeypatch.setattr(
        thumbnail_cache_module,
        "enforce_cache_size_cap",
        lambda cache_dir_arg, max_bytes: calls.append((cache_dir_arg, max_bytes)),
    )

    load_or_generate_thumbnail(source, cache_dir)

    assert calls == []
