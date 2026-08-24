from datetime import datetime, timedelta

from blackvue.export.trip_stats import compute_trip_stats
from blackvue.telemetry.gps_reader import GpsFix
from blackvue.telemetry.movement import DEFAULT_SPEED_THRESHOLD_KMH


def _fix(offset_seconds, lat, lon, speed_kmh=None, *, valid=True, altitude=None):
    return GpsFix(
        timestamp=datetime(2026, 7, 15, 13, 0, 0) + timedelta(seconds=offset_seconds),
        valid=valid,
        latitude=lat,
        longitude=lon,
        speed_kmh=speed_kmh,
        course=45.0,
        altitude_meters=altitude,
    )


def test_compute_trip_stats_returns_none_for_fewer_than_two_positioned_fixes():
    assert compute_trip_stats(()) is None
    assert compute_trip_stats((_fix(0, 59.30, 18.000),)) is None


def test_compute_trip_stats_skips_invalid_and_unpositioned_fixes():
    fixes = (
        _fix(0, 59.30, 18.000, 40.0),
        _fix(1, None, None, 40.0),  # unpositioned - not a real fix
        _fix(2, 59.31, 18.001, 40.0, valid=False),  # invalid - GPS lost
        _fix(3, 59.32, 18.002, 40.0),
    )

    stats = compute_trip_stats(fixes)

    assert stats is not None
    # Only the two valid, positioned fixes (offsets 0 and 3) count.
    assert stats.distance_km > 0


def test_compute_trip_stats_distance_matches_known_geography():
    # Stockholm Central to Uppsala Central is roughly 68km as the crow
    # flies - a coarse sanity check on the haversine math, not an
    # exact fixture.
    fixes = (
        _fix(0, 59.3300, 18.0592, 0.0),
        _fix(3600, 59.8586, 17.6389, 0.0),
    )

    stats = compute_trip_stats(fixes)

    assert 60 < stats.distance_km < 75


def test_compute_trip_stats_average_and_max_speed():
    fixes = (
        _fix(0, 59.30, 18.000, 20.0),
        _fix(1, 59.31, 18.001, 40.0),
        _fix(2, 59.32, 18.002, 60.0),
    )

    stats = compute_trip_stats(fixes)

    assert stats.average_speed_kmh == 40.0
    assert stats.max_speed_kmh == 60.0


def test_compute_trip_stats_speed_fields_are_none_without_any_speed_data():
    fixes = (
        _fix(0, 59.30, 18.000, None),
        _fix(1, 59.31, 18.001, None),
    )

    stats = compute_trip_stats(fixes)

    assert stats.average_speed_kmh is None
    assert stats.max_speed_kmh is None
    assert stats.moving_seconds is None
    assert stats.idle_seconds is None


def test_compute_trip_stats_max_speed_rejects_a_lone_bad_gps_fix():
    # Real-archive report (Christer, 2026-08-23): a car with a real
    # 250 km/h electronic limiter showed a Stats-dashboard max speed
    # of 322.3 km/h - a single glitched $GPRMC speed-over-ground
    # reading, not a real jump (nothing before or after it confirms
    # the vehicle was ever going that fast). The lone-spike check
    # (jumps away from the last accepted reading, next reading snaps
    # straight back) should drop the 322.3 reading entirely, the same
    # way _reject_altitude_outliers() already drops an equivalent
    # altitude spike.
    fixes = (
        _fix(0, 59.30, 18.000, 118.0),
        _fix(1, 59.301, 18.0001, 120.5),
        _fix(2, 59.302, 18.0002, 322.3),
        _fix(3, 59.303, 18.0003, 119.8),
        _fix(4, 59.304, 18.0004, 121.2),
    )

    stats = compute_trip_stats(fixes)

    assert stats.max_speed_kmh == 121.2
    assert 118.0 <= stats.average_speed_kmh <= 122.0


