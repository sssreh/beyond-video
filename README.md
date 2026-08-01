# Beyond Video

A personal, command-line toolkit for a BlackVue dashcam: download recordings, transcribe/translate them, detect trips and export each one into its own folder (video, GPX track, route map, g-sensor overlay), plus a live browser dashboard for watching the camera in real time. Built for one person's own camera, run from their own machine - no accounts, no cloud, no telemetry beyond what your own camera sends you.

The project was inspired by a BlackVue DR900S installed in a vehicle named Kirby and a Bash script written to automatically synchronize event recordings to a Synology NAS.

## What it does

| Command | Does |
|---|---|
| `bv-config` | Create or edit a camera's configuration (endpoints, archive location). |
| `bv-download` | Download recordings from a camera into a local archive. |
| `bv-ls` | List and inspect recordings in an archive. |
| `bv-generate` | Generate derived assets per recording: audio, real duration, transcript, translation, subtitles. |
| `bv-lang` | Manage the offline translation packages `bv-generate --translate` uses. |
| `bv-export` | Detect trips in an archive and assemble each one into its own folder - video, GPX, map overlay, g-sensor overlay, a combined "stitch" video. |
| `bv-gps` | Fetch a camera's current GPS reading live, one-shot. |
| `bv-live` | Serve a live browser dashboard for a camera: live video, a scrolling map, a scrolling g-sensor strip. |
| `bv-web` | A small multi-user web app for browsing trips `bv-export` has already produced (a separate side project - see `docs/WEB_ARCHITECTURE.md`). |

Full reference for every command and flag lives under `docs/man/`. `docs/PIPELINE.md` walks through the pipeline stage by stage with example commands; `docs/ARCHITECTURE.md` is the helicopter view of how the pieces fit together.

## Install

Requires Python 3.13+ and [ffmpeg](https://ffmpeg.org/) (used for concatenation, transcoding, and rendering; must be on `PATH`).

```
git clone https://github.com/sssreh/beyond-video.git
cd beyond-video
pip install -e .
```

That covers `bv-config`/`bv-download`/`bv-ls`/`bv-export`/`bv-gps` and `bv-generate`'s non-transcribe work. A few commands need optional extras:

```
pip install -e ".[speech]"      # bv-generate --transcribe/--diarize
pip install -e ".[translate]"   # bv-generate --translate, bv-lang
pip install -e ".[web]"         # bv-web and bv-live
```

## Quick start

```
bv-config Kirby                              # interactive wizard: name, endpoints, archive location
bv-download Kirby                            # downloads into the archive directory you set above
bv-ls ~/blackvue-archive                     # list what's in the archive
bv-export ~/blackvue-archive --target ~/trips  # detect trips, export each one into its own folder
```

`bv-config` is fully interactive - it prompts you for the camera's name, endpoints, and archive location rather than taking them as flags. `bv-download`/`bv-gps`/`bv-live` then refer to that camera by the ID you gave it; `bv-ls`/`bv-export` instead take the archive directory itself as their argument (default: current directory).

See `docs/PIPELINE.md` for a fuller walkthrough, including transcription/translation and the map/g-sensor/stitch export options.

## Camera compatibility

**Tested:** a BlackVue DR900S-2CH, plus one Elite 10 firmware build analyzed offline.

**Likely to work, untested:** BlackVue750, BlackVue750X3Plus, BlackVue750XLTEPlus, BlackVue770X, BlackVue770XBoxP, BlackVue900X, BlackVue900XPlus, BlackVue970XLTE, BlackVue970XLTEP. Nobody's confirmed these against real hardware yet - if you own one, see `CONTRIBUTING.md` for how to check and report back.

If you own a different BlackVue model entirely (not in either list above), the same applies - running the scan script against your own camera and reporting what it finds is a direct, concrete way to help extend support.

## Deployment

Both the CLI pipeline and `bv-web` can run on a single machine, or be split across a NAS (always-on, `bv-web` + optionally the pipeline) and a PC (the faster path for `bv-generate`/`bv-export`'s heavier steps). See `docs/DEPLOY.md` for the concrete setup, including Docker images for both.

## License

Copyright (C) 2026 Christer R. (sssreh).

This project is licensed under the GNU General Public License v3.0 or later (GPL-3.0-or-later). See the `LICENSE` file for details.
