"""
List recordings in a BlackVue archive.
"""

from pathlib import Path

from blackvue.archive import Archive, Asset


_COLUMNS = (
    ("Front", Asset.FRONT),
    ("Rear", Asset.REAR),
    ("GPS", Asset.GPS),
    ("3G", Asset.GSENSOR),
    ("Front_Thm", Asset.FRONT_THUMBNAIL),
    ("Rear_Thm", Asset.REAR_THUMBNAIL),
    ("Audio", Asset.AUDIO),
    ("GPX", Asset.GPX),
    ("Transcript", Asset.TRANSCRIPT),
    ("Translate", Asset.TRANSLATION),
    ("Summary", Asset.SUMMARY),
)


def bv_ls(path: str | Path = ".") -> int:
    """List recordings."""

    archive = Archive(path)

    recording_width = max(
        len("Recording"),
        *(len(str(recording.id)) for recording in archive),
    )

    column_widths = [
        len(header)
        for header, _ in _COLUMNS
    ]

    #
    # Header
    #

    print(f'{"Recording":<{recording_width}}', end="  ")

    for (header, _), width in zip(_COLUMNS, column_widths):
        print(f"{header:<{width}}", end=" ")

    print()

    total_width = (
        recording_width
        + 2
        + sum(column_widths)
        + len(column_widths)
    )

    print("-" * total_width)

    #
    # Rows
    #

    for recording in archive:

        print(f"{recording.id:<{recording_width}}", end="  ")

        for (_, asset), width in zip(_COLUMNS, column_widths):

            mark = "✔" if recording.has(asset) else ""

            # Center the mark within the column.
            print(f"{mark:^{width}}", end=" ")

        print()

    return 0