def test_compute_trip_stats_max_speed_keeps_a_real_sustained_acceleration():
    # A genuinely large but *sustained* speed change (every reading
    # after the jump keeps climbing further, none of them snap back)
    # must not be mistaken for a lone bad fix - only a jump that
    # nothing afterward confirms gets dropped. The jump from ~40 to
    # 150 km/h is itself bigger than _SPEED_OUTLIER_JUMP_KMH, but the
    # two readings after it (155, 160) keep moving the same direction
    # instead of snapping back to ~40.
    fixes = (
        _fix(0, 59.30, 18.000, 35.0),
        _fix(1, 59.301, 18.0001, 40.0),
        _fix(2, 59.302, 18.0002, 150.0),
        _fix(3, 59.303, 18.0003, 155.0),
        _fix(4, 59.304, 18.0004, 160.0),
    )

    stats = compute_trip_stats(fixes)

    assert stats.max_speed_kmh == 160.0


def test_compute_trip_stats_max_speed_ignores_small_normal_fluctuation():
    # Ordinary reading-to-reading speed changes (well under
    # _SPEED_OUTLIER_JUMP_KMH's 100 km/h threshold) must pass through
    # completely unfiltered - this isn't a smoothing filter.
    fixes = (
        _fix(0, 59.30, 18.000, 90.0),
        _fix(1, 59.31, 18.001, 110.0),
        _fix(2, 59.32, 18.002, 95.0),
    )

    stats = compute_trip_stats(fixes)

    assert stats.max_speed_kmh == 110.0


def test_compute_trip_stats_max_speed_rejects_a_bad_leading_gps_fix():
    # Real-archive report (Christer, 2026-08-23, after the fix above
    # was already live): the Stats dashboard's archive-wide max speed
    # was still 322.3 km/h - the exact same number as before the fix
    # existed. The interior lone-spike check never even looks at a
    # recording's own first or last reading (it only evaluates
    # candidates against a "last accepted" reading that comes before
    # them), so a bad fix sitting at a recording's very first GPS
    # reading - exactly where a receiver's speed estimate is most
    # likely to glitch, right as it (re)acquires satellites at the
    # start of a fresh ~1-3 minute recording segment - sailed straight
    # through untouched and became the whole trip's own max. Here the
    # first reading is the glitch; the next two agree with each other,
    # so they're what should survive.
    fixes = (
        _fix(0, 59.30, 18.000, 322.3),
        _fix(1, 59.301, 18.0001, 80.0),
        _fix(2, 59.302, 18.0002, 82.0),
        _fix(3, 59.303, 18.0003, 85.0),
    )

    stats = compute_trip_stats(fixes)

    assert stats.max_speed_kmh == 85.0


def test_compute_trip_stats_max_speed_rejects_a_bad_trailing_gps_fix():
    # Mirror of the leading-edge case above - a bad fix at a
    # recording's very last GPS reading, which the interior check also
    # never evaluates (nothing comes after it to serve as its own
    # "following" confirmation).
    fixes = (
        _fix(0, 59.30, 18.000, 80.0),
        _fix(1, 59.301, 18.0001, 82.0),
        _fix(2, 59.302, 18.0002, 85.0),
        _fix(3, 59.303, 18.0003, 322.3),
    )

    stats = compute_trip_stats(fixes)

    assert stats.max_speed_kmh == 85.0


