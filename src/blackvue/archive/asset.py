"""
BlackVue archive assets.
"""

from enum import Enum


class Asset(Enum):
    """An asset belonging to a recording."""

    # Downloaded from the camera

    FRONT = ("Front",)
    REAR = ("Rear",)
    # Interior (cabin-facing) camera - seen on some BlackVue models
    # alongside front/rear. Recognition/listing only for now; nothing
    # in bv-export/--stitch processes interior video yet. Shortened to
    # "Int" (from "Interior") along with the three *_THUMBNAIL labels
    # below - these four were bv-ls's widest columns by far (8-12
    # chars vs. everything else's 3-5), the main reason the table ran
    # so wide (see WORKING_CONTEXT.md).
    INTERIOR = ("Int",)

    GPS = ("GPS",)
    GSENSOR = ("3G",)

    FRONT_THUMBNAIL = ("FThm",)
    REAR_THUMBNAIL = ("RThm",)
    INTERIOR_THUMBNAIL = ("IThm",)

    # Generated assets

    AUDIO = ("Aud",)
    DURATION = ("Dur",)
    # A generated frame-grab thumbnail, permanently written next to a
    # recording's other generated assets (see cli/bv_generate.py's
    # --thumbnail action) - the FolderAdapter/GoProAdapter equivalent
    # of a BlackVue camera's own downloaded FRONT_THUMBNAIL .thm file,
    # for adapters whose manifest declares "thumbnails": "generated"
    # (no camera-native sidecar exists to download in the first
    # place). web/archive_browser.py's ArchiveRecording.thumbnail_path()
    # checks this before falling back to generating one itself on the
    # spot.
    THUMBNAIL = ("Thm",)

    # Per-recording computed statistics (distance, speed, g-force,
    # driver guess, ...) written by bv-generate --stats as a single
    # <id>.stats.json file - see WORKING_CONTEXT.md's "bv-web
    # statistics dashboard + per-recording stats asset" note for the
    # schema and the read-merge-write persistence design. Deliberately
    # one JSON file with many fields rather than one Asset per field,
    # unlike everything else in this enum - the fields are cheap to
    # (re)compute together and are always consumed together too.
    RECORDING_STATS = ("Stats",)

    TRANSCRIPT = ("Plain", "Transcript")
    TRANSCRIPT_DIARIZED = ("Diar", "Transcript")
    TRANSLATION = ("Plain", "Translate")
    TRANSLATION_DIARIZED = ("Diar", "Translate")
    SUBTITLES = ("SRT",)
    # Grouped like TRANSCRIPT/TRANSCRIPT_DIARIZED above - "Front"/"Rear"
    # distinguish which camera a scene description came from, "Scene"
    # is the shared two-row bv-ls header group.
    SCENE_DESCRIPTION = ("Front", "Scene")
    SCENE_DESCRIPTION_REAR = ("Rear", "Scene")

    def __init__(self, label: str, group: str | None = None):
        self._label = label
        self._group = group

    @property
    def label(self) -> str:
        """Return the display label."""
        return self._label

    @property
    def group(self) -> str | None:
        """Return the group label this asset's column is shown under
        in bv-ls's two-row header, or None if it has no group.
        """
        return self._group

    @classmethod
    def display_order(cls) -> tuple["Asset", ...]:
        """Return assets in display order."""
        return tuple(cls)

    @property
    def is_downloaded(self) -> bool:
        """True if this asset comes straight off the camera (or a
        recursive-scan adapter's own source folder), False if it's
        something bv-generate/bv-scribe/bv-export derived from that
        source data afterwards.

        Mirrors the "Downloaded from the camera" / "Generated assets"
        comment split above this enum's members - kept as an explicit
        property rather than callers re-deriving the same list, since
        getting this wrong has a real consequence: a caller that wants
        to know whether a recording was actually captured (not just
        that *some* file happens to exist for its id) needs to ignore
        generated assets, which can (and for Parking-mode recordings,
        normally do) exist without their source video ever having been
        downloaded at all - see bv_drivers.py's Parking-ending trip
        filter (Christer: "bara nedladdade P assets raknas, inte
        genererade" - only downloaded P assets count, not generated
        ones)."""
        return self in _DOWNLOADED_ASSETS


_DOWNLOADED_ASSETS = frozenset(
    {
        Asset.FRONT,
        Asset.REAR,
        Asset.INTERIOR,
        Asset.GPS,
        Asset.GSENSOR,
        Asset.FRONT_THUMBNAIL,
        Asset.REAR_THUMBNAIL,
        Asset.INTERIOR_THUMBNAIL,
    }
)
