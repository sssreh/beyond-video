# Scene-Scribe (prototype)

A standalone script that asks a local vision-language model (Qwen2.5-VL)
to describe what's happening in a dashcam clip and read any on-screen
text. Not part of beyond-video - this is a "is it good enough" test.
If it proves useful, the plan is to fold it into `bv-generate` as a
`--describe` action later, alongside `--transcribe`/`--diarize`.

## Why this model

Cloud models (Gemini 2.5 Pro) score noticeably higher on general video
understanding benchmarks than any open-source model you can run
locally today. But dashcam description is a narrow task compared to
those benchmarks, your GPU has headroom to spare, and running it
locally keeps dashcam footage (plates, faces, your daily routes) off
someone else's cloud - which is the whole point of beyond-video.
Qwen2.5-VL is currently the strongest practical open-source option for
local video understanding, and it has strong built-in OCR too, so one
model does both jobs (description + reading on-screen text) in one
pass.

## Setup (Windows / PowerShell)

Give this its own venv - keep it separate from beyond-video's:

```powershell
cd path\to\scene-scribe
python -m venv .venv
.venv\Scripts\Activate.ps1
```

**Install torch first, separately, with the CUDA 12.8 wheel index.**
Your GPU is an RTX 5090 Laptop (Blackwell architecture, compute
capability sm_120) - it needs PyTorch 2.7.0 or newer, built against
CUDA 12.8, to actually use the GPU at all. Older/plain `pip install
torch` builds will silently fall back to CPU (or error) because they
were compiled before Blackwell existed:

```powershell
pip install torch torchvision --index-url https://download.pytorch.org/whl/cu128
```

Then verify the GPU is actually visible before installing anything
else - saves debugging time later if something's off:

```powershell
python -c "import torch; print(torch.cuda.is_available(), torch.cuda.get_device_name(0))"
```

That should print `True` and `NVIDIA GeForce RTX 5090 Laptop GPU` (or
similar). If it prints `False`, stop here and fix that first - nothing
below will use your GPU until this does.

Then install the rest:

```powershell
pip install -r requirements.txt
```

Note on `qwen-vl-utils[decord]`: `decord` (the fast video-frame
loader) often doesn't have prebuilt wheels for Windows. If it fails to
install, that's fine - just run `pip install qwen-vl-utils` instead
(drop the `[decord]` extra) and it'll automatically fall back to
`torchvision` for video loading, which works everywhere, just a bit
slower.

No Hugging Face token needed - `Qwen/Qwen2.5-VL-7B-Instruct` (the
default model) isn't gated, unlike the diarization model beyond-video
uses.

## Usage

```powershell
python scene_scribe.py C:\path\to\a\recording.mp4
```

First run downloads the model (~16GB, one-time, cached under
`~/.cache/huggingface`) - expect that to take a while depending on
your connection.

Useful flags:

```powershell
# Just the scene description, skip OCR
python scene_scribe.py recording.mp4 --task describe

# Just read on-screen text
python scene_scribe.py recording.mp4 --task ocr

# Save the result to a file too
python scene_scribe.py recording.mp4 --output description.txt

# Smaller/faster model - good for quick iteration or if you're tight on VRAM
python scene_scribe.py recording.mp4 --model Qwen/Qwen2.5-VL-3B-Instruct

# Quantized version of the default model - lower VRAM, faster, some quality tradeoff
python scene_scribe.py recording.mp4 --model Qwen/Qwen2.5-VL-7B-Instruct-AWQ
```

`--fps` (default 1.0) and `--max-frames` (default 32) control how many
frames get sampled from the video - raise `--fps` if you're worried
about missing something brief, lower `--max-frames` or the model size
if you run out of VRAM on a long clip.

## What to actually judge

Run it against a few real recordings from your archive - ideally a
mix: a boring highway stretch, something with actual traffic/an event
if you have one, and a recording with visible street signs or a
speed/GPS overlay to judge the OCR half. Things worth checking:

- Does the description actually match what's in the clip, or does it
  hallucinate events that didn't happen?
- Does it correctly say "nothing notable" for boring clips instead of
  inventing drama (the prompt explicitly asks it not to, but worth
  checking it listens)?
- How good is the on-screen text reading - your overlay text, street
  signs, anything else legible?
- How's the speed, on your hardware, for a typical recording length?

If it's good enough on those, it's a real candidate for a `bv-generate
--describe` flag. If not, that's useful information too - it tells us
whether it's worth trying a bigger local model, or just call out to
Gemini for this one specific feature instead of everything else this
project deliberately keeps local.
