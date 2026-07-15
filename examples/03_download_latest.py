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

    camera.disconnect()


if __name__ == "__main__":
    main()
    