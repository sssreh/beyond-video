"""Idle-timeout eviction for bv-web's voice-search models (Qwen3-ASR-1.7B
in voice_asr.py, Qwen3-1.7B in voice_llm.py).

Christer: "something is holding about 10GB of vram". Root cause: both
modules already had their own unload_*_model() functions (mirroring
generate/scene.py's unload_scene_model()), written for exactly this
purpose, but neither was ever called anywhere - transcribe_voice_search()
in web/app.py is a plain FastAPI request/response, not a JobRunner Job,
so there was no "job finished" hook to unload from the way
unload_scene_model() gets called after every bv-scribe/bv-generate run
(see jobs.py's own call site). Both unload_*_model() docstrings already
said as much ("Not currently wired into any cleanup hook").

Two designs were on the table: unload after every single search, or
unload only after a period of inactivity. Christer picked idle-timeout.
Immediate unload-after-every-request was rejected because a burst of
several voice searches in a row is the common case, and reloading both
models from disk before every single one would add several seconds of
latency to searches that otherwise take a couple of seconds.

So: one shared idle timer, (re)started via touch() every time a voice
search actually used the models (see web/app.py's transcribe_voice_search()
call site, right before it returns). If nothing touches it again within
IDLE_SECONDS, both models are evicted and their VRAM freed. A burst of
searches keeps re-pushing the deadline out and never fires mid-burst; a
lone search - or the last one in a session - unloads a few minutes
later, same as if voice search had never been touched at all.

Deliberately its own tiny module rather than folded into voice_asr.py
or voice_llm.py: it needs to import both of them to unload them, and
neither of those modules should import the other or need to know this
scheduling exists.
"""

from __future__ import annotations

import threading

#: How long the voice-search models sit idle before being unloaded.
#: 5 minutes: long enough that a normal back-and-forth of a few voice
#: searches while someone tunes a place/radius never trips it, short
#: enough that the VRAM doesn't sit held for the rest of a long-running
#: bv-web server's uptime after the last search of a session.
IDLE_SECONDS = 300.0

_lock = threading.Lock()
_timer: threading.Timer | None = None


def touch(idle_seconds: float = IDLE_SECONDS) -> None:
    """Impure - (re)start the idle-unload countdown. Call this once per
    voice-search request, after whichever of the ASR/LLM calls it made
    (if any) have finished. Cancels any previously pending timer first,
    so a steady stream of searches keeps pushing the actual unload out
    rather than firing mid-session."""

    global _timer

    with _lock:
        if _timer is not None:
            _timer.cancel()
        _timer = threading.Timer(idle_seconds, _unload_all)
        _timer.daemon = True
        _timer.start()


def cancel_pending_unload() -> None:
    """Impure - cancel any pending idle-unload timer without firing it.
    Exposed mainly for tests, so a test doesn't leave a background
    Timer thread scheduled past its own lifetime; not called anywhere
    in the app itself."""

    global _timer

    with _lock:
        if _timer is not None:
            _timer.cancel()
            _timer = None


def _unload_all() -> None:
    """Impure - the timer callback. Deferred imports of voice_asr/
    voice_llm, same reasoning as those modules' own deferred torch
    imports inside unload_asr_model()/unload_text_model(): nothing
    heavy should load just because this module got imported."""

    from .voice_asr import unload_asr_model
    from .voice_llm import unload_text_model

    unload_asr_model()
    unload_text_model()
