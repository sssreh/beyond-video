#!/usr/bin/env python3

"""
Verify communication with a BlackVue camera.
"""

from blackvue import BlackVueCamera
from blackvue import BlackVueClient

from config import CAMERA_IP


def main() -> None:

    print("Creating client...")

    client = BlackVueClient(f"http://{CAMERA_IP}")

    print("Creating camera...")

    camera = BlackVueCamera(client)

    print("Fetching recordings...")

    recordings = camera.recordings()

    print(f"Connected successfully ({len(recordings)} recordings).")


if __name__ == "__main__":
    main()
    