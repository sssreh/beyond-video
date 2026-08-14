"""
bv-export CLI - scan an archive, detect trips, and assemble each one
into its own folder under --target (concatenated video/audio/text,
merged GPX track, merged g-sensor log).

Copyright (C) 2026 Christer R. (sssreh)

SPDX-License-Identifier: GPL-3.0-or-later
"""

from __future__ import annotations

import argparse
import shlex
import shutil
import sys
import threading
from collections.abc import Callable
from collections.abc import Iterable
from datetime import timedelta
from pathlib import Path

from blackvue.archive import Archive
from blackvue.archive.recording import Recording
from blackvue.archive.recording_id import RecordingId
from blackvue.cli.errors import run_cli
from blackvue.core.camera_config import default_config_dir
from blackvue.core.camera_config import resolve_archive_path
from blackvue.core.joblog import wrap_say
from blackvue.core.joblog import wrap_warn
from blackvue.export import ExportCancelled
from blackvue.export import export_trip
from blackvue.export import folder_name_for_trip
from blackvue.export.map_video import DEFAULT_INTRO_SECONDS
from blackvue.export.map_video import DEFAULT_MAP_ICON_PATH
from blackvue.export.mirror_icon import DEFAULT_MIRROR_ICON_PATH
from blackvue.export.osm_roads import DEFAULT_ZOOM_RADIUS_METERS
from blackvue.export.stitch import ALL_LAYOUTS
from blackvue.export.stitch import AUTO_LAYOUT
from blackvue.export.stitch import DEFAULT_GRAPH_SIZE_PERCENT
from blackvue.export.stitch import DEFAULT_GSENSOR_POSITION
from blackvue.export.stitch import DEFAULT_GSENSOR_SIZE_PERCENT
from blackvue.export.stitch import DEFAULT_MIRROR_PAN_X_PERCENT
from blackvue.export.stitch import DEFAULT_MIRROR_RADIUS_PERCENT
from blackvue.export.stitch import MAX_GRAPH_SIZE_PERCENT
from blackvue.export.stitch import MAX_GSENSOR_SIZE_PERCENT
from blackvue.export.stitch import MAX_MAP_SIZE_PERCENT
from blackvue.export.stitch import MAX_MIRROR_PAN_PERCENT
from blackvue.export.stitch import MAX_MIRROR_RADIUS_PERCENT
from blackvue.export.stitch import MAX_MIRROR_SIZE_PERCENT
from blackvue.export.stitch import MAX_MIRROR_ZOOM_PERCENT
from blackvue.export.stitch import MAX_STITCH_SCALE_PERCENT
from blackvue.export.stitch import MIN_GRAPH_SIZE_PERCENT
from blackvue.export.stitch import MIN_GSENSOR_SIZE_PERCENT
from blackvue.export.stitch import MIN_MAP_SIZE_PERCENT
from blackvue.export.stitch import MIN_MIRROR_PAN_PERCENT
from blackvue.export.stitch import MIN_MIRROR_RADIUS_PERCENT
from blackvue.export.stitch import MIN_MIRROR_SIZE_PERCENT
from blackvue.export.stitch import MIN_MIRROR_ZOOM_PERCENT
from blackvue.export.stitch import MIN_STITCH_SCALE_PERCENT
from blackvue.export.stitch import parse_gsensor_position
from blackvue.generate.media import MediaToolError
from blackvue.generate.media import load_or_compute_duration
from blackvue.generate.media import read_duration_seconds
from blackvue.lexicaltimeparser import LexicalTimeParser
from blackvue.telemetry.movement import movement_bridges_gap
from blackvue.trip.trip_builder import DEFAULT_GAP_TOLERANCE
from blackvue.trip.trip_builder import DEFAULT_MAX_GAP
from blackvue.trip.trip_builder import DEFAULT_MAX_PARKING_DURATION
from blackvue.trip.trip_builder import TripBuilder
from blackvue.trip.trip_builder import recordings_with_front_video

# bv-export's own opinionated defaults for --stitch-mirror-size/-zoom/
# -pan-y, Christer's preferred rearview-mirror viewing setup - a
# closer, already-panned-up view rather than the plain full rear frame
# stitch.py's own DEFAULT_MIRROR_SIZE_PERCENT/DEFAULT_MIRROR_ZOOM
# _PERCENT/DEFAULT_MIRROR_PAN_Y_PERCENT default to when stitch_cameras()
# /export_trip() are called directly (as this project's own test suite
# does in many places, relying on that plain/uncropped default).
# Deliberately kept as bv_export.py's own constants rather than
# changing stitch.py's shared ones - same "CLI has its own opinionated
# default, library stays neutral" split already used for
# --stitch-mirror-icon (see DEFAULT_MIRROR_ICON_PATH's own docstring).
# --stitch-mirror-pan-x isn't included here - Christer only asked for
# pan_y to change, so pan_x keeps stitch.py's own DEFAULT_MIRROR_PAN_X
# _PERCENT (0, centered) as both the library's and the CLI's default.
_DEFAULT_CLI_MIRROR_SIZE_PERCENT = 40.0
_DEFAULT_CLI_MIRROR_ZOOM_PERCENT = 40.0
_DEFAULT_CLI_MIRROR_PAN_Y_PERCENT = -30.0

# --parking-speed's own valid range - originally Christer's requested
# window (0.10x-5x) when asking for the feature, later widened to
# 0.10x-10x (Christer: "i would like --parking-speed be between 0.1
# and 10"). Kept local to this CLI module rather than alongside
# stitch.py's MIN_/MAX_STITCH_SCALE_PERCENT and friends: unlike those,
# nothing about this range is shared with a non-CLI caller -
# trip_export.py's export_trip() and media.py's change_playback_speed()
# both take a plain float and leave range-checking entirely to
# whoever's calling them (see change_playback_speed()'s own docstring
# for why), so there's no lower-level constant to reuse here the way
# the stitch ones are.
MIN_PARKING_SPEED = 0.10
MAX_PARKING_SPEED = 10.0


def _resolve_icon_path(
    value: str | Path | None, default_path: Path
) -> Path | None:
    """Shared three-state resolution for --map-icon and --stitch-mirror
    -icon, both of which now default to a bundled image rather than
    plain procedural drawing (an arrow / a rounded rectangle). Omitted
    (`None`, argparse's own default when the flag isn't given) ->
    `default_path` - a bundled image this package ships (see
    map_video.DEFAULT_MAP_ICON_PATH / mirror_icon.DEFAULT_MIRROR_ICON
    _PATH). The literal string `"none"` -> `None`, explicitly opting
    back out to whichever procedural fallback the caller uses instead.
    Anything else -> that path, for a custom image. Kept as bv_export.py
    's own resolution step (not pushed into map_video.py/mirror_icon.py
    /stitch.py's own function defaults, which stay plain `None`/"no
    icon" as before) - same reasoning as this module's own
    _DEFAULT_CLI_MIRROR_SIZE_PERCENT and friends: other direct callers
    of those lower-level functions (including this project's own test
    suite) shouldn't have their default behavior changed by bv-export's
    own CLI preferences.
    """

    if value is None:
        return default_path
    if isinstance(value, str) and value.strip().lower() == "none":
        return None
    return Path(value)


def _interactive() -> bool:
    """Return True if running attached to a real terminal, on the
    main thread.

    sys.stdin/sys.stdout are process-wide, not per-thread - if
    bv-web's own server process happens to be launched attached to a
    real terminal (Christer's native, non-Docker setup: `bv-web
    serve ...` typed directly into a pwsh window), isatty() returns
    True even inside a background job thread, where there is no one
    actually watching that console for this specific prompt. Without
    the main-thread check below, the `folder.exists()` branch in
    `_run()` below then calls `_ask_wipe_existing()` -> `input()` on
    that thread, which blocks forever - the job's own output box
    shows nothing (the prompt text goes to the server's own,
    unwatched console) and the job just sits "Running" indefinitely.
    Same root cause, same fix, as `_should_write()`'s own
    `_interactive()` in bv_scribe.py/bv_generate.py (see
    WORKING_CONTEXT.md's "Fix _interactive() false positive hanging
    bv-web jobs on input()" entry) - this one was missed there because
    it only triggers when a trip's own folder already exists (a rerun
    of the same trip without --overwrite), not on every run. Requiring
    the main thread too means only a genuine direct CLI invocation
    (always main-thread) can hit the interactive wipe/keep prompt;
    every bv-web job (always a background thread, per
    JobRunner._spawn()) now safely falls through to the `else False`
    branch instead - "keep existing files, only update what this run
    actually produces", the same non-interactive default documented in
    `overwrite`'s own docstring above.
    """

    return (
        sys.stdin.isatty()
        and sys.stdout.isatty()
        and threading.current_thread() is threading.main_thread()
    )


def _default_warn(message: str) -> None:
    """Default `warn` for `bv_export()`/`_run()` below - real stderr,
    the CLI's normal error-output contract. Same "every say/warn
    -taking function defaults to real print/stderr" convention
    bv_config.py/bv_gps.py/bv_generate.py's own `_run()`s (and
    bv_generate.py's internal helpers) already use, extended here to
    `bv_export()` itself (not just a thin `_run()` wrapper around it)
    so bv-web's job runner (see web/jobs.py) can capture this
    command's per-trip progress into a job's transcript - a real
    export can run for many minutes, so unlike bv-config/bv-gps this
    one especially needs live progress visible in the browser, not
    just a final result. See bv_gps.py's own `_default_warn` for why
    this is a named function rather than a lambda."""

    print(message, file=sys.stderr)


def _parse_resolution(value: str) -> tuple[int, int]:
    try:
        width_str, height_str = value.lower().split("x")
        return int(width_str), int(height_str)
    except ValueError:
        raise argparse.ArgumentTypeError(
            f"invalid resolution {value!r} (expected WIDTHxHEIGHT, "
            "e.g. 320x240)"
        )


