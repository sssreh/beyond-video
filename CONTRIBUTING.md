# Contributing

beyond-video is a personal, single-maintainer project - a toolkit for one person's own BlackVue dashcam, built and tested against the cameras I actually own (a DR900S-2CH, plus one BlackVue Elite 10 firmware build analyzed offline). It isn't run as a community project with a roadmap or a process for taking on large pull requests, and I'd rather be upfront about that than imply otherwise.

That said, there's one specific way outside help is genuinely useful right now: **camera compatibility**.

## Help find endpoints for your BlackVue camera model

Every `bv-*` command that talks to a camera live (`bv-gps`, `bv-live`, and the download step in `bv-download`) works by calling a handful of CGI endpoints the camera's own web server exposes on your local network - things like `blackvue_vod.cgi` for the recording list, `blackvue_live.cgi` for the live video feed, `blackvue_livedata.cgi` for live GPS/g-sensor data. Different BlackVue models and firmware versions expose different subsets of these, sometimes under different paths entirely (the Elite 10, for example, has a few endpoints the DR900S-2CH doesn't, and vice versa - see `WORKING_CONTEXT.md`'s firmware-analysis entries for what's been found so far).

If you own a BlackVue camera model that isn't a DR900S-2CH - including any of the models listed as "likely to work, untested" in `README.md`'s Camera compatibility section - running a quick scan against your own camera and reporting back what you find is one of the most useful things you could do for this project - no firmware access, reverse engineering, or coding required.

**How:**

1. Connect to your camera's WiFi (or find its IP on your home network - check your router's device list, or your BlackVue app's connection settings).
2. Run the scan script against it:
   ```
   python scripts/scan_blackvue_endpoints.py <camera-ip>
   ```
   Read the script's own docstring first if you want the details, but the short version: it only ever does read-only GET/HEAD requests against the IP you give it, never writes/deletes/uploads anything, and safely handles the endpoints that stream continuously (it reads a short prefix and closes the connection itself rather than hanging).
3. Open a [GitHub issue](https://github.com/sssreh/beyond-video/issues/new/choose) using the "Camera endpoint scan" template, and paste in the script's output along with your camera's exact model name and firmware version (both are in the camera's own settings menu or the BlackVue app).

That's it. I'll take it from there to figure out what beyond-video needs to support your model.

One privacy note before you post: the recording-listing endpoint's response includes your own recording filenames, which BlackVue's own naming convention encodes with the recording's date and time. Skim the output before pasting it into a public issue if that's not something you want to share.

## Code contributions

If you want to go further than that - fixing a bug, adding support for an endpoint you found - a small, focused pull request is welcome, but please open an issue describing what you're planning first. This is a personal project I actively use for my own camera, so I'm cautious about changes that could affect my own working setup; discussing the approach before you write the code saves both of us time if it turns out not to be the direction I'd want to take it.

If you do send a PR: the project has a real test suite under `tests/` (`pytest`), and `ruff`/`mypy` are listed in `requirements-dev.txt` - matching the existing code's style and adding tests for new behavior makes a PR much easier to review and merge.

## License

beyond-video is licensed under GPL-3.0 (see `LICENSE`). Contributions are accepted under the same license.
