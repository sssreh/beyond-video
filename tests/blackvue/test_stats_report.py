import json

from blackvue.archive.asset import Asset
from blackvue.archive.asset_file import AssetFile
from blackvue.archive.recording import Recording
from blackvue.archive.recording_id import RecordingId
from blackvue.stats_report import GPS_DEPENDENT_FIELDS
from blackvue.stats_report import STAT_FIELDS
from blackvue.stats_report import _boundary_bridge_km
from blackvue.stats_report import aggregate_recording_stats
from blackvue.stats_report import count_recordings_without_gps
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


def test_count_recordings_without_gps_counts_missing_and_false_has_gps():
    entries = [
        (RecordingId("20260101_100000_NF"), {"has_gps": True, "distance_km": 1.0}),
        (RecordingId("20260102_100000_NF"), {"has_gps": False}),
        # An older stats.json predating the has_gps field entirely -
        # missing the key altogether should count the same as False,
        # not silently pass a stats.get("has_gps") truthiness check.
        (RecordingId("20260103_100000_NF"), {"distance_km": 3.0}),
    ]

    assert count_recordings_without_gps(entries) == 2


def test_count_recordings_without_gps_zero_when_all_have_gps():
    entries = [
        (RecordingId("20260101_100000_NF"), {"has_gps": True}),
        (RecordingId("20260102_100000_NF"), {"has_gps": True}),
    ]

    assert count_recordings_without_gps(entries) == 0


def test_gps_dependent_fields_excludes_duration_and_gforce():
    # duration_seconds has its own non-GPS fallback chain (video span,
    # then .3gf) and every g-force field comes from the g-sensor
    # sidecar, not GPS - neither should trip the "no GPS" coverage
    # warning bv_stats.py prints when none of the *requested* fields
    # actually depend on a GPS fix.
    assert "duration_seconds" not in GPS_DEPENDENT_FIELDS
    assert "max_gforce_x" not in GPS_DEPENDENT_FIELDS
    assert "avg_gforce_z" not in GPS_DEPENDENT_FIELDS
    assert "distance_km" in GPS_DEPENDENT_FIELDS
    assert "elevation_gain_m" in GPS_DEPENDENT_FIELDS


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


