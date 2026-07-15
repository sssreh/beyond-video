#!/usr/bin/env python3

from blackvue import BlackVue

from config import CAMERA_IP


def main():

    camera = BlackVue(CAMERA_IP)

    camera.connect()

    recordings = camera.recordings()

    print(f"Recordings : {len(recordings)}")

    if recordings:

        print(f"First : {recordings[0]}")
        print(f"Last  : {recordings[-1]}")

    camera.disconnect()


if __name__ == "__main__":
    main()
    