def _parse_stitch_scale(value: str) -> float:
    try:
        scale = float(value)
    except ValueError:
        raise argparse.ArgumentTypeError(f"invalid scale {value!r} (expected a number)")

    if not (MIN_STITCH_SCALE_PERCENT <= scale <= MAX_STITCH_SCALE_PERCENT):
        raise argparse.ArgumentTypeError(
            f"scale {value!r} out of range "
            f"({MIN_STITCH_SCALE_PERCENT:g}-{MAX_STITCH_SCALE_PERCENT:g})"
        )

    return scale


def _parse_parking_speed(value: str) -> float:
    try:
        speed = float(value)
    except ValueError:
        raise argparse.ArgumentTypeError(
            f"invalid speed {value!r} (expected a number)"
        )

    if not (MIN_PARKING_SPEED <= speed <= MAX_PARKING_SPEED):
        raise argparse.ArgumentTypeError(
            f"speed {value!r} out of range "
            f"({MIN_PARKING_SPEED:g}-{MAX_PARKING_SPEED:g})"
        )

    return speed


def _parse_positive_pixels(value: str) -> int:
    try:
        pixels = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError(
            f"invalid pixel value {value!r} (expected a whole number)"
        )

    if pixels <= 0:
        raise argparse.ArgumentTypeError(
            f"pixel value {value!r} must be positive"
        )

    return pixels


def _parse_gsensor_size(value: str) -> float:
    try:
        size = float(value)
    except ValueError:
        raise argparse.ArgumentTypeError(f"invalid size {value!r} (expected a number)")

    if not (MIN_GSENSOR_SIZE_PERCENT <= size <= MAX_GSENSOR_SIZE_PERCENT):
        raise argparse.ArgumentTypeError(
            f"size {value!r} out of range "
            f"({MIN_GSENSOR_SIZE_PERCENT:g}-{MAX_GSENSOR_SIZE_PERCENT:g})"
        )

    return size


def _parse_mirror_size(value: str) -> float:
    try:
        size = float(value)
    except ValueError:
        raise argparse.ArgumentTypeError(f"invalid size {value!r} (expected a number)")

    if not (MIN_MIRROR_SIZE_PERCENT <= size <= MAX_MIRROR_SIZE_PERCENT):
        raise argparse.ArgumentTypeError(
            f"size {value!r} out of range "
            f"({MIN_MIRROR_SIZE_PERCENT:g}-{MAX_MIRROR_SIZE_PERCENT:g})"
        )

    return size


def _parse_mirror_radius(value: str) -> float:
    try:
        radius = float(value)
    except ValueError:
        raise argparse.ArgumentTypeError(
            f"invalid radius {value!r} (expected a number)"
        )

    if not (MIN_MIRROR_RADIUS_PERCENT <= radius <= MAX_MIRROR_RADIUS_PERCENT):
        raise argparse.ArgumentTypeError(
            f"radius {value!r} out of range "
            f"({MIN_MIRROR_RADIUS_PERCENT:g}-{MAX_MIRROR_RADIUS_PERCENT:g})"
        )

    return radius


def _parse_mirror_zoom(value: str) -> float:
    try:
        zoom = float(value)
    except ValueError:
        raise argparse.ArgumentTypeError(
            f"invalid zoom {value!r} (expected a number)"
        )

    if not (MIN_MIRROR_ZOOM_PERCENT <= zoom <= MAX_MIRROR_ZOOM_PERCENT):
        raise argparse.ArgumentTypeError(
            f"zoom {value!r} out of range "
            f"({MIN_MIRROR_ZOOM_PERCENT:g}-{MAX_MIRROR_ZOOM_PERCENT:g})"
        )

    return zoom


def _parse_mirror_pan(value: str) -> float:
    try:
        pan = float(value)
    except ValueError:
        raise argparse.ArgumentTypeError(
            f"invalid pan {value!r} (expected a number)"
        )

    if not (MIN_MIRROR_PAN_PERCENT <= pan <= MAX_MIRROR_PAN_PERCENT):
        raise argparse.ArgumentTypeError(
            f"pan {value!r} out of range "
            f"({MIN_MIRROR_PAN_PERCENT:g}-{MAX_MIRROR_PAN_PERCENT:g})"
        )

    return pan


def _parse_map_size(value: str) -> float:
    try:
        size = float(value)
    except ValueError:
        raise argparse.ArgumentTypeError(f"invalid size {value!r} (expected a number)")

    if not (MIN_MAP_SIZE_PERCENT <= size <= MAX_MAP_SIZE_PERCENT):
        raise argparse.ArgumentTypeError(
            f"size {value!r} out of range "
            f"({MIN_MAP_SIZE_PERCENT:g}-{MAX_MAP_SIZE_PERCENT:g})"
        )

    return size


def _parse_graph_size(value: str) -> float:
    try:
        size = float(value)
    except ValueError:
        raise argparse.ArgumentTypeError(f"invalid size {value!r} (expected a number)")

    if not (MIN_GRAPH_SIZE_PERCENT <= size <= MAX_GRAPH_SIZE_PERCENT):
        raise argparse.ArgumentTypeError(
            f"size {value!r} out of range "
            f"({MIN_GRAPH_SIZE_PERCENT:g}-{MAX_GRAPH_SIZE_PERCENT:g})"
        )

    return size


