"""
bv-search.

Search a BlackVue archive by text (transcript/translation/scene-
description content) and/or GPS proximity to a point or place name,
combinable in one run - the same LexicalTimeParser-based recording
selection every other bv-* command uses, applied first to narrow the
candidate recordings before either search runs.

Copyright (C) 2026 Christer R. (sssreh)

SPDX-License-Identifier: GPL-3.0-or-later
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from ..archive import Archive
from .errors import run_cli
from ..core.camera_config import default_config_dir
from ..core.camera_config import resolve_archive_path
from ..generate import MediaToolError
from ..lexicaltimeparser import LexicalTimeParser
from ..search import TEXT_SEARCH_ASSETS
from ..search import search_near
from ..search import search_text

EXIT_OK = 0
EXIT_ARGS_ERROR = 1
EXIT_HAD_ERRORS = 2

DEFAULT_RADIUS_METERS = 200.0


def _parse_coordinates(value: str) -> tuple[float, float]:
    """argparse `type=` for --near LAT,LON - a single comma-separated
    token rather than two separate arguments, so it composes cleanly
    with the rest of the flag set (no risk of the longitude being
    mistaken for a second positional/option)."""

    parts = value.split(",")
    if len(parts) != 2:
        raise argparse.ArgumentTypeError(
            f"{value!r} - expected LAT,LON (e.g. 59.3293,18.0686)"
        )

    try:
        return float(parts[0]), float(parts[1])
    except ValueError:
        raise argparse.ArgumentTypeError(
            f"{value!r} - LAT/LON must both be numbers"
        ) from None


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Parse command line arguments."""

    parser = argparse.ArgumentParser(
        prog="bv-search",
        description=(
            "Search recordings by text (transcript/translation/scene "
            "description) and/or GPS proximity to a coordinate or "
            "place name, combinable in one run. Uses the same "
            "recording selection every other bv-* command does."
        ),
        allow_abbrev=False,
    )

    parser.add_argument(
        "path",
        nargs="?",
        default=".",
        help=(
            "Archive directory. Also accepts a camera system id (see "
            "bv-config), resolved to that camera's archive target - "
            "use an explicit ./name or .\\name to force a literal "
            "directory of the same name instead."
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
        "--from",
        dest="from_",
        metavar="TIMESTAMP",
        help="Only consider recordings from this timestamp.",
    )
    parser.add_argument(
        "--until",
        metavar="TIMESTAMP",
        help="Only consider recordings up to this timestamp.",
    )
    parser.add_argument(
        "--timestamp",
        metavar="TIMESTAMP",
        help="Only consider recordings matching this timestamp or prefix.",
    )

    parser.add_argument(
        "--text",
        metavar="PATTERN",
        help=(
            "Search for PATTERN in transcript/translation/scene-"
            "description text (case-insensitive substring match by "
            "default - see --regex/--case-sensitive)."
        ),
    )
    parser.add_argument(
        "--asset",
        choices=["all", "transcript", "translation", "scene"],
        default="all",
        help=(
            "Restrict --text to one category of text asset (default: "
            "all - transcript, translation, and scene description, "
            "including diarized/rear variants where they exist)."
        ),
    )
    parser.add_argument(
        "--regex",
        action="store_true",
        help="Treat --text as a regular expression instead of a plain substring.",
    )
    parser.add_argument(
        "--case-sensitive",
        action="store_true",
        help="Make --text case-sensitive (default: case-insensitive).",
    )

    location = parser.add_mutually_exclusive_group()
    location.add_argument(
        "--near",
        metavar="LAT,LON",
        type=_parse_coordinates,
        help=(
            "Only consider recordings with a GPS fix within --radius "
            "of this coordinate."
        ),
    )
    location.add_argument(
        "--place",
        metavar="NAME",
        help=(
            "Same as --near, but geocodes a free-text place name to "
            "a coordinate first, via OpenStreetMap Nominatim - needs "
            "network access on first use for a given name; results "
            "are cached under <archive>/.osm_cache afterward."
        ),
    )
    parser.add_argument(
        "--radius",
        type=float,
        default=DEFAULT_RADIUS_METERS,
        metavar="METERS",
        help=(
            "Search radius for --near/--place, in meters "
            f"(default: {DEFAULT_RADIUS_METERS:g})."
        ),
    )

    return parser.parse_args(argv)


def _default_warn(message: str) -> None:
    print(message, file=sys.stderr)


def _report_text_match(say, match) -> None:
    say(f"  {match.path.name}:{match.line_number}: {match.line.strip()}")


def _report_geo_match(say, match) -> None:
    fix = match.fix
    say(
        f"  GPS: {match.distance_meters:.0f}m from target at "
        f"{fix.timestamp:%Y-%m-%d %H:%M:%S} "
        f"({fix.latitude:.5f},{fix.longitude:.5f})"
    )


def _run(args: argparse.Namespace, *, say=print, warn=_default_warn) -> int:
    """Run bv-search for already-parsed arguments. `say`/`warn` are
    injectable (default: real stdout/stderr), same pattern as every
    other bv-* CLI's own `_run()`."""

    if args.text is None and args.near is None and args.place is None:
        warn("bv-search: give at least one of --text, --near, or --place")
        return EXIT_ARGS_ERROR

    archive_path, _camera_config = resolve_archive_path(args.path, args.config_dir)
    archive = Archive(archive_path)

    try:
        interval = LexicalTimeParser(
            timestamp=args.timestamp, from_=args.from_, until=args.until,
        ).parse()
    except ValueError as exc:
        warn(f"bv-search: {exc}")
        return EXIT_ARGS_ERROR

    target: tuple[float, float] | None = args.near
    target_lines: tuple[tuple[tuple[float, float], ...], ...] = ()

    if args.place is not None:
        # Deferred import: blackvue.export's package __init__ pulls in
        # the whole ffmpeg/PIL/numpy-heavy export toolkit (stitching,
        # map rendering, ...) just to get to this one small geocoding
        # helper - not worth paying for on every bv-search run, only
        # the ones that actually use --place. Same pattern speech.py/
        # scene.py already use for torch/ctranslate2.
        from ..export.geocoding import load_or_forward_geocode

        cache_dir = archive_path / ".osm_cache"
        try:
            result = load_or_forward_geocode(args.place, cache_dir)
        except (MediaToolError, OSError) as exc:
            warn(f"bv-search: {exc}")
            return EXIT_HAD_ERRORS
        if result is None:
            warn(f"bv-search: no place found matching {args.place!r}")
            return EXIT_HAD_ERRORS
        target = result.point
        target_lines = result.lines
        geometry_note = (
            f" (road/area geometry, {len(target_lines)} segment(s) - "
            "searching along the whole shape, not just this point)"
            if target_lines
            else ""
        )
        say(
            f"bv-search: {args.place!r} -> "
            f"{target[0]:.5f},{target[1]:.5f}{geometry_note}"
        )

    recordings = [
        recording for recording in archive.recordings
        if recording.id.value in interval
    ]

    if not recordings:
        say(f"bv-search: {archive_path} - no recordings found in range.")
        return EXIT_OK

    text_assets = TEXT_SEARCH_ASSETS[args.asset]
    had_error = False
    match_count = 0

    for recording in recordings:
        text_matches = []

        if args.text is not None:
            try:
                text_matches = search_text(
                    recording, args.text,
                    assets=text_assets,
                    case_sensitive=args.case_sensitive,
                    regex=args.regex,
                )
            except MediaToolError as exc:
                warn(f"bv-search: {recording.id}: {exc}")
                had_error = True
                continue
            if not text_matches:
                continue

        geo_match = None
        if target is not None:
            geo_match = search_near(
                recording, target[0], target[1], args.radius, lines=target_lines
            )
            if geo_match is None:
                continue

        match_count += 1
        say(str(recording.id))
        for match in text_matches:
            _report_text_match(say, match)
        if geo_match is not None:
            _report_geo_match(say, geo_match)

    if match_count == 0:
        say("bv-search: no matches.")

    return EXIT_HAD_ERRORS if had_error else EXIT_OK


def main(argv: list[str] | None = None) -> int:
    """Run bv-search."""

    args = parse_args(argv)
    return run_cli("bv-search", lambda: _run(args))


if __name__ == "__main__":
    raise SystemExit(main())
