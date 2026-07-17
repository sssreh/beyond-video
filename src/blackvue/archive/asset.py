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
    GPX = ("GPX",)

    TRANSCRIPT = ("Transcript",)
    TRANSLATION = ("Translation",)
    SUMMARY = ("Summary",)

    def __init__(self, label: str):
        self._label = label

    @property
    def label(self) -> str:
        """Return the display label."""
        return self._label

    @classmethod
    def display_order(cls) -> tuple["Asset", ...]:
        """Return assets in display order."""
        return tuple(cls)
    