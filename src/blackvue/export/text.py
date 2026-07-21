"""
Text-asset merging for bv-export (transcripts/translations).

Copyright (C) 2026 Christer R. (sssreh)

SPDX-License-Identifier: GPL-3.0-or-later
"""

from __future__ import annotations

from ..archive.asset import Asset
from ..trip.trip import Trip


def merge_text_assets(trip: Trip, asset: Asset) -> str | None:
    """Return the trip's recordings' text for `asset`, concatenated in
    recording order, each block prefixed with a '# <recording_id>'
    header - or None if no recording in the trip has this asset.
    """

    blocks = []

    for recording in trip:
        asset_file = recording.file(asset)
        if asset_file is None:
            continue
        text = asset_file.path.read_text(encoding="utf-8").strip()
        blocks.append(f"# {recording.id}\n\n{text}")

    if not blocks:
        return None

    return "\n\n".join(blocks) + "\n"
