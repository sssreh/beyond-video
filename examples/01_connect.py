#!/usr/bin/env python3

from blackvue import BlackVue

from config import CAMERA_IP


def main():

    print("Connecting...")

    camera = BlackVue(CAMERA_IP)

    camera.connect()

    print("Connected.")

    camera.disconnect()

    print("Disconnected.")


if __name__ == "__main__":
    main()
