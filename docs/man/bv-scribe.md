# bv-scribe(1)

## NAME

`bv-scribe` - describe recordings' contents and read their on-screen text with a local vision-language model

## SYNOPSIS

```
bv-scribe [--raw]
          [--from TIMESTAMP] [--until TIMESTAMP] [--timestamp TIMESTAMP]
          [--camera {front,rear,both}]
          [--task {describe,ocr,both}] [--model MODEL]
          [--fps N] [--max-frames N] [--max-pixels N]
          [--resized-width N] [--resized-height N]
          [--crop-top FRAC] [--crop-bottom FRAC]
          [--max-new-tokens N] [--repetition-penalty N] [--no-repeat-ngram-size N]
          [--do-sample] [--temperature N] [--top-p N] [--top-k N]
          [--zoom-signs | --no-zoom-signs] [--zoom-frames N] [--zoom-detect-width N]
          [--zoom-padding N] [--zoom-ocr-width N] [--zoom-debug-dir DIR]
          [--zoom-max-new-tokens N] [--zoom-detect-max-new-tokens N]
          [--zoom-repetition-penalty N] [--zoom-no-repeat-ngram-size N]
          [--zoom-plate-confidence-check | --no-zoom-plate-confidence-check]
          [--cpu] [--quantize {auto,none,int8,int4}]
          [--gpu-memory-fraction FRACTION]
          [--config-dir DIR]
          [--overwrite] [--dry-run] [-v]
          [PATH]
```

## DESCRIPTION

`bv-scribe` is the batch-oriented, fully-tunable counterpart to `bv-generate --describe-scene` (see `bv-generate(1)`) - same underlying vision-language model and output (`<recording>.scene.txt`), but with the full set of tuning flags real-footage testing converged on. Selects recordings from a local archive the same way every other `bv-*` command does - by timestamp/`--from`/`--until`/`--timestamp` - rather than the raw file/folder arguments the original standalone scene-scribe prototype took.

**Parking-mode recordings are never considered.** Unlike `bv-generate --describe-scene` (which deliberately does run on them), `bv-scribe` excludes every Parking-mode (`P`) recording from its selection entirely - a dedicated batch run over a whole archive shouldn't spend GPU time on typically long, uneventful parking footage. A skip count is printed when any are excluded. Point `--raw` directly at a parking `.mp4` if you genuinely want one described - that mode has no recording-id-based filtering at all.

**One bad recording never stops the batch.** If describing a recording fails for any reason (a corrupted file, a network read error on a mounted archive, an out-of-VRAM error, ...), `bv-scribe` logs it, moves on to the next recording, and prints a summary of every failed recording at the end of the run - a multi-hour archive-scale run is never lost over one bad file.

Every run prints a `bv-scribe: started HH:MM:SS` line up front and a `bv-scribe: finished HH:MM:SS (N.Ns)` line on every exit path from there on (archive mode and `--raw` mode alike) - same pattern `bv-search(1)` uses. A large archive-scale batch can run for hours (CPU-only vision-language inference is especially slow - see the CUDA note below), so both when it started and how long it took are visible without checking output-file timestamps by hand afterward.

