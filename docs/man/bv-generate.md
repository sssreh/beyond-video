# bv-generate(1)

## NAME

`bv-generate` - generate derived assets (audio, duration, transcript, translation) for recordings

## SYNOPSIS

```
bv-generate [--from TIMESTAMP] [--until TIMESTAMP] [--timestamp TIMESTAMP]
            [--resume]
            [--extract-audio] [--get-duration] [--thumbnail]
            [--transcribe] [--translate LANG] [--language LANG]
            [--model-size SIZE] [--cpu] [--npu-model-dir PATH]
            [--diarize] [--hf-token TOKEN]
            [--srt]
            [--describe-scene] [--scene-model MODEL] [--scene-quantize {auto,none,int8,int4}]
            [--scene-gpu-memory-fraction FRACTION]
            [--adaptive-sampling]
            [--adaptive-context-frames N] [--adaptive-context-offset-seconds SECONDS]
            [--camera {front,rear,both}]
            [--config-dir DIR]
            [--ignore-lock] [--overwrite] [--dry-run] [-v]
            [PATH]
```

## DESCRIPTION

`bv-generate` produces derived assets for recordings already downloaded into a local archive (see `bv-download(1)`), writing each one next to its source recording so it shows up in `bv-ls(1)` and is picked up automatically by `bv-export(1)` (trip-level subtitle/transcript merging, `--get-duration`'s span feeding trip-gap detection, etc.).

At least one action flag (`--extract-audio`, `--get-duration`, `--thumbnail`, `--transcribe`, `--translate`, or `--describe-scene`) must be given - `bv-generate` with no action does nothing.

Parking-mode (`P`) recordings are 1-frame-per-second timelapses with no audio - audio-dependent actions (`--extract-audio`, `--transcribe`, `--translate`) are automatically skipped for them, while `--get-duration` still works (reporting the real elapsed time span, not the timelapse video's own short playback length).

A still photo scanned into the archive (GoPro/folder archives only - see `CAMERA_ADAPTERS.md`) is a recording with only Front video and no Audio, same principle as Parking-mode above: `--extract-audio`, `--transcribe`, and `--translate` are skipped for it with a "photo has no audio" message. `--describe-scene` still runs normally on a photo's own image - a vision-language model has no trouble describing a still photo the same way it describes a video frame.

Some BlackVue cameras keep a real AAC audio stream in every recording even with in-camera voice recording turned off - the track exists, it's just silent. `bv-generate` checks the mean volume of every audio track it extracts (fresh, via `--extract-audio`, or as an internal step of `--transcribe`/`--translate`) and, if it's at or below a fixed loudness threshold, discards the `.aac` and skips transcription for that recording instead of keeping a silent file or wasting Whisper time on it. If the loudness check itself can't run (e.g. ffmpeg unexpectedly unavailable), the audio is kept rather than risk discarding something real.

A track can also be *not* silent (road/wind/engine noise, or the camera's own short voice prompts like "Parking mode off") while still having no actual speech in it. If Whisper transcribes such a track and comes back with nothing, `bv-generate` doesn't write a transcript/SRT/translation file for it - an empty result forcing a language guess tends to produce a wrong one, so the alternative was empty files with a bogus language suffix (e.g. `<recording>_nno.transcript.txt`) instead of no file at all.

`--translate` implies transcription internally - `--transcribe` doesn't need to also be given.

Every run prints a `bv-generate: started HH:MM:SS` line up front and a `bv-generate: finished HH:MM:SS (N.Ns)` line on every exit path from there on (including argument errors and an empty selection) - same pattern `bv-search(1)` uses. A batch run over hundreds of recordings with `--describe-scene`/`--transcribe` can take hours with nothing else printed in between, so both when it ran and how long it took are visible without timing it yourself.

Before walking the archive at all, `bv-generate` checks whether `bv-lock(1)` has already marked the selected range as done for every action flag given on this run. If so, the whole run is skipped with a single summary line instead of touching a single recording - see `bv-lock(1)` for how to mark a finished stretch of the archive (a whole year, typically) this way so it's never walked again. `--ignore-lock` bypasses this check for one run.

## ARGUMENTS

| Argument | Description |
|---|---|
| `PATH` | Archive directory, or a camera system id (see `bv-config(1)`) - resolved to that camera's configured target directory. A path that looks like a real path (starts with `./`/`.\`, is `.`/`..`, is absolute, or contains a path separator) is always used literally, so `./Kirby` forces a literal directory named `Kirby` even if a camera with that id also exists. Default: current directory. |

## OPTIONS

### Selection

| Option | Description |
|---|---|
| `--config-dir DIR` | Directory camera configs live in, for resolving `PATH` as a camera id. Default: the platform's standard config directory (same default as `bv-config(1)`). |
| `--from TIMESTAMP` | Only consider recordings from this timestamp. |
| `--until TIMESTAMP` | Only consider recordings up to this timestamp. |
| `--timestamp TIMESTAMP` | Only consider recordings matching this timestamp or prefix. |
| `--resume` | Skip straight to new recordings instead of walking the whole archive every run - for a daily/cron invocation with a stable set of action flags. Remembers, per exact combination of action flags used, the newest recording reached by the last `--resume` run, saved as `.bv-generate-resume.json` next to the archive; each later `--resume` run narrows `--from` up to that point (combined with an explicit `--from`/`--until`, not replacing them - whichever bound is later wins). The cursor is written after every single recording, not just once at the end of the run, so a crash or Ctrl-C partway through an hours-long batch keeps everything already attempted - the next `--resume` run picks up from there instead of re-walking from the start. A recording is included once more on the very next run after being the newest one seen (cheap - already-generated files are still skipped the normal way), as a safety margin against an interrupted run. Changing the action flags starts that new combination's own cursor from the beginning rather than risking a silent gap. `--dry-run` never advances the cursor. First run for a given combination (no cursor yet) behaves exactly like not passing `--resume` at all. Doesn't reduce the initial archive scan itself (a single directory listing, not per-recording work) - it eliminates the walk-and-skip pass over every already-done historical recording, which is what a growing daily archive actually costs over time. |

### Actions

| Option | Description |
|---|---|
| `--extract-audio` | Extract the audio track from the front camera video (or rear, if there's no front). Saved as `<recording>.aac`. Skipped (and any extracted file removed) if the track is effectively silent - see below. |
| `--get-duration` | Compute the real-world duration in seconds. Saved as `<recording>.duration.txt`. |
| `--thumbnail` | Generate a small JPEG frame-grab thumbnail from the front camera video (or rear if there's no front). Saved as `<recording>.thumb.jpg`. Only useful for archives with no camera-native thumbnail sidecar (FolderAdapter/GoProAdapter - see `CAMERA_ADAPTERS.md`); a recording that already has one, or that's a photo (which already serves as its own thumbnail), is skipped. `bv-web`'s archive browser generates the same permanent file itself on first view if this hasn't been run yet, so running this ahead of time just avoids paying that cost on first view. |
| `--transcribe` | Transcribe the recording's audio to text. Saved as `<recording>.transcript.txt`. |
| `--translate LANG` | Translate the transcript into `LANG` (e.g. `es`, `fr`). Saved as `<recording>.translation.txt`. |
| `--srt` | Also write an SRT subtitle file (`<recording>.srt`) with per-segment timestamps. Requires `--transcribe` or `--translate`. If `--translate` is also given, the subtitles are in the translated language, not the original spoken one. |
| `--describe-scene` | Describe the recording's contents and read its on-screen text using a local vision-language model (Qwen2.5-VL/Qwen3-VL). Saved as `<recording>.scene.txt`. Works on Parking-mode recordings too (still video, just no audio) - a Parking recording's raw video is transparently swapped for a repaired copy first, so the vision model's own video decoder no longer prints raw `"contradictionary STSC and STCO"` / `"error reading header"` lines to the terminal for the known, harmless broken-empty-audio-track container quirk (see `generate/mp4_repair.py`'s own docstring). Requires the `scene` extra: `pip install .[scene]` (plus torch installed separately - see `bv-scribe(1)` for the CUDA-build note). Uses fixed sensible defaults; see `bv-scribe(1)` for the full set of tuning flags (frame sampling, resolution, the sign-zoom sub-pipeline, batch mode, trip summaries). |
| `--scene-model MODEL` | Vision-language model for `--describe-scene`. Default: `Qwen/Qwen3-VL-8B-Instruct` (~16GB download on first use, cached under `~/.cache/huggingface`; requires `transformers>=4.57.0`, already the `scene` extra's own floor). |
| `--scene-quantize {auto,none,int8,int4}` | Loading precision for `--scene-model`, not a different model - `auto` (default) picks based on the largest single GPU on this machine: `none` (full precision) at 20GB+ VRAM, `int8` at 10-20GB, `int4` below that; a machine with no CUDA GPU (or `--cpu`) always resolves to `none`, since bitsandbytes' int8/int4 loading is CUDA-only and quantizing on the way to a CPU load buys nothing. Sized off the largest single card, not total VRAM across cards, so e.g. two 12GB GPUs resolve to `int8` (fits on one card) rather than `none` (which would shard the model across both via `device_map="auto"`, a slower PCIe-pipelined load). Explicit `none`/`int8`/`int4` overrides the auto-detection; combining an explicit non-`none` level with `--cpu` is an error. Needs the `bitsandbytes` package (part of the `scene` extra). |
| `--scene-gpu-memory-fraction FRACTION` | Caps `--describe-scene`'s CUDA allocations to this fraction (greater than 0, at most `1.0`) of each visible GPU's total VRAM, via `torch.cuda.set_per_process_memory_fraction()`. Not set by default (no cap - the model claims whatever VRAM it wants, e.g. ~19.3GB of a 24GB card for unquantized `Qwen3-VL-8B-Instruct`). Useful for guaranteeing some VRAM stays free for something else running on the same GPU at the same time, instead of hoping the driver is polite about it - there's no true per-process GPU *compute* priority knob on consumer NVIDIA/Windows drivers (that's an MPS priority-queue feature, data-center/Linux only), so this memory cap is the practical lever. CUDA-only, same as `--scene-quantize`; combining it with `--cpu` is an error. |
| `--camera {front,rear,both}` | Which camera(s) `--describe-scene` processes (default: `front` - same as before this flag existed: front video, or rear if there's no front). `rear` processes only the rear video, with the normal full description+OCR treatment, saved as `<recording>.rear.scene.txt` - a deliberate choice to look at the rear camera gets full treatment, not just plates. `both` adds a cheap OCR-only bonus pass on the rear video alongside the normal front pass (also `<recording>.rear.scene.txt`), skipped with a note if the recording has no distinct rear video (i.e. front was already using rear as its own fallback) - a full rear-camera description would mostly just restate the front one's, so only plates/signs are worth the extra inference call. |
| `--adaptive-sampling` | For `--describe-scene`: pick which video frames to show the model based on this recording's own GPS/g-sensor telemetry, instead of evenly spaced frames - a long stopped stretch (a red light, a queue) gets fewer frames, a turn or hard brake/accel gets more. Falls back to today's even spacing on its own whenever a recording has no usable GPS/g-sensor data (or its adapter doesn't support either) - always safe to leave on. Off by default (today's fixed even-spacing sampling, unchanged). The real per-second timestamps actually sampled are recorded in the output's own `## Sampled frames` section, which `bv-web`'s recording-detail frame viewer prefers over its own even-spacing guess when present. |
| `--adaptive-context-frames N` | With `--adaptive-sampling`: also pull `N` extra real frames on *each* side of every adaptively-chosen highlight (spaced `--adaptive-context-offset-seconds` apart), so every highlight sits inside a short burst of genuinely continuous motion instead of one isolated snapshot next to other snapshots seconds or minutes away - a direct answer to descriptions reading choppier, one-sentence-per-frame, once frames got stitched into a real video clip. `N=2` turns one highlight into 5 real frames. Ignored unless `--adaptive-sampling` is also given. Default `0` (today's exact one-frame-per-highlight behavior, unchanged) - it multiplies frame/decode count, so it's a real speed/cost trade-off; opt in and compare before leaving it on for good. |
| `--adaptive-context-offset-seconds SECONDS` | Real-time spacing between each `--adaptive-context-frames` context frame and its highlight - e.g. `--adaptive-context-frames 2 --adaptive-context-offset-seconds 0.5` turns a highlight at `t=30.0s` into frames at `29.0s, 29.5s, 30.0s, 30.5s, 31.0s`. Ignored unless `--adaptive-context-frames` is greater than `0`. Default: `0.5`. |

**A note on trusting `--describe-scene`'s output.** Real-footage testing found two distinct failure modes worth knowing about before treating any of this as fact: the model can confidently misread a license plate (not flag it as illegible, just report the wrong characters), and it can invent plausible-sounding but unrelated text on an ambiguous scene (a real trip near Stockholm once got "Palm Jumeirah" - a Dubai landmark - as on-screen text, more than once). Every `--describe-scene` output ends with a disclaimer to this effect. Plate reads specifically get a mitigation: each detected plate crop is read twice (once greedy, once with sampling forced on), and reported as unverified if the two disagree rather than picked between - see `--zoom-plate-confidence-check` in `bv-scribe(1)`.

### Transcription tuning

| Option | Description |
|---|---|
| `--language LANG` | Spoken language hint (e.g. `en`). Auto-detected if omitted - except with `--npu-model-dir`, which requires it (see below). |
| `--model-size SIZE` | faster-whisper model size. Default: `large` if a CUDA GPU is detected on this machine, otherwise `small` - see `--cpu` to force the small/CPU combination anyway on a GPU machine (e.g. to compare against the GPU default). Ignored when `--npu-model-dir` is given. |
| `--cpu` | Force faster-whisper onto CPU even if a GPU is available. Useful for comparing e.g. `bv-generate --transcribe --model-size small --cpu` against the GPU-default `bv-generate --transcribe` (`large`, on GPU). Has no effect with `--npu-model-dir`, which has no CPU/GPU choice of its own. |
| — | Transcription always uses voice-activity-detection filtering and does not condition segments on prior text, to avoid faster-whisper's classic repetition-loop hallucination on non-speech noise (road/wind/engine) common in dashcam audio. |
| `--npu-model-dir PATH` | Use an Intel NPU (OpenVINO GenAI) instead of faster-whisper, pointed at an already-converted OpenVINO IR Whisper model directory. Requires `--language` - this backend cannot auto-detect the spoken language. See "Intel NPU transcription" below. **Not verified against real Intel NPU hardware** - built from OpenVINO GenAI's own published API docs; there's no Intel NPU in this project's dev/test environment. Try it and report back if it doesn't work as documented. |
| `--diarize` | Label who is speaking (e.g. `[SPEAKER_00] ...`), using pyannote.audio. Requires a HuggingFace access token. |
| `--hf-token TOKEN` | HuggingFace token for `--diarize`. Create one at <https://huggingface.co/settings/tokens>, then accept the model license at <https://huggingface.co/pyannote/speaker-diarization-community-1>. Falls back to the `HF_TOKEN` environment variable if omitted. |

### General

| Option | Description |
|---|---|
| `--ignore-lock` | Run even if the selected range is fully locked (see `bv-lock(1)`) for every action flag given here. For the rare case of needing to touch an otherwise-locked range again (e.g. a bug fix, or a genuinely new value for an already-locked asset like a fresh `--translate`) without editing the lock itself - narrow the selection with `--timestamp`/`--from`/`--until` first, this does not also narrow it for you. |
| `--overwrite` | Regenerate files that already exist, without asking. |
| `--dry-run` | Show what would be generated without generating it. |
| `-v`, `--verbose` | Print each file as it is generated. |
| `-h`, `--help` | Show help and exit. |

## INTEL NPU TRANSCRIPTION

`--npu-model-dir` transcribes using an Intel NPU (Neural Processing Unit, e.g. the ones built into Core Ultra-series CPUs) via OpenVINO GenAI's `WhisperPipeline`, instead of the default faster-whisper/CTranslate2 backend, which has no NPU support at all. This is a completely separate code path (`blackvue.generate.speech._npu_whisper_transcribe`), not a device switch on the default one.

**Not fully verified against real Intel NPU hardware as of this writing.** Christer tried it on a real Core Ultra desktop and it surfaced a real conversion-format issue (see Troubleshooting below), now fixed in the setup instructions below - but a full end-to-end `--transcribe` run with the corrected export hasn't been confirmed working yet.

Setup:

1. Install the extra: `pip install .[npu]` (or `pip install openvino-genai` directly).
2. Convert a Whisper model to OpenVINO's IR format once, ahead of time - `--npu-model-dir` expects an already-converted model directory, not a model name:

   ```
   pip install optimum[openvino]
   optimum-cli export openvino --trust-remote-code \
       --model openai/whisper-large-v3-turbo \
       --weight-format int4 \
       /path/to/npu-model
   ```

   Do **not** add `--disable-stateful` - see Troubleshooting below for why. Pre-converted models are also available on Hugging Face (e.g. search for `whisper-large-v3-turbo-int4-ov-npu`) as an alternative to converting one yourself.
3. Use it: `bv-generate --transcribe --language en --npu-model-dir /path/to/npu-model`

`--npu-model-dir` always requires `--language` alongside it - unlike the default backend, this path cannot auto-detect the spoken language, so `bv-generate` refuses to start without one rather than fail partway through.

**Troubleshooting.**

- `Stateful models without 'beam_idx' input are not supported in StatefulToStateless transformation`: your model was converted with `--disable-stateful`. That flag was correct guidance for OpenVINO versions before 2025.1, but current (2025.1+) OpenVINO GenAI handles stateful Whisper models on NPU natively and expects a normal (stateful) export instead - drop `--disable-stateful` and re-convert.
- If `WhisperPipeline` hangs or fails to run on NPU even with a correctly-converted (stateful) model, check your Intel NPU driver version - Intel's own guidance recommends driver 32.0.100.3104 or newer.

## EXIT STATUS

| Code | Meaning |
|---|---|
| 0 | OK. |
| 1 | Argument error (e.g. no action flag given). |
| 2 | Completed, but one or more recordings had errors. |

## EXAMPLES

Compute real durations for every recording (feeds `bv-ls --trips`/`bv-export`'s gap detection):

```
bv-generate --get-duration
```

Transcribe and translate to Swedish, with subtitles, for one day:

```
bv-generate --timestamp 20260715 --translate sv --srt
```

Transcribe with speaker labels:

```
export HF_TOKEN=hf_...
bv-generate --transcribe --diarize
```

Transcribe using an Intel NPU instead of faster-whisper (see "Intel NPU transcription" above for the one-time model setup):

```
bv-generate --transcribe --language en --npu-model-dir /path/to/npu-model
```

Compare the `small`/CPU and GPU-default (`large`) models on the same recording:

```
bv-generate --timestamp 20260715_1430 --transcribe --model-size small --cpu --overwrite
bv-generate --timestamp 20260715_1430 --transcribe --overwrite
```

Regenerate everything from scratch for a specific recording prefix:

```
bv-generate --timestamp 20260715_1430 --extract-audio --get-duration --transcribe --overwrite
```

Daily cron job that only ever looks at new recordings, not the whole archive's history:

```
bv-generate --extract-audio --transcribe --describe-scene --resume
```

Describe a day's recordings alongside transcribing them:

```
bv-generate --timestamp 20260715 --transcribe --describe-scene
```

Touch one already-locked recording anyway (e.g. a fresh translation into a second language), without unlocking the whole range it belongs to:

```
bv-generate --timestamp 20190715_140000 --translate sv --ignore-lock
```

## SEE ALSO

`bv-download(1)` populates the archive this reads from, `bv-lang(1)` manages the language packages `--translate` needs, `bv-ls(1)` shows which derived assets already exist, `bv-export(1)` picks up `.duration.txt`/transcript/subtitle files automatically, `bv-lock(1)` marks a finished range so it's skipped on future runs.
