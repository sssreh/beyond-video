"""
Per-recording computed statistics (the RECORDING_STATS asset, written
by bv-generate --stats as a single <id>.stats.json file).

See WORKING_CONTEXT.md's "bv-web statistics dashboard + per-recording
stats asset" note for the full design discussion this module
implements: why one JSON file rather than one Asset per field, the
video -> .3gf -> .gps duration fallback order (and why it's in that
order - GPS needs satellite acquisition, which can take about a
minute from a cold start; .3gf has no such dependency at all), and the
read-merge-write persistence model this module deliberately does NOT
implement itself (see cli/bv_generate.py's _do_stats(), which owns
merging this module's freshly-computed fields into any existing
.stats.json rather than duplicating that concern here).

Everything computed here is "cheap": a single linear pass over a
recording's own GPS fixes and g-sensor samples, the same order of
magnitude as export/trip_stats.py's compute_trip_stats() (already
proven fast enough to run on every bv-export). This module
deliberately computes nothing driver-related (driver1/driver2/
unknown guesses) - no classifier exists yet for any of the gforce/
voice/route signals the design note describes, so v1 leaves the whole
`driver` key out of its output rather than pre-declaring empty
placeholders for a field nothing can fill in yet.

Copyright (C) 2026 Christer R. (sssreh)

SPDX-License-Identifier: GPL-3.0-or-later
"""

from __future__ import annotations

from typing import TYPE_CHECKING
from typing import Any

from ..archive.recording import Recording
from .media import MediaToolError
from .media import get_span
from .media import select_source

if TYPE_CHECKING:
    # All deferred (see the matching deferred imports inside
    # compute_recording_stats() below) - adapters/base.py's own import
    # chain (adapters.base -> telemetry.gps_reader -> generate.media
    # -> generate/__init__.py -> this module) means importing anything
    # from `..adapters`, `..telemetry`, or `..export` at this module's
    # top level, while it's itself being imported as part of
    # generate/__init__.py's own load, closes a real circular-import
    # loop back through whichever of those three packages happened to
    # trigger generate/__init__ in the first place (confirmed for real
    # against all three: adapters.base, telemetry.gps_reader via
    # export.gpx_writer, and export itself). TYPE_CHECKING-only here
    # avoids that at runtime while still giving type checkers the real
    # types.
    from ..adapters.base import CameraAdapter
    from ..telemetry.gps_reader import GpsFix
    from ..telemetry.gsensor_reader import GSensorSample


def _positioned_fixes(fixes: tuple[GpsFix, ...]) -> tuple[GpsFix, ...]:
    """The subset of `fixes` with a real, valid position - the same
    "valid and has lat/lon" filter export/trip_stats.py's
    compute_trip_stats() applies internally, needed here too since
    this module reads start/end position itself rather than through
    that function.
    """

    return tuple(
        fix
        for fix in fixes
        if fix.valid and fix.latitude is not None and fix.longitude is not None
    )


def _resolve_duration(
    recording: Recording,
    *,
    gsensor_samples: tuple[GSensorSample, ...],
    positioned_fixes: tuple[GpsFix, ...],
) -> int | None:
    """Video -> .3gf -> .gps fallback chain for a single recording's
    duration, in whole seconds, or None if none of the three sources
    is usable.

    Video first (via get_span(), the same ffprobe-then-MP4-box-parser
    chain .duration.txt already uses) - the most accurate source when
    it works. Falling back to g-sensor next, not GPS: a .3gf sample's
    `offset` is milliseconds since the *recording itself* started, no
    satellite dependency at all, whereas a cold-start GPS fix can take
    roughly a minute to acquire a lock - unusable in Christer's garage,
    for example. The last GPS fix is the final fallback, paired
    against `recording.id.timestamp` (the camera's own filename clock)
    rather than the *first* GPS fix - the satellite-acquisition gap
    only ever delays the first fix, never the last one, so this
    avoids baking that gap into the computed duration.
    """

    source_file = select_source(recording)
    if source_file is not None:
        try:
            return get_span(recording.id, source_file.path)
        except MediaToolError:
            pass

    if gsensor_samples:
        return round(gsensor_samples[-1].offset.total_seconds())

    if positioned_fixes:
        last_fix = positioned_fixes[-1]
        elapsed = (last_fix.timestamp - recording.id.timestamp).total_seconds()
        if elapsed >= 0:
            return round(elapsed)

    return None


def _axis_values(samples: tuple[GSensorSample, ...], axis: str) -> list[int]:
    return [getattr(sample, axis) for sample in samples]


def _gforce_stats(gsensor_samples: tuple[GSensorSample, ...]) -> dict[str, float | None]:
    """Peak and average per-axis g-sensor magnitude, taken over each
    sample's *absolute* value rather than its raw signed reading.

    gsensor_reader.py's own docstring is explicit that the physical
    unit and sign convention of these readings aren't confirmed - only
    relative variance is meaningful. A raw signed max would only ever
    catch the largest excursion in one arbitrary direction per axis
    (e.g. only hard acceleration, never hard braking, depending on
    which way that axis happens to be wired); taking the absolute
    value first catches the largest deviation in *either* direction,
    which is the actually-useful "how rough was this drive" signal -
    and avg of the same absolute values gives a comparable "how much
    motion on average" figure. Both are None if there are no g-sensor
    samples at all for this recording.
    """

    if not gsensor_samples:
        return {
            "max_gforce_x": None, "avg_gforce_x": None,
            "max_gforce_y": None, "avg_gforce_y": None,
            "max_gforce_z": None, "avg_gforce_z": None,
        }

    result: dict[str, float | None] = {}
    for axis in ("x", "y", "z"):
        magnitudes = [abs(value) for value in _axis_values(gsensor_samples, axis)]
        result[f"max_gforce_{axis}"] = max(magnitudes)
        result[f"avg_gforce_{axis}"] = sum(magnitudes) / len(magnitudes)

    return result