Requires the `scene` extra: `pip install .[scene]` (pulls in `transformers`, `accelerate`, `qwen-vl-utils`, and `torchvision`). Install torch separately first, per its own CUDA-build instructions for your GPU (a plain `pip install .[scene]` pulls in whatever torch resolves to from PyPI's default index, which is CPU-only, not necessarily the right CUDA build):

```
pip install torch torchvision --index-url https://download.pytorch.org/whl/cu128
pip install .[scene]
```

Check [PyTorch's "Get Started" page](https://pytorch.org/get-started/locally/) for the current recommended `cuXXX` index tag - it changes over time and by GPU generation (e.g. RTX 50-series/Blackwell needs a build new enough to support it at all; older CUDA builds don't error on an unsupported GPU, they just silently fall back to CPU). Verify with `python -c "import torch; print(torch.cuda.is_available())"` before running anything - it should print `True`. On CPU, an 8B-parameter vision-language model can look completely hung (no output at all) for many minutes on just the first recording, which is easy to mistake for a bug rather than expected CPU slowness.

**Trusting the output.** Real-footage testing surfaced two distinct failure modes: the model can confidently misread a license plate (report the wrong characters rather than flagging it illegible), and it can invent plausible-sounding but unrelated text on an ambiguous scene (a real Stockholm-area trip once got "Palm Jumeirah" - a Dubai landmark - as on-screen text, more than once, across separate runs). Every output file ends with a disclaimer to this effect - treat every read, especially plates/signs/place names, as unverified until checked against the source video. `--zoom-plate-confidence-check` (on by default) mitigates the plate case specifically: each detected plate crop is read twice (once greedy, once with sampling forced on), and reported as unverified rather than picked between if the two disagree.

`--raw` points `bv-scribe` at non-BlackVue footage instead of an archive - a video file or a directory of video files with no BlackVue filename/sidecar structure at all. See "Raw video mode" below.

**Trip-level narrative synthesis lives in `bv-export`, not here.** `bv-scribe` only ever writes one `.scene.txt` per recording - `bv-export --trip-summary` reads those files back and synthesizes the trip-level `trip_summary.txt` itself (see `bv-export(1)`). This used to be a `bv-scribe --trip-summary` pass, but Christer's call once the boundary problems that split showed up ("A trip summary should be created and placed in a trip folder, so it should be done by bv-export only") moved it to the command that actually owns trip folders.

## ARGUMENTS

| Argument | Description |
|---|---|
| `PATH` | Archive directory, or (with `--raw`) a raw video file or a directory of raw video files. Also accepts a camera system id (see `bv-config(1)`) - resolved to that camera's configured target directory; not used with `--raw`. A path that looks like a real path (starts with `./`/`.\`, is `.`/`..`, is absolute, or contains a path separator) is always used literally, so `./Kirby` forces a literal directory named `Kirby` even if a camera with that id also exists. Default: current directory. |

## OPTIONS

### Selection

| Option | Description |
|---|---|
| `--config-dir DIR` | Directory camera configs live in, for resolving `PATH` as a camera id. Default: the platform's standard config directory (same default as `bv-config(1)`). Not used with `--raw`. |
| `--raw` | Treat `PATH` as a raw video file or a directory of raw video files instead of a BlackVue archive. See "Raw video mode" below. |
| `--from TIMESTAMP` | Only consider recordings from this timestamp. Not used with `--raw`. |
| `--until TIMESTAMP` | Only consider recordings up to this timestamp. Not used with `--raw`. |
| `--timestamp TIMESTAMP` | Only consider recordings matching this timestamp or prefix. Not used with `--raw`. |
| `--camera {front,rear,both}` | Which camera(s) to process (default: `front` - front video, or rear as fallback if there's no front). `rear` processes only the rear video with the normal full `--task` treatment, saved as `<recording>.rear.scene.txt` - a deliberate choice to look at the rear camera gets full treatment, not just plates. `both` adds a cheap OCR-only bonus pass on the rear video alongside the normal front pass, skipped with a note if the recording has no distinct rear video - a full rear-camera description would mostly just restate the front one's. Not used with `--raw` (raw video files have no front/rear distinction). |

### Task / model

| Option | Description |
|---|---|
| `--task {describe,ocr,both}` | `describe` for what's happening, `ocr` for on-screen text only, `both` for a single combined pass. Default: `both`. |
| `--model MODEL` | Hugging Face model id. Default: `Qwen/Qwen3-VL-8B-Instruct` (~16GB download on first use, cached under `~/.cache/huggingface`; requires `transformers>=4.57.0`, already the `scene` extra's own floor). A smaller or quantized (`-AWQ`) variant trades accuracy for speed/VRAM - `Qwen/Qwen2.5-VL-7B-Instruct` (this feature's original default, `transformers>=4.49.0`) remains a solid fallback if you want the more real-footage-tested option instead. |
| `--cpu` | Force CPU inference. Extremely slow for a 7B+ video model - mainly useful to confirm the pipeline runs at all without a working CUDA setup. |
| `--quantize {auto,none,int8,int4}` | Loading precision for `--model`, not a different model - `auto` (default) picks based on the largest single GPU on this machine: `none` (full precision) at 20GB+ VRAM, `int8` at 10-20GB, `int4` below that; a machine with no CUDA GPU (or `--cpu`) always resolves to `none`. Sized off the largest single card, not total VRAM across cards, so e.g. two 12GB GPUs resolve to `int8` (fits on one card) rather than `none` (which would shard the model across both via `device_map="auto"`, a slower PCIe-pipelined load). Explicit `none`/`int8`/`int4` overrides the auto-detection; combining an explicit non-`none` level with `--cpu` is an error. Needs the `bitsandbytes` package (part of the `scene` extra). |
| `--gpu-memory-fraction FRACTION` | Caps this process's CUDA allocations to this fraction (greater than 0, at most `1.0`) of each visible GPU's total VRAM, via `torch.cuda.set_per_process_memory_fraction()`. Not set by default (no cap - the model claims whatever VRAM it wants, e.g. ~19.3GB of a 24GB card for unquantized `Qwen3-VL-8B-Instruct`). Useful for guaranteeing some VRAM stays free for something else running on the same GPU at the same time, instead of hoping the driver is polite about it - there's no true per-process GPU *compute* priority knob on consumer NVIDIA/Windows drivers (that's an MPS priority-queue feature, data-center/Linux only), so this memory cap is the practical lever. CUDA-only, same as `--quantize`; combining it with `--cpu` is an error. |

### Frame sampling / resolution

| Option | Description |
|---|---|
| `--fps N` | Frames per second of video to sample. Default: `1.0`. |
| `--max-frames N` | Hard cap on sampled frames regardless of `--fps`. Default: `16`. Briefly went to `64` then `32` on 2026-08-19 via a `--video-total-pixels` budgeting scheme meant to trade resolution for frame count, but Christer asked to go back to `16` outright rather than keep that added complexity - see `generate/scene.py`'s `SceneOptions` docstring for the full history. Lower this first if generation is too slow. |
| `--max-pixels N` | Resolution cap per sampled frame, in total pixels. Default: `151200` (~420x360). Only used when `--resized-width`/`--resized-height` are both `0`. |
| `--resized-width N` | Force an exact frame width, bypassing `--max-pixels`. Default: `1092`. Pass `0` (with `--resized-height 0`) to fall back to `--max-pixels` instead. |
| `--resized-height N` | Force an exact frame height. Default: `588`. See `--resized-width`. |
| `--crop-top FRAC` | Fraction of frame height to crop off the top before the model sees it, to cut out BlackVue's burned-in overlay text (timestamp/speed/camera name). Default: `0.0378`, or `0` (disabled) with `--raw` - see "Raw video mode" below. Pass `0` explicitly to disable in archive mode too. |
| `--crop-bottom FRAC` | Fraction of frame height to crop off the bottom. Default: `0.0344`, or `0` with `--raw`. See `--crop-top`. |

### Generation tuning

| Option | Description |
|---|---|
| `--max-new-tokens N` | Cap on generated answer length. Default: `768`. |
| `--repetition-penalty N` | Penalizes repeated tokens. Default: `1.15` (`1.0` = off). |
| `--no-repeat-ngram-size N` | Forbids repeating any N-token sequence. Default: `3` (`0` = off). |
| `--do-sample` / `--no-do-sample` | Enable probabilistic sampling instead of greedy decoding. Default: off (greedy) - greedy avoids the model re-rolling a different hallucinated guess on the same illegible text every run. |
| `--temperature N` | Sampling temperature, only with `--do-sample`. Default: `0.7`. |
| `--top-p N` | Nucleus sampling cutoff, only with `--do-sample`. Default: `0.8`. |
| `--top-k N` | Top-k sampling cutoff, only with `--do-sample`. Default: `20`. |

### Sign/plate zoom pipeline

After the main pass, a few full-resolution frames are separately checked for signs/plates and re-OCR'd at native resolution - fixes small/distant signage the main pass's downscaled frames can't resolve.

| Option | Description |
|---|---|
| `--zoom-signs` / `--no-zoom-signs` | Enable the zoom sub-pipeline. Default: on. |
| `--zoom-frames N` | How many full-res frames to sample for sign detection. Default: `4`. |
| `--zoom-detect-width N` | Resolution for the detection (grounding) step. Default: `1092`. |
| `--zoom-padding N` | Padding fraction added around each detected box before cropping. Default: `0.15`. |
| `--zoom-ocr-width N` | Minimum width a cropped sign/plate is upscaled to before OCR. Default: `640`. |
| `--zoom-debug-dir DIR` | Save every crop the zoom pipeline attempts to OCR into this directory, plus a `manifest.tsv` mapping each to its timestamp/label/read - lets you inspect the raw source pixels instead of just trusting a "not legible" read. |
| `--zoom-max-new-tokens N` | Cap on generated tokens for each crop's OCR read. Default: `200`. |
| `--zoom-detect-max-new-tokens N` | Cap on generated tokens for the detection call. Default: `500`. |
| `--zoom-repetition-penalty N` | Separate `--repetition-penalty` for detection/OCR calls. Default: `1.0` (off) - the main pass's repetition guards actively corrupt structured JSON detection output. |
| `--zoom-no-repeat-ngram-size N` | Separate `--no-repeat-ngram-size` for detection/OCR calls. Default: `0` (off). |
| `--zoom-plate-confidence-check` / `--no-zoom-plate-confidence-check` | Read every detected plate crop twice (once greedy, once with sampling forced on) and flag the read as unverified if the two disagree, instead of reporting a single possibly-wrong read as fact. Default: on. Costs one extra inference call per detected plate. |

### General

| Option | Description |
|---|---|
| `--overwrite` | Regenerate files that already exist, without asking. |
| `--dry-run` | Show what would be generated without generating it. |
| `-v`, `--verbose` | Print each file as it is generated. |
| `-h`, `--help` | Show help and exit. |

## RAW VIDEO MODE

`--raw` processes video that never went through a BlackVue archive at all - footage from another dashcam, a phone, whatever - by pointing `PATH` directly at a video file or a directory of video files, instead of an archive directory.

With `--raw`:

- `PATH` is a single video file, or a directory - every recognized video file directly inside it (not recursive) is processed, sorted by name. Recognized extensions: `.mp4`, `.mov`, `.avi`, `.mkv`, `.m4v`, `.webm`.
- `--from`/`--until`/`--timestamp` don't apply (raw footage has no BlackVue recording-id timestamp to select on) and `--camera` doesn't apply (no front/rear distinction) - `bv-scribe` exits with an argument error if either is given alongside `--raw`.
- `--crop-top`/`--crop-bottom` default to `0` (disabled) instead of the BlackVue-tuned defaults, since those defaults exist specifically to cut out BlackVue's own burned-in overlay text, which won't be there on non-BlackVue footage. Pass either explicitly to crop anyway.
- Output is written next to each source video as `<video-stem>.scene.txt`, rather than into the archive next to a `RecordingId`-named file.

## EXIT STATUS

| Code | Meaning |
|---|---|
| 0 | OK. |
| 1 | Argument error (e.g. bad `--timestamp`). |
| 2 | Completed, but one or more recordings had errors. |

## EXAMPLES

Describe and OCR one day's recordings with default settings:

```
bv-scribe --timestamp 20260715
```

OCR only, skipping the sign-zoom pipeline (faster, lower quality on small/distant signage):

```
bv-scribe --timestamp 20260715 --task ocr --no-zoom-signs
```

Inspect what the sign-zoom pipeline is actually seeing on a plate it keeps reporting as unverified:

```
bv-scribe --timestamp 20260715_1430 --zoom-debug-dir ./zoom-debug --overwrite
```

Try Qwen3-VL instead of the default Qwen2.5-VL (less tested against real footage):

```
bv-scribe --timestamp 20260715 --model Qwen/Qwen3-VL-8B-Instruct
```

Describe only the rear camera for a day, with full description+OCR treatment:

```
bv-scribe --timestamp 20260715 --camera rear
```

Describe the front camera normally, plus a cheap OCR-only pass on the rear camera for plates/signs:

```
bv-scribe --timestamp 20260715 --camera both
```

Describe a folder of footage from a non-BlackVue camera:

```
bv-scribe --raw /path/to/other-dashcam-footage
```

## SEE ALSO

`bv-generate(1)` for `--describe-scene` - the same underlying model/output, run alongside other generation actions with fixed defaults instead of the full tuning surface here.
