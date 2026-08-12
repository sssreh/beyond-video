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

    TRANSCRIPT = ("Plain", "Transcript")
    TRANSCRIPT_DIARIZED = ("Diar", "Transcript")
    TRANSLATION = ("Plain", "Translate")
    TRANSLATION_DIARIZED = ("Diar", "Translate")
    SUBTITLES = ("SRT",)
    LYRICS = ("LRC",)
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
    