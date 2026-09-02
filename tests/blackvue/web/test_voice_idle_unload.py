"""
Tests for web/voice_idle_unload.py - the shared idle-timeout timer that
evicts bv-web's voice-search models (Qwen3-ASR-1.7B, Qwen3-1.7B) after a
period of inactivity. See the module's own docstring for the full story:
Christer reported "something is holding about 10GB of vram" - both
models' unload_*_model() functions existed but were never called from
anywhere, and his explicit choice between "unload after every search" and
"idle-timeout auto-unload" was the latter.

touch()/cancel_pending_unload() are tested against the real
threading.Timer with a tiny idle_seconds so these tests stay fast (well
under a second) without needing to fake time. _unload_all() is tested
separately by calling it directly (bypassing the timer/sleep entirely)
with voice_asr.unload_asr_model/voice_llm.unload_text_model monkeypatched,
so it doesn't need a GPU/qwen_asr/torch in this sandbox - same reasoning
test_voice_asr.py/test_voice_llm.py already document for not exercising
the real model-loading code.

Copyright (C) 2026 Christer R. (sssreh)

SPDX-License-Identifier: GPL-3.0-or-later
"""

from __future__ import annotations

import time

from blackvue.web import voice_idle_unload
from blackvue.web import voice_llm
from blackvue.web.voice_idle_unload import cancel_pending_unload
from blackvue.web.voice_idle_unload import touch


def teardown_function(_fn):
    # Never leave a real background Timer thread scheduled past a test's
    # own lifetime - would otherwise fire _unload_all() (and its deferred
    # imports) at an arbitrary point during a later, unrelated test.
    cancel_pending_unload()


# ---------------------------------------------------------------------------
# touch() / cancel_pending_unload()
# ---------------------------------------------------------------------------


def test_touch_schedules_unload_after_idle_seconds(monkeypatch):
    calls = []
    monkeypatch.setattr(voice_idle_unload, "_unload_all", lambda: calls.append(1))

    touch(idle_seconds=0.05)
    assert calls == []  # hasn't fired yet

    time.sleep(0.2)
    assert calls == [1]


def test_touch_reschedules_and_does_not_fire_early(monkeypatch):
    calls = []
    monkeypatch.setattr(voice_idle_unload, "_unload_all", lambda: calls.append(1))

    touch(idle_seconds=0.15)
    time.sleep(0.05)
    touch(idle_seconds=0.15)  # a second "search" pushes the deadline out
    time.sleep(0.05)
    assert calls == []  # first timer's original deadline has passed, but was cancelled

    time.sleep(0.2)
    assert calls == [1]  # only fires once, from the second touch()


def test_cancel_pending_unload_prevents_firing(monkeypatch):
    calls = []
    monkeypatch.setattr(voice_idle_unload, "_unload_all", lambda: calls.append(1))

    touch(idle_seconds=0.05)
    cancel_pending_unload()
    time.sleep(0.2)
    assert calls == []


def test_cancel_pending_unload_is_a_no_op_with_nothing_scheduled():
    cancel_pending_unload()  # must not raise


# ---------------------------------------------------------------------------
# _unload_all()
# ---------------------------------------------------------------------------


def test_unload_all_calls_both_model_unloaders(monkeypatch):
    calls = []
    monkeypatch.setattr(
        "blackvue.web.voice_asr.unload_asr_model", lambda: calls.append("asr")
    )
    monkeypatch.setattr(
        "blackvue.web.voice_llm.unload_text_model", lambda: calls.append("text")
    )

    voice_idle_unload._unload_all()

    assert calls == ["asr", "text"]


def test_unload_all_with_empty_caches_is_harmless():
    # No monkeypatching - exercises the real unload_*_model() functions
    # with empty caches, same "clear an empty dict, no torch/GPU work
    # happens" path both already cover on their own in isolation.
    voice_llm._TEXT_MODEL_CACHE.clear()
    voice_idle_unload._unload_all()
