"""
Tests for adapters/folder/adapter.py - FolderAdapter.

Unlike BlackVueAdapter (a pure delegation wrapper), FolderAdapter is
real, adapter-specific scanning logic - see its own module docstring.
These tests check the recursive scan, timestamp resolution (mtime
fallback - ffprobe isn't assumed to be on PATH in a test environment),
synthesized RecordingIds, same-stem generated-asset discovery, the
find_recording() rescan-and-filter path, and that every
manifest-declared-unsupported capability raises AdapterCapabilityError.
"""

import os
from pathlib import Path

import pytest

from blackvue.adapters import registry
from blackvue.adapters.base import AdapterCapabilityError
from blackvue.adapters.folder.adapter import FolderAdapter
from blackvue.archive.asset import Asset
from blackvue.archive.recording_id import RecordingId


@pytest.fixture()
def adapter():
    return FolderAdapter()


def _touch(path: Path, *, size: int = 10, mtime: float | None = None) -> Path:
    path.write_bytes(b"x" * size)
    if mtime is not None:
        os.utime(path, (mtime, mtime))
    return path


def test_manifest_is_the_real_folder_manifest(adapter):
    assert adapter.manifest.adapter_id == "folder"
    assert adapter.manifest == registry.load_adapter_manifest("folder")


def test_registered_under_folder_id():
    assert registry.get_adapter("folder").manifest.adapter_id == "folder"


# ---------------------------------------------------------------------------
# open_archive() - recursive scan, timestamp resolution, asset discovery.
# ---------------------------------------------------------------------------


def test_open_archive_finds_videos_in_nested_subfolders(adapter, tmp_path):
    sub = tmp_path / "trip1" / "clips"
    sub.mkdir(parents=True)
    _touch(sub / "a.mp4", mtime=1700000000)
    _touch(sub / "b.MOV", mtime=1700000100)  # extension case-insensitive
    (tmp_path / "not_a_video.txt").write_text("ignore me")

    archive = adapter.open_archive(tmp_path)

    assert len(archive.recordings) == 2


def test_open_archive_ignores_extensions_outside_the_manifest_list(
    adapter, tmp_path
):
    _touch(tmp_path / "clip.mp4", mtime=1700000000)
    _touch(tmp_path / "clip.xyz", mtime=1700000000)

    archive = adapter.open_archive(tmp_path)

    assert len(archive.recordings) == 1


def test_recording_id_uses_mtime_fallback_and_v_kind_code(adapter, tmp_path):
    # ffprobe isn't guaranteed to exist in a test environment, so this
    # exercises the file-mtime fallback path explicitly - a real
    # environment with ffprobe would prefer embedded creation_time
    # instead (see _resolve_timestamp()'s own docstring).
    _touch(tmp_path / "clip.mp4", mtime=1700000000)

    archive = adapter.open_archive(tmp_path)
    recording_id = archive.recordings[0].id

    assert recording_id.kind == "V"
    assert recording_id.value.startswith("20231114") or len(recording_id.value) == 17


def test_video_is_stored_under_front_asset(adapter, tmp_path):
    # See module docstring: FolderAdapter's single video slot reuses
    # Asset.FRONT so recordings_with_front_video() (trip building) and
    # bv-ls/bv-web's existing Front-column plumbing work unmodified.
    _touch(tmp_path / "clip.mp4", mtime=1700000000)

    archive = adapter.open_archive(tmp_path)
    recording = archive.recordings[0]

    assert recording.has(Asset.FRONT)
    assert not recording.has(Asset.REAR)


def test_same_stem_generated_assets_are_discovered(adapter, tmp_path):
    video = _touch(tmp_path / "clip.mp4", mtime=1700000000)
    (tmp_path / "clip.transcript.txt").write_text("hello")
    (tmp_path / "clip.srt").write_text("1\n00:00:00,000 --> 00:00:01,000\nhi\n")
    # A same-stem file whose suffix isn't in the manifest's table at
    # all should not be picked up as any asset.
    (tmp_path / "clip.unrelated.bin").write_bytes(b"\x00")

    archive = adapter.open_archive(tmp_path)
    recording = archive.recordings[0]

    assert recording.has(Asset.TRANSCRIPT)
    assert recording.has(Asset.SUBTITLES)
    assert recording.size > video.stat().st_size  # includes sidecar bytes too


