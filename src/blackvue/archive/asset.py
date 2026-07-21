"""
BlackVue archive assets.
"""

from enum import Enum


class Asset(Enum):
    """An asset belonging to a recording."""

    # Downloaded from the camera

    FRONT = ("Front",)
    REAR = ("Rear",)

    GPS = ("GPS",)
    GSENSOR = ("3G",)

    FRONT_THUMBNAIL = ("Front_Thm",)
    REAR_THUMBNAIL = ("Rear_Thm",)

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
    