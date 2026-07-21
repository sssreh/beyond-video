from blackvue.archive.asset import Asset
from blackvue.archive.asset_file import AssetFile
from blackvue.archive.recording import Recording
from blackvue.archive.recording_id import RecordingId
from blackvue.export.text import merge_text_assets
from blackvue.trip.trip import Trip


def _recording(ts: str, *, transcript: str | None, path) -> Recording:
    assets = {}
    if transcript is not None:
        transcript_path = path / f"{ts}_N.transcript.txt"
        transcript_path.write_text(transcript, encoding="utf-8")
        assets[Asset.TRANSCRIPT] = AssetFile(Asset.TRANSCRIPT, transcript_path)
    return Recording(id=RecordingId(f"{ts}_N"), assets=assets)


def test_merge_text_assets_joins_in_order_with_headers(tmp_path):
    first = _recording("20260720_100000", transcript="Hello there.", path=tmp_path)
    second = _recording("20260720_100500", transcript="General Kenobi.", path=tmp_path)
    trip = Trip((first, second))

    merged = merge_text_assets(trip, Asset.TRANSCRIPT)

    assert "# 20260720_100000_N" in merged
    assert "Hello there." in merged
    assert "# 20260720_100500_N" in merged
    assert "General Kenobi." in merged
    assert merged.index("Hello there.") < merged.index("General Kenobi.")


def test_merge_text_assets_returns_none_when_no_recording_has_it(tmp_path):
    recording = _recording("20260720_100000", transcript=None, path=tmp_path)
    trip = Trip((recording,))

    assert merge_text_assets(trip, Asset.TRANSCRIPT) is None


def test_merge_text_assets_skips_recordings_missing_the_asset(tmp_path):
    with_transcript = _recording(
        "20260720_100000", transcript="Only me.", path=tmp_path
    )
    without_transcript = _recording(
        "20260720_100500", transcript=None, path=tmp_path
    )
    trip = Trip((with_transcript, without_transcript))

    merged = merge_text_assets(trip, Asset.TRANSCRIPT)

    assert "Only me." in merged
    assert merged.count("# 202607") == 1
