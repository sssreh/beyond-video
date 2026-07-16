#!/usr/bin/env python3

"""
List recordings available on the camera.
"""

from blackvue import BlackVueCamera
from blackvue import BlackVueClient

from config import CAMERA_IP


def main() -> None:

    client = BlackVueClient(f"http://{CAMERA_IP}")
    camera = BlackVueCamera(client)

    recordings = camera.recordings()

    print(f"{len(recordings)} recording(s)\n")

    for recording in recordings:

        print(recording.id)

        if recording.front is not None:
            print(f"  Front : {recording.front.path}")

        if recording.rear is not None:
            print(f"  Rear  : {recording.rear.path}")

        print()


if __name__ == "__main__":
    main()
    