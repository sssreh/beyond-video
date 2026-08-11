# Beyond Video

A personal, command-line toolkit for a BlackVue dashcam: download recordings, transcribe/translate them, detect trips and export each one into its own folder (video, GPX track, route map, g-sensor overlay), plus a live browser dashboard for watching the camera in real time. Built for one person's own camera, run from their own machine - no accounts, no cloud, no telemetry beyond what your own camera sends you.

The project was inspired by a BlackVue DR900S installed in a vehicle named Kirby and a Bash script written to automatically synchronize event recordings to a Synology NAS.

## What it does

| Command | Does |
|---|---|
| `bv-config` | Create or edit a camera's configuration (endpoints, archive location). |
| `bv-download` | Download recordings from a camera into a local archive. |
| `bv-ls` | List and inspect recordings in an archive. |
| `bv-generate` | Generate derived assets per recording: audio, real duration, transcript, translation, subtitles, scene description. |
| `bv-lang` | Manage the offline translation packages `bv-generate --translate` uses. |
| `bv-export` | Detect trips in an archive and assemble each one into its own folder - video, GPX, map overlay, g-sensor overlay, a combined "stitch" video. |
| `bv-scribe` | Describe what's happening in a recording (or raw video file) using a vision-language model - scene description and/or on-screen text (signs, plates) via OCR. |
| `bv-search` | Search an archive by text (transcript/translation/scene description) and/or GPS proximity to a point or place name. |
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
pip install -e ".[scene]"       # bv-generate --describe-scene, bv-scribe
```

**If you have an NVIDIA GPU and want `[scene]` to actually use it**, install `torch`/`torchvision` from PyTorch's CUDA wheel index *before* the command above, not after - a plain `pip install -e ".[scene]"` pulls torch from PyPI's default index, which is a CPU-only build. This matters most on recent GPUs (e.g. RTX 50-series/Blackwell), since older CUDA wheel builds don't support them at all and silently fall back to CPU rather than erroring:

```
pip install torch torchvision --index-url https://download.pytorch.org/whl/cu128
pip install -e ".[scene]"
```

Check `https://pytorch.org/get-started/locally/` for the current recommended `cuXXX` index tag for your GPU/driver, then verify it worked before running anything scene-related:

```
python -c "import torch; print(torch.__version__, torch.cuda.is_available())"
```

Should print `True`. An 8B-parameter vision-language model on CPU is extremely slow (can look "stuck" on the very first recording for many minutes) - if `bv-scribe`/`bv-generate --describe-scene` seems hung with no output, this is the first thing to check.

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

**Tested (full pipeline):** a BlackVue DR900S-2CH - download, live view, and export all run for real against actual hardware.

**Confirmed via scan (endpoints verified live, not the full pipeline):** an Elite 10, plus DR750X-2CH, DR750X-3CH Plus, DR750X-2CH LTE Plus, DR770X, DR770X-BOX-PRO, DR900X-2CH, DR900X-2CH PLUS, DR970X-2CH LTE, and DR970X-2CH LTE Plus. Each model's core CGI endpoints (recording listing, live front/rear view, live GPS/g-sensor data, config) have been confirmed live and working via `scan_blackvue_endpoints.py`, but `bv-download`/`bv-export`/`bv-live` haven't been run against real hardware for any of them yet - see `CONTRIBUTING.md` for what that distinction means in practice.

If you own a BlackVue model not listed above, running the scan script against your own camera and reporting what it finds is a direct, concrete way to help extend support.

## Deployment

Both the CLI pipeline and `bv-web` can run on a single machine, or be split across a NAS (always-on, `bv-web` + optionally the pipeline) and a PC (the faster path for `bv-generate`/`bv-export`'s heavier steps). See `docs/DEPLOY.md` for the concrete setup, including Docker images for both.

## License

Copyright (C) 2026 Christer R. (sssreh).

This project is licensed under the GNU General Public License v3.0 or later (GPL-3.0-or-later). See the `LICENSE` file for details.
