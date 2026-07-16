"""
BlackVue archive assets.
"""

from enum import Enum


class Asset(Enum):
    """An asset belonging to a recording."""

    # Downloaded from the camera

    FRONT = "Front"
    REAR = "Rear"

    GPS = "GPS"
    GSENSOR = "3G"

    FRONT_THUMBNAIL = "Front_Thm"
    REAR_THUMBNAIL = "Rear_Thm"

    # Generated assets

    AUDIO = "Audio"
    GPX = "GPX"

    TRANSCRIPT = "Transcript"
    TRANSLATION = "Translation"
    SUMMARY = "Summary"

    @classmethod
    def display_order(cls) -> tuple["Asset", ...]:
        """Return assets in display order."""

        return (
            cls.FRONT,
            cls.REAR,
            cls.GPS,
            cls.GSENSOR,
            cls.FRONT_THUMBNAIL,
            cls.REAR_THUMBNAIL,
            cls.AUDIO,
            cls.GPX,
            cls.TRANSCRIPT,
            cls.TRANSLATION,
            cls.SUMMARY,
        )
