#!/usr/bin/env python3

from blackvue import BlackVue

from config import CAMERA_IP


def main():

    camera = BlackVue(CAMERA_IP)

    camera.connect()

    recordings = camera.recordings()

    latest = recordings[-1]

    print(f"Downloading {latest}")

    camera.download(latest)

    print()
    print("Interrupt the download and run this example again.")

    camera.disconnect()


if __name__ == "__main__":
    main()
    