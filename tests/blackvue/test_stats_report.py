import json

from blackvue.archive.asset import Asset
from blackvue.archive.asset_file import AssetFile
from blackvue.archive.recording import Recording
from blackvue.archive.recording_id import RecordingId
from blackvue.stats_report import STAT_FIELDS
from blackvue.stats_report import aggregate_recording_stats
from blackvue.stats_report import load_recording_stats


def _make_recording_with_stats(recording_id: str, tmp_path, stats: dict) -> Recording:
    recording = Recording(id=RecordingId(recording_id))
    path = tmp_path / f"{recording_id}.stats.json"
    path.write_text(json.dumps(stats), encoding="utf-8")
    recording.assets[Asset.RECORDING_STATS] = AssetFile(
        asset=Asset.RECORDING_STATS, path=path
    )
    return recording


def test_load_recording_stats_returns_none_without_asset():
    recording = Recording(id=RecordingId("20260823_100000_NF"))

    assert load_recording_stats(recording) is None


def test_load_recording_stats_returns_none_for_missing_file(tmp_path):
    recording = Recording(id=RecordingId("20260823_100000_NF"))
    missing_path = tmp_path / "20260823_100000_N.stats.json"
    recording.assets[Asset.RECORDING_STATS] = AssetFile(
        asset=Asset.RECORDING_STATS, path=missing_path
    )

    assert load_recording_stats(recording) is None


def test_load_recording_stats_returns_none_for_corrupt_json(tmp_path):
    recording = Recording(id=RecordingId("20260823_100000_NF"))
    path = tmp_path / "20260823_100000_N.stats.json"
    path.write_text("{not valid json", encoding="utf-8")
    recording.assets[Asset.RECORDING_STATS] = AssetFile(
        asset=Asset.RECORDING_STATS, path=path
    )

    assert load_recording_stats(recording) is None


def test_load_recording_stats_parses_real_json(tmp_path):
    recording = _make_recording_with_stats(
        "20260823_100000_NF", tmp_path, {"distance_km": 1.5}
    )

    assert load_recording_stats(recording) == {"distance_km": 1.5}


def test_aggregate_groups_all_into_one_bucket():
    entries = [
        (RecordingId("20260101_100000_NF"), {"distance_km": 1.0}),
        (RecordingId("20260823_100000_NF"), {"distance_km": 2.0}),
    ]

    buckets = aggregate_recording_stats(
        entries, grouping="all", fields=["distance_km"]
    )

    assert len(buckets) == 1
    assert buckets[0].key == "all"
    assert buckets[0].values["distance_km"] == 3.0
    assert set(buckets[0].recordings) == {entry[0] for entry in entries}


def test_aggregate_groups_by_year():
    entries = [
        (RecordingId("20250101_100000_NF"), {"distance_km": 1.0}),
        (RecordingId("20260823_100000_NF"), {"distance_km": 2.0}),
    ]

    buckets = aggregate_recording_stats(
        entries, grouping="year", fields=["distance_km"]
    )

    assert [bucket.key for bucket in buckets] == ["2025", "2026"]
    assert buckets[0].values["distance_km"] == 1.0
    assert buckets[1].values["distance_km"] == 2.0


def test_aggregate_groups_by_month():
    entries = [
        (RecordingId("20260701_100000_NF"), {"distance_km": 1.0}),
        (RecordingId("20260823_100000_NF"), {"distance_km": 2.0}),
    ]

    buckets = aggregate_recording_stats(
        entries, grouping="month", fields=["distance_km"]
    )

    assert [bucket.key for bucket in buckets] == ["2026-07", "2026-08"]


def test_aggregate_groups_by_monthday():
    entries = [
        (RecordingId("20260823_090000_NF"), {"distance_km": 1.0}),
        (RecordingId("20260823_180000_NF"), {"distance_km": 2.0}),
        (RecordingId("20260824_090000_NF"), {"distance_km": 3.0}),
    ]

    buckets = aggregate_recording_stats(
        entries, grouping="monthday", fields=["distance_km"]
    )

    assert [bucket.key for bucket in buckets] == ["2026-08-23", "2026-08-24"]
    assert buckets[0].values["distance_km"] == 3.0
    assert len(buckets[0].recordings) == 2


def test_aggregate_groups_by_week():
    # 2026-08-23 is a Sunday (ISO week 34); 2026-08-24 is a Monday
    # (ISO week 35) - a real week boundary, not an arbitrary date pair.
    entries = [
        (RecordingId("20260823_090000_NF"), {"distance_km": 1.0}),
        (RecordingId("20260824_090000_NF"), {"distance_km": 2.0}),
    ]

    buckets = aggregate_recording_stats(
        entries, grouping="week", fields=["distance_km"]
    )

    assert [bucket.key for bucket in buckets] == ["2026-W34", "2026-W35"]