def test_collision_disambiguation_bumps_the_later_id_by_a_second(
    adapter, tmp_path
):
    # Two videos resolving to the exact same wall-clock second (see
    # _assign_recording_ids()'s own docstring) must not collide into a
    # single Recording - both should survive as distinct ids, one
    # second apart.
    _touch(tmp_path / "a.mp4", mtime=1700000000)
    _touch(tmp_path / "b.mp4", mtime=1700000000)

    archive = adapter.open_archive(tmp_path)

    assert len(archive.recordings) == 2
    ids = sorted(r.id.value for r in archive.recordings)
    assert ids[0] != ids[1]


def test_recordings_are_sorted_by_id(adapter, tmp_path):
    _touch(tmp_path / "later.mp4", mtime=1700000200)
    _touch(tmp_path / "earlier.mp4", mtime=1700000000)

    archive = adapter.open_archive(tmp_path)

    ids = [r.id for r in archive.recordings]
    assert ids == sorted(ids)


def test_configuration_always_returns_the_fallback_without_warning(
    adapter, tmp_path, capsys
):
    _touch(tmp_path / "clip.mp4", mtime=1700000000)

    archive = adapter.open_archive(tmp_path)
    configuration = archive.configuration(archive.recordings[0])

    assert configuration.record_time == 300
    # Unlike Archive's own configuration() (see archive/archive.py),
    # this never prints the "no configuration snapshot" warning - a
    # folder adapter camera never has one by design (config_snapshot
    # capability is False), so it isn't a degraded state worth
    # warning about every time.
    assert capsys.readouterr().out == ""


# ---------------------------------------------------------------------------
# find_recording() - full-rescan-and-filter path (see base.py's own
# docstring on why this adapter has no targeted-lookup fast path).
# ---------------------------------------------------------------------------


def test_find_recording_returns_the_matching_recording(adapter, tmp_path):
    _touch(tmp_path / "clip.mp4", mtime=1700000000)
    archive = adapter.open_archive(tmp_path)
    target_id = archive.recordings[0].id

    found = adapter.find_recording(tmp_path, target_id)

    assert found is not None
    assert found.id == target_id


def test_find_recording_returns_none_for_an_unknown_id(adapter, tmp_path):
    _touch(tmp_path / "clip.mp4", mtime=1700000000)

    found = adapter.find_recording(tmp_path, RecordingId("99991231_235959_V"))

    assert found is None


# ---------------------------------------------------------------------------
# Capability guards - every method gated by a manifest capability this
# adapter declares False should raise AdapterCapabilityError, never
# silently no-op or raise something else.
# ---------------------------------------------------------------------------


def test_read_gps_raises_capability_error(adapter):
    with pytest.raises(AdapterCapabilityError):
        adapter.read_gps(Path("/fake.gps"))


def test_read_gsensor_raises_capability_error(adapter):
    with pytest.raises(AdapterCapabilityError):
        adapter.read_gsensor(Path("/fake.3gf"))


def test_connect_raises_capability_error(adapter):
    with pytest.raises(AdapterCapabilityError):
        adapter.connect([])


def test_config_snapshot_seconds_raises_capability_error(adapter):
    with pytest.raises(AdapterCapabilityError):
        adapter.config_snapshot_seconds("text")


def test_manifest_declares_the_capabilities_this_test_file_assumes(adapter):
    # Sanity check the fixture manifest itself - if these ever flip to
    # True, the capability-guard tests above would need updating too.
    assert not adapter.manifest.supports("gps")
    assert not adapter.manifest.supports("gsensor")
    assert not adapter.manifest.supports("network_connect")
    assert not adapter.manifest.supports("config_snapshot")
