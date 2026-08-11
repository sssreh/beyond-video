# bv-scribe(1)

## NAME

`bv-scribe` - describe recordings' contents and read their on-screen text with a local vision-language model

## SYNOPSIS

```
bv-scribe [--from TIMESTAMP] [--until TIMESTAMP] [--timestamp TIMESTAMP]
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
          [--cpu]
          [--trip-summary] [--trip-summary-max-new-tokens N]
          [--overwrite] [--dry-run] [-v]
          [PATH]
```

## DESCRIPTION

`bv-scribe` is the batch-oriented, fully-tunable counterpart to `bv-generate --describe-scene` (see `bv-generate(1)`) - same underlying vision-language model and output (`<recording>.scene.txt`), but with the full set of tuning flags real-footage testing converged on, plus an opt-in `--trip-summary` pass. Selects recordings from a local archive the same way every other `bv-*` command does - by timestamp/`--from`/`--until`/`--timestamp` - rather than the raw file/folder arguments the original standalone scene-scribe prototype took.

Requires the `scene` extra: `pip install .[scene]`. Install torch separately first, per its own CUDA-build instructions for your GPU (a plain `pip install .[scene]` pulls in whatever torch resolves to, not necessarily the right CUDA build) - see PyTorch's own "Get Started" page.

**Trusting the output.** Real-footage testing surfaced two distinct failure modes: the model can confidently misread a license plate (report the wrong characters rather than flagging it illegible), and it can invent plausible-sounding but unrelated text on an ambiguous scene (a real Stockholm-area trip once got "Palm Jumeirah" - a Dubai landmark - as on-screen text, more than once, across separate runs). Every output file ends with a disclaimer to this effect - treat every read, especially plates/signs/place names, as unverified until checked against the source video. `--zoom-plate-confidence-check` (on by default) mitigates the plate case specifically: each detected plate crop is read twice (once greedy, once with sampling forced on), and reported as unverified rather than picked between if the two disagree.

## ARGUMENTS

| Argument | Description |
|---|---|
| `PATH` | Archive directory. Default: current directory. |

## OPTIONS

### Selection

| Option | Description |
|---|---|
| `--from TIMESTAMP` | Only consider recordings from this timestamp. |
| `--until TIMESTAMP` | Only consider recordings up to this timestamp. |
| `--timestamp TIMESTAMP` | Only consider recordings matching this timestamp or prefix. |

### Task / model

| Option | Description |
|---|---|
| `--task {describe,ocr,both}` | `describe` for what's happening, `ocr` for on-screen text only, `both` for a single combined pass. Default: `both`. |
| `--model MODEL` | Hugging Face model id. Default: `Qwen/Qwen2.5-VL-7B-Instruct` (~16GB download on first use, cached under `~/.cache/huggingface`). A smaller Qwen2.5-VL or a quantized (`-AWQ`) variant trades accuracy for speed/VRAM. Qwen3-VL (any id containing `qwen3-vl`) is also supported but less tested against real footage - requires `transformers>=4.57.0`. |
| `--cpu` | Force CPU inference. Extremely slow for a 7B+ video model - mainly useful to confirm the pipeline runs at all without a working CUDA setup. |

### Frame sampling / resolution

| Option | Description |
|---|---|
| `--fps N` | Frames per second of video to sample. Default: `1.0`. |
| `--max-frames N` | Hard cap on sampled frames regardless of `--fps`. Default: `16`. Lower this first if generation is too slow. |
| `--max-pixels N` | Resolution cap per sampled frame, in total pixels. Default: `151200` (~420x360). Only used when `--resized-width`/`--resized-height` are both `0`. |
| `--resized-width N` | Force an exact frame width, bypassing `--max-pixels` - the actual resolution knob in practice. Default: `1092`. Pass `0` (with `--resized-height 0`) to fall back to `--max-pixels` instead. |
| `--resized-height N` | Force an exact frame height. Default: `588`. See `--resized-width`. |
| `--crop-top FRAC` | Fraction of frame height to crop off the top before the model sees it, to cut out BlackVue's burned-in overlay text (timestamp/speed/camera name). Default: `0.0378`. Pass `0` to disable. |
| `--crop-bottom FRAC` | Fraction of frame height to crop off the bottom. Default: `0.0344`. See `--crop-top`. |

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

### Trip summary

| Option | Description |
|---|---|
| `--trip-summary` | After processing every selected recording, run one extra text-only pass synthesizing a single trip-level narrative from their `## Description` sections (explicitly tracking how conditions changed over the trip, e.g. "moderate traffic became heavier after a while", rather than restating each segment) into `trip_summary.txt`. Needs 2+ described recordings in the selection. |
| `--trip-summary-max-new-tokens N` | Cap on generated tokens for the `--trip-summary` pass. Default: `768`. |

### General

| Option | Description |
|---|---|
| `--overwrite` | Regenerate files that already exist, without asking. |
| `--dry-run` | Show what would be generated without generating it. |
| `-v`, `--verbose` | Print each file as it is generated. |
| `-h`, `--help` | Show help and exit. |

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

Describe a trip and get a synthesized trip-level narrative:

```
bv-scribe --timestamp 20260715 --trip-summary
```

Inspect what the sign-zoom pipeline is actually seeing on a plate it keeps reporting as unverified:

```
bv-scribe --timestamp 20260715_1430 --zoom-debug-dir ./zoom-debug --overwrite
```

Try Qwen3-VL instead of the default Qwen2.5-VL (less tested against real footage):

```
bv-scribe --timestamp 20260715 --model Qwen/Qwen3-VL-8B-Instruct
```

## SEE ALSO

`bv-generate(1)` for `--describe-scene` - the same underlying model/output, run alongside other generation actions with fixed defaults instead of the full tuning surface here.
