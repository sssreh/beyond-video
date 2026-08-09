# bv-generate(1)

## NAME

`bv-generate` - generate derived assets (audio, duration, transcript, translation) for recordings

## SYNOPSIS

```
bv-generate [--from TIMESTAMP] [--until TIMESTAMP] [--timestamp TIMESTAMP]
            [--extract-audio] [--get-duration]
            [--transcribe] [--translate LANG] [--language LANG]
            [--model-size SIZE] [--npu-model-dir PATH]
            [--diarize] [--hf-token TOKEN]
            [--srt] [--lrc]
            [--overwrite] [--dry-run] [-v]
            [PATH]
```

## DESCRIPTION

`bv-generate` produces derived assets for recordings already downloaded into a local archive (see `bv-download(1)`), writing each one next to its source recording so it shows up in `bv-ls(1)` and is picked up automatically by `bv-export(1)` (trip-level subtitle/transcript merging, `--get-duration`'s span feeding trip-gap detection, etc.).

At least one action flag (`--extract-audio`, `--get-duration`, `--transcribe`, or `--translate`) must be given - `bv-generate` with no action does nothing.

Parking-mode (`P`) recordings are 1-frame-per-second timelapses with no audio - audio-dependent actions (`--extract-audio`, `--transcribe`, `--translate`) are automatically skipped for them, while `--get-duration` still works (reporting the real elapsed time span, not the timelapse video's own short playback length).

`--translate` implies transcription internally - `--transcribe` doesn't need to also be given.

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

### Actions

| Option | Description |
|---|---|
| `--extract-audio` | Extract the audio track from the front camera video (or rear, if there's no front). Saved as `<recording>.aac`. |
| `--get-duration` | Compute the real-world duration in seconds. Saved as `<recording>.duration.txt`. |
| `--transcribe` | Transcribe the recording's audio to text. Saved as `<recording>.transcript.txt`. |
| `--translate LANG` | Translate the transcript into `LANG` (e.g. `es`, `fr`). Saved as `<recording>.translation.txt`. |
| `--srt` | Also write an SRT subtitle file (`<recording>.srt`) with per-segment timestamps. Requires `--transcribe` or `--translate`. If `--translate` is also given, the subtitles are in the translated language, not the original spoken one. |
| `--lrc` | Also write an LRC timestamp file (`<recording>.lrc`), one `[mm:ss.xx]` line per segment. Requires `--transcribe` or `--translate`. If `--translate` is also given, the lines are in the translated language, not the original spoken one. |

### Transcription tuning

| Option | Description |
|---|---|
| `--language LANG` | Spoken language hint (e.g. `en`). Auto-detected if omitted - except with `--npu-model-dir`, which requires it (see below). |
| `--model-size SIZE` | faster-whisper model size. Default: `small`. Ignored when `--npu-model-dir` is given. |
| `--npu-model-dir PATH` | Use an Intel NPU (OpenVINO GenAI) instead of faster-whisper, pointed at an already-converted OpenVINO IR Whisper model directory. Requires `--language` - this backend cannot auto-detect the spoken language. See "Intel NPU transcription" below. **Not verified against real Intel NPU hardware** - built from OpenVINO GenAI's own published API docs; there's no Intel NPU in this project's dev/test environment. Try it and report back if it doesn't work as documented. |
| `--diarize` | Label who is speaking (e.g. `[SPEAKER_00] ...`), using pyannote.audio. Requires a HuggingFace access token. |
| `--hf-token TOKEN` | HuggingFace token for `--diarize`. Create one at <https://huggingface.co/settings/tokens>, then accept the model license at <https://huggingface.co/pyannote/speaker-diarization-community-1>. Falls back to the `HF_TOKEN` environment variable if omitted. |

### General

| Option | Description |
|---|---|
| `--overwrite` | Regenerate files that already exist, without asking. |
| `--dry-run` | Show what would be generated without generating it. |
| `-v`, `--verbose` | Print each file as it is generated. |
| `-h`, `--help` | Show help and exit. |

## INTEL NPU TRANSCRIPTION

`--npu-model-dir` transcribes using an Intel NPU (Neural Processing Unit, e.g. the ones built into Core Ultra-series CPUs) via OpenVINO GenAI's `WhisperPipeline`, instead of the default faster-whisper/CTranslate2 backend, which has no NPU support at all. This is a completely separate code path (`blackvue.generate.speech._npu_whisper_transcribe`), not a device switch on the default one.

**Not verified against real Intel NPU hardware as of this writing.** It was built from OpenVINO GenAI's own published API (the device string swaps from `"CPU"`/`"GPU"` to `"NPU"`; no other call changes are documented as needed), but there's no Intel NPU available in this project's dev/test environment to confirm it against. If you try it on real hardware and something doesn't match what's described here, that's useful to know.

Setup:

1. Install the extra: `pip install .[npu]` (or `pip install openvino-genai` directly).
2. Convert a Whisper model to OpenVINO's IR format once, ahead of time - `--npu-model-dir` expects an already-converted model directory, not a model name:

   ```
   pip install optimum[openvino] --break-system-packages
   optimum-cli export openvino --trust-remote-code \
       --model openai/whisper-large-v3-turbo \
       --weight-format int4 --disable-stateful \
       /path/to/npu-model
   ```

   `--disable-stateful` is required specifically for the NPU's KV-cache decoder (not needed for CPU/GPU). Pre-converted models are also available on Hugging Face (e.g. search for `whisper-large-v3-turbo-int4-ov-npu`) as an alternative to converting one yourself.
3. Use it: `bv-generate --transcribe --language en --npu-model-dir /path/to/npu-model`

`--npu-model-dir` always requires `--language` alongside it - unlike the default backend, this path cannot auto-detect the spoken language, so `bv-generate` refuses to start without one rather than fail partway through.

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

Regenerate everything from scratch for a specific recording prefix:

```
bv-generate --timestamp 20260715_1430 --extract-audio --get-duration --transcribe --overwrite
```

## SEE ALSO

`bv-download(1)` populates the archive this reads from, `bv-lang(1)` manages the language packages `--translate` needs, `bv-ls(1)` shows which derived assets already exist, `bv-export(1)` picks up `.duration.txt`/transcript/subtitle files automatically.
