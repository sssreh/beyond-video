# Contributing

beyond-video is a personal, single-maintainer project - a toolkit for one person's own BlackVue dashcam, built and tested against the cameras I actually own (a DR900S-2CH, plus one BlackVue Elite 10 firmware build analyzed offline). It isn't run as a community project with a roadmap or a process for taking on large pull requests, and I'd rather be upfront about that than imply otherwise.

That said, there's one specific way outside help is genuinely useful right now: **camera compatibility**.

## Help find endpoints for your BlackVue camera model

Every `bv-*` command that talks to a camera live (`bv-gps`, `bv-live`, and the download step in `bv-download`) works by calling a handful of CGI endpoints the camera's own web server exposes on your local network - things like `blackvue_vod.cgi` for the recording list, `blackvue_live.cgi` for the live video feed, `blackvue_livedata.cgi` for live GPS/g-sensor data. Different BlackVue models and firmware versions expose different subsets of these, sometimes under different paths entirely (the Elite 10, for example, has a few endpoints the DR900S-2CH doesn't, and vice versa - see `WORKING_CONTEXT.md`'s firmware-analysis entries for what's been found so far).

If you own a BlackVue camera model that isn't a DR900S-2CH - including any of the models already listed as "confirmed via scan" in `README.md`'s Camera compatibility section, since none of those have had the full `bv-download`/`bv-export`/`bv-live` pipeline run against them yet either - running a quick scan against your own camera and reporting back what you find is one of the most useful things you could do for this project - no firmware access, reverse engineering, or coding required.

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

**What happens after your report lands:** I only own a DR900S-2CH myself (plus one Elite 10 firmware build analyzed offline, no physical unit), so for any other model I can't run the full download/export pipeline against real hardware before merging anything. What I *can* do, and have already done once for real (the Elite 10 - see `WORKING_CONTEXT.md`'s firmware-analysis entries): compare your scan output against the endpoints beyond-video already knows about, and if your model exposes the same ones (even under different paths), add the necessary endpoint handling from the scan alone. That gets your model marked "confirmed via scan, not full pipeline" in `README.md`'s compatibility table - real, working support for the parts a scan can verify (recording listing, live view, live GPS/g-sensor), without me having to trust it blind or make you iterate on test patches for something I can't personally validate either way. If your camera turns out to need something a scan alone can't answer (a genuinely different response format, say), I'll ask follow-up questions on the issue rather than guess.

## Code contributions

If you want to go further than that - fixing a bug, adding support for an endpoint you found - a small, focused pull request is welcome, but please open an issue describing what you're planning first (use the "Feature request" template, or "Bug report" if you're fixing something broken). This is a personal project I actively use for my own camera, so I'm cautious about changes that could affect my own working setup; discussing the approach before you write the code saves both of us time if it turns out not to be the direction I'd want to take it.

If you do send a PR: the project has a real test suite under `tests/` (`pytest`), and `ruff`/`mypy` are listed in `requirements-dev.txt` - matching the existing code's style and adding tests for new behavior makes a PR much easier to review and merge. CI runs `pytest` on every PR (must pass) plus `ruff`/`mypy` (informational for now - the codebase hasn't been fully brought in line with either yet, so their output doesn't block a merge on its own).

## Response time

This is a hobby project maintained alongside a day job by one person - I'll get to issues and PRs when I can, but there's no SLA. A well-described report (see the templates above) is the single biggest thing that speeds that up, since I usually can't reproduce a camera-specific issue on my own hardware.

## License

beyond-video is licensed under GPL-3.0 (see `LICENSE`). Contributions are accepted under the same license.
