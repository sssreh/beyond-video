"""
Photo-file recognition, shared across every layer that needs to tell
a still image apart from a real video once it's already been scanned
into a Recording.

Christer's own framing, verbatim, is the whole design here: "if I
want to play with words I would say a picture is also a video, but 1
frame only." Rather than giving a photo its own `Asset` type and
threading a parallel "is this a photo instead of a video" path
through every consumer (trip building, bv-ls, --describe-scene, the
export concat pipeline, ...), a photo scanned by a recursive-scan
adapter (folder/gopro - see adapters/_recursive_scan.py) is stored
under `Asset.FRONT` exactly like a real video. Every existing FRONT
consumer therefore already works unchanged; the only two places that
actually need to know "this FRONT file is a still image, not a real
video" are (1) duration - there's nothing to probe or cache, it's a
fixed, configurable span (see `generate.media.photo_aware_duration()`)
and (2) turning it into an actual video segment before it can be
spliced into a trip's concatenated front.mp4 (see
`export.media.render_image_as_video()` and
`export.trip_export._photo_clip_overrides()`). Both of those, and
bv-generate's audio/transcribe skip (there's no audio track to
extract from a JPEG), key off `is_photo_path()`/`recording_is_photo()`
below rather than a new Asset member.

Extension-based, not adapter-manifest-driven: photo support only ever
applies to the two adapters that share adapters/_recursive_scan.py
(folder, gopro) - BlackVue's own flat, filename-convention archive
never contains photos, and never routes through this module's
scanning side at all. Kept as a plain constant here instead of a new
manifest.json field so every consumer (bv-generate, the export
pipeline, the archive browser) can ask "is this recording's FRONT a
photo?" without needing to know which adapter/manifest produced the
Recording in the first place.
"""

from __future__ import annotations

from pathlib import Path

from .asset import Asset
from .recording import Recording

# Christer's own answer when asked which extensions should count:
# "All of them" - jpg/jpeg (GoPro's own still-photo format), png (some
# folder-adapter sources, e.g. phone/other-camera footage mixed into a
# folder archive), heic (iPhone-sourced stills), and gpr (GoPro's RAW
# photo format). Case-insensitive - see is_photo_path().
PHOTO_EXTENSIONS = frozenset(
    {".jpg", ".jpeg", ".png", ".heic", ".gpr"}
)

# How long a photo plays for in an exported trip when nothing more
# specific overrides it - Christer's own number ("a specified time
# that defaults to 5 second"). See bv-export's own --photo-duration
# flag for the configurable path; this is just the fallback when
# nothing sets it explicitly (bv-ls --trips, which has no CLI flag of
# its own for this, uses this constant directly).
DEFAULT_PHOTO_DURATION_SECONDS = 5


def is_photo_path(path: Path) -> bool:
    """True if `path`'s extension (case-insensitive) is one this
    project recognizes as a still photo rather than a video - see
    PHOTO_EXTENSIONS."""

    return path.suffix.lower() in PHOTO_EXTENSIONS


def recording_is_photo(recording: Recording) -> bool:
    """True if `recording`'s FRONT asset is a still photo rather than
    a real video file. False for a recording with no FRONT asset at
    all (nothing to check) as well as for a genuine video."""

    front = recording.file(Asset.FRONT)
    return front is not None and is_photo_path(front.path)
