from __future__ import annotations

from collections.abc import Callable, Iterable
from datetime import datetime, timedelta

from blackvue.archive.asset import Asset
from blackvue.archive.recording import Recording
from blackvue.archive.recording_id import RecordingId
from blackvue.lexicaltimeparser import TimeInterval
from blackvue.trip.trip import Trip


def recordings_with_front_video(
    recordings: Iterable[Recording],
) -> tuple[Recording, ...]:
    """Filter to only recordings with a Front asset - meant to be
    applied to whatever's handed to TripBuilder.build(), not a change
    to TripBuilder itself (which stays asset-agnostic).

    Not every recording in an archive has video: BlackVue keeps
    logging GPS/g-sensor/thumbnail data even for stretches whose
    Front/Rear video was never downloaded (a real, common case for an
    archive that isn't downloaded in full by default - only specific
    ranges pulled down later, once something nearby turned out to be
    worth keeping). Feeding those video-less recordings into
    TripBuilder alongside real ones caused two confirmed problems on a
    real archive:

    1. TripBuilder's gap rule is evaluated pairwise between
       consecutive recordings, not video segment to video segment. A
       run of several video-less GPS pings a few minutes apart each
       (individually under the gap threshold) could chain-bridge what
       was actually a much longer real gap between two genuine video
       segments into one trip.
    2. Even where trip splitting was otherwise correct, a video-less
       recording's own GPS fixes still got merged into the trip's
       route by trip_export.py. But concatenated Front/Rear video has
       no representation of missing time at all - it jump-cuts
       straight from one real segment to the next - while the merged
       GPS track still spans the real, unrepresented gap in between.
       render_map_video() has no way to know that gap didn't actually
       play out on screen, so it kept smoothly interpolating position
       across it: the map visibly crawling forward while the real
       video had already jump-cut past that whole stretch.

    A video-less recording filtered out here never belongs to any
    trip - it's simply not part of trip detection or export, the same
    as if it weren't in the archive at all for that purpose. It still
    shows up in a plain (non-`--trips`) `bv-ls` listing, since that
    doesn't go through TripBuilder.
    """

    return tuple(recording for recording in recordings if recording.has(Asset.FRONT))


DEFAULT_MAX_GAP = timedelta(minutes=5)

# Small fixed safety margin added on top of max_gap before a gap counts
# as a split. Exists to absorb measurement noise that has nothing to
# do with whether the vehicle actually stopped: .duration.txt is
# rounded to the nearest second (see compute_span), recording
# timestamps come from filenames with only 1-second resolution, and
# real dashcams take a moment to close one file and open the next
# even during genuinely continuous recording. None of that should be
# mistaken for a real gap. It's not a trip-detection knob the way
# max_gap is - just noise-absorption - so it defaults on rather than
# being opt-in like `bridge`/`recording_duration`.
DEFAULT_GAP_TOLERANCE = timedelta(seconds=10)

# Default cap on how long a continuous run of Parking-mode footage can
# span (real elapsed time, via `recording_duration` - see
# `max_parking_duration` below) before the drive that follows it
# starts a new trip. Confirmed as a real problem on Christer's own
# archive: BlackVue's Parking-mode timelapse can play back in a few
# minutes while representing well over an hour of real parked time,
# and `recording_duration`'s own fold-in (see its docstring) folded
# that entire real span in as "still continuous" with no ceiling -
# merging a drive, an hour-plus stop, and the next drive into one
# trip. 60 minutes was Christer's own choice: long enough that a
# normal errand-length stop doesn't split, short enough that his real
# ~90-minute case does.
DEFAULT_MAX_PARKING_DURATION = timedelta(minutes=60)

# build_for_interval()'s own tuning knobs - see its docstring for the
# full algorithm. Small enough that the common case (a single
# requested trip, out of an archive with many more before/after it)
# proves both of its real boundaries in one or two build() passes
# without ever reading duration data for recordings far outside it.
_INITIAL_BOUNDARY_MARGIN = 4
# Each retry doubles the margin (a "galloping search") rather than
# growing by a fixed step, so a request whose true boundary happens to
# sit unusually far from the seed range still converges in a small,
# bounded number of build() passes instead of one recording at a time.
_BOUNDARY_MARGIN_GROWTH = 2