def test_aggregate_groups_by_weekday_in_calendar_order_not_alphabetical():
    # 2026-08-24 is a Monday, 2026-08-26 is a Wednesday - alphabetical
    # order would put "Monday" after "Wednesday", calendar order
    # shouldn't.
    entries = [
        (RecordingId("20260826_090000_NF"), {"distance_km": 1.0}),
        (RecordingId("20260824_090000_NF"), {"distance_km": 2.0}),
    ]

    buckets = aggregate_recording_stats(
        entries, grouping="weekday", fields=["distance_km"]
    )

    assert [bucket.key for bucket in buckets] == ["Monday", "Wednesday"]


def test_aggregate_weekday_recurs_across_multiple_weeks():
    # Both dates are Mondays, three weeks apart - "weekday" grouping
    # should merge them into a single "Monday" bucket, unlike
    # "monthday"/"week" which would keep them separate.
    entries = [
        (RecordingId("20260803_090000_NF"), {"distance_km": 1.0}),
        (RecordingId("20260824_090000_NF"), {"distance_km": 2.0}),
    ]

    buckets = aggregate_recording_stats(
        entries, grouping="weekday", fields=["distance_km"]
    )

    assert [bucket.key for bucket in buckets] == ["Monday"]
    assert buckets[0].values["distance_km"] == 3.0
    assert len(buckets[0].recordings) == 2


def test_aggregate_sum_field():
    entries = [
        (RecordingId("20260823_090000_NF"), {"elevation_gain_m": 10.0}),
        (RecordingId("20260823_180000_NF"), {"elevation_gain_m": 5.0}),
    ]

    buckets = aggregate_recording_stats(
        entries, grouping="all", fields=["elevation_gain_m"]
    )

    assert buckets[0].values["elevation_gain_m"] == 15.0


def test_aggregate_avg_field():
    entries = [
        (RecordingId("20260823_090000_NF"), {"avg_speed_kmh": 10.0}),
        (RecordingId("20260823_180000_NF"), {"avg_speed_kmh": 20.0}),
    ]

    buckets = aggregate_recording_stats(
        entries, grouping="all", fields=["avg_speed_kmh"]
    )

    assert buckets[0].values["avg_speed_kmh"] == 15.0


def test_aggregate_max_field():
    entries = [
        (RecordingId("20260823_090000_NF"), {"max_speed_kmh": 80.0}),
        (RecordingId("20260823_180000_NF"), {"max_speed_kmh": 95.0}),
    ]

    buckets = aggregate_recording_stats(
        entries, grouping="all", fields=["max_speed_kmh"]
    )

    assert buckets[0].values["max_speed_kmh"] == 95.0


def test_aggregate_min_field():
    entries = [
        (RecordingId("20260823_090000_NF"), {"min_altitude_m": 10.0}),
        (RecordingId("20260823_180000_NF"), {"min_altitude_m": -5.0}),
    ]

    buckets = aggregate_recording_stats(
        entries, grouping="all", fields=["min_altitude_m"]
    )

    assert buckets[0].values["min_altitude_m"] == -5.0


def test_aggregate_field_none_when_no_recording_has_a_reading():
    entries = [
        (RecordingId("20260823_090000_NF"), {"distance_km": 1.0}),
    ]

    buckets = aggregate_recording_stats(
        entries, grouping="all", fields=["max_speed_kmh"]
    )

    assert buckets[0].values["max_speed_kmh"] is None


def test_aggregate_field_skips_missing_readings_rather_than_treating_as_zero():
    entries = [
        (RecordingId("20260823_090000_NF"), {"distance_km": 4.0}),
        (RecordingId("20260823_180000_NF"), {}),  # no distance_km at all
    ]

    buckets = aggregate_recording_stats(
        entries, grouping="all", fields=["distance_km"]
    )

    # If the missing reading were treated as 0.0 this would still be
    # 4.0 for a sum - the real regression this guards is an average:
    assert buckets[0].values["distance_km"] == 4.0


def test_aggregate_avg_skips_missing_readings_not_averaged_as_zero():
    entries = [
        (RecordingId("20260823_090000_NF"), {"avg_speed_kmh": 40.0}),
        (RecordingId("20260823_180000_NF"), {}),  # no avg_speed_kmh at all
    ]

    buckets = aggregate_recording_stats(
        entries, grouping="all", fields=["avg_speed_kmh"]
    )

    # A naive "sum / recording_count" would give 20.0 - the missing
    # reading must not silently drag the average down.
    assert buckets[0].values["avg_speed_kmh"] == 40.0


def test_stat_fields_cover_every_generate_stats_numeric_field():
    expected = {
        "duration_seconds", "distance_km", "moving_seconds", "idle_seconds",
        "avg_speed_kmh", "max_speed_kmh", "min_altitude_m", "max_altitude_m",
        "elevation_gain_m", "max_gforce_x", "avg_gforce_x", "max_gforce_y",
        "avg_gforce_y", "max_gforce_z", "avg_gforce_z",
    }

    assert set(STAT_FIELDS) == expected