def test_aggregate_groups_by_date():
    entries = [
        (RecordingId("20260823_090000_NF"), {"distance_km": 1.0}),
        (RecordingId("20260823_180000_NF"), {"distance_km": 2.0}),
        (RecordingId("20260824_090000_NF"), {"distance_km": 3.0}),
    ]

    buckets = aggregate_recording_stats(
        entries, grouping="date", fields=["distance_km"]
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
    # "date"/"week" which would keep them separate.
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


def test_aggregate_groups_by_monthday_zero_padded_and_in_numeric_order():
    # "date" partitions by exact calendar date; "monthday" is the
    # weekday-style recurring one - Christer's own "an x axis with 31
    # positions ... think like weekdays with 7 positions" - so day 3
    # of any month lands in the same bucket as day 3 of any other.
    # Named "monthday" (not this grouping's original "dayofmonth") to
    # actually match "weekday"'s own naming pattern - see
    # stats_report.py's _bucket_key() docstring for the full story.
    # Zero-padded keys ("03" not "3") so plain string sort still
    # yields numeric order (no "10" before "3" the way unpadded
    # strings would sort).
    entries = [
        (RecordingId("20260810_090000_NF"), {"distance_km": 1.0}),
        (RecordingId("20260703_090000_NF"), {"distance_km": 2.0}),
    ]

    buckets = aggregate_recording_stats(
        entries, grouping="monthday", fields=["distance_km"]
    )

    assert [bucket.key for bucket in buckets] == ["03", "10"]


def test_aggregate_monthday_recurs_across_different_months():
    # Both recordings are on the 5th of their own month - "monthday"
    # should merge them into a single "05" bucket the same way
    # "weekday" merges same-weekday recordings across weeks.
    entries = [
        (RecordingId("20260105_090000_NF"), {"distance_km": 1.0}),
        (RecordingId("20260805_090000_NF"), {"distance_km": 2.0}),
    ]

    buckets = aggregate_recording_stats(
        entries, grouping="monthday", fields=["distance_km"]
    )

    assert [bucket.key for bucket in buckets] == ["05"]
    assert buckets[0].values["distance_km"] == 3.0
    assert len(buckets[0].recordings) == 2


def test_aggregate_sum_field():
    entries = [
        (RecordingId("20260823_090000_NF"), {"distance_km": 10.0}),
        (RecordingId("20260823_180000_NF"), {"distance_km": 5.0}),
    ]

    buckets = aggregate_recording_stats(
        entries, grouping="all", fields=["distance_km"]
    )

    assert buckets[0].values["distance_km"] == 15.0


def test_aggregate_elevation_gain_is_bucket_range_not_summed_per_recording():
    # Christer, after a long-range summary reported 39302m of
    # elevation gain: "what goes up must come down." elevation_gain_m
    # used to be "sum" - each recording's own already-computed max-min
    # gain added into the bucket's total - which grows without bound
    # the more (short, individually-small-gain) recordings a bucket
    # spans, rather than reflecting the bucket's real net elevation
    # span. It's now "range": max(every recording's own
    # max_altitude_m) - min(every recording's own min_altitude_m).
    # Three recordings whose own per-recording elevation_gain_m
    # readings (10+15+8=33) would have summed well past what the
    # bucket's own altitude readings actually span (45-2=43 is
    # *more* here on purpose - the two numbers have no fixed relation
    # to each other, which is exactly the point: "sum" and "range" are
    # different quantities, not two ways of writing the same one).
    entries = [
        (
            RecordingId("20260823_090000_NF"),
            {"max_altitude_m": 30.0, "min_altitude_m": 20.0, "elevation_gain_m": 10.0},
        ),
        (
            RecordingId("20260823_120000_NF"),
            {"max_altitude_m": 45.0, "min_altitude_m": 30.0, "elevation_gain_m": 15.0},
        ),
        (
            RecordingId("20260823_180000_NF"),
            {"max_altitude_m": 10.0, "min_altitude_m": 2.0, "elevation_gain_m": 8.0},
        ),
    ]

    buckets = aggregate_recording_stats(
        entries, grouping="all", fields=["elevation_gain_m"]
    )

    assert buckets[0].values["elevation_gain_m"] == 43.0  # 45 (max) - 2 (min)


def test_aggregate_elevation_gain_survives_over_many_recordings():
    # The actual regression this guards: a bucket spanning thousands
    # of short recordings, each with a small but real max-min gain -
    # summing those (the old behavior) would keep growing without any
    # relation to real terrain; the bucket-wide range stays bounded by
    # the real min/max regardless of how many recordings contribute.
    entries = [
        (
            RecordingId(f"202608{(i % 28) + 1:02d}_{i % 24:02d}0000_NF"),
            {"max_altitude_m": 40.0 + (i % 5), "min_altitude_m": 10.0 - (i % 3)},
        )
        for i in range(500)
    ]

    buckets = aggregate_recording_stats(
        entries, grouping="all", fields=["elevation_gain_m"]
    )

    assert buckets[0].values["elevation_gain_m"] == 44.0 - 8.0


def test_aggregate_elevation_gain_ignores_fields_missing_altitude_readings():
    entries = [
        (RecordingId("20260823_090000_NF"), {"elevation_gain_m": 10.0}),  # no min/max
        (
            RecordingId("20260823_180000_NF"),
            {"max_altitude_m": 20.0, "min_altitude_m": 5.0},
        ),
    ]

    buckets = aggregate_recording_stats(
        entries, grouping="all", fields=["elevation_gain_m"]
    )

    # The first recording has no min/max_altitude_m at all, so it
    # simply doesn't contribute - same "missing isn't zero, and isn't
    # excluded from the rest" convention every other field follows.
    assert buckets[0].values["elevation_gain_m"] == 15.0


def test_aggregate_elevation_gain_none_when_no_recording_has_altitude_readings():
    entries = [
        (RecordingId("20260823_090000_NF"), {"distance_km": 4.0}),
    ]

    buckets = aggregate_recording_stats(
        entries, grouping="all", fields=["elevation_gain_m"]
    )

    assert buckets[0].values["elevation_gain_m"] is None


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


def test_estimate_gaps_fills_missing_distance_from_bucket_speed_basis():
    # Two recordings with real GPS data (50 km/h average, from 5km/0.1h
    # and 5km/0.1h - a clean, easy-to-check basis), plus one no-GPS
    # recording with only a duration - 0.1h at that same 50 km/h basis
    # should add 5.0 km.
    entries = [
        (RecordingId("20260823_090000_NF"), {"distance_km": 5.0, "duration_seconds": 360}),
        (RecordingId("20260823_091000_NF"), {"distance_km": 5.0, "duration_seconds": 360}),
        (RecordingId("20260823_092000_NF"), {"duration_seconds": 360}),  # no GPS at all
    ]

    buckets = aggregate_recording_stats(
        entries, grouping="all", fields=["distance_km"], estimate_gaps=True,
    )

    assert buckets[0].values["distance_km"] == 15.0
    assert buckets[0].estimated_distance_km == 5.0
    assert buckets[0].estimated_recording_count == 1


def test_estimate_gaps_never_estimates_parking_recordings():
    # A Parking-mode recording is stationary by definition - applying a
    # moving average speed to it would invent distance that was never
    # driven, so it must be excluded even though it has a duration and
    # no distance_km of its own, same as any other no-GPS recording.
    entries = [
        (RecordingId("20260823_090000_NF"), {"distance_km": 10.0, "duration_seconds": 600}),
        (RecordingId("20260823_091000_PF"), {"duration_seconds": 9}),
    ]

    buckets = aggregate_recording_stats(
        entries, grouping="all", fields=["distance_km"], estimate_gaps=True,
    )

    assert buckets[0].values["distance_km"] == 10.0
    assert buckets[0].estimated_distance_km is None
    assert buckets[0].estimated_recording_count == 0


def test_estimate_gaps_falls_back_to_global_basis_when_bucket_has_no_gps_data():
    # --group weekday (or any grouping) can put every no-GPS recording
    # into a bucket with zero real distance data of its own - there's
    # nothing bucket-local to build a speed basis from, so it must fall
    # back to the whole selection's basis instead of leaving the bucket
    # unfilled.
    entries = [
        (RecordingId("20260817_090000_NF"), {"distance_km": 6.0, "duration_seconds": 360}),  # Monday
        (RecordingId("20260818_090000_NF"), {"duration_seconds": 360}),  # Tuesday, no GPS at all
    ]

    buckets = aggregate_recording_stats(
        entries, grouping="weekday", fields=["distance_km"], estimate_gaps=True,
    )

    tuesday = next(b for b in buckets if b.key == "Tuesday")
    assert tuesday.estimated_recording_count == 1
    assert tuesday.estimated_distance_km == 6.0  # 6.0 km/h basis * 0.1h
    assert tuesday.values["distance_km"] == 6.0


def test_estimate_gaps_no_effect_without_the_flag():
    entries = [
        (RecordingId("20260823_090000_NF"), {"distance_km": 5.0, "duration_seconds": 360}),
        (RecordingId("20260823_092000_NF"), {"duration_seconds": 360}),
    ]

    buckets = aggregate_recording_stats(
        entries, grouping="all", fields=["distance_km"],
    )

    assert buckets[0].values["distance_km"] == 5.0
    assert buckets[0].estimated_distance_km is None
    assert buckets[0].estimated_recording_count == 0


def test_estimate_gaps_leaves_bucket_unfilled_when_nothing_anywhere_has_a_basis():
    entries = [
        (RecordingId("20260823_090000_NF"), {"duration_seconds": 360}),
        (RecordingId("20260823_092000_NF"), {"duration_seconds": 360}),
    ]

    buckets = aggregate_recording_stats(
        entries, grouping="all", fields=["distance_km"], estimate_gaps=True,
    )

    assert buckets[0].values["distance_km"] is None
    assert buckets[0].estimated_distance_km is None
    assert buckets[0].estimated_recording_count == 0


def test_boundary_bridge_splits_gap_proportionally_by_time():
    # Christer: "our stats file does not contain first and last gps
    # position, that could help a little if you have a previous
    # recording and a next recording gps position, yes i know, but a
    # straight line is better than nothing" then "based of time can
    # you see how much of the distance belong to each recording id".
    # Recording A's own GPS drops at 13:32:00, but A's own video (180s
    # from a 13:30:00 start) keeps running until 13:33:00 - a 60s tail
    # with no coverage. Recording B starts at 13:33:00 but doesn't
    # reacquire GPS until 13:33:30 - a 30s head with no coverage. The
    # 90s total gap between A's last fix and B's first should split
    # 60/90 to A, 30/90 to B.
    id_a = RecordingId("20260823_133000_NF")
    id_b = RecordingId("20260823_133300_NF")
    stats_a = {
        "duration_seconds": 180,
        "end_gps": {"lat": 60.0, "lon": 20.0, "time": "2026-08-23T13:32:00"},
    }
    stats_b = {
        "duration_seconds": 180,
        "start_gps": {"lat": 60.01, "lon": 20.02, "time": "2026-08-23T13:33:30"},
    }

    bonus = _boundary_bridge_km([(id_a, stats_a), (id_b, stats_b)])

    assert id_a in bonus and id_b in bonus
    total = bonus[id_a] + bonus[id_b]
    assert round(bonus[id_a] / total, 3) == round(60 / 90, 3)
    assert round(bonus[id_b] / total, 3) == round(30 / 90, 3)
    assert total > 0


def test_boundary_bridge_adds_to_bucket_distance_as_its_own_visible_figure():
    id_a = RecordingId("20260823_133000_NF")
    id_b = RecordingId("20260823_133300_NF")
    entries = [
        (
            id_a,
            {
                "duration_seconds": 180,
                "distance_km": 1.0,
                "end_gps": {"lat": 60.0, "lon": 20.0, "time": "2026-08-23T13:32:00"},
            },
        ),
        (
            id_b,
            {
                "duration_seconds": 180,
                "distance_km": 1.0,
                "start_gps": {
                    "lat": 60.01, "lon": 20.02, "time": "2026-08-23T13:33:30",
                },
            },
        ),
    ]

    buckets = aggregate_recording_stats(entries, grouping="all", fields=["distance_km"])
    bucket = buckets[0]

    # Real per-recording sums (1.0 + 1.0) plus the bridged bonus, kept
    # visible separately rather than silently blended in - same
    # "never blend measured and inferred with no trace" convention
    # estimated_distance_km already follows.
    assert bucket.bridged_recording_count == 2
    assert bucket.bridged_distance_km > 0
    assert round(bucket.values["distance_km"], 6) == round(
        2.0 + bucket.bridged_distance_km, 6
    )


def test_boundary_bridge_skipped_when_gap_exceeds_five_minutes():
    # A gap this long is more likely two unrelated trips (a long
    # parked stop) than one continuous GPS dropout - see
    # _boundary_bridge_km()'s own docstring for why it reuses trip/
    # trip_builder.py's own 5-minute DEFAULT_MAX_GAP as the cutoff.
    id_a = RecordingId("20260823_133000_NF")
    id_b = RecordingId("20260823_140000_NF")
    stats_a = {
        "duration_seconds": 180,
        "end_gps": {"lat": 60.0, "lon": 20.0, "time": "2026-08-23T13:32:00"},
    }
    stats_b = {
        "duration_seconds": 180,
        "start_gps": {"lat": 60.01, "lon": 20.02, "time": "2026-08-23T14:00:10"},
    }

    bonus = _boundary_bridge_km([(id_a, stats_a), (id_b, stats_b)])

    assert bonus == {}


def test_boundary_bridge_excludes_parking_recordings():
    id_a = RecordingId("20260823_133000_NF")
    id_p = RecordingId("20260823_133300_PF")
    stats_a = {
        "duration_seconds": 180,
        "end_gps": {"lat": 60.0, "lon": 20.0, "time": "2026-08-23T13:32:00"},
    }
    stats_p = {
        "duration_seconds": 180,
        "start_gps": {"lat": 60.01, "lon": 20.02, "time": "2026-08-23T13:33:30"},
    }

    bonus = _boundary_bridge_km([(id_a, stats_a), (id_p, stats_p)])

    assert bonus == {}


def test_boundary_bridge_ignores_stats_json_without_migrated_timestamp():
    # A .stats.json written before this feature shipped has start_gps/
    # end_gps with lat/lon but no "time" key yet - self-heals the next
    # time bv-generate --stats runs over it (see _do_stats()'s read
    # -merge-write), but until then there's nothing to bridge with.
    id_a = RecordingId("20260823_133000_NF")
    id_b = RecordingId("20260823_133300_NF")
    stats_a = {
        "duration_seconds": 180,
        "end_gps": {"lat": 60.0, "lon": 20.0},  # no "time"
    }
    stats_b = {
        "duration_seconds": 180,
        "start_gps": {"lat": 60.01, "lon": 20.02, "time": "2026-08-23T13:33:30"},
    }

    bonus = _boundary_bridge_km([(id_a, stats_a), (id_b, stats_b)])

    assert bonus == {}


def test_boundary_bridge_excluded_from_estimate_gaps_double_counting():
    # The one case where estimate_gaps and boundary bridging could
    # both apply to the same recording: a single-fix recording (real
    # start_gps/end_gps, but too few fixes for compute_trip_stats() to
    # derive distance_km at all - so distance_km is None). Once the
    # boundary bridge has already credited it, estimate_gaps must not
    # also extrapolate a whole-duration speed-basis guess on top.
    id_a = RecordingId("20260823_090000_NF")  # has real distance_km + duration
    id_b = RecordingId("20260823_133000_NF")  # single-fix, distance_km None
    id_c = RecordingId("20260823_133300_NF")
    entries = [
        (id_a, {"duration_seconds": 3600, "distance_km": 60.0}),
        (
            id_b,
            {
                "duration_seconds": 180,
                "distance_km": None,
                "end_gps": {
                    "lat": 60.0, "lon": 20.0, "time": "2026-08-23T13:32:30",
                },
            },
        ),
        (
            id_c,
            {
                "duration_seconds": 180,
                "start_gps": {
                    "lat": 60.005, "lon": 20.01, "time": "2026-08-23T13:33:10",
                },
            },
        ),
    ]

    buckets = aggregate_recording_stats(
        entries, grouping="all", fields=["distance_km"], estimate_gaps=True,
    )
    bucket = buckets[0]

    # id_b received a boundary bridge contribution, so estimate_gaps'
    # own "missing" list must exclude it - only a genuinely GPS-less
    # recording would still need the speed-basis extrapolation.
    assert bucket.estimated_recording_count == 0
    assert bucket.estimated_distance_km is None
    assert bucket.bridged_recording_count >= 1


def test_stat_fields_cover_every_generate_stats_numeric_field():
    expected = {
        "duration_seconds", "distance_km", "moving_seconds", "idle_seconds",
        "avg_speed_kmh", "max_speed_kmh", "min_altitude_m", "max_altitude_m",
        "elevation_gain_m", "max_gforce_x", "avg_gforce_x", "max_gforce_y",
        "avg_gforce_y", "max_gforce_z", "avg_gforce_z",
    }

    assert set(STAT_FIELDS) == expected
