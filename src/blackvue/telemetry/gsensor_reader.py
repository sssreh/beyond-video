"""
Raw BlackVue .3gf (g-sensor) file reader.

File format (reverse-engineered from a real recording, not officially
documented): fixed 10-byte records, back to back, no header or
trailer - a file's size is always an exact multiple of 10. Each
record is:

    4 bytes  big-endian unsigned int   milliseconds since recording start
    2 bytes  big-endian signed short   X axis
    2 bytes  big-endian signed short   Y axis
    2 bytes  big-endian signed short   Z axis

Verified against a real ~170 second file: the millisecond field
counts up smoothly with ~100ms steps (a real 4-byte field - it keeps
counting past the 65536 mark a 2-byte field would wrap at, with no
reset). The physical unit of the X/Y/Z values isn't confirmed (could
be milli-g, raw ADC counts, or something else) - in the one real
sample available, a stationary/idling vehicle, they sit in a tight,
stable band throughout. Because the unit is unconfirmed, don't build
logic here that assumes a calibrated g-force threshold; anything
using these values for a movement heuristic should work off relative
variance instead.

Copyright (C) 2026 Christer R. (sssreh)

SPDX-License-Identifier: GPL-3.0-or-later
"""

from __future__ import annotations

import struct
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path

from ..generate.media import MediaToolError

_RECORD_FORMAT = ">Ihhh"
_RECORD_SIZE = struct.calcsize(_RECORD_FORMAT)


@dataclass(frozen=True)
class GSensorSample:
    """One g-sensor reading."""

    offset: timedelta
    x: int
    y: int
    z: int


def read_gsensor(path: Path) -> tuple[GSensorSample, ...]:
    """Read every sample from a raw BlackVue .3gf file."""

    try:
        data = path.read_bytes()
    except OSError as exc:
        raise MediaToolError(f"could not read {path.name}: {exc}") from exc

    if len(data) % _RECORD_SIZE != 0:
        raise MediaToolError(
            f"{path.name}: size ({len(data)} bytes) isn't a multiple "
            f"of the {_RECORD_SIZE}-byte record size - file may be "
            "truncated or not a .3gf file"
        )

    samples = []

    for offset in range(0, len(data), _RECORD_SIZE):
        milliseconds, x, y, z = struct.unpack_from(
            _RECORD_FORMAT, data, offset
        )
        samples.append(
            GSensorSample(
                offset=timedelta(milliseconds=milliseconds),
                x=x,
                y=y,
                z=z,
            )
        )

    return tuple(samples)


def write_gsensor(samples: tuple[GSensorSample, ...], path: Path) -> None:
    """Write samples back out in the same raw .3gf binary format.

    Used by bv-export to write a trip-level .3gf file whose sample
    offsets have been rebased from per-recording to per-trip (see
    blackvue.export.gsensor).
    """

    data = b"".join(
        struct.pack(
            _RECORD_FORMAT,
            round(sample.offset.total_seconds() * 1000),
            sample.x,
            sample.y,
            sample.z,
        )
        for sample in samples
    )

    path.write_bytes(data)