# `bridge` may return any truthy value to bridge a gap (False/None to
# not) - conventionally a short human-readable reason string, which is
# what movement_bridges_gap() returns (see telemetry/movement.py) so
# build()'s own `reasons` output (below) can show *what* evidence
# bridged a given gap, not just that something did. A plain bool
# still works fine (see test_trip_builder.py's own fake bridges) -
# build() only ever checks truthiness, never the exact type.
Bridge = Callable[[Recording, Recording], "str | bool | None"]
RecordingDuration = Callable[[Recording], "int | None"]


class TripBuilder:
    """Groups recordings into trips.

    The primary rule is a time gap: consecutive recordings more than
    `max_gap` (plus `gap_tolerance`, a small fixed noise margin - see
    DEFAULT_GAP_TOLERANCE) apart start a new trip. The gap is measured
    from the *end* of the earlier recording where possible, not just
    its start - see `recording_duration` below.

    An optional `bridge` callback can override the gap rule for a
    specific gap: if `bridge(previous, current)` returns True for a
    gap that would otherwise split the trip, the two recordings are
    kept in the same trip anyway. `bridge` is only ever consulted
    when the (duration-adjusted) gap rule would split - it never
    forces a split on its own.

    An optional `recording_duration` callback returns a recording's
    real-world length in seconds (typically backed by its
    `.duration.txt` file - see
    `blackvue.generate.media.read_duration_seconds`), or None if
    unknown. When known, the gap to the *next* recording is measured
    from `previous.timestamp + duration` instead of bare
    `previous.timestamp` - i.e. the duration is folded in before the
    result is ever compared against `max_gap`. This matters most for
    long recordings (Parking-mode timelapses in particular, where the
    played-back file length is nothing like the real elapsed time):
    without it, a recording that's itself longer than `max_gap` can
    look like a gap to the *next* recording even when there was no
    real gap at all. A recording with no known duration falls back to
    its raw start timestamp, so this is backward compatible one
    recording at a time, not just when unset entirely.

    Passing neither `bridge` nor `recording_duration`, and leaving
    `gap_tolerance` at its default, reproduces the original pure
    start-to-start-gap behaviour for any max_gap realistically used
    (minutes, not single-digit seconds) - pass `gap_tolerance=
    timedelta(0)` for the literal old behaviour at any max_gap.

    An optional `max_parking_duration` caps how long a *continuous run*
    of Parking-mode (`RecordingId.is_parking`) recordings can span in
    real elapsed time (their `recording_duration`, summed across the
    trailing run - not just one recording's own length) before it
    forces a split, independent of the ordinary gap rule above. This
    only ever matters because of `recording_duration`'s own fold-in:
    a Parking-mode timelapse can play back in a few minutes while
    covering well over an hour of real parked time, and without a
    ceiling that entire span reads as "no gap at all" between the
    drive before it and the drive after - see
    DEFAULT_MAX_PARKING_DURATION's own comment for the real case this
    was built for.

    The check is prospective: before a recording is added to the
    current trip, build() asks whether *including* it would push the
    trailing run's total over the cap - if so, that recording is kept
    out of the trip it would otherwise have ended, and instead becomes
    the first recording of the next trip. Christer was explicit about
    this direction on a real case: a single Parking recording whose
    own real span already exceeds the cap must never be part of the
    trip it would otherwise close out. The same prospective check
    naturally covers a *combined* span too - two or more consecutive
    Parking recordings, individually under the cap, whose running
    total crosses it partway through still split from each other, not
    just at the point driving resumes - a single long parked stretch
    chunked into several Parking files by the dashcam itself
    shouldn't dodge the cap just because no one file individually is
    long enough. A trip can still legitimately *start* with a
    recording whose own span alone exceeds the cap, since there's
    nowhere earlier to exclude it from - the running total for that
    new trip is then that recording's own span, so whether the
    *next* recording joins it depends on the same prospective check
    same as anywhere else.

    A cap-forced split is never offered to `bridge` - it's a
    deliberate policy decision ("a stop this long is a new trip"), not
    ambiguous gap evidence like an unexplained time gap is, so there's
    nothing for movement evidence to usefully weigh in on. Requires
    `recording_duration` to be set (same as `max_gap`'s own duration
    -aware behaviour) - with it unset, or a specific recording's own
    duration lookup returning None, that recording contributes nothing
    towards the cap, so `max_parking_duration` is a safe no-op rather
    than an error in either case.

    A recording whose `timestamp_reliable` is False (see
    archive/recording.py's own docstring on that field - set by a
    recursive-folder scan when it had to fall all the way back to file
    mtime, with no telemetry/EXIF/container metadata to resolve a real
    capture time from) always forces a split on both sides, checked
    before the ordinary gap rule and never offered to `bridge` - same
    reasoning as the parking cap: there's no real time evidence for
    movement bridging to weigh in on when the gap measurement itself
    might be meaningless. A real case that motivated this: several
    stock/sample test-fixture clips with no embedded timestamp of any
    kind landed within a second of each other purely by mtime
    coincidence (a batch copy/download), and were about to be grouped
    into one trip despite having nothing to do with each other. This
    check is always on, unlike `max_parking_duration` - it's a
    correctness guard against a meaningless gap measurement, not an
    opt-in tuning knob. Checked via `getattr(recording,
    "timestamp_reliable", True)`, so any recording (real or a test
    double) that doesn't define the attribute at all is treated as
    reliable - unaffected, exactly as if this check didn't exist.
    """

    def __init__(
        self,
        max_gap: timedelta = DEFAULT_MAX_GAP,
        *,
        bridge: Bridge | None = None,
        recording_duration: RecordingDuration | None = None,
        gap_tolerance: timedelta = DEFAULT_GAP_TOLERANCE,
        max_parking_duration: timedelta | None = None,
    ):
        self.max_gap = max_gap
        self.bridge = bridge
        self.recording_duration = recording_duration
        self.gap_tolerance = gap_tolerance
        self.max_parking_duration = max_parking_duration

    def _end_timestamp(self, recording: Recording) -> datetime:
        if self.recording_duration is not None:
            duration_seconds = self.recording_duration(recording)
            if duration_seconds is not None:
                return recording.id.timestamp + timedelta(
                    seconds=duration_seconds
                )

        return recording.id.timestamp

    def _parking_contribution(self, recording: Recording) -> float:
        """How many seconds `recording` adds to a trailing run of
        continuous Parking-mode footage for `max_parking_duration`
        purposes - its own real `recording_duration` if it's a Parking
        -mode recording with a known duration, otherwise 0 (a non
        -Parking recording breaks the run entirely - see build()'s own
        reset of the running total - and an unknown duration simply
        can't be counted, the same "no signal, no contribution"
        handling `_end_timestamp()` already gives a missing duration).

        `max_parking_duration is None` (the feature unused) short
        -circuits to 0 before ever touching `recording.id.is_parking` -
        deliberately, so callers whose `recording`/`recording.id`
        stand-ins don't define `is_parking` at all (this project's own
        test suite includes several, predating this feature) still
        work unchanged as long as they never opt in.
        """

        if self.max_parking_duration is None or self.recording_duration is None:
            return 0.0

        if not recording.id.is_parking:
            return 0.0

        duration_seconds = self.recording_duration(recording)
        return float(duration_seconds) if duration_seconds is not None else 0.0

    def build(
        self,
        recordings: Iterable[Recording],
        *,
        reasons: dict[RecordingId, str] | None = None,
    ) -> list[Trip]:
        """Group `recordings` (assumed already sorted chronologically -
        see ArchiveReader.read(), which sorts by RecordingId) into
        trips.

        `reasons`, if given, is populated in place with one entry per
        recording (keyed by `recording.id`) explaining why it starts a
        new trip or continues the current one - the exact gap, the
        threshold it was compared against, and (if a gap over
        threshold was still bridged) what evidence bridged it. Meant
        for bv-export's own per-trip log file (see export/trip_log.py)
        so a surprising trip membership decision can be checked against
        the real reasoning that produced it, not re-derived after the
        fact by guessing.
        """

        recordings = tuple(recordings)

        if not recordings:
            return []

        trips: list[Trip] = []

        current_trip: list[Recording] = [recordings[0]]
        if reasons is not None:
            reasons[recordings[0].id] = "first recording in the archive"

        threshold = self.max_gap + self.gap_tolerance
        # Running total for the trailing (most recent, unbroken) run of
        # Parking-mode footage at the end of current_trip - reset to 0
        # by any non-Parking recording or any split, below. Always 0.0
        # (so `parking_cap_exceeded` can never fire) when
        # max_parking_duration is unset - see _parking_contribution()'s
        # own "safe no-op" handling of an unset/unknown duration.
        trailing_parking_seconds = self._parking_contribution(recordings[0])

        for recording in recordings[1:]:
            previous = current_trip[-1]

            gap = recording.id.timestamp - self._end_timestamp(previous)
            gap_desc = self._describe_gap(gap)
            threshold_desc = f"{threshold.total_seconds():.1f}s"

            # See build()'s own docstring on timestamp_reliable - a
            # gap measurement is only meaningful when both endpoints'
            # timestamps are real capture-time data, so this is
            # checked (and, if True, acted on) before parking_cap/gap/
            # bridge get anywhere near the gap value above.
            unreliable_timestamp = not getattr(
                previous, "timestamp_reliable", True
            ) or not getattr(recording, "timestamp_reliable", True)

            # Prospective, not retrospective: this asks whether
            # *including* `recording` would push the trailing run's
            # total over the cap - not whether it already is. That's
            # what keeps an over-the-cap Parking recording out of the
            # trip it would otherwise have ended, rather than letting
            # it in and only excluding whatever comes after it - see
            # build()'s own docstring for the real case that mattered
            # for.
            parking_increment = self._parking_contribution(recording)
            prospective_parking_seconds = trailing_parking_seconds + parking_increment

            parking_cap_exceeded = (
                self.max_parking_duration is not None
                and prospective_parking_seconds
                > self.max_parking_duration.total_seconds()
            )

            # A cap-forced split (or an unreliable-timestamp forced
            # split, below) is never offered to bridge - see build()'s
            # own docstring for why (both are deliberate policy
            # decisions / correctness guards, not ambiguous gap
            # evidence for movement bridging to weigh in on).
            bridge_reason = None
            if (
                not parking_cap_exceeded
                and not unreliable_timestamp
                and gap > threshold
                and self.bridge
            ):
                bridge_reason = self.bridge(previous, recording)

            if parking_cap_exceeded:
                if reasons is not None:
                    prospective_desc = f"{prospective_parking_seconds / 60:.1f}m"
                    cap_desc = (
                        f"{self.max_parking_duration.total_seconds() / 60:.1f}m"
                    )
                    reasons[recording.id] = (
                        "starts a new trip - including this recording would "
                        f"bring continuous Parking-mode footage to "
                        f"{prospective_desc}, over the {cap_desc} "
                        "max_parking_duration limit, so it starts the next "
                        "trip instead of ending this one"
                    )
                trips.append(
                    Trip(
                        tuple(current_trip),
                        recording_duration=self.recording_duration,
                    )
                )
                current_trip = [recording]
                trailing_parking_seconds = parking_increment
            elif unreliable_timestamp:
                if reasons is not None:
                    culprit = (
                        f"{previous.id}'s"
                        if not getattr(previous, "timestamp_reliable", True)
                        else "this recording's own"
                    )
                    reasons[recording.id] = (
                        f"starts a new trip - {culprit} timestamp could not "
                        "be reliably resolved (no telemetry/EXIF/container "
                        "metadata, fell back to file mtime), so it's never "
                        "auto-grouped with a neighboring recording "
                        "regardless of the measured gap"
                    )
                trips.append(
                    Trip(
                        tuple(current_trip),
                        recording_duration=self.recording_duration,
                    )
                )
                current_trip = [recording]
                trailing_parking_seconds = parking_increment
            elif gap > threshold and not bridge_reason:
                if reasons is not None:
                    reasons[recording.id] = (
                        f"starts a new trip - gap since {previous.id} was "
                        f"{gap_desc}, over the {threshold_desc} "
                        "max_gap+gap_tolerance threshold, and no movement "
                        "evidence bridged it"
                    )
                trips.append(
                    Trip(
                        tuple(current_trip),
                        recording_duration=self.recording_duration,
                    )
                )
                current_trip = [recording]
                trailing_parking_seconds = parking_increment
            else:
                if reasons is not None:
                    if gap > threshold:
                        reasons[recording.id] = (
                            f"continues the trip - gap since {previous.id} "
                            f"was {gap_desc}, over the {threshold_desc} "
                            f"max_gap+gap_tolerance threshold, but bridged "
                            f"by: {bridge_reason}"
                        )
                    else:
                        reasons[recording.id] = (
                            f"continues the trip - gap since {previous.id} "
                            f"was {gap_desc}, within the {threshold_desc} "
                            "max_gap+gap_tolerance threshold"
                        )
                current_trip.append(recording)
                # A non-Parking recording breaks the trailing run
                # entirely (reset, not left unchanged) - a Parking one
                # extends it by its own contribution (already computed
                # above as prospective_parking_seconds, which passed
                # the cap check to get here).
                if self.max_parking_duration is not None:
                    trailing_parking_seconds = (
                        prospective_parking_seconds
                        if recording.id.is_parking
                        else 0.0
                    )

        trips.append(
            Trip(tuple(current_trip), recording_duration=self.recording_duration)
        )

        return trips

    def build_for_interval(
        self,
        recordings: Iterable[Recording],
        interval: TimeInterval,
        *,
        reasons: dict[RecordingId, str] | None = None,
    ) -> list[Trip]:
        """Like build(), but reads only as much of `recordings` as is
        needed to produce every complete trip touching `interval`,
        instead of the whole archive - bv-export used to hand build()
        the entire archive on every run, even to export a single day,
        so trip detection (and the duration lookups its gap
        calculation depends on) cost the same however small the actual
        request was. Christer's own framing of the fix: "from time
        range, seek backwards until start found, then forward until
        end is found."

        The approach: seed a slice of `recordings` (assumed already
        sorted chronologically, same precondition as build()) to just
        the ones whose own `id.value` falls inside `interval`, run
        build() on it, and check whether the earliest/latest trip that
        actually touches `interval` still starts/ends exactly at the
        slice's own edge. If it does, that boundary isn't proven yet -
        there might be more of that trip just outside the slice that a
        real gap would otherwise have excluded, and there's no way to
        tell the difference without looking - so the slice grows
        further in that direction (see _INITIAL_BOUNDARY_MARGIN/
        _BOUNDARY_MARGIN_GROWTH) and build() runs again. Repeats until
        both boundaries land on a real, build()-confirmed gap, or the
        slice can no longer grow (it already spans the whole of
        `recordings`) - at which point every trip in the result is
        exactly what a full build() over all of `recordings` would
        also have produced, just without ever computing a duration for
        a recording far outside `interval`. An `interval` that already
        matches "the whole archive" (LexicalTimeParser's own all-open
        sentinel range) seeds the slice to everything and returns after
        exactly one build() pass - no slower than calling build()
        directly, so callers don't need to special-case that request
        shape themselves (though bv-export's own CLI does anyway, for
        clarity about when the archive-wide cost is expected).

        Deliberately grows both sides together each retry rather than
        independently, even once one side is already proven - simpler
        to reason about, and the extra recordings re-read on the
        already-settled side cost nothing beyond a few more (already-
        cached, after the first pass) duration lookups.

        `reasons` behaves exactly as it does for build() - populated
        from whichever slice this ultimately settles on, so an
        exported trip's own trip.log still shows the real gap/bridge/
        parking-cap reasoning that decided its membership, not a
        placeholder. Entries for recordings pulled in only to prove a
        boundary (a neighboring trip that turned out not to touch
        `interval`) are included too, same as a real archive-wide
        build() would leave in `reasons` for every recording it looked
        at - harmless, since callers look reasons up by a specific
        recording's own id, never iterate the whole dict.

        If `recording_duration` is set, every recording in the final
        window is guaranteed to have had it called on it at least once
        before this returns - not just the ones build()'s own gap
        -walking loop happened to need (it never calls it for a
        window's own last recording, since nothing follows it to
        trigger `_end_timestamp()` - the same gap this project's own
        archive-wide detection used to leave for its own chronologically
        last recording). This is what makes `load_or_compute_duration`
        as `recording_duration` (bv-export's own
        `--duration-heal-archive`) a clean, complete guarantee: every
        recording this search actually settled on gets a real
        `.duration.txt`, including whichever one happens to land right
        on the final window's own edge.
        """

        recordings = tuple(recordings)
        total = len(recordings)
        if total == 0:
            return []

        seed_indices = [
            index
            for index, recording in enumerate(recordings)
            if recording.id.value in interval
        ]
        if not seed_indices:
            return []

        seed_lo, seed_hi = seed_indices[0], seed_indices[-1]
        margin = _INITIAL_BOUNDARY_MARGIN

        while True:
            window_lo = max(0, seed_lo - margin)
            window_hi = min(total - 1, seed_hi + margin)

            window_reasons: dict[RecordingId, str] | None = (
                {} if reasons is not None else None
            )
            trips = self.build(
                recordings[window_lo : window_hi + 1], reasons=window_reasons
            )

            # seed_indices was non-empty, so at least one trip in this
            # window overlaps `interval` - relevant is never empty.
            relevant = [
                trip
                for trip in trips
                if any(recording.id.value in interval for recording in trip)
            ]
            first_relevant, last_relevant = relevant[0], relevant[-1]

            left_proven = (
                window_lo == 0
                or first_relevant.first_recording is not recordings[window_lo]
            )
            right_proven = (
                window_hi == total - 1
                or last_relevant.last_recording is not recordings[window_hi]
            )

            if (left_proven and right_proven) or (
                window_lo == 0 and window_hi == total - 1
            ):
                if reasons is not None:
                    reasons.update(window_reasons)
                if self.recording_duration is not None:
                    # See this method's own docstring for why - build()
                    # 's gap-walking loop alone never calls
                    # recording_duration on a window's own last
                    # recording, which would otherwise leave a hole in
                    # what --duration-heal-archive is supposed to mean.
                    for recording in recordings[window_lo : window_hi + 1]:
                        self.recording_duration(recording)
                return trips

            margin *= _BOUNDARY_MARGIN_GROWTH

    @staticmethod
    def _describe_gap(gap: timedelta) -> str:
        """A human-readable rendering of a gap for `reasons` messages -
        flags a negative gap explicitly (the previous/current
        recordings overlap, or aren't in chronological order) rather
        than printing a bare, easy-to-miss negative number - this is
        exactly the shape a real sort-order or duration-parsing bug
        would take, so it's worth being loud about here rather than
        letting it blend into an otherwise-normal-looking log line.
        """

        seconds = gap.total_seconds()
        if seconds < 0:
            return (
                f"{-seconds:.1f}s BEFORE the previous recording's own end "
                "(overlapping or out-of-order timestamps)"
            )
        return f"{seconds:.1f}s"
    