def test_compute_trip_stats_max_speed_excludes_post_reacquisition_decay():
    # Real-archive report (Christer, 2026-08-23, third follow-up): the
    # Stats dashboard's max speed was STILL 322.3 km/h even after both
    # the interior and leading/trailing-edge outlier filters shipped.
    # Tracing the actual recording's raw .gps file found why: this
    # wasn't a single bad tick that snaps back - it was a 3-tick
    # *decaying* run (322.3 -> 174.3 -> 80.4, then back to ~normal)
    # immediately after a real GPS dropout ended. Because each reading
    # differs from the one before it rather than snapping back in one
    # tick, _reject_speed_outliers() never flags any of them (proven
    # by this exact fixture: without the settle-window exclusion this
    # test is checking, the filtered max here would still be 322.3 -
    # see this module's own test for that shape,
    # test_compute_trip_stats_max_speed_rejects_a_lone_bad_gps_fix,
    # which only ever has *one* bad tick, not a multi-tick decay).
    #
    # The three valid=False fixes at offsets 3-5 simulate the real
    # dropout - a run of $GPRMC sentences reporting mode 'N' (no fix
    # at all), which read_gps() still turns into GpsFix objects (mode
    # 'N' just means valid=False, latitude/longitude/speed_kmh all
    # None - see gps_reader.py's _parse_rmc()), unlike the module's
    # first (superseded) design, which assumed those sentences never
    # reach compute_trip_stats() at all. compute_trip_stats() itself
    # still only reports stats over its "positioned" fixes, but
    # _speeds_excluding_reacquisition_settle() needs the *raw* fixes
    # (including these invalid ones) to tell "a real dropout just
    # ended" apart from "this recording just has sparse GPS" - see
    # that function's own docstring for why. The three readings right
    # after the dropout are the decaying glitch; readings a few
    # seconds later are back to a normal, plausible speed.
    fixes = (
        _fix(0, 59.30, 18.0000, 30.0),
        _fix(1, 59.301, 18.0001, 32.0),
        _fix(2, 59.302, 18.0002, 29.0),
        _fix(3, None, None, None, valid=False),
        _fix(4, None, None, None, valid=False),
        _fix(5, None, None, None, valid=False),
        _fix(6, 59.303, 18.0003, 322.3),
        _fix(7, 59.304, 18.0004, 174.0),
        _fix(8, 59.305, 18.0005, 80.0),
        _fix(9, 59.306, 18.0006, 31.0),
        _fix(10, 59.307, 18.0007, 28.0),
    )

    stats = compute_trip_stats(fixes)

    assert stats.max_speed_kmh == 32.0


def test_compute_trip_stats_max_speed_rejects_a_pre_dropout_glitch_cluster():
    # Real-archive report (Christer, 2026-08-24, on a separate archive
    # from a year earlier - 2025 - freshly stats'd for the first
    # time): archive-wide max speed was 305.9 km/h, again in the same
    # car with the same 250 km/h electronic limiter. Tracing the
    # culprit recording (20250730_070613_E) found a third shape: its
    # own first four $GPRMC readings (163.4/163.4/165.1/164.1 knots =
    # ~302.7/302.6/305.9/304.0 km/h) all mutually agree with EACH
    # OTHER - not a lone spike that jumps away and snaps back
    # (_reject_speed_outliers()'s shape, see
    # test_compute_trip_stats_max_speed_rejects_a_lone_bad_gps_fix)
    # and not a decay after a dropout ends
    # (_speeds_excluding_reacquisition_settle()'s shape, see the test
    # above this one) - immediately followed by a genuine ~45-second
    # dropout, after which the receiver reacquires at the *correct*
    # position with normal speeds. Because all four bad readings
    # corroborate each other, neither existing neighbor-relative
    # filter flags any of them (proven by this exact fixture: without
    # _reject_implausible_speeds(), the filtered max here would still
    # be ~305.9). This is why the ceiling fix is a flat cap rather
    # than another neighbor-relative heuristic - a fourth or fifth new
    # shape would just defeat those too.
    fixes = (
        _fix(0, 56.19158, 13.48568, 302.68),
        _fix(1, 56.19199, 13.48603, 302.57),
        _fix(2, 56.19242, 13.48637, 305.86),
        _fix(3, 56.19283, 13.48672, 304.02),
        _fix(4, None, None, None, valid=False),
        _fix(5, None, None, None, valid=False),
        _fix(6, None, None, None, valid=False),
        _fix(7, 59.25820, 18.08299, 0.42),
        _fix(8, 59.25819, 18.08300, 4.01),
        _fix(9, 59.25822, 18.08307, 10.62),
        _fix(10, 59.25826, 18.08319, 16.30),
        _fix(11, 59.25831, 18.08337, 21.72),
        _fix(12, 59.25837, 18.08359, 24.09),
    )

    stats = compute_trip_stats(fixes)

    assert stats.max_speed_kmh == 24.09


