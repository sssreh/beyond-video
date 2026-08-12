import time

from blackvue.generate.cache_utils import enforce_cache_size_cap


def _write(path, size, *, mtime=None):
    path.write_bytes(b"\x00" * size)
    if mtime is not None:
        import os

        os.utime(path, (mtime, mtime))


def test_does_nothing_when_cache_dir_does_not_exist(tmp_path):
    missing = tmp_path / "does-not-exist"

    enforce_cache_size_cap(missing, max_bytes=10)

    assert not missing.exists()


def test_does_nothing_when_already_under_the_cap(tmp_path):
    cache_dir = tmp_path / "cache"
    cache_dir.mkdir()
    _write(cache_dir / "a.mp4", 100)
    _write(cache_dir / "b.mp4", 100)

    enforce_cache_size_cap(cache_dir, max_bytes=1_000)

    assert {p.name for p in cache_dir.iterdir()} == {"a.mp4", "b.mp4"}


def test_evicts_oldest_entries_first_until_back_under_the_cap(tmp_path):
    cache_dir = tmp_path / "cache"
    cache_dir.mkdir()
    now = time.time()
    # Oldest to newest, 100 bytes each, total 300 - a 150-byte cap
    # needs both the oldest and the middle entry gone (down to 100)
    # before it's satisfied, leaving only the newest.
    _write(cache_dir / "oldest.mp4", 100, mtime=now - 300)
    _write(cache_dir / "middle.mp4", 100, mtime=now - 200)
    _write(cache_dir / "newest.mp4", 100, mtime=now - 100)

    enforce_cache_size_cap(cache_dir, max_bytes=150)

    remaining = {p.name for p in cache_dir.iterdir()}
    assert remaining == {"newest.mp4"}


def test_evicts_nothing_more_than_needed(tmp_path):
    cache_dir = tmp_path / "cache"
    cache_dir.mkdir()
    now = time.time()
    _write(cache_dir / "oldest.mp4", 100, mtime=now - 300)
    _write(cache_dir / "newest.mp4", 100, mtime=now - 100)

    # Cap only just below the combined size (200) - a single 100-byte
    # eviction (the oldest) is enough to satisfy it.
    enforce_cache_size_cap(cache_dir, max_bytes=150)

    remaining = {p.name for p in cache_dir.iterdir()}
    assert remaining == {"newest.mp4"}


def test_never_deletes_a_tmp_file(tmp_path):
    """An in-progress atomic-rename temp file (see hevc_preview.py's own
    concurrent-write-race fix) must never be swept up as an eviction
    candidate - it could belong to another request's transcode that
    hasn't finished yet."""

    cache_dir = tmp_path / "cache"
    cache_dir.mkdir()
    now = time.time()
    _write(cache_dir / "old-real-entry.mp4", 100, mtime=now - 300)
    _write(cache_dir / "some-digest.abcd1234.tmp", 100, mtime=now - 300)

    enforce_cache_size_cap(cache_dir, max_bytes=1)

    remaining = {p.name for p in cache_dir.iterdir()}
    # The real entry can be evicted (over cap, oldest available), but
    # the .tmp file must survive regardless of the cap or its own age.
    assert remaining == {"some-digest.abcd1234.tmp"}


def test_skips_subdirectories(tmp_path):
    cache_dir = tmp_path / "cache"
    cache_dir.mkdir()
    (cache_dir / "a_subdir").mkdir()
    _write(cache_dir / "entry.mp4", 100)

    # Should not raise trying to stat/delete the subdirectory as if it
    # were a cache file.
    enforce_cache_size_cap(cache_dir, max_bytes=1)

    assert (cache_dir / "a_subdir").is_dir()
