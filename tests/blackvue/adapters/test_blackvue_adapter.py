"""
Tests for adapters/blackvue/adapter.py - BlackVueAdapter.

BlackVueAdapter is deliberately a pure delegation wrapper (see its own
module docstring): these tests check two things - that it actually
forwards to the real, existing functions (not a silent reimplementation
that could drift), and that its manifest-driven capability guard fires
where the manifest says it should.
"""

from pathlib import Path

import pytest

from blackvue.adapters import registry
from blackvue.adapters.base import AdapterCapabilityError
from blackvue.adapters.blackvue import adapter as adapter_module
from blackvue.adapters.blackvue.adapter import BlackVueAdapter
from blackvue.archive.archive import Archive
from blackvue.archive.asset import Asset


@pytest.fixture()
def adapter():
    return BlackVueAdapter()


def test_manifest_is_the_real_blackvue_manifest(adapter):
    assert adapter.manifest.adapter_id == "blackvue"
    assert adapter.manifest == registry.load_adapter_manifest("blackvue")


# ---------------------------------------------------------------------------
# open_archive() - real, working delegation to Archive(path), proven by
# reading an actual (synthetic) BlackVue-shaped archive rather than just
# mocking it away.
# ---------------------------------------------------------------------------


def test_open_archive_reads_a_synthetic_archive_like_archive_class_does(
    adapter, tmp_path
):
    (tmp_path / "20260101_120000_NF.mp4").write_bytes(b"front")
    (tmp_path / "20260101_120000_NR.mp4").write_bytes(b"rear")
    (tmp_path / "20260101_120000_NF.thm").write_bytes(b"thumb")

    via_adapter = adapter.open_archive(tmp_path)
    via_direct = Archive(tmp_path)

    assert [r.id for r in via_adapter.recordings] == [r.id for r in via_direct.recordings]
    assert len(via_adapter.recordings) == 1

    recording = via_adapter.recordings[0]
    assert recording.has(Asset.FRONT)
    assert recording.has(Asset.REAR)
    assert recording.has(Asset.FRONT_THUMBNAIL)


def test_open_archive_returns_a_real_archive_instance(adapter, tmp_path):
    assert isinstance(adapter.open_archive(tmp_path), Archive)


# ---------------------------------------------------------------------------
# read_gps() / read_gsensor() - true pass-through delegation, verified by
# monkeypatching the underlying module-level functions this class
# imports and checking the exact same args/return value cross the
# boundary unchanged.
# ---------------------------------------------------------------------------


def test_read_gps_delegates_to_telemetry_gps_reader(adapter, monkeypatch):
    calls = []

    def fake_read_gps(path):
        calls.append(path)
        return ("SENTINEL_GPS",)

    monkeypatch.setattr(adapter_module, "_read_gps_file", fake_read_gps)

    result = adapter.read_gps(Path("/fake/rec.gps"))

    assert result == ("SENTINEL_GPS",)
    assert calls == [Path("/fake/rec.gps")]


def test_read_gsensor_delegates_to_telemetry_gsensor_reader(adapter, monkeypatch):
    calls = []

    def fake_read_gsensor(path):
        calls.append(path)
        return ("SENTINEL_GSENSOR",)

    monkeypatch.setattr(adapter_module, "_read_gsensor_file", fake_read_gsensor)

    result = adapter.read_gsensor(Path("/fake/rec.3gf"))

    assert result == ("SENTINEL_GSENSOR",)
    assert calls == [Path("/fake/rec.3gf")]


# ---------------------------------------------------------------------------
# connect() / config_snapshot_seconds() - same pass-through pattern, plus
# the manifest-driven capability guard.
# ---------------------------------------------------------------------------


def test_connect_delegates_to_core_connection_connect(adapter, monkeypatch):
    calls = []

    def fake_connect(endpoints, timeout=5):
        calls.append((endpoints, timeout))
        return ("SENTINEL_ENDPOINT", "SENTINEL_CLIENT")

    monkeypatch.setattr(adapter_module, "_connect", fake_connect)

    result = adapter.connect(["fake-endpoints"], timeout=9)

    assert result == ("SENTINEL_ENDPOINT", "SENTINEL_CLIENT")
    assert calls == [(["fake-endpoints"], 9)]


def test_config_snapshot_seconds_delegates_to_parse_record_time_seconds(
    adapter, monkeypatch
):
    calls = []

    def fake_parse(text):
        calls.append(text)
        return 999

    monkeypatch.setattr(adapter_module, "parse_record_time_seconds", fake_parse)

    assert adapter.config_snapshot_seconds("dummy ini text") == 999
    assert calls == ["dummy ini text"]


def test_connect_raises_when_manifest_disallows_network_connect(adapter):
    class _FakeManifest:
        adapter_id = "blackvue"

        def supports(self, capability):
            return False

    adapter.manifest = _FakeManifest()

    with pytest.raises(AdapterCapabilityError):
        adapter.connect([])


def test_config_snapshot_seconds_raises_when_manifest_disallows_it(adapter):
    class _FakeManifest:
        adapter_id = "blackvue"

        def supports(self, capability):
            return False

    adapter.manifest = _FakeManifest()

    with pytest.raises(AdapterCapabilityError):
        adapter.config_snapshot_seconds("text")


def test_real_manifest_actually_allows_connect_and_config_snapshot(adapter):
    # Sanity check the fixture manifest itself supports what the guard
    # tests above assume it would otherwise allow.
    assert adapter.manifest.supports("network_connect")
    assert adapter.manifest.supports("config_snapshot")
