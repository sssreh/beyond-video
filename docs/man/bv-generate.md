# bv-generate(1)

## NAME

`bv-generate` - generate derived assets (audio, duration, transcript, translation) for recordings

## SYNOPSIS

```
bv-generate [--from TIMESTAMP] [--until TIMESTAMP] [--timestamp TIMESTAMP]
            [--extract-audio] [--get-duration]
            [--transcribe] [--translate LANG] [--language LANG]
            [--model-size SIZE] [--diarize] [--hf-token TOKEN]
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
| `--srt` | Also write an SRT subtitle file (`<recording>.srt`) with per-segment timestamps. Requires `--transcribe` or `--translate`. |
| `--lrc` | Also write an LRC timestamp file (`<recording>.lrc`), one `[mm:ss.xx]` line per segment. Requires `--transcribe` or `--translate`. |

### Transcription tuning

| Option | Description |
|---|---|
| `--language LANG` | Spoken language hint (e.g. `en`). Auto-detected if omitted. |
| `--model-size SIZE` | faster-whisper model size. Default: `small`. |
| `--diarize` | Label who is speaking (e.g. `[SPEAKER_00] ...`), using pyannote.audio. Requires a HuggingFace access token. |
| `--hf-token TOKEN` | HuggingFace token for `--diarize`. Create one at <https://huggingface.co/settings/tokens>, then accept the model license at <https://huggingface.co/pyannote/speaker-diarization-community-1>. Falls back to the `HF_TOKEN` environment variable if omitted. |

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

Regenerate everything from scratch for a specific recording prefix:

```
bv-generate --timestamp 20260715_1430 --extract-audio --get-duration --transcribe --overwrite
```

## SEE ALSO

`bv-download(1)` populates the archive this reads from, `bv-lang(1)` manages the language packages `--translate` needs, `bv-ls(1)` shows which derived assets already exist, `bv-export(1)` picks up `.duration.txt`/transcript/subtitle files automatically.