def compute_recording_stats(
    recording: Recording, adapter: CameraAdapter
) -> dict[str, Any]:
    """Compute one recording's own statistics - distance, speed,
    altitude, moving/idle time, per-axis g-force, and duration - as a
    plain JSON-serializable dict matching the RECORDING_STATS schema
    (see this module's own docstring and WORKING_CONTEXT.md).

    Every GPS/altitude/speed field mirrors export/trip_stats.py's
    compute_trip_stats() exactly (this function delegates to it
    directly, on this one recording's own fixes rather than a whole
    trip's merged fixes) - None wherever that function's own docstring
    says None, e.g. fewer than two valid positioned fixes leaves
    distance_km/avg_speed_kmh/etc. all None while still reporting
    has_gps and duration_seconds.

    Reads this recording's GPS/g-sensor data via the adapter
    abstraction (adapters/telemetry_bridge.py), not BlackVue's raw
    .gps/.3gf sidecars directly - works unchanged for a FolderAdapter/
    GoProAdapter recording with no GPS at all (has_gps=False, every
    GPS-derived field None) or one whose telemetry lives inside its
    video's own GPMF stream instead of a sidecar file.

    Does not read or write any existing .stats.json - that's
    cli/bv_generate.py's _do_stats()'s job (the read-merge-write logic
    described in WORKING_CONTEXT.md). This function only ever computes
    fresh values from source telemetry; every field it returns is one
    of the "cheap, always recomputed" fields in that design, never one
    of the expensive/optional driver.* fields (which this function
    doesn't compute at all - see this module's own top docstring).
    """

    # Deferred, not top-level imports - see the TYPE_CHECKING import
    # note above this module's function definitions for why importing
    # anything from `..adapters`/`..export` at module load time here
    # would close a real circular-import loop.
    from ..adapters.telemetry_bridge import read_recording_gps
    from ..adapters.telemetry_bridge import read_recording_gsensor
    from ..export.trip_stats import compute_trip_stats

    fixes = read_recording_gps(adapter, recording)
    gsensor_samples = read_recording_gsensor(adapter, recording)
    positioned_fixes = _positioned_fixes(fixes)

    trip_stats = compute_trip_stats(fixes) if len(positioned_fixes) >= 2 else None

    stats: dict[str, Any] = {
        "duration_seconds": _resolve_duration(
            recording,
            gsensor_samples=gsensor_samples,
            positioned_fixes=positioned_fixes,
        ),
        "has_gps": bool(positioned_fixes),
        # "time" (the fix's own timestamp, ISO 8601, naive - matching
        # GpsFix.timestamp's own naive-UTC convention, see
        # telemetry/gps_reader.py) lets stats_report.py's
        # _boundary_bridge_km() work out how much of the gap between
        # this recording's own last real fix and the *next* recording's
        # first real fix falls inside this recording's own video span
        # versus the next one's, when bridging a GPS dropout that
        # straddles the boundary between two recordings (Christer,
        # 2026-08-23: "our stats file does not contain first and last
        # gps position, that could help a little if you have a previous
        # recording and a next recording gps position ... based of time
        # can you see how much of the distance belong to each recording
        # id"). Without a timestamp there'd be no way to do that time
        # -proportional split - lat/lon alone only gives the *where*,
        # not the *when*.
        "start_gps": (
            {
                "lat": positioned_fixes[0].latitude,
                "lon": positioned_fixes[0].longitude,
                "time": positioned_fixes[0].timestamp.isoformat(),
            }
            if positioned_fixes else None
        ),
        "end_gps": (
            {
                "lat": positioned_fixes[-1].latitude,
                "lon": positioned_fixes[-1].longitude,
                "time": positioned_fixes[-1].timestamp.isoformat(),
            }
            if positioned_fixes else None
        ),
        # Rounded to 3 decimals (~1m precision) - a single recording is
        # only ~3 minutes, so distance_km is very often sub-1km, and
        # compute_trip_stats()'s own raw haversine-sum float prints
        # with 15+ meaningless digits (e.g. 0.017387641027105147) if
        # passed straight through. Trip-level trip_info.txt keeps the
        # unrounded value from compute_trip_stats() itself - this
        # rounding is local to the per-recording asset, not a change
        # to trip_stats.py.
        "distance_km": (
            round(trip_stats.distance_km, 3) if trip_stats else None
        ),
        "avg_speed_kmh": trip_stats.average_speed_kmh if trip_stats else None,
        "max_speed_kmh": trip_stats.max_speed_kmh if trip_stats else None,
        "moving_seconds": trip_stats.moving_seconds if trip_stats else None,
        "idle_seconds": trip_stats.idle_seconds if trip_stats else None,
        "min_altitude_m": trip_stats.min_altitude_meters if trip_stats else None,
        "max_altitude_m": trip_stats.max_altitude_meters if trip_stats else None,
        "elevation_change_m": trip_stats.elevation_change_meters if trip_stats else None,
    }
    stats.update(_gforce_stats(gsensor_samples))

    return stats
