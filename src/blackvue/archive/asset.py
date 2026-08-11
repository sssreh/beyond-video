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
    # in bv-export/--stitch processes interior video yet.
    INTERIOR = ("Interior",)

    GPS = ("GPS",)
    GSENSOR = ("3G",)

    FRONT_THUMBNAIL = ("Front_Thm",)
    REAR_THUMBNAIL = ("Rear_Thm",)
    INTERIOR_THUMBNAIL = ("Interior_Thm",)

    # Generated assets

    AUDIO = ("Audio",)
    DURATION = ("Dur",)
    GPX = ("GPX",)

    TRANSCRIPT = ("Plain", "Transcript")
    TRANSCRIPT_DIARIZED = ("Diar", "Transcript")
    TRANSLATION = ("Plain", "Translate")
    TRANSLATION_DIARIZED = ("Diar", "Translate")
    SUBTITLES = ("SRT",)
    LYRICS = ("LRC",)
    SUMMARY = ("Summ",)
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
    