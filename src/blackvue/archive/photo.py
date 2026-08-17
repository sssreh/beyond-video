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

import json
import subprocess
from pathlib import Path

from .asset import Asset
from .recording import Recording

# Christer's own answer when asked which extensions should count:
# "All of them" - jpg/jpeg (GoPro's own still-photo format), png (some
# folder-adapter sources, e.g. phone/other-camera footage mixed into a
# folder archive), heic (iPhone-sourced stills), and gpr (GoPro's RAW
# photo format). Case-insensitive - see is_photo_path().
#
# .gif is deliberately NOT in here. Christer's own framing when asked
# ("how do you define a gif file, a picture or a silent video?"): the
# honest answer is "it depends on the gif" - an animated GIF is really
# a silent video (it already has its own real per-frame timing baked
# in, nothing to hold for a fixed --photo-duration), while a static,
# single-frame GIF is really a photo. Extension alone can't tell those
# two apart, so .gif gets its own extension set (GIF_EXTENSIONS,
# below) and its own frame-count check in recording_is_photo() instead
# of a blanket "always/never a photo" rule.
PHOTO_EXTENSIONS = frozenset(
    {".jpg", ".jpeg", ".png", ".heic", ".gpr"}
)

# Scanned in alongside PHOTO_EXTENSIONS (see _recursive_scan.py's own
# extension set) so both animated and static GIFs land under
# Asset.FRONT in the first place - which one a given file turns out to
# be is then decided per-file by recording_is_photo()/count_gif_frames()
# below, not by this extension set itself.
GIF_EXTENSIONS = frozenset({".gif"})

# How long a photo plays for in an exported trip when nothing more
# specific overrides it - Christer's own number ("a specified time
# that defaults to 5 second"). See bv-export's own --photo-duration
# flag for the configurable path; this is just the fallback when
# nothing sets it explicitly (bv-ls --trips, which has no CLI flag of
# its own for this, uses this constant directly).
DEFAULT_PHOTO_DURATION_SECONDS = 5


def is_photo_path(path: Path) -> bool:
    """True if `path`'s extension (case-insensitive) is one this
    project recognizes as an always-a-photo format - see
    PHOTO_EXTENSIONS. Deliberately False for `.gif`, even a static
    single-frame one - see recording_is_photo()'s own docstring for
    why that case needs an actual frame-count check, not just the
    extension."""

    return path.suffix.lower() in PHOTO_EXTENSIONS


def is_gif_path(path: Path) -> bool:
    """True if `path`'s extension (case-insensitive) is `.gif` -
    see GIF_EXTENSIONS."""

    return path.suffix.lower() in GIF_EXTENSIONS


def count_gif_frames(path: Path) -> int | None:
    """Return how many frames `path` (a `.gif`) actually has, via
    ffprobe's `nb_read_frames` - the only reliable way to tell a
    static, single-frame GIF (a photo, held for --photo-duration
    seconds like any other still image) apart from an animated one
    (an ordinary silent video, its own real per-frame timing already
    baked into the file) - see recording_is_photo()'s own docstring
    for the full animated-vs-static framing this exists to serve.
    Extension alone can't make that call: nothing about a `.gif`
    suffix says whether the file actually animates.

    Returns None (not 0 or 1) if ffprobe is missing, the file can't
    be probed at all, or the frame count isn't parseable - callers
    treat that the same as "assume it's an ordinary video, not a
    photo": an unreadable/corrupt file should fall through to the
    normal video pipeline's own established skip-on-failure handling
    (trip_export.py's corrupted-source-skip behavior) rather than be
    force-classified as a photo it was never actually confirmed to
    be.
    """

    try:
        result = subprocess.run(
            [
                "ffprobe", "-v", "error",
                "-select_streams", "v:0",
                "-count_frames",
                "-show_entries", "stream=nb_read_frames",
                "-of", "json",
                str(path),
            ],
            capture_output=True,
            text=True,
            check=True,
        )
    except (FileNotFoundError, subprocess.CalledProcessError):
        return None

    try:
        data = json.loads(result.stdout)
        return int(data["streams"][0]["nb_read_frames"])
    except (KeyError, IndexError, ValueError, json.JSONDecodeError):
        return None


def recording_is_photo(recording: Recording) -> bool:
    """True if `recording`'s FRONT asset is a still photo rather than
    a real video file. False for a recording with no FRONT asset at
    all (nothing to check) as well as for a genuine video.

    A `.gif` FRONT is the one case this can't decide from the
    extension alone: an animated GIF is really a silent video (see
    PHOTO_EXTENSIONS' own docstring for Christer's framing on this),
    so only a GIF that `count_gif_frames()` confirms has exactly one
    frame counts as a photo here. A GIF whose frame count can't be
    determined at all (ffprobe missing/failed) is treated as False -
    i.e. left to flow through the ordinary video pipeline, which
    already degrades gracefully on a genuinely unreadable source -
    rather than risk mis-classifying it as a photo it was never
    actually confirmed to be.
    """

    front = recording.file(Asset.FRONT)
    if front is None:
        return False
    if is_photo_path(front.path):
        return True
    if is_gif_path(front.path):
        return count_gif_frames(front.path) == 1
    return False