def test_compute_trip_stats_splits_moving_and_idle_time_by_speed_threshold():
    below = DEFAULT_SPEED_THRESHOLD_KMH - 1.0
    above = DEFAULT_SPEED_THRESHOLD_KMH + 20.0

    # Each segment is classified by the *mean* of its two endpoint
    # speeds (see compute_trip_stats()'s own docstring) - not by
    # either fix's instantaneous value alone. So: segment 1 (both ends
    # below threshold, 30s) is idle; segment 2 (below -> above, 45s)
    # and segment 3 (above -> below, 15s) each have a mean pulled
    # above threshold by the one fast endpoint they share, so both
    # count as moving.
    fixes = (
        _fix(0, 59.300, 18.0000, below),
        _fix(30, 59.301, 18.0005, below),
        _fix(75, 59.310, 18.0100, above),
        _fix(90, 59.311, 18.0105, below),
    )

    stats = compute_trip_stats(fixes)

    assert stats.idle_seconds == 30.0
    assert stats.moving_seconds == 45.0 + 15.0


def test_compute_trip_stats_classifies_a_segment_by_the_mean_of_both_endpoints():
    # One fix well above threshold, one well below - the segment's
    # mean speed lands above threshold, so the whole 10s segment
    # counts as moving, not idle.
    fixes = (
        _fix(0, 59.300, 18.0000, DEFAULT_SPEED_THRESHOLD_KMH + 20.0),
        _fix(10, 59.301, 18.0002, DEFAULT_SPEED_THRESHOLD_KMH - 4.0),
    )

    stats = compute_trip_stats(fixes)

    assert stats.moving_seconds == 10.0
    assert stats.idle_seconds == 0.0


def test_compute_trip_stats_skips_the_leading_gap_before_any_speed_data_exists():
    # Only the very first segment (before *any* real speed reading has
    # appeared yet anywhere in the trip) is genuinely unclassifiable -
    # there's nothing earlier to carry forward from. Once the first
    # real reading (offset 20) appears, it classifies the segment
    # leading into it too (see effective_speeds' forward-fill).
    fixes = (
        _fix(0, 59.300, 18.0000, None),
        _fix(10, 59.301, 18.0002, None),
        _fix(20, 59.302, 18.0004, DEFAULT_SPEED_THRESHOLD_KMH + 20.0),
    )

    stats = compute_trip_stats(fixes)

    assert stats.moving_seconds == 10.0
    assert stats.idle_seconds == 0.0


def test_compute_trip_stats_carries_the_last_known_speed_across_a_gap():
    # Regression test for a real bug Christer found on his own
    # archive: a long, continuously GPS-tracked drive (1708 fixes at
    # ~1Hz over ~28 real minutes) reported barely 40% of that span
    # across moving_seconds+idle_seconds combined, because any segment
    # between two fixes that *both* happened to lack their own speed
    # reading (common in practice, apparently, even with a good
    # position fix) was silently dropped - counted toward neither
    # bucket, with nothing in trip_info.txt to show time was missing.
    #
    # Here: a real 40 km/h reading at offset 0, then two fixes in a
    # row with no speed reading of their own (offsets 10 and 20 -
    # exactly the "neither endpoint has one" case that used to be
    # dropped entirely), then a real reading again at offset 30. The
    # whole 0->30 span must now be classified using the carried
    # -forward 40 km/h (above threshold - moving), not silently
    # dropped.
    above = DEFAULT_SPEED_THRESHOLD_KMH + 20.0
    fixes = (
        _fix(0, 59.3000, 18.0000, above),
        _fix(10, 59.3005, 18.0002, None),
        _fix(20, 59.3010, 18.0004, None),
        _fix(30, 59.3015, 18.0006, above),
    )

    stats = compute_trip_stats(fixes)

    assert stats.moving_seconds == 30.0
    assert stats.idle_seconds == 0.0


