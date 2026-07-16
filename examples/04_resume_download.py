#!/usr/bin/env python3

"""
Resume downloading a specific recording.

This example uses a fixed recording ID so it can be run repeatedly while
testing resume support.
"""

from pathlib import Path

from blackvue import BlackVueCamera
from blackvue import BlackVueClient

from config import CAMERA_IP
from config import DOWNLOAD_DIRECTORY


RECORDING_ID = "20260715_143340_N"


def main() -> None:

    client = BlackVueClient(f"http://{CAMERA_IP}")
    camera = BlackVueCamera(client)

    recordings = camera.recordings()

    recording = None

    for candidate in recordings:
        if candidate.id == RECORDING_ID:
            recording = candidate
            break

    if recording is None:
        print(f"Recording not found: {RECORDING_ID}")
        return

    destination = Path(DOWNLOAD_DIRECTORY)

    print(f"Recording   : {recording.id}")
    print(f"Destination : {destination}")
    print()

    changed = camera.download(
        recording=recording,
        destination=destination,
    )

    if changed:
        print("Download completed or resumed.")
    else:
        print("Recording already up to date.")

    print()
    print("Files:")

    for entry in recording.entries:
        print(f"  {entry.path.name}")


if __name__ == "__main__":
    main()
    