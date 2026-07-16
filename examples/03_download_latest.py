#!/usr/bin/env python3

"""
Download the latest recording from the camera.
"""

from pathlib import Path

from blackvue import BlackVueCamera
from blackvue import BlackVueClient

from config import CAMERA_IP
from config import DOWNLOAD_DIRECTORY


def main() -> None:

    client = BlackVueClient(f"http://{CAMERA_IP}")
    camera = BlackVueCamera(client)

    recordings = camera.recordings()

    if not recordings:
        print("No recordings found.")
        return

    recording = recordings[-1]

    destination = Path(DOWNLOAD_DIRECTORY)

    print(f"Latest recording : {recording.id}")
    print(f"Destination      : {destination}")
    print()

    changed = camera.download(
        recording=recording,
        destination=destination,
    )

    if changed:
        print("Download completed.")
    else:
        print("Recording already up to date.")

    print()
    print("Files:")

    for entry in recording.entries:
        print(f"  {entry.path.name}")


if __name__ == "__main__":
    main()
    