def test_compute_trip_stats_carry_forward_still_respects_a_later_speed_change():
    # The carried-forward value must actually update once a new real
    # reading appears, not just latch onto the very first one seen -
    # here the vehicle is fast (above threshold), then a gap with no
    # readings, then a real slow (idle) reading, then another gap.
    # That second gap must carry the *slow* reading forward, not the
    # original fast one.
    above = DEFAULT_SPEED_THRESHOLD_KMH + 20.0
    below = DEFAULT_SPEED_THRESHOLD_KMH - 1.0
    fixes = (
        _fix(0, 59.3000, 18.0000, above),
        _fix(10, 59.3005, 18.0002, None),
        _fix(20, 59.3010, 18.0004, below),
        _fix(30, 59.3015, 18.0006, None),
        _fix(40, 59.3020, 18.0008, None),
    )

    stats = compute_trip_stats(fixes)

    # 0->10 (carries `above` into the gap) and 10->20 (real `below`
    # reading arrives, but the segment is still classified by the mean
    # of the carried `above` and the new `below` - see the "classifies
    # by the mean" test above for that same rule) both land above
    # threshold; 20->30 and 30->40 both carry the newer `below`
    # reading forward and land below threshold.
    assert stats.moving_seconds == 10.0 + 10.0
    assert stats.idle_seconds == 10.0 + 10.0


# ---------------------------------------------------------------------------
# Elevation (altitude_meters, sourced from $GPGGA - see gps_reader.py) -
# Christer asked whether height could be calculated from the GPS data at
# all, with an eye toward a future stitch-video/playback overlay.
# ---------------------------------------------------------------------------


def test_compute_trip_stats_elevation_fields_are_none_without_any_altitude_data():
    fixes = (
        _fix(0, 59.30, 18.000),
        _fix(1, 59.31, 18.001),
    )

    stats = compute_trip_stats(fixes)

    assert stats.min_altitude_meters is None
    assert stats.max_altitude_meters is None
    assert stats.elevation_change_meters is None


def test_compute_trip_stats_min_max_altitude():
    # Deltas (15m, -25m) stay comfortably under
    # _ALTITUDE_OUTLIER_JUMP_METERS (30m) so none of these are treated
    # as a bad-fix spike - see the outlier-rejection tests below for
    # that case specifically.
    fixes = (
        _fix(0, 59.30, 18.000, altitude=100.0),
        _fix(1, 59.31, 18.001, altitude=115.0),
        _fix(2, 59.32, 18.002, altitude=90.0),
    )

    stats = compute_trip_stats(fixes)

    assert stats.min_altitude_meters == 90.0
    assert stats.max_altitude_meters == 115.0


def test_compute_trip_stats_elevation_change_is_the_net_change_not_a_range_or_total():
    # This field's own history (see trip_stats.py's top-of-file
    # comments for the full account): started as a max-minus-min
    # range, which couldn't tell "climbed, then descended, then
    # climbed again" apart from "never climbed" - Christer's "How is
    # that a gain?" (2026-08-23). Redefined to a cumulative-ascent
    # total (sum of every climb, descents excluded) - correct for a
    # "gain" label, but Christer's later "rename itto Elevation
    # change" would have left that math mismatched with a label that
    # implies negative values are possible, so it was redefined again
    # to a true net change: the dead-banded reference's final position
    # minus its starting position. For 100 -> 115 -> 90 -> 105: the
    # reference tracks 100 -> 115 -> 90 -> 105 (each step clears the
    # 2m dead-band), so net change is 105-100=5 - not the old
    # cumulative-ascent total of 30 (15 up, then 25 down un-counted,
    # then 15 up again), and not the range's 115-90=25 either. min/max
    # still track the actual altitude span independently (see
    # TripStats' own docstring).
    fixes = (
        _fix(0, 59.30, 18.000, altitude=100.0),
        _fix(1, 59.31, 18.001, altitude=115.0),
        _fix(2, 59.32, 18.002, altitude=90.0),
        _fix(3, 59.33, 18.003, altitude=105.0),
    )

    stats = compute_trip_stats(fixes)

    assert stats.min_altitude_meters == 90.0
    assert stats.max_altitude_meters == 115.0
    assert stats.elevation_change_meters == 5.0


def test_compute_trip_stats_elevation_change_is_zero_for_a_genuine_round_trip():
    # The exact shape Christer's "How is that a gain?" report was
    # about, now reframed for net-change semantics: drive up a hill
    # and back down to the same altitude. Under the old cumulative
    # -ascent definition this reported 50.0 (the climb counted, the
    # matching descent didn't) - correct for a "gain" label, but not
    # for "change": ending up at the same altitude you started at is
    # zero net change, full stop, the same way a round-trip drive is
    # zero net displacement even though real distance was covered.
    # min/max still show the full 0-50m span was reached (see
    # TripStats' own docstring) - "how high did it get" and "did it
    # end up net higher or lower" are two separate questions, both
    # answered correctly here.
    #
    # Climbs and descends gradually (5 readings, each 25m step) rather
    # than in one single jump-and-return - a lone up-then-immediately
    # -back-down single-fix shape is indistinguishable from a bad GPS
    # spike (see _reject_altitude_outliers()'s own docstring) and would
    # get filtered out entirely before this test's real point (that a
    # *genuine* descent nets back out the matching climb) ever gets
    # exercised.
    fixes = (
        _fix(0, 59.30, 18.000, altitude=0.0),
        _fix(1, 59.301, 18.0001, altitude=25.0),
        _fix(2, 59.302, 18.0002, altitude=50.0),
        _fix(3, 59.303, 18.0003, altitude=25.0),
        _fix(4, 59.304, 18.0004, altitude=0.0),
    )

    stats = compute_trip_stats(fixes)

    assert stats.min_altitude_meters == 0.0
    assert stats.max_altitude_meters == 50.0
    assert stats.elevation_change_meters == 0.0


def test_compute_trip_stats_elevation_bridges_gaps_missing_a_reading():
    # The middle fix has no altitude reading - unlike the speed-based
    # fields' own carry-forward fix (moving/idle above), altitude
    # simply drops any fix with no reading from the sequence fed into
    # _hysteresis_altitude_stats() rather than fragmenting into
    # disconnected segments around it, so offsets 0 and 2 are treated
    # as consecutive real readings 100m apart - contributing to the net
    # change, not None. See compute_trip_stats()'s own docstring for why (same
    # "bridge across gaps, don't silently drop the span" philosophy as
    # moving_seconds/idle_seconds).
    fixes = (
        _fix(0, 59.30, 18.000, altitude=100.0),
        _fix(1, 59.31, 18.001, altitude=None),
        _fix(2, 59.32, 18.002, altitude=200.0),
    )

    stats = compute_trip_stats(fixes)

    assert stats.min_altitude_meters == 100.0
    assert stats.max_altitude_meters == 200.0
    assert stats.elevation_change_meters == 100.0


def test_compute_trip_stats_elevation_change_none_with_only_one_reading():
    # Only one fix in the whole trip has an altitude reading at all -
    # min/max can still report that one value, but there's no delta to
    # measure "change" from.
    fixes = (
        _fix(0, 59.30, 18.000, altitude=100.0),
        _fix(1, 59.31, 18.001, altitude=None),
    )

    stats = compute_trip_stats(fixes)

    assert stats.min_altitude_meters == 100.0
    assert stats.max_altitude_meters == 100.0
    assert stats.elevation_change_meters is None


def test_compute_trip_stats_elevation_change_ignores_sub_deadband_gps_noise():
    # Real-archive confirmation (Christer, 2026-08-23): raw automotive
    # GPS altitude jitters +-1-4m tick-to-tick even at steady highway
    # speed - here the readings wobble by 1-1.5m around ~10m five
    # times in a row (real net change: 0m), which a naive raw-delta
    # sum would report as ~4m of bogus "change". The dead-band (2.0m)
    # should absorb all of it.
    fixes = (
        _fix(0, 59.30, 18.000, altitude=10.0),
        _fix(1, 59.301, 18.0001, altitude=11.0),
        _fix(2, 59.302, 18.0002, altitude=9.7),
        _fix(3, 59.303, 18.0003, altitude=11.2),
        _fix(4, 59.304, 18.0004, altitude=10.0),
    )

    stats = compute_trip_stats(fixes)

    assert stats.elevation_change_meters == 0.0
    # min/max track the dead-band reference, not each raw reading, so
    # neither one ever moves once sub-deadband noise wobbles under it.
    assert stats.min_altitude_meters == 10.0
    assert stats.max_altitude_meters == 10.0