def _parse_gsensor_position(value: str) -> str:
    try:
        parse_gsensor_position(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(str(exc))

    return value


def _parse_gsensor_xy(value: str) -> tuple[float, float]:
    try:
        x_str, y_str = value.split(",")
        return float(x_str), float(y_str)
    except ValueError:
        raise argparse.ArgumentTypeError(
            f"invalid position {value!r} (expected X,Y as percentages, "
            "e.g. 80,10)"
        )


def _ask_wipe_existing(folder: Path) -> bool:
    answer = input(
        f"{folder.name} already exists. Wipe and rebuild trip folders "
        "from scratch this run, or keep existing files and only "
        "update what each run actually produces? [w/K] "
    ).strip().lower()
    return answer in ("w", "wipe")


def _default_max_gap(
    archive: Archive,
    recordings: Iterable[Recording],
    interval: TimeInterval,
) -> timedelta:
    """Derive the default --max-gap from the camera's own configured
    RecordTime (see archive/configuration.py) instead of the flat
    DEFAULT_MAX_GAP constant - Christer's own reasoning: the gap a
    single dropped/missing segment leaves behind is close to
    RecordTime itself, so a camera set to 1-minute segments and one
    set to 3-minute segments warrant different defaults, not the same
    fixed number. Formula is `RecordTime + gap_tolerance` - not
    RecordTime + Configuration.TOLERANCE, since TripBuilder already
    adds its own gap_tolerance (default 10s, same value) on top of
    max_gap; using Configuration.maximum_gap here instead of
    record_time alone would double that margin.

    A `--timestamp`/`--from`/`--until` range uses whichever
    configuration was active for the *earliest* recording actually
    touching `interval` - the same "most recent snapshot at or before"
    lookup Archive.configuration() already does. A range spanning a
    real RecordTime change mid-archive still only gets one max_gap for
    the whole run (TripBuilder takes a single scalar, not a
    per-recording one) - accepted as a real but rare simplification.

    A full-archive export (no range at all - LexicalTimeParser's own
    all-open sentinel) instead uses the *latest* known configuration,
    not the earliest recording's - "what my camera is set to now" is
    the more useful assumption than an export spanning the archive's
    entire history defaulting to whatever the very first recording
    was made under, months or years ago.

    Falls back to DEFAULT_MAX_GAP if `recordings` has nothing touching
    `interval` (nothing to key a lookup to) or the archive has no
    RecordTime snapshots at all yet (an archive never downloaded with
    this feature's bv-download build, or bv-download simply never
    connected to the camera successfully).

    The "no snapshots at all" case is checked once, up front, for
    *both* branches below - not just the full-archive one - and
    returns DEFAULT_MAX_GAP directly without ever calling
    Archive.configuration(). An earlier version only special-cased the
    full-archive branch, letting the far more common ranged-export
    branch (any real `--timestamp`/`--from`/`--until` run) fall
    through to Archive.configuration()'s own fallback instead -
    functionally identical (that fallback is exactly 300s, the same
    value as DEFAULT_MAX_GAP) but printing "Warning: archive contains
    no configuration snapshot" on *every* export from an archive that
    never got one, not just once - Christer, after already being told
    the warning was harmless: "I still get Warning: archive contains
    no configuration snapshot... Both recordings have a duration
    file." A warning that fires on every run for a fact about the
    archive that isn't going to change run-to-run, and doesn't affect
    the result, isn't earning its keep. Archive.configuration()'s own
    warning is kept for the genuinely different case it still covers -
    an archive *with* some snapshots, just none old enough to cover a
    particular recording - which is real, useful information a flat
    "no snapshots at all" isn't.
    """

    if not archive.configurations:
        return DEFAULT_MAX_GAP

    if interval.first == "00000000_000000" and interval.last == "99999999_999999":
        return timedelta(seconds=archive.configurations[-1].record_time)

    reference = next(
        (recording for recording in recordings if recording.id.value in interval),
        None,
    )

    if reference is None:
        return DEFAULT_MAX_GAP

    return timedelta(seconds=archive.configuration(reference).record_time)


def bv_export(
    path: str | Path = ".",
    *,
    target: str | Path,
    prefix: str | None = None,
    from_: str | None = None,
    until: str | None = None,
    timestamp: str | None = None,
    max_gap_minutes: int | None = None,
    movement: bool = False,
    duration: bool = True,
    duration_heal_archive: bool = False,
    gap_tolerance_seconds: int | None = None,
    max_parking_duration_minutes: int | None = None,
    render_map: bool = False,
    map_icon: str | Path | None = None,
    map_zoom_meters: float | None = None,
    map_track_up: bool = False,
    render_map_intro: bool = False,
    map_intro_seconds: float = DEFAULT_INTRO_SECONDS,
    render_gsensor: bool = False,
    render_gsensor_graph: bool = False,
    gsensor_graph_x: bool = False,
    stitch_layout: str | None = None,
    stitch_resolution: tuple[int, int] | None = None,
    stitch_bitrate: str | None = None,
    stitch_scale: float | None = None,
    stitch_max_width: int | None = None,
    stitch_max_height: int | None = None,
    stitch_mirror_size: float = _DEFAULT_CLI_MIRROR_SIZE_PERCENT,
    stitch_mirror_radius: float = DEFAULT_MIRROR_RADIUS_PERCENT,
    stitch_mirror_zoom: float = _DEFAULT_CLI_MIRROR_ZOOM_PERCENT,
    stitch_mirror_pan_x: float = DEFAULT_MIRROR_PAN_X_PERCENT,
    stitch_mirror_pan_y: float = _DEFAULT_CLI_MIRROR_PAN_Y_PERCENT,
    stitch_mirror_icon: str | Path | None = None,
    stitch_map: str | None = None,
    stitch_map_side: str | None = None,
    stitch_map_size: float | None = None,
    stitch_map_circle: bool | None = None,
    stitch_gsensor: bool = False,
    stitch_gsensor_size: float = DEFAULT_GSENSOR_SIZE_PERCENT,
    stitch_gsensor_pos: str | None = None,
    stitch_gsensor_xy: tuple[float, float] | None = None,
    stitch_graph: bool = False,
    stitch_graph_side: str | None = None,
    stitch_graph_size: float | None = None,
    stitch_subtitles: bool = False,
    stitch_subtitles_background: bool = True,
    include_parking: bool = False,
    parking_speed: float = 1.0,
    overwrite: bool = False,
    dry_run: bool = False,
    debug: bool = False,
    command_line: str | None = None,
    should_continue: Callable[[], bool] = lambda: True,
    say=print,
    warn=_default_warn,
) -> int:
    """Export every detected trip in `path` to its own folder under
    `target`. Returns 0 on success, 1 if any trip failed.

    A trip folder that already exists from a previous run is, by
    default, left in place - this run only overwrites whatever files
    it actually regenerates, so an output that's expensive to redo
    (--map in particular) survives a later run that doesn't ask for
    it again. `--overwrite` wipes and rebuilds every trip folder from
    scratch instead, without asking. Without `--overwrite`: an
    interactive run asks once (on the first trip folder that already
    exists) whether to wipe or keep, and reuses that answer for every
    other trip folder touched this run; a non-interactive run (cron/
    batch) always keeps, since there's no one to ask.

    `debug=True` prints wall-clock timing to stderr for each trip's
    concatenation/map/stitch phases, plus which decode method (nvdec
    or cpu) --stitch actually used and how long it took - diagnostic
    breadcrumbs for tracking down where time went on a slow run, off
    by default since most runs don't need them.

    `say` (always available, not gated behind `debug`) also gets each
    trip's own export-phase progress - "starting map.mp4 render",
    "rendered map.mp4", and so on, the same lines export_trip() writes
    to that trip's own trip.log - live, as they happen, via
    `export_trip()`'s own `say` param. Before this, a long export gave
    no live signal of any kind unless `--debug` was passed, and even
    then the underlying print()s went to raw stderr rather than
    through `say`, so a bv-web job never saw them at all - see
    trip_export.py's own `say` param docstring.

    `--timestamp`/`--from`/`--until` select *trips*, not recordings: a
    trip is included if any of its own recordings fall inside the
    requested range - the whole trip is then exported, including
    whatever recordings pushed it before or after the range's own
    boundaries. Filtering recordings by the range *before* detecting
    trips (the original approach) could silently truncate a trip that
    merely overlaps the requested window - e.g. a long continuous drive
    that started a few minutes before a `--timestamp` window opens
    would lose its earlier recordings entirely, since they'd never
    even reach TripBuilder, and the exported "trip" would be missing
    real footage that belongs to it.

    Detection itself is *bounded* to `interval`, not an archive-wide
    scan - see `TripBuilder.build_for_interval()`'s own docstring for
    the algorithm (seed on the recordings actually inside `interval`,
    then grow outward only as far as needed to prove a real gap on
    each side, same as a full scan would have found, just without
    reading duration data for recordings nowhere near the request).
    This used to run across the *entire* archive on every single run
    regardless of how small the actual request was - correct, but a
    real, growing cost as an archive gets larger, since even exporting
    one day still meant detecting and duration-checking every trip
    that ever existed. Christer's own framing of the fix: "from time
    range, seek backwards until start found, then forward until end
    is found." An `--timestamp`/`--from`/`--until`-less run (export
    the whole archive) still does the plain archive-wide `build()` -
    there's nothing to bound against when everything is being
    exported anyway.

    Bounded detection reads `.duration.txt` (see
    `read_duration_seconds()`) rather than computing a missing one -
    Christer found the alternative (computing and writing every
    missing file before this function could even ask its first "trip
    already exists?" question) made a first run against a large
    archive look hung for a long, silent stretch. Instead, only the
    recordings belonging to a trip actually being exported this run
    get self-healed (see `load_or_compute_duration()` below, right
    before that trip's own folder name/overwrite prompt is resolved) -
    a real, if small, drop in detection accuracy for a recording that's
    never been probed yet and happens to fall right at a boundary the
    bounded search is trying to prove (it falls back to the old bare
    -start-timestamp gap calculation there, same as before self
    -healing existed, until its own trip is exported at least once),
    accepted in favor of never blocking a prompt on unrelated work.
    `duration_heal_archive=True` (bv-export's own
    `--duration-heal-archive`) hands `load_or_compute_duration` itself
    to the bounded search instead of the cache-only reader, so every
    recording the search actually looks at gets ffprobed for real
    rather than falling back - "don't just trust the cache" - but still
    only within whatever the bounded search touches, not the whole
    archive; it stopped meaning "scan everything" once detection itself
    stopped doing that. Mutually exclusive with `duration=False`
    (`--no-duration`) - there's nothing to heal against when duration
    data is turned off entirely.

    `command_line`, if given, is written verbatim into every trip's
    own trip.log as the exact command that produced it - main() below
    reconstructs it from sys.argv/argv before calling here.

    `max_parking_duration_minutes` caps how long a continuous run of
    Parking-mode footage can span in real elapsed time (not its
    played-back length - a Parking-mode timelapse can run for well
    over an hour of real time while playing back in a few minutes)
    before a Parking recording is kept out of the trip it would
    otherwise end and starts the next trip instead - so a single
    Parking recording longer than this on its own is never appended
    to the drive before it. See TripBuilder's own docstring for the
    full mechanism, including why two or more chained Parking
    recordings whose combined real span crosses this can split from
    each other the same way, not just at the point driving resumes.
    Depends on real duration data the same way `--max-gap`'s own
    duration-aware gap calculation does, so `--no-duration` disables
    this too. Defaults to DEFAULT_MAX_PARKING_DURATION (60 minutes).

    `include_parking=False` (the default) leaves every Parking-mode
    recording out of front.mp4/rear.mp4/audio.aac entirely, wherever
    it falls in a trip - leading, trailing, or mid-trip - with nothing
    substituted in its place. Pass `include_parking=True` (bv-export's
    own `--include-parking`) to include every Parking recording's real
    footage/audio unconditionally instead, the original behavior.

    An earlier version of this feature spliced a short synthetic
    "PARKING FOOTAGE SKIPPED" transition clip (optionally one of
    Christer's own AI-generated clips, or his own image/video) in place
    of a mid-trip Parking recording. Dropped after a real export from
    Christer's own 4K HEVC dashcam showed the splice corrupting
    front.mp4/rear.mp4 from that point onward - the two files never
    share MP4-container-level parameter sets unless they come from the
    exact same encoder session, so a stream-copy splice (no re-encode)
    mixing the camera's own footage with anything independently
    encoded is fundamentally unreliable, not fixable by matching codec
    names alone. A full re-encode would avoid this, but at real
    trip-length/4K cost for one skipped recording's worth of benefit -
    Christer: "we don't want time consuming stuff if it not gives us
    something great back. Just skip it altogether" - so a Parking
    recording is now simply left out, matching the treatment
    leading/trailing Parking recordings already had.

    `parking_speed` (bv-export's own `--parking-speed`, default 1.0,
    range 0.10-10.0) re-encodes every included Parking recording's
    video at that playback speed before it's concatenated into the
    rest of the trip - 2.0 plays it twice as fast, 0.5 half as fast.
    Parking-mode footage is motion-triggered and sparse, so a long
    real-world span can otherwise compress into a slow, uneventful
    stretch of the final export; this lets it play back faster (or
    slower) without touching the pace of the rest of the trip. Has no
    effect when `include_parking=False` - there's no Parking footage
    in the video to speed up in that case. Left at 1.0 (a strict
    no-op, zero extra ffmpeg work), this behaves exactly as before
    `--parking-speed` existed.

    `should_continue` (default always-True) is forwarded straight into
    every trip's own `export_trip()` call - see that function's own
    docstring for the checkpoint mechanism and its deliberate scope
    (phase boundaries and per-frame Python render loops only, not
    in-flight ffmpeg subprocess calls). A trip that raises
    `ExportCancelled` here stops this whole run - the trip loop below
    breaks rather than moving on to the next trip, since a cancellation
    is a request to stop everything, not just skip one trip - and
    `bv_export()` returns 1, the same exit code any other failed trip
    already produces. bv-web's job runner (see web/jobs.py's
    `start_bv_export()`) is what actually supplies a real one here,
    tied to the job's own Cancel button; a plain terminal run never
    passes anything but the default, since Ctrl-C already works there
    via `run_cli()`'s own `KeyboardInterrupt` handling.
    """

    if duration_heal_archive and not duration:
        raise SystemExit(
            "bv-export: --duration-heal-archive has nothing to heal "
            "against when --no-duration is also given."
        )

    archive = Archive(path)

    try:
        interval = LexicalTimeParser(
            timestamp=timestamp,
            from_=from_,
            until=until,
        ).parse()
    except ValueError as exc:
        raise SystemExit(str(exc))

    # Trip detection only considers recordings with a Front asset -
    # see recordings_with_front_video()'s own docstring for why
    # (GPS/g-sensor/thumbnail-only recordings, common when Front/Rear
    # video for a stretch was never downloaded, used to be able to
    # chain-bridge a real gap between two actual video segments into
    # one trip, and to pull a trip's merged GPS fixes across time the
    # concatenated video doesn't actually cover - both confirmed on a
    # real archive, both fixed by this filter). Computed early (before
    # max_gap below) since _default_max_gap() also needs it.
    front_recordings = recordings_with_front_video(archive.recordings)

    max_gap = (
        timedelta(minutes=max_gap_minutes)
        if max_gap_minutes is not None
        else _default_max_gap(archive, front_recordings, interval)
    )
    gap_tolerance = (
        timedelta(seconds=gap_tolerance_seconds)
        if gap_tolerance_seconds is not None
        else DEFAULT_GAP_TOLERANCE
    )
    max_parking_duration = (
        timedelta(minutes=max_parking_duration_minutes)
        if max_parking_duration_minutes is not None
        else DEFAULT_MAX_PARKING_DURATION
    )
    bridge = movement_bridges_gap if movement else None

    # Trip *detection* (below) is bounded to `interval` rather than
    # scanning the whole archive on every run - see TripBuilder.
    # build_for_interval()'s own docstring for the full algorithm.
    # Christer's own framing: "from time range, seek backwards until
    # start found, then forward until end is found." Only recordings
    # the bounded search actually looks at (roughly: the trip(s)
    # touching `interval`, plus however far it had to grow to prove
    # both real boundaries) ever get a duration lookup - not every
    # recording in the archive, which is what made a first run against
    # a large, growing archive feel slow even just to export one day
    # out of it. `duration_heal_archive` still means "don't just trust
    # the cache, verify/compute for real" - now within that bounded
    # set, not across the whole archive - by handing
    # load_or_compute_duration itself to the builder instead of the
    # cache-only read_duration_seconds, so a recording without a
    # `.duration.txt` yet gets ffprobed (and cached) the moment the
    # bounded search actually reads it, rather than needing a separate
    # eager pass first.
    if not duration:
        recording_duration = None
    elif duration_heal_archive:
        recording_duration = load_or_compute_duration
    else:
        recording_duration = read_duration_seconds
    # Populated in place by build()/build_for_interval() with one
    # membership-reasoning entry per recording (see TripBuilder.build()
    # 's own docstring) - forwarded to every trip's own trip.log below
    # so a surprising trip membership decision (e.g. a recording that
    # seems to belong to the wrong trip) can be checked against the
    # real reasoning that produced it.
    reasons: dict[RecordingId, str] = {}
    builder = TripBuilder(
        max_gap=max_gap,
        bridge=bridge,
        recording_duration=recording_duration,
        gap_tolerance=gap_tolerance,
        max_parking_duration=max_parking_duration,
    )
    if interval.first == "00000000_000000" and interval.last == "99999999_999999":
        # A true full-archive export (no --timestamp/--from/--until at
        # all) - nothing to bound against, so build every trip
        # directly rather than paying for build_for_interval()'s own
        # seed/grow bookkeeping for no benefit (it would settle on the
        # same full-archive slice after its own first pass anyway, but
        # saying so here is clearer than relying on that fact).
        all_trips = builder.build(front_recordings, reasons=reasons)
    else:
        all_trips = builder.build_for_interval(
            front_recordings, interval, reasons=reasons
        )

    trips = [
        trip
        for trip in all_trips
        if any(recording.id.value in interval for recording in trip)
    ]

    if not trips:
        say("bv-export: no recordings found in range - nothing to export.")
        return 0

    target_path = Path(target)
    # Shared across every trip in this run (and across runs) rather
    # than living inside any one trip's own folder, so it survives
    # even a --overwrite wipe of an individual trip folder - see
    # export_trip()'s map_cache_dir docstring.
    map_cache_dir = target_path / ".osm_cache"
    # Both --map-icon and --stitch-mirror-icon now default to a bundled
    # image rather than plain procedural drawing (an arrow / a rounded
    # rectangle) - see _resolve_icon_path()'s own docstring for the
    # omitted/"none"/custom-path three-way split.
    map_icon_path = _resolve_icon_path(map_icon, DEFAULT_MAP_ICON_PATH)
    stitch_mirror_icon_path = _resolve_icon_path(
        stitch_mirror_icon, DEFAULT_MIRROR_ICON_PATH
    )
    exit_code = 0
    # Cached on the first existing trip folder this run encounters,
    # then reused for every other one - so an interactive run only
    # asks once, the same "ask once per run" pattern bv-generate uses
    # for its own overwrite prompt.
    wipe_decision: bool | None = None

    for trip in trips:
        if duration and not dry_run:
            # Self-heal only this trip's own recordings (see this
            # function's own docstring for why the archive-wide
            # detection pass above deliberately doesn't) - right here,
            # before folder_name_for_trip() below reads
            # trip.end_timestamp (the last recording's real span) and
            # before this trip's own overwrite prompt, so neither one
            # waits on any other trip's recordings. A harmless no-op
            # cache-hit loop when duration_heal_archive already healed
            # everything above. dry_run skips this entirely, same as
            # everything else it doesn't touch.
            for recording in trip:
                load_or_compute_duration(recording)

        folder = target_path / folder_name_for_trip(
            trip, prefix, include_parking=include_parking,
        )

        if dry_run:
            if not folder.exists():
                action = "create"
            elif overwrite:
                action = "wipe and rebuild"
            else:
                action = "update in place"
            say(f"bv-export: [dry run] would {action} {folder} "
                f"({len(trip)} recording(s))")
            continue

        if folder.exists():
            if overwrite:
                shutil.rmtree(folder)
            else:
                if wipe_decision is None:
                    wipe_decision = (
                        _ask_wipe_existing(folder) if _interactive() else False
                    )
                if wipe_decision:
                    shutil.rmtree(folder)

        try:
            result = export_trip(
                trip,
                folder,
                render_map=render_map,
                map_cache_dir=map_cache_dir,
                map_icon=map_icon_path,
                map_zoom_meters=map_zoom_meters,
                map_track_up=map_track_up,
                render_map_intro=render_map_intro,
                map_intro_seconds=map_intro_seconds,
                render_gsensor=render_gsensor,
                render_gsensor_graph=render_gsensor_graph,
                gsensor_graph_x=gsensor_graph_x,
                stitch_layout=stitch_layout,
                stitch_resolution=stitch_resolution,
                stitch_bitrate=stitch_bitrate,
                stitch_scale=stitch_scale,
                stitch_max_width=stitch_max_width,
                stitch_max_height=stitch_max_height,
                stitch_mirror_size=stitch_mirror_size,
                stitch_mirror_radius=stitch_mirror_radius,
                stitch_mirror_zoom=stitch_mirror_zoom,
                stitch_mirror_pan_x=stitch_mirror_pan_x,
                stitch_mirror_pan_y=stitch_mirror_pan_y,
                stitch_mirror_icon=stitch_mirror_icon_path,
                stitch_map=stitch_map,
                stitch_map_side=stitch_map_side,
                stitch_map_size=stitch_map_size,
                stitch_map_circle=stitch_map_circle,
                stitch_gsensor=stitch_gsensor,
                stitch_gsensor_size=stitch_gsensor_size,
                stitch_gsensor_pos=stitch_gsensor_pos,
                stitch_gsensor_xy=stitch_gsensor_xy,
                stitch_graph=stitch_graph,
                stitch_graph_side=stitch_graph_side,
                stitch_graph_size=stitch_graph_size,
                stitch_subtitles=stitch_subtitles,
                stitch_subtitles_background=stitch_subtitles_background,
                include_parking=include_parking,
                parking_speed=parking_speed,
                command_line=command_line,
                reasons=reasons,
                debug=debug,
                should_continue=should_continue,
                say=say,
            )
        except ExportCancelled as exc:
            # A real cancellation (bv-web's Cancel button, via
            # should_continue) - stop the whole run, not just this
            # trip: unlike MediaToolError below (one trip's own
            # failure, the rest may still be worth attempting), this
            # means "stop doing new work" globally, so the trip loop
            # breaks here rather than continuing to the next trip.
            warn(f"bv-export: cancelled ({exc})")
            exit_code = 1
            break
        except MediaToolError as exc:
            warn(f"bv-export: {trip.label}: {exc}")
            exit_code = 1
            continue

        written = [
            written_path
            for written_path in (
                result.front_video, result.rear_video, result.audio,
                result.gpx, result.gsensor, result.map, result.map_zoom,
                result.gsensor_video, result.gsensor_graph_video,
                result.stitch, result.srt,
            )
            if written_path is not None
        ] + list(result.text)

        say(f"bv-export: {folder} - {len(written)} file(s) written")

        for warning in result.warnings:
            warn(f"bv-export: {trip.label}: warning: {warning}")

    return exit_code


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Build bv-export's argument parser and parse `argv` (defaulting
    to sys.argv[1:] when None, argparse's own convention) - pulled out
    of main() into its own function, the same parse_args()/main()
    split every other bv-* CLI in this package already uses (see
    bv_generate.py's own parse_args() for the identical shape). Lets
    bv-web's job runner (see web/jobs.py's start_bv_export()) parse a
    web form's own argv and run bv-export in-process without going
    through main()'s command_line reconstruction, which assumes a
    real terminal invocation with a real sys.argv.
    """
    parser = argparse.ArgumentParser(
        prog="bv-export",
        description=(
            "Detect trips in a BlackVue archive and export each one "
            "(concatenated video/audio/text, merged GPX track, merged "
            "g-sensor log) into its own folder."
        ),
        # argparse's default prefix-abbreviation matching (e.g. an
        # unambiguous --gsensor standing in for --gsensor-video) breaks
        # silently the moment a sibling flag is added later that shares
        # the same prefix - which happened for real: --gsensor-graph
        # -video's own addition turned a previously-fine --gsensor into
        # "ambiguous option: --gsensor could match --gsensor-video,
        # --gsensor-graph-video". This CLI has several other flag
        # families sharing a prefix too (--stitch-map/-side/-size,
        # --stitch-gsensor + 3 variants, --stitch-graph + 2 variants,
        # --stitch-mirror + 6 variants) that are just as exposed to the
        # same failure mode as more flags get added - so abbreviation
        # is turned off globally rather than patched flag-by-flag.
        # Every flag must be spelled out in full from here on.
        allow_abbrev=False,
    )

    parser.add_argument(
        "path",
        nargs="?",
        default=".",
        help=(
            "Archive directory, or a configured camera system id (see "
            "bv-config) - resolved to that camera's own archive "
            "directory. A path containing a separator (e.g. ./Kirby) "
            "is always used literally, never as an id, so a real "
            "directory sharing a camera's name is never ambiguous."
        ),
    )

    parser.add_argument(
        "--target",
        metavar="DIR",
        help=(
            "Directory to create trip subfolders in. Required unless "
            "`path` resolves to a camera id whose config has a "
            "Target directory set (see bv-config) - that becomes the "
            "default."
        ),
    )

    parser.add_argument(
        "--config-dir",
        type=Path,
        default=default_config_dir(),
        help=(
            "Directory camera configs live in, for resolving `path` "
            "as a camera id (default: %(default)s)."
        ),
    )

    parser.add_argument(
        "--prefix",
        metavar="PREFIX",
        default=None,
        help=(
            "Prepend PREFIX_ to each trip's folder name, e.g. "
            "--prefix Holiday -> "
            "Holiday_trip_20260715_133458_20260715_141235."
        ),
    )

    parser.add_argument(
        "--from",
        dest="from_",
        metavar="TIMESTAMP",
        help=(
            "Export every trip that has at least one recording from "
            "this timestamp onward, in full - including any of that "
            "trip's own recordings that fall before it."
        ),
    )

    parser.add_argument(
        "--until",
        metavar="TIMESTAMP",
        help=(
            "Export every trip that has at least one recording up to "
            "this timestamp, in full - including any of that trip's "
            "own recordings that fall after it."
        ),
    )

    parser.add_argument(
        "--timestamp",
        metavar="TIMESTAMP",
        help=(
            "Export every trip that has at least one recording "
            "matching this timestamp or prefix, in full - including "
            "any of that trip's own recordings that fall outside it."
        ),
    )

    parser.add_argument(
        "--max-gap",
        dest="max_gap_minutes",
        type=int,
        metavar="MINUTES",
        default=None,
        help=(
            "The largest gap (in minutes) between two recordings "
            "that still counts as the same trip. Default: derived "
            "from the camera's own configured RecordTime, if the "
            "archive has a RecordTime snapshot (see bv-download) - "
            "the segment length itself, so a single dropped/missing "
            "segment doesn't split a trip (--gap-tolerance is added "
            "on top, same as always). Falls back to "
            f"{int(DEFAULT_MAX_GAP.total_seconds() // 60)} if no "
            "snapshot exists yet."
        ),
    )

    parser.add_argument(
        "--movement",
        dest="movement",
        action="store_true",
        default=False,
        help=(
            "Use GPS/g-sensor data to bridge a gap over --max-gap into "
            "one trip anyway, if the vehicle looks like it was still "
            "moving at the edge of the gap. Off by default: this "
            "heuristic has no ceiling on how large a gap it'll bridge "
            "- a single GPS speed reading right at the start of a "
            "recording was found to bridge a genuine 6-day gap into "
            "one trip on a real archive, folding in an unrelated "
            "day's footage. Until that has a fix, --max-gap (plus "
            "--gap-tolerance and --duration's real-span adjustment) is "
            "the sole trip-splitting rule unless you opt into this."
        ),
    )

    parser.add_argument(
        "--no-duration",
        dest="duration",
        action="store_false",
        help=(
            "Ignore .duration.txt files and measure gaps from each "
            "recording's start timestamp only. By default, a "
            "recording's real span is added to its start before "
            "comparing the gap to the next recording against "
            "--max-gap, so a long recording isn't mistaken for a gap - "
            "reusing an existing .duration.txt (from an earlier "
            "bv-generate --get-duration run) when there is one, or "
            "computing and writing one on the spot otherwise, so this "
            "works out of the box without needing that separate pass "
            "first. That self-healing is scoped to just the trip(s) "
            "actually being exported this run (see "
            "--duration-heal-archive to widen it) - --no-duration skips "
            "all of it entirely (no ffprobe/ffmpeg calls for this, no "
            "files written, no per-trip scope either) and falls back to "
            "plain start-to-start timestamps."
        ),
    )

    parser.add_argument(
        "--duration-heal-archive",
        action="store_true",
        default=False,
        help=(
            "Self-heal a missing .duration.txt for every recording in "
            "the archive during trip detection, not just the trip(s) "
            "being exported this run. By default, trip detection reads "
            "whatever .duration.txt already exists but doesn't compute "
            "a missing one - fast, but a recording that's never been "
            "probed yet could still occasionally land in the wrong trip "
            "until it's actually been exported once. This flag trades "
            "that fast default for maximum detection accuracy across "
            "the whole archive up front, at the cost of a real, "
            "possibly long, one-time ffprobe pass before the first "
            "overwrite prompt can even appear - worth it for an "
            "unattended batch run, less so interactively. Rejected "
            "together with --no-duration - nothing to heal against with "
            "duration data turned off entirely."
        ),
    )

    parser.add_argument(
        "--gap-tolerance",
        dest="gap_tolerance_seconds",
        type=int,
        metavar="SECONDS",
        default=None,
        help=(
            "A small fixed margin (in seconds) added on top of "
            "--max-gap before a gap counts as a split - absorbs "
            "measurement noise (duration/timestamp rounding, brief "
            "file-rotation overhead), not a detection setting like "
            f"--max-gap. Default: "
            f"{int(DEFAULT_GAP_TOLERANCE.total_seconds())}."
        ),
    )

    parser.add_argument(
        "--max-parking-duration",
        dest="max_parking_duration_minutes",
        type=int,
        metavar="MINUTES",
        default=None,
        help=(
            "How long a continuous run of Parking-mode footage can "
            "span, in real elapsed time (not its played-back length - "
            "a Parking-mode timelapse can run for well over an hour of "
            "real time while playing back in a few minutes), before a "
            "Parking recording is kept out of the trip it would "
            "otherwise end and starts the next trip instead - so a "
            "single Parking recording longer than this on its own is "
            "never appended to the drive before it. Two (or more) "
            "chained Parking recordings whose combined real span "
            "crosses this can split from each other the same way, not "
            "just at the point driving resumes. Requires real duration "
            "data the same way --max-gap's own duration-aware gap "
            "calculation does, so --no-duration disables this too. "
            f"Default: "
            f"{int(DEFAULT_MAX_PARKING_DURATION.total_seconds() // 60)}."
        ),
    )

    parser.add_argument(
        "--map",
        dest="render_map",
        action="store_true",
        help=(
            "Also render map.mp4: a route/position/speed overlay on "
            "an OpenStreetMap road basemap for each trip, framing the "
            "whole trip at once (a static overview). Off by default - "
            "the first trip through a given area needs a one-time "
            "network fetch of that area's road data (cached under "
            "--target/.osm_cache afterward, then fully offline), and "
            "rendering adds real time per trip. See --map-zoom for a "
            "closer, scrolling 'follow camera' view instead (a "
            "separate file, works with or without --map). The "
            "current-position marker is a bundled red car icon, "
            "rotated to match the GPS course over ground - see "
            "--map-icon to use a custom image or the plain arrow "
            "instead."
        ),
    )

    parser.add_argument(
        "--map-icon",
        metavar="PATH",
        default=None,
        help=(
            "Use a custom image as the position marker on --map and/or "
            "--map-zoom, rotated each frame to match the GPS course "
            "over ground. A PNG with transparency, drawn pointing "
            "'up'/north in its own file, works best. Default: a "
            "bundled red car icon - pass the literal value 'none' to "
            "use a plain rotating arrow instead, or a path to use your "
            "own image."
        ),
    )

    parser.add_argument(
        "--map-zoom",
        dest="map_zoom_meters",
        type=float,
        nargs="?",
        const=DEFAULT_ZOOM_RADIUS_METERS,
        default=None,
        metavar="METERS",
        help=(
            "Also render map_zoom_METERSm.mp4: a 'follow camera' view "
            "of real-world half-width METERS, centered on the "
            "vehicle's current position every frame, scrolling/panning "
            "as it moves - a separate file from --map's static "
            "whole-trip overview, and independent of it (works with or "
            "without --map given too). Defaults to "
            f"{DEFAULT_ZOOM_RADIUS_METERS:g}m if given with no value."
        ),
    )

    parser.add_argument(
        "--map-track-up",
        dest="map_track_up",
        action="store_true",
        help=(
            "Rotate --map and/or --map-zoom so the vehicle's current "
            "heading always points 'up' on screen, like a phone "
            "turn-by-turn app, instead of the default fixed north-up "
            "orientation. One switch for both - applies to whichever "
            "of --map/--map-zoom is given (meaningless alone). Costs "
            "real extra render time on --map specifically: its normal "
            "static overview draws the whole road network once and "
            "reuses it for every frame, but a rotating scene needs a "
            "fresh redraw whenever the heading changes. Doesn't affect "
            "--stitch-map's own embedded panel. Written to a "
            "differently-named file (map_tu.mp4 / "
            "map_zoom_METERSm_tu.mp4) than a plain --map/--map-zoom "
            "render, so re-running with the opposite setting never "
            "overwrites the other mode's file."
        ),
    )

    parser.add_argument(
        "--map-intro",
        dest="render_map_intro",
        action="store_true",
        help=(
            "Also render intro.mp4: a short establishing-shot flyover "
            "of the trip's whole route, zooming from a wide overview "
            "into the same framing --map's static overview uses. If "
            "--stitch is also given, intro.mp4 is automatically "
            "prepended onto the front of stitch.mp4 (sized/timed to "
            "match it exactly, with its own real audio carried "
            "through, delayed to stay in sync); without --stitch, "
            "intro.mp4 is written standalone. See --map-intro-seconds "
            "to change its length."
        ),
    )

    parser.add_argument(
        "--map-intro-seconds",
        dest="map_intro_seconds",
        type=float,
        default=DEFAULT_INTRO_SECONDS,
        metavar="SECONDS",
        help=(
            "Length of --map-intro's flyover, in seconds. Meaningless "
            f"without --map-intro. Default: {DEFAULT_INTRO_SECONDS:g}."
        ),
    )

    parser.add_argument(
        "--gsensor-video",
        dest="render_gsensor",
        action="store_true",
        help=(
            "Also render gsensor.mp4: a dot moving around a gauge, "
            "tracking the trip's g-sensor (x, y) readings with a "
            "short fading trail, on a flat chroma-key green "
            "background meant for compositing over the front/rear "
            "footage later. No network involved, but off by default "
            "- it adds real render time per trip."
        ),
    )

    parser.add_argument(
        "--gsensor-graph-video",
        dest="render_gsensor_graph",
        action="store_true",
        help=(
            "Also render gsensor_graph.mp4: a second, alternate "
            "g-sensor visualization - a static whole-trip strip chart "
            "of the trip's Y/Z (and X, see --gsensor-graph-x) g-sensor "
            "readings as colored line traces, with a vertical playhead "
            "marking the current position, on the same flat chroma-key "
            "green background as --gsensor-video's dot-gauge. "
            "Independent of --gsensor-video - either, both, or neither "
            "can be given; this is a separate file, not a replacement. "
            "Off by default - it adds real render time per trip."
        ),
    )

    parser.add_argument(
        "--gsensor-graph-x",
        dest="gsensor_graph_x",
        action="store_true",
        help=(
            "Also plot X (up/down) on the g-sensor graph - both "
            "--gsensor-graph-video's own gsensor_graph.mp4 and "
            "--stitch-graph's panel. X is hidden by default: \"Z is "
            "just not useful, unless you hit a giant pothole, but then "
            "the video probably got that and the reaction of the "
            "driver\" (originally about Z, moved to X once the axes' "
            "own meanings settled) - the one situation where that axis "
            "genuinely matters is already captured by the footage "
            "itself, so it's opt-in for a specific look at a bump/"
            "vibration event rather than on by default. Meaningless on "
            "its own without --gsensor-graph-video and/or "
            "--stitch-graph also given."
        ),
    )

    parser.add_argument(
        "--stitch",
        action="store_true",
        help=(
            "Also render stitch.mp4: the trip's front and rear video "
            "composed into one, side by side, stacked, or as a "
            "rearview-mirror inset (see --stitch-layout), optionally "
            "with a map panel (see --stitch-map), a g-sensor overlay "
            "(see --stitch-gsensor), and/or burned-in subtitles (see "
            "--stitch-subtitles). A trip with only one camera falls "
            "back to a plain copy of whichever one exists, ignoring "
            "all of those too. Auto-picking a layout from the trip's "
            "own geometry is still planned for later."
        ),
    )

    parser.add_argument(
        "--stitch-layout",
        choices=[*ALL_LAYOUTS, AUTO_LAYOUT],
        default=AUTO_LAYOUT,
        help=(
            "Camera arrangement for --stitch: 'side_by_side' (front | "
            "rear), 'top_down' (front / rear), or 'rearview_mirror' "
            "(front full-frame, rear flipped horizontally and shrunk "
            "into a mirror-style inset overlaid top-center - see "
            "--stitch-mirror-size). Only used together with --stitch. "
            "Default: 'auto' - picks side_by_side or top_down from the "
            "trip's own north-south/east-west GPS extent (falls back "
            "to side_by_side with a warning if there's no GPS data). "
            "rearview_mirror is never auto-picked - name it explicitly "
            "to use it."
        ),
    )

    parser.add_argument(
        "--stitch-mirror-size",
        type=_parse_mirror_size,
        default=_DEFAULT_CLI_MIRROR_SIZE_PERCENT,
        metavar="PERCENT",
        help=(
            f"Mirror inset size as a percentage of the composite's own "
            f"width ({MIN_MIRROR_SIZE_PERCENT:g}-"
            f"{MAX_MIRROR_SIZE_PERCENT:g}). Only meaningful with "
            f"--stitch-layout rearview_mirror. Default: "
            f"{_DEFAULT_CLI_MIRROR_SIZE_PERCENT:g}."
        ),
    )

    parser.add_argument(
        "--stitch-mirror-radius",
        type=_parse_mirror_radius,
        default=DEFAULT_MIRROR_RADIUS_PERCENT,
        metavar="PERCENT",
        help=(
            f"Round the mirror inset's four corners, as a percentage of "
            f"the inset's own min(width, height)/2 "
            f"({MIN_MIRROR_RADIUS_PERCENT:g}-{MAX_MIRROR_RADIUS_PERCENT:g}"
            f") - 0 leaves them square, 100 rounds each corner all the "
            f"way to a quarter-circle of that radius. Only meaningful "
            f"with --stitch-layout rearview_mirror. Default: "
            f"{DEFAULT_MIRROR_RADIUS_PERCENT:g}."
        ),
    )

    parser.add_argument(
        "--stitch-mirror-zoom",
        type=_parse_mirror_zoom,
        default=_DEFAULT_CLI_MIRROR_ZOOM_PERCENT,
        metavar="PERCENT",
        help=(
            f"Zoom the mirror inset in, by cropping this percentage off "
            f"each edge of the rear source (toward its own center) "
            f"before it's scaled in "
            f"({MIN_MIRROR_ZOOM_PERCENT:g}-{MAX_MIRROR_ZOOM_PERCENT:g}) "
            f"- 0 shows the whole rear frame unchanged, higher values "
            f"show progressively less of it. Only meaningful with "
            f"--stitch-layout rearview_mirror. Default: "
            f"{_DEFAULT_CLI_MIRROR_ZOOM_PERCENT:g}."
        ),
    )

    parser.add_argument(
        "--stitch-mirror-pan-x",
        type=_parse_mirror_pan,
        default=DEFAULT_MIRROR_PAN_X_PERCENT,
        metavar="PERCENT",
        help=(
            f"Pan the mirror inset's crop window left/right within the "
            f"margin --stitch-mirror-zoom already crops away "
            f"({MIN_MIRROR_PAN_PERCENT:g}-{MAX_MIRROR_PAN_PERCENT:g}) - 0 "
            f"stays centered, negative pans left, positive "
            f"pans right, +/-{MAX_MIRROR_PAN_PERCENT:g} pushes the crop "
            f"window flush against one edge. Only has room to move once "
            f"--stitch-mirror-zoom is above 0 - at 0 there's no "
            f"cropped-away margin to pan into. Only meaningful with "
            f"--stitch-layout rearview_mirror. Default: "
            f"{DEFAULT_MIRROR_PAN_X_PERCENT:g}."
        ),
    )

    parser.add_argument(
        "--stitch-mirror-pan-y",
        type=_parse_mirror_pan,
        default=_DEFAULT_CLI_MIRROR_PAN_Y_PERCENT,
        metavar="PERCENT",
        help=(
            f"Pan the mirror inset's crop window up/down within the "
            f"margin --stitch-mirror-zoom already crops away "
            f"({MIN_MIRROR_PAN_PERCENT:g}-{MAX_MIRROR_PAN_PERCENT:g}) - 0 "
            f"stays centered, negative pans up, positive "
            f"pans down, +/-{MAX_MIRROR_PAN_PERCENT:g} pushes the crop "
            f"window flush against one edge. Same --stitch-mirror-zoom "
            f"-dependent behavior as --stitch-mirror-pan-x. Only "
            f"meaningful with --stitch-layout rearview_mirror. Default: "
            f"{_DEFAULT_CLI_MIRROR_PAN_Y_PERCENT:g} (panned up, since "
            f"the default --stitch-mirror-zoom already crops in)."
        ),
    )

    parser.add_argument(
        "--stitch-mirror-icon",
        metavar="PATH",
        default=None,
        help=(
            "Composite the mirror inset into a photo of a real physical "
            "rearview mirror instead of the plain procedural rounded "
            "rectangle - the rear camera's footage is clipped into that "
            "photo's own glass area, and the photo's frame/mount is "
            "drawn on top, so the inset reads as footage playing inside "
            "an actual mirror. A plain product-style photo works best "
            "(a clearly darker frame/mount around a lighter glass area, "
            "on a light background) - the image is segmented "
            "automatically, no transparency or pre-editing required. "
            "Only meaningful with --stitch-layout rearview_mirror. "
            "--stitch-mirror-radius is ignored when this is given (the "
            "photo's own frame shape is used instead); --stitch-mirror "
            "-zoom still applies to how much of the rear frame is shown. "
            "Falls back to the plain procedural inset with a warning if "
            "the image can't be read or segmented. Default: a bundled "
            "reference mirror photo - pass the literal value 'none' to "
            "use the plain procedural inset instead, or a path to use "
            "your own photo."
        ),
    )

    parser.add_argument(
        "--stitch-resolution",
        type=_parse_resolution,
        default=None,
        metavar="WIDTHxHEIGHT",
        help=(
            "Scale stitch.mp4 to this resolution (e.g. 320x240) "
            "instead of leaving it at front's own resolution - a fast "
            "small test render instead of waiting on a full-size "
            "encode. Only used together with --stitch."
        ),
    )

    parser.add_argument(
        "--stitch-bitrate",
        default=None,
        metavar="RATE",
        help=(
            "Target video bitrate for stitch.mp4 (e.g. 256k, 2M), "
            "passed straight to ffmpeg (-b:v/-maxrate/-bufsize all set "
            "to RATE). Capped to twice the original front/rear "
            "footage's own combined bitrate (front alone for "
            "rearview_mirror), so an unreasonably high request can't "
            "ask for detail the source never had. Omitting this flag "
            "doesn't mean 'no limit' - it defaults to matching that "
            "same source bitrate directly, rather than a flat quality "
            "target independent of the source. Only used together "
            "with --stitch."
        ),
    )

    parser.add_argument(
        "--stitch-scale",
        type=_parse_stitch_scale,
        default=None,
        metavar="PERCENT",
        help=(
            "Scale stitch.mp4's own natural resolution down to this "
            f"percentage ({MIN_STITCH_SCALE_PERCENT:g}-"
            f"{MAX_STITCH_SCALE_PERCENT:g}) - e.g. 50 halves both "
            "dimensions. Unlike --stitch-resolution, this always "
            "preserves the natural composite's own aspect ratio "
            "exactly (camera composite plus any --stitch-map panel), "
            "so it never adds letterbox/pillarbox black bars - use "
            "this instead of guessing a --stitch-resolution that "
            "happens to match. Combines with --stitch-max-width/"
            "--stitch-max-height (whichever cap shrinks the output "
            "most wins). Only used together with --stitch."
        ),
    )

    parser.add_argument(
        "--stitch-max-width",
        type=_parse_positive_pixels,
        default=None,
        metavar="PIXELS",
        help=(
            "Cap stitch.mp4's own natural width at this many pixels, "
            "scaling the whole frame down (never up) just enough to "
            "fit - the natural aspect ratio is always preserved, so "
            "this never adds black bars the way an exact "
            "--stitch-resolution might. Combines with --stitch-scale/"
            "--stitch-max-height (whichever cap shrinks the output "
            "most wins). Only used together with --stitch."
        ),
    )

    parser.add_argument(
        "--stitch-max-height",
        type=_parse_positive_pixels,
        default=None,
        metavar="PIXELS",
        help=(
            "Cap stitch.mp4's own natural height at this many pixels - "
            "see --stitch-max-width. Combines with --stitch-scale/"
            "--stitch-max-width (whichever cap shrinks the output most "
            "wins). Only used together with --stitch."
        ),
    )

    parser.add_argument(
        "--stitch-map",
        nargs="?",
        choices=["map", "zoom"],
        const="map",
        default=None,
        help=(
            "Also compose a map panel alongside the camera composite "
            "in stitch.mp4, rendered fresh at whatever size fits the "
            "composite (not a copy of --map's own map.mp4) - bare flag "
            "uses a static whole-trip overview, --stitch-map zoom uses "
            "a follow-camera view instead (reusing --map-zoom METERS "
            "as its radius - --map-zoom must also be given for that "
            "variant). Only used together with --stitch."
        ),
    )

    parser.add_argument(
        "--stitch-map-side",
        choices=["left", "right", "top", "down"],
        default=None,
        help=(
            "Override --stitch-map's panel side. Default: left for "
            "--stitch-layout top_down, down for side_by_side or "
            "rearview_mirror (capped at 30%% of width/height in "
            "rearview_mirror specifically, vs. the general 50%%)."
        ),
    )
    parser.add_argument(
        "--stitch-map-size",
        type=_parse_map_size,
        default=None,
        help=(
            "Override --stitch-map's panel width/height as a percent "
            f"of the camera composite's matching dimension "
            f"({MIN_MAP_SIZE_PERCENT:g}-{MAX_MAP_SIZE_PERCENT:g}). "
            "Default: sized automatically from the trip's own real "
            "-world aspect ratio, clamped to 20-50%% (30%% for "
            "rearview_mirror) - a near-straight-line trip can land "
            "right at that floor and read as too thin; this asks for "
            "an exact size instead."
        ),
    )

    parser.add_argument(
        "--stitch-map-circle",
        dest="stitch_map_circle",
        action="store_true",
        default=None,
        help=(
            "Mask --stitch-map's panel into a full ellipse (a circle "
            "if the panel happens to be square, an oval otherwise) "
            "instead of a plain rectangle - corners render as solid "
            "black. Applies to either --stitch-map variant. Only used "
            "together with --stitch-map. Default: on automatically "
            "for --stitch-map zoom (Christer: \"maaybe circel should "
            "be default, it looks so much better\" -> \"Make it the "
            "default zoom map\"), off for the static overview - use "
            "--no-stitch-map-circle to force it off in zoom mode too."
        ),
    )

    parser.add_argument(
        "--no-stitch-map-circle",
        dest="stitch_map_circle",
        action="store_false",
        help=(
            "Force --stitch-map's panel to stay a plain rectangle even "
            "in --stitch-map zoom mode, where circle is on by default."
        ),
    )

    parser.add_argument(
        "--stitch-gsensor",
        action="store_true",
        help=(
            "Also composite gsensor.mp4 (see --gsensor-video) as a "
            "transparent overlay on top of the camera footage in "
            "stitch.mp4. Unlike --stitch-map, this never generates "
            "gsensor.mp4 itself - it must already exist (this run's "
            "own --gsensor-video, or an earlier run's), or the "
            "overlay is skipped with a warning. Only used together "
            "with --stitch."
        ),
    )

    parser.add_argument(
        "--stitch-gsensor-size",
        type=_parse_gsensor_size,
        default=DEFAULT_GSENSOR_SIZE_PERCENT,
        metavar="PERCENT",
        help=(
            f"Overlay size as a percentage of the camera composite's "
            f"width ({MIN_GSENSOR_SIZE_PERCENT:g}-"
            f"{MAX_GSENSOR_SIZE_PERCENT:g}). Default: "
            f"{DEFAULT_GSENSOR_SIZE_PERCENT:g}."
        ),
    )

    gsensor_position_group = parser.add_mutually_exclusive_group()
    gsensor_position_group.add_argument(
        "--stitch-gsensor-pos",
        type=_parse_gsensor_position,
        default=None,
        metavar="POSITION",
        help=(
            "Named overlay position: any combination of left/right/"
            "top/down/center (e.g. top-right, plain center). Defined "
            "relative to the camera footage only, excluding whatever "
            "space --stitch-map's panel occupies. Default: "
            f"{DEFAULT_GSENSOR_POSITION}. Mutually exclusive with "
            "--stitch-gsensor-xy."
        ),
    )
    gsensor_position_group.add_argument(
        "--stitch-gsensor-xy",
        type=_parse_gsensor_xy,
        default=None,
        metavar="X,Y",
        help=(
            "Explicit overlay position as X,Y percentages (not "
            "pixels) of the footage region's top-left corner, e.g. "
            "80,10. A deliberate override - allowed to land anywhere, "
            "including on top of --stitch-map's panel. Mutually "
            "exclusive with --stitch-gsensor-pos."
        ),
    )

    parser.add_argument(
        "--stitch-graph",
        action="store_true",
        help=(
            "Also compose a --stitch-graph panel alongside the camera "
            "composite in stitch.mp4: a strip chart of this trip's "
            "X/Y/Z g-sensor readings with a moving playhead - a "
            "second, alternate g-sensor visualization alongside "
            "--stitch-gsensor's dot-gauge overlay. Unlike "
            "--stitch-gsensor, this is rendered fresh at the exact "
            "panel size and grows the composite, the same way "
            "--stitch-map does, rather than needing an already"
            "-rendered file overlaid on top. Composed after any "
            "--stitch-map panel, so the two can be used together - "
            "e.g. a map on the bottom (--stitch-map) and this graph "
            "as a vertical side panel (its own default side). Only "
            "used together with --stitch."
        ),
    )

    parser.add_argument(
        "--stitch-graph-side",
        choices=["left", "right", "top", "down"],
        default=None,
        help=(
            "Override --stitch-graph's panel side. Default: whichever "
            "side --stitch-map's own panel *didn't* use (e.g. a map on "
            "the left defaults the graph to the bottom, and vice versa), "
            "so the two grow the frame in different directions and stay "
            "closer to a 16:9 shape overall; defaults to the bottom if "
            "there's no map panel actually present at all. The panel's "
            "own orientation follows automatically: 'left'/'right' "
            "renders a tall, narrow panel with upright tick labels and "
            "time running top to bottom; 'top'/'down' renders a short, "
            "wide panel with time running left to right, like the "
            "standalone gsensor_graph.mp4 default."
        ),
    )
    parser.add_argument(
        "--stitch-graph-size",
        type=_parse_graph_size,
        default=None,
        help=(
            "Override --stitch-graph's panel width/height as a "
            f"percent of the camera composite's matching dimension "
            f"({MIN_GRAPH_SIZE_PERCENT:g}-{MAX_GRAPH_SIZE_PERCENT:g}). "
            f"Default: a fixed {DEFAULT_GRAPH_SIZE_PERCENT:g}%%, "
            "matching --stitch-map's own size ceiling - there's no "
            "--stitch-map-style automatic geography-based sizing here, "
            "a synthetic chart has no equivalent real-world shape to "
            "derive one from."
        ),
    )

    parser.add_argument(
        "--stitch-subtitles",
        action="store_true",
        help=(
            "Also burn this trip's own trip.srt into stitch.mp4's "
            "final frame - centered, near the bottom, after any "
            "g-sensor overlay/map panel. Unlike --stitch-gsensor, "
            "there's nothing to render first: trip.srt is written "
            "automatically whenever the trip has any transcript data "
            "at all. If it doesn't, the burn-in is skipped with a "
            "warning. Only used together with --stitch."
        ),
    )

    parser.add_argument(
        "--no-subtitles-bg",
        dest="subtitles_bg",
        action="store_false",
        help=(
            "Disable the dark, semi-transparent background bar behind "
            "burned-in subtitle text (on by default when "
            "--stitch-subtitles is given)."
        ),
    )

    parser.add_argument(
        "--include-parking",
        action="store_true",
        default=False,
        help=(
            "Include Parking-mode recordings as-is in "
            "front.mp4/rear.mp4/audio.aac. By default, every Parking "
            "recording is left out entirely - wherever it falls in "
            "the trip - with nothing substituted in its place. (An "
            "earlier version of this flag replaced mid-trip Parking "
            "recordings with a short synthetic transition clip; this "
            "was removed after real HEVC dashcam footage showed the "
            "splice corrupting front.mp4/rear.mp4 from that point "
            "onward - see WORKING_CONTEXT.md for the root cause.)"
        ),
    )

    parser.add_argument(
        "--parking-speed",
        dest="parking_speed",
        type=_parse_parking_speed,
        default=1.0,
        metavar="SPEED",
        help=(
            "Play back included Parking-mode footage at SPEED times its "
            f"own natural pace ({MIN_PARKING_SPEED:g}-{MAX_PARKING_SPEED:g} "
            "- e.g. 2 plays it twice as fast, 0.5 half as fast). Parking "
            "footage is motion-triggered and sparse, so a long real-world "
            "span can compress into a slow, uneventful stretch of the "
            "final export; this speeds it up (or slows it down) without "
            "touching the pace of the rest of the trip. Has no effect "
            "without --include-parking. Default: 1 (no change)."
        ),
    )

    parser.add_argument(
        "--overwrite",
        action="store_true",
        help=(
            "Wipe and rebuild each trip's folder from scratch, without "
            "asking. Without this: an interactive run asks once whether "
            "to wipe or keep existing trip folders (the answer applies "
            "to every trip folder touched this run); a non-interactive "
            "run always keeps them, only overwriting whatever files it "
            "actually regenerates - useful since some outputs (--map in "
            "particular) are expensive to redo."
        ),
    )

    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show which trip folders would be created/refreshed "
             "without writing anything.",
    )

    parser.add_argument(
        "--debug",
        action="store_true",
        help=(
            "Print wall-clock timing to stderr for each trip's "
            "concatenation/map/stitch phases, plus which decode "
            "method (nvdec or cpu) --stitch used and how long it "
            "took - useful for tracking down where time went on a "
            "slow run."
        ),
    )

    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)

    # The exact invoking command, written verbatim into every trip's
    # own trip.log (see export_trip()'s docstring) - reconstructed
    # from argv rather than args, since args has already been through
    # argparse's own parsing/defaulting and wouldn't necessarily read
    # back as what Christer actually typed.
    raw_argv = argv if argv is not None else sys.argv[1:]
    command_line = "bv-export " + shlex.join(raw_argv)

    # See bv_scribe.py's own main() for why - wrap_say()/wrap_warn()
    # (core/joblog.py) mirror every printed line into the persistent
    # output log alongside the real terminal output.
    say = wrap_say("bv-export")
    warn = wrap_warn("bv-export", _default_warn)
    return run_cli(
        "bv-export",
        lambda: _run(args, command_line=command_line, say=say, warn=warn),
        argv=argv,
    )


def _run(
    args: argparse.Namespace,
    *,
    command_line: str | None = None,
    should_continue: Callable[[], bool] = lambda: True,
    say=print,
    warn=_default_warn,
) -> int:
    """Run bv-export for already-parsed arguments - the same
    args-to-`bv_export()`-kwargs mapping `main()` always did, pulled
    out into its own function so bv-web's job runner (see
    web/jobs.py's `start_bv_export()`) can call it directly with
    `parse_args()`-built args, the same shape every other bv-*
    command's own `_run()` already takes.

    `command_line` defaults to None here (no fabricated shell command
    for a web-triggered export - jobs.py builds and passes a real one
    reconstructed from the same argv it used for parse_args(), so it's
    normally given) rather than being reconstructed from sys.argv the
    way `main()` does, since there is no real argv for a job that was
    never actually typed at a shell.

    `should_continue` is forwarded straight into `bv_export()` - see
    that function's own docstring. Left at its always-True default for
    a real terminal run (via main() below); bv-web's job runner is the
    only caller that passes a real one, tied to its own Job's Cancel
    button.

    `bv_export()` itself still raises `SystemExit` for its own two
    fatal-argument-combination checks (`--duration-heal-archive` with
    `--no-duration`; an unparseable `--from`/`--until`/`--timestamp`) -
    deliberately left as-is (see bv_export()'s own tests, which assert
    this) rather than changed to a `warn()`+return like everywhere
    else, since that's this function's own already-tested public
    contract. Caught here instead, right at this one call site, and
    turned into a normal `warn()`+return-1 - the only place that needs
    to know about it, so neither a real terminal run through main()
    (SystemExit already propagates correctly there via run_cli, same
    as always) nor bv-web's job runner (which must never see a raw
    SystemExit escape a background thread - see jobs.py's own
    docstring for why) has to handle it a second time.
    """

    archive_path, camera_config = resolve_archive_path(args.path, args.config_dir)

    target = args.target
    if target is None and camera_config is not None:
        target = camera_config.target
    if target is None:
        warn(
            "bv-export: --target is required (no Target directory set "
            f"in {args.path!r}'s camera config - see bv-config)"
            if camera_config is not None
            else "bv-export: --target is required"
        )
        return 1

    # An explicit --target that diverges from the camera's own
    # configured default is a deliberate one-off (nothing stops you
    # exporting to wherever you like), but it's worth flagging: bv-web
    # discovers trips by reading each configured camera's own Target
    # (see web/trips.py's scan_all_trips()), not by walking arbitrary
    # directories - a trip written somewhere else won't show up there
    # on its own. Only fires when both a camera config *and* a
    # configured Target exist to diverge from; an explicit --target on
    # a bare path (no camera config at all) has nothing to compare
    # against and is never "diverging" from anything.
    if (
        args.target is not None
        and camera_config is not None
        and camera_config.target is not None
        and Path(args.target).resolve() != camera_config.target.resolve()
    ):
        say(
            "bv-export: note - this --target differs from "
            f"{args.path!r}'s configured Target ({camera_config.target}). "
            "bv-web won't discover trips exported here automatically; "
            "you're on your own trip (pun intended)."
        )

    try:
        return bv_export(
            path=archive_path,
            target=target,
            prefix=args.prefix,
            from_=args.from_,
            until=args.until,
            timestamp=args.timestamp,
            max_gap_minutes=args.max_gap_minutes,
            movement=args.movement,
            duration=args.duration,
            duration_heal_archive=args.duration_heal_archive,
            gap_tolerance_seconds=args.gap_tolerance_seconds,
            max_parking_duration_minutes=args.max_parking_duration_minutes,
            render_map=args.render_map,
            map_icon=args.map_icon,
            map_zoom_meters=args.map_zoom_meters,
            map_track_up=args.map_track_up,
            render_map_intro=args.render_map_intro,
            map_intro_seconds=args.map_intro_seconds,
            render_gsensor=args.render_gsensor,
            render_gsensor_graph=args.render_gsensor_graph,
            gsensor_graph_x=args.gsensor_graph_x,
            stitch_layout=args.stitch_layout if args.stitch else None,
            stitch_resolution=args.stitch_resolution,
            stitch_bitrate=args.stitch_bitrate,
            stitch_scale=args.stitch_scale,
            stitch_max_width=args.stitch_max_width,
            stitch_max_height=args.stitch_max_height,
            stitch_mirror_size=args.stitch_mirror_size,
            stitch_mirror_radius=args.stitch_mirror_radius,
            stitch_mirror_zoom=args.stitch_mirror_zoom,
            stitch_mirror_pan_x=args.stitch_mirror_pan_x,
            stitch_mirror_pan_y=args.stitch_mirror_pan_y,
            stitch_mirror_icon=args.stitch_mirror_icon,
            stitch_map=args.stitch_map if args.stitch else None,
            stitch_map_side=args.stitch_map_side,
            stitch_map_size=args.stitch_map_size,
            stitch_map_circle=args.stitch_map_circle,
            stitch_gsensor=args.stitch_gsensor if args.stitch else False,
            stitch_gsensor_size=args.stitch_gsensor_size,
            stitch_gsensor_pos=args.stitch_gsensor_pos,
            stitch_gsensor_xy=args.stitch_gsensor_xy,
            stitch_graph=args.stitch_graph if args.stitch else False,
            stitch_graph_side=args.stitch_graph_side,
            stitch_graph_size=args.stitch_graph_size,
            stitch_subtitles=args.stitch_subtitles if args.stitch else False,
            stitch_subtitles_background=args.subtitles_bg,
            include_parking=args.include_parking,
            parking_speed=args.parking_speed,
            overwrite=args.overwrite,
            dry_run=args.dry_run,
            debug=args.debug,
            command_line=command_line,
            should_continue=should_continue,
            say=say,
            warn=warn,
        )
    except SystemExit as exc:
        # Not warn(f"bv-export: {exc}") - one of the two raise sites
        # this can only ever be (see this function's own docstring)
        # already bakes its own "bv-export: " prefix into the message
        # itself, and the other never had one even when this
        # propagated all the way to Python's default top-level
        # SystemExit handler (str(exc) printed verbatim) - adding a
        # second, universal prefix here would double up the first
        # case and inconsistently decorate the second, changing
        # already-real output for no benefit.
        warn(str(exc))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
