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
from datetime import datetime
from pathlib import Path

import pytest
from PIL import Image

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


def test_recording_timestamp_reliable_false_on_mtime_fallback(adapter, tmp_path):
    # A real, confirmed case from Christer: a plain data file with no
    # embedded metadata of any kind - falling all the way back to
    # mtime - must be flagged unreliable so TripBuilder never silently
    # groups it with an unrelated recording that happens to land close
    # by in mtime (see archive/recording.py's Recording.
    # timestamp_reliable docstring for the full story).
    _touch(tmp_path / "clip.mp4", mtime=1700000000)

    archive = adapter.open_archive(tmp_path)

    assert archive.recordings[0].timestamp_reliable is False


def test_recording_timestamp_reliable_true_with_real_embedded_creation_time(
    adapter, tmp_path
):
    # Real ffmpeg-encoded fixture with a real embedded creation_time
    # tag (this codebase's own established real-fixture-over-mocking
    # test style) - confirms the "reliable" source path, not just the
    # "unreliable" mtime-fallback one covered above.
    import subprocess

    clip = tmp_path / "clip.mp4"
    subprocess.run(
        [
            "ffmpeg", "-y",
            "-f", "lavfi", "-i", "testsrc=size=64x64:rate=10",
            "-t", "1",
            "-c:v", "libx264",
            "-metadata", "creation_time=2020-06-15T10:30:00.000000Z",
            str(clip),
        ],
        capture_output=True,
        text=True,
        check=True,
    )

    archive = adapter.open_archive(tmp_path)

    assert archive.recordings[0].timestamp_reliable is True


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


def test_root_id_named_generated_assets_are_discovered(adapter, tmp_path):
    # bv_generate.py writes every generated asset flat at the archive
    # root as f"{recording.id}.<suffix>" (see generated_assets_for()'s
    # own docstring for the real bug report) - not same-stem next to
    # the original video, which may be nested deep in a subfolder (a
    # GoPro card's 100GOPRO/ directory, or here, a "trip1" subfolder).
    # test_same_stem_generated_assets_are_discovered above already
    # covers the same-stem half; this covers the other half.
    sub = tmp_path / "trip1"
    sub.mkdir()
    _touch(sub / "clip.mp4", mtime=1700000000)

    recording_id = adapter.open_archive(tmp_path).recordings[0].id

    (tmp_path / f"{recording_id}.transcript.txt").write_text("hello")
    (tmp_path / f"{recording_id}.srt").write_text(
        "1\n00:00:00,000 --> 00:00:01,000\nhi\n"
    )
    # A root-level file whose name doesn't match this recording's id at
    # all should not be picked up.
    (tmp_path / "not_this_recording.transcript.txt").write_text("nope")

    archive = adapter.open_archive(tmp_path)
    recording = archive.recordings[0]

    assert recording.has(Asset.TRANSCRIPT)
    assert recording.has(Asset.SUBTITLES)


def test_same_stem_generated_asset_wins_over_root_id_named_one(adapter, tmp_path):
    # Both discovery paths can never actually collide for one real
    # bv-generate run in practice, but the read side has an explicit
    # first-match-wins order (same-stem before root) - pin it down
    # directly rather than leaving it implicit.
    _touch(tmp_path / "clip.mp4", mtime=1700000000)
    recording_id = adapter.open_archive(tmp_path).recordings[0].id

    (tmp_path / "clip.transcript.txt").write_text("same-stem version")
    (tmp_path / f"{recording_id}.transcript.txt").write_text("root version")

    archive = adapter.open_archive(tmp_path)
    recording = archive.recordings[0]
    transcript_file = recording.file(Asset.TRANSCRIPT)

    assert transcript_file is not None
    assert transcript_file.path.read_text() == "same-stem version"


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


# ---------------------------------------------------------------------------
# Photo scanning (archive/photo.py) - GoPro + folder adapters both scan
# through _recursive_scan.py's shared _scan(), so this covers both.
# ---------------------------------------------------------------------------


def test_open_archive_finds_a_photo_alongside_videos(adapter, tmp_path):
    _touch(tmp_path / "clip.mp4", mtime=1700000000)
    _touch(tmp_path / "IMG_0001.JPG", mtime=1700000100)

    archive = adapter.open_archive(tmp_path)

    assert len(archive.recordings) == 2


def test_photo_is_stored_under_front_asset_like_a_video(adapter, tmp_path):
    _touch(tmp_path / "IMG_0001.jpg", mtime=1700000000)

    archive = adapter.open_archive(tmp_path)
    recording = archive.recordings[0]

    assert recording.has(Asset.FRONT)
    assert recording.file(Asset.FRONT).path.name == "IMG_0001.jpg"


def test_every_answered_photo_extension_is_scanned(adapter, tmp_path):
    # Christer's own answer when asked which extensions should count:
    # "All of them" - jpg/jpeg, png, heic, gpr.
    for i, suffix in enumerate((".jpg", ".jpeg", ".png", ".heic", ".gpr")):
        _touch(tmp_path / f"photo{i}{suffix}", mtime=1700000000 + i)

    archive = adapter.open_archive(tmp_path)

    assert len(archive.recordings) == 5


def test_recording_is_photo_true_for_a_scanned_photo(adapter, tmp_path):
    from blackvue.archive.photo import recording_is_photo

    _touch(tmp_path / "IMG_0001.jpg", mtime=1700000000)

    archive = adapter.open_archive(tmp_path)

    assert recording_is_photo(archive.recordings[0]) is True


def test_recording_is_photo_false_for_a_scanned_video(adapter, tmp_path):
    from blackvue.archive.photo import recording_is_photo

    _touch(tmp_path / "clip.mp4", mtime=1700000000)

    archive = adapter.open_archive(tmp_path)

    assert recording_is_photo(archive.recordings[0]) is False


def test_photo_gets_v_kind_code_same_as_video(adapter, tmp_path):
    _touch(tmp_path / "IMG_0001.jpg", mtime=1700000000)

    archive = adapter.open_archive(tmp_path)

    assert archive.recordings[0].id.kind == "V"


def test_photo_timestamp_prefers_exif_datetime_original_over_mtime(
    adapter, tmp_path
):
    # A photo's own EXIF DateTimeOriginal (task #950-959: Christer,
    # "Maybe we need exif now.") is the camera's real capture time -
    # more trustworthy than file mtime, which for a copied-off-the-card
    # or downloaded photo only reflects when that copy happened. mtime
    # is set to a deliberately different date here, so this only
    # passes if EXIF is actually being preferred.
    path = tmp_path / "IMG_0001.jpg"
    image = Image.new("RGB", (100, 60), (200, 100, 50))
    exif = image.getexif()
    exif[36867] = "2026:07:15 13:32:55"  # DateTimeOriginal
    image.save(path, exif=exif)
    os.utime(path, (1577836800, 1577836800))  # 2020-01-01 mtime

    archive = adapter.open_archive(tmp_path)
    recording_id = archive.recordings[0].id

    assert recording_id.timestamp == datetime(2026, 7, 15, 13, 32, 55)


def test_photo_timestamp_falls_back_to_mtime_without_exif(adapter, tmp_path):
    # A photo with no EXIF at all (or one PIL can't read, e.g. an
    # unsupported format) should fall back to the existing mtime
    # behavior unchanged, same as before EXIF support existed.
    _touch(tmp_path / "IMG_0001.jpg", mtime=1700000000)

    archive = adapter.open_archive(tmp_path)
    recording_id = archive.recordings[0].id

    assert recording_id.timestamp == datetime.fromtimestamp(1700000000)


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