def test_compute_trip_stats_elevation_change_registers_a_real_sustained_climb():
    # A genuine, larger-than-noise climb (each step exceeds the
    # dead-band) should pass through essentially unfiltered.
    fixes = (
        _fix(0, 59.30, 18.000, altitude=0.0),
        _fix(1, 59.301, 18.0001, altitude=5.0),
        _fix(2, 59.302, 18.0002, altitude=10.0),
        _fix(3, 59.303, 18.0003, altitude=15.0),
    )

    stats = compute_trip_stats(fixes)

    assert stats.elevation_change_meters == 15.0
    assert stats.min_altitude_meters == 0.0
    assert stats.max_altitude_meters == 15.0


def test_compute_trip_stats_elevation_rejects_a_lone_bad_gps_fix():
    # Second real-archive report (Christer, 2026-08-23): a stationary
    # ~50m baseline with one badly wrong fix (300m - multipath/bad
    # geometry, not a real 250m jump and drop in under two seconds)
    # used to blow elevation_change_meters up to roughly the size of the
    # bad jump, since it cleared the dead-band and got treated as a
    # real climb. The lone-spike check (jumps away, next reading snaps
    # straight back) should drop the 300m reading entirely before it
    # ever reaches the dead-band pass, leaving only the genuine
    # sub-deadband wobble around ~50m.
    fixes = (
        _fix(0, 59.30, 18.000, altitude=50.0),
        _fix(1, 59.301, 18.0001, altitude=50.5),
        _fix(2, 59.302, 18.0002, altitude=300.0),
        _fix(3, 59.303, 18.0003, altitude=49.8),
        _fix(4, 59.304, 18.0004, altitude=50.2),
    )

    stats = compute_trip_stats(fixes)

    assert stats.elevation_change_meters == 0.0
    assert stats.min_altitude_meters == 50.0
    assert stats.max_altitude_meters == 50.0


def test_compute_trip_stats_elevation_keeps_a_real_sustained_jump():
    # A genuinely large, *sustained* change (every reading after the
    # first big jump keeps climbing further, none of them snap back to
    # the earlier baseline) must not be mistaken for a lone bad fix -
    # only a jump that nothing afterward confirms gets dropped. Here
    # the jump from ~50m to 90m is itself bigger than
    # _ALTITUDE_OUTLIER_JUMP_METERS, same as the bad-fix case above,
    # but the two readings after it (95m, 100m) keep moving the same
    # direction instead of snapping back - confirmation the outlier
    # check requires before rejecting anything.
    fixes = (
        _fix(0, 59.30, 18.000, altitude=50.0),
        _fix(1, 59.301, 18.0001, altitude=50.5),
        _fix(2, 59.302, 18.0002, altitude=90.0),
        _fix(3, 59.303, 18.0003, altitude=95.0),
        _fix(4, 59.304, 18.0004, altitude=100.0),
    )

    stats = compute_trip_stats(fixes)

    assert stats.min_altitude_meters == 50.0
    assert stats.max_altitude_meters == 100.0
    assert stats.elevation_change_meters == 50.0


def test_compute_trip_stats_elevation_change_is_negative_for_a_net_descent():
    # The one shape none of the earlier definitions (range, cumulative
    # -ascent total) could ever produce: a trip that ends up net lower
    # than it started reports a *negative* elevation_change_meters, not
    # a clamped-to-zero or absolute-value figure - a real, honest
    # answer to "did it end up net higher or lower" (see
    # _hysteresis_altitude_stats()'s own docstring). A steady descent
    # from 100m to 40m nets to -60m.
    fixes = (
        _fix(0, 59.30, 18.000, altitude=100.0),
        _fix(1, 59.301, 18.0001, altitude=80.0),
        _fix(2, 59.302, 18.0002, altitude=60.0),
        _fix(3, 59.303, 18.0003, altitude=40.0),
    )

    stats = compute_trip_stats(fixes)

    assert stats.min_altitude_meters == 40.0
    assert stats.max_altitude_meters == 100.0
    assert stats.elevation_change_meters == -60.0
