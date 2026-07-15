#!/usr/bin/env python3

from blackvue import BlackVue

from config import CAMERA_IP


def main():

    camera = BlackVue(CAMERA_IP)

    camera.connect()

    response = camera.cgi("/blackvue_vod.cgi")

    print(response)

    camera.disconnect()


if __name__ == "__main__":
    main()
    