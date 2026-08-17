# Contributing

beyond-video is a personal, single-maintainer project - a toolkit for one person's own BlackVue dashcam, built and tested against the DR900S-2CH I actually own (one BlackVue Elite 10 firmware build has also been analyzed offline, without physical hardware). It isn't run as a community project with a roadmap or a process for taking on large pull requests, and I'd rather be upfront about that than imply otherwise.

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

## The beginning

This project started with a question: how can I replace my bash scripts that download my dashcam videos to my Synology NAS - running since early 2019 - with something better?

### The early collaboration with Charlie

Before Beyond Video became the application it is today, I worked with **ChatGPT ("Charlie")** on the basic architecture and some of the fundamental ideas behind the project.

Charlie was particularly useful during the early design phase. We spent a lot of time discussing how the archive should be represented, how recordings, recording IDs and assets should relate to each other, and how to keep the filesystem-based design simple without introducing unnecessary abstractions or a database.

The guiding question was often:

> **Can this be made simpler?**

That way of thinking had a significant influence on the architecture.

#### The lexical timestamp

The most important idea to come out of our work together was the **lexical timestamp**.

I had a problem with searching for recordings using incomplete timestamps. The obvious approach would have been to interpret them as dates and deal with calendars, months, different numbers of days, leap years, and so on.

Instead, we arrived at a much simpler idea:

**Treat timestamps as lexical values.**

A partial timestamp simply defines a lexical interval.

The lower bound is completed with `0`s and the upper bound with `9`s.

For example:

```text
2025
    ↓
20250000_000000
20259999_999999
```

and:

```text
20250630_154
    ↓
20250630_154000
20250630_154999
```

The particularly nice part was realizing that the lexical representation does not have to be a valid calendar date.

For example:

```text
20250639
```

is perfectly valid as a **search boundary**, even though June obviously does not have 39 days.

That meant the search mechanism did not need to know anything about calendars at all. It could simply compare strings.

This idea became a major part of Beyond Video and is still used throughout the project with little or no change.

#### Lexical time and human time

From this came another useful separation.

**Lexical time is for the computer.
Human time is for the user.**

The archive and search mechanisms could therefore remain purely lexical, while human-friendly formatting could be handled separately.

This eventually led to the creation of:

```text
humantimeformatter.py
```

I asked Charlie for the complete implementation, added it to the project, and that was the last Beyond Video file we actually created together.

#### `bv-ls` and archive searching

We also worked extensively on `bv-ls` and archive traversal.

We looked at `os.scandir()`, filename comparisons, timestamp filtering and the performance of searching large directories.

We compared the Python implementation with Unix tools such as `ls` and `awk`, and measured the actual execution time rather than optimizing based on assumptions.

One useful conclusion was that `os.scandir()` itself was already very fast, and that the roughly two-second execution time of `bv-ls` on the archive was acceptable.

#### The human timestamp formatter

We also discussed how the human formatter should deal with deliberately permissive lexical timestamps.

For the upper human representation, fields are constrained to their meaningful human limits:

```text
Month       > 12 → 12
Day         > 31 → 31
Hour        > 23 → 23
Minute      > 59 → 59
Second      > 59 → 59
```

This allowed the lexical search language to remain extremely simple while the human-facing representation remained sensible.

### What happened afterwards

After the basic architecture and lexical timestamp work had been established, I got tired of the workflow with Charlie and continued development with **Claude ("Klåd")**, who turned out to be far more implementation-oriented.

That collaboration is still going, and it's the one that actually built the application. Essentially everything in this repository beyond the early architecture and the lexical timestamp - `bv-ls`, `bv-generate`, `bv-gps`, `bv-live`, `bv-download`, `bv-scribe`, `bv-search`, `bv-lock`, `bv-config`, `bv-export`, `bv-web`, the plugin/adapter architecture that lets other cameras (GoPro, plain folders) plug in, trip detection and export, stitched video, GPS maps with track-up rotation and flyover intros, g-sensor visualizations, subtitles, the HEVC browser preview, light and dark themes, and the long tail of bug fixes and refinements documented commit by commit in `WORKING_CONTEXT.md` - was built together with Klåd.

### My thanks

I want to acknowledge **ChatGPT ("Charlie")** for its contribution to the **early architecture and conceptual development of Beyond Video**.

The most important contribution was helping me arrive at the lexical timestamp concept and then following that idea through its consequences for searching, recording IDs, archive handling and human-readable formatting.

Charlie was particularly good as a sounding board for architectural ideas:

> *"Can this be simpler?"*

That question turned out to be useful throughout the project.

I also want to acknowledge **Claude ("Klåd")**, who has been the one actually building Beyond Video since I moved on from Charlie. Nearly everything a user of this project touches today - every `bv-*` command beyond the earliest ones, the web interface, the export pipeline, the camera-adapter architecture - came out of that ongoing collaboration, one task and one commit at a time.

The implementation and the finished Beyond Video application are mine, but neither Charlie's early architectural discussions nor Klåd's sustained implementation work should go unacknowledged - both were an important part of how the project began, and how it kept going.

### In retrospect

If Beyond Video is a journey, **Charlie** helped me with some of the **initial map-making**.

**Klåd** has been building the road and driving alongside me for nearly the whole trip since.

I drove the journey.

But the map helped determine which road we took, and the road wouldn't exist without the one who helped build it.

## License

beyond-video is licensed under GPL-3.0 (see `LICENSE`). Contributions are accepted under the same license.
