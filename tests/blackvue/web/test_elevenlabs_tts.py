"""
Tests for web/elevenlabs_tts.py - the ElevenLabs text-to-speech
client backing archive_recording_detail.html's "Read aloud" feature
(see that module's own docstring for the full backstory).

Follows export/test_geocoding.py's own established pattern for
testing a urllib-based external HTTP call: monkeypatch the module's
`urlopen` name with a fake, rather than mocking at the socket level
or depending on `requests`/`httpx`'s own test tooling (this repo has
neither as a dependency - see elevenlabs_tts.py's docstring on why it
uses stdlib urllib in the first place).

Copyright (C) 2026 Christer R. (sssreh)

SPDX-License-Identifier: GPL-3.0-or-later
"""

from __future__ import annotations

import json
from urllib.error import HTTPError
from urllib.error import URLError

import pytest

from blackvue.web import elevenlabs_tts as tts_module
from blackvue.web.elevenlabs_tts import ELEVENLABS_API_KEY_ENV_VAR
from blackvue.web.elevenlabs_tts import ElevenLabsError
from blackvue.web.elevenlabs_tts import Voice
from blackvue.web.elevenlabs_tts import api_key
from blackvue.web.elevenlabs_tts import list_voices
from blackvue.web.elevenlabs_tts import synthesize_with_timestamps


class _FakeResponse:
    def __init__(self, data: bytes):
        self._data = data

    def read(self, size=-1):
        return self._data

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return False


def _fake_urlopen(payload: bytes, *, captured: list | None = None):
    def urlopen(request, timeout=None):
        if captured is not None:
            captured.append(request)
        return _FakeResponse(payload)

    return urlopen


def _http_error(code: int, body: bytes) -> HTTPError:
    exc = HTTPError(
        url="https://api.elevenlabs.io/v1/voices",
        code=code,
        msg="error",
        hdrs=None,
        fp=None,
    )
    # HTTPError.read() delegates to its own file-like wrapper - stub
    # it directly rather than constructing a real one, same shortcut
    # the rest of this file's _FakeResponse takes for the happy path.
    exc.read = lambda: body
    return exc


# ---------------------------------------------------------------------------
# api_key()
# ---------------------------------------------------------------------------


def test_api_key_reads_env_var(monkeypatch):
    monkeypatch.setenv(ELEVENLABS_API_KEY_ENV_VAR, "sk-test-123")

    assert api_key() == "sk-test-123"


def test_api_key_is_none_when_unset(monkeypatch):
    monkeypatch.delenv(ELEVENLABS_API_KEY_ENV_VAR, raising=False)

    assert api_key() is None


def test_api_key_is_none_when_blank(monkeypatch):
    # docker-compose.yml ships this variable present but empty by
    # default (see its own comment) - a blank string must behave the
    # same as unset, not as a truthy-but-useless "key".
    monkeypatch.setenv(ELEVENLABS_API_KEY_ENV_VAR, "")

    assert api_key() is None


# ---------------------------------------------------------------------------
# list_voices()
# ---------------------------------------------------------------------------


def test_list_voices_parses_premade_and_cloned(monkeypatch):
    payload = {
        "voices": [
            {"voice_id": "v1", "name": "Rachel", "category": "premade"},
            {"voice_id": "v2", "name": "My Voice", "category": "cloned"},
        ]
    }
    monkeypatch.setattr(
        tts_module, "urlopen", _fake_urlopen(json.dumps(payload).encode("utf-8"))
    )

    voices = list_voices(api_key="sk-test")

    assert Voice(voice_id="v1", name="Rachel", category="premade") in voices
    assert Voice(voice_id="v2", name="My Voice", category="cloned") in voices


def test_list_voices_sends_api_key_header(monkeypatch):
    payload = {"voices": []}
    captured: list = []
    monkeypatch.setattr(
        tts_module,
        "urlopen",
        _fake_urlopen(json.dumps(payload).encode("utf-8"), captured=captured),
    )

    list_voices(api_key="sk-test-456")

    assert captured[0].get_header("Xi-api-key") == "sk-test-456"


def test_list_voices_sorts_cloned_first_then_alphabetical(monkeypatch):
    payload = {
        "voices": [
            {"voice_id": "v1", "name": "Zeb", "category": "premade"},
            {"voice_id": "v2", "name": "Adam", "category": "premade"},
            {"voice_id": "v3", "name": "Bob's Voice", "category": "cloned"},
        ]
    }
    monkeypatch.setattr(
        tts_module, "urlopen", _fake_urlopen(json.dumps(payload).encode("utf-8"))
    )

    voices = list_voices(api_key="sk-test")

    assert [v.voice_id for v in voices] == ["v3", "v2", "v1"]


def test_list_voices_skips_entries_missing_id_or_name(monkeypatch):
    payload = {
        "voices": [
            {"voice_id": "v1", "name": "Rachel", "category": "premade"},
            {"voice_id": "", "name": "No id", "category": "premade"},
            {"voice_id": "v2", "name": "", "category": "premade"},
        ]
    }
    monkeypatch.setattr(
        tts_module, "urlopen", _fake_urlopen(json.dumps(payload).encode("utf-8"))
    )

    voices = list_voices(api_key="sk-test")

    assert [v.voice_id for v in voices] == ["v1"]


def test_list_voices_defaults_category_to_premade_when_missing(monkeypatch):
    payload = {"voices": [{"voice_id": "v1", "name": "Rachel"}]}
    monkeypatch.setattr(
        tts_module, "urlopen", _fake_urlopen(json.dumps(payload).encode("utf-8"))
    )

    voices = list_voices(api_key="sk-test")

    assert voices[0].category == "premade"


def test_list_voices_raises_on_network_error(monkeypatch):
    def urlopen(request, timeout=None):
        raise URLError("no route to host")

    monkeypatch.setattr(tts_module, "urlopen", urlopen)

    with pytest.raises(ElevenLabsError):
        list_voices(api_key="sk-test")


def test_list_voices_raises_on_malformed_json(monkeypatch):
    monkeypatch.setattr(tts_module, "urlopen", _fake_urlopen(b"not json"))

    with pytest.raises(ElevenLabsError):
        list_voices(api_key="sk-test")


def test_list_voices_raises_elevenlabs_error_with_detail_on_http_error(monkeypatch):
    body = json.dumps({"detail": {"status": "invalid_api_key", "message": "bad key"}})

    def urlopen(request, timeout=None):
        raise _http_error(401, body.encode("utf-8"))

    monkeypatch.setattr(tts_module, "urlopen", urlopen)

    with pytest.raises(ElevenLabsError, match="bad key"):
        list_voices(api_key="sk-bad")


# ---------------------------------------------------------------------------
# synthesize_with_timestamps()
# ---------------------------------------------------------------------------


def _timestamps_payload(
    *, audio_base64: str = "ZmFrZS1tcDMtYnl0ZXM=", alignment: dict | None = "default"
) -> dict:
    if alignment == "default":
        alignment = {
            "characters": ["H", "i"],
            "character_start_times_seconds": [0.0, 0.1],
            "character_end_times_seconds": [0.1, 0.2],
        }
    payload = {"audio_base64": audio_base64}
    if alignment is not None:
        payload["alignment"] = alignment
    return payload


def test_synthesize_with_timestamps_returns_audio_and_alignment(monkeypatch):
    monkeypatch.setattr(
        tts_module,
        "urlopen",
        _fake_urlopen(json.dumps(_timestamps_payload()).encode("utf-8")),
    )

    result = synthesize_with_timestamps("Hello there", "v1", api_key="sk-test")

    assert result.audio_base64 == "ZmFrZS1tcDMtYnl0ZXM="
    assert result.alignment.characters == ("H", "i")
    assert result.alignment.character_start_times_seconds == (0.0, 0.1)
    assert result.alignment.character_end_times_seconds == (0.1, 0.2)


def test_synthesize_with_timestamps_posts_to_the_right_url_with_headers_and_body(
    monkeypatch,
):
    captured: list = []
    monkeypatch.setattr(
        tts_module,
        "urlopen",
        _fake_urlopen(
            json.dumps(_timestamps_payload()).encode("utf-8"), captured=captured
        ),
    )

    synthesize_with_timestamps("Hello there", "voice-abc", api_key="sk-test-789")

    request = captured[0]
    assert (
        request.full_url
        == "https://api.elevenlabs.io/v1/text-to-speech/voice-abc/with-timestamps"
    )
    assert request.get_header("Xi-api-key") == "sk-test-789"
    body = json.loads(request.data.decode("utf-8"))
    assert body["text"] == "Hello there"
    assert body["model_id"] == tts_module.DEFAULT_MODEL_ID


def test_synthesize_with_timestamps_handles_null_alignment(monkeypatch):
    payload = _timestamps_payload(alignment=None)
    monkeypatch.setattr(
        tts_module, "urlopen", _fake_urlopen(json.dumps(payload).encode("utf-8"))
    )

    result = synthesize_with_timestamps("Hello", "v1", api_key="sk-test")

    assert result.audio_base64 == "ZmFrZS1tcDMtYnl0ZXM="
    assert result.alignment is None


def test_synthesize_with_timestamps_raises_on_empty_text(monkeypatch):
    monkeypatch.setattr(
        tts_module,
        "urlopen",
        _fake_urlopen(json.dumps(_timestamps_payload()).encode("utf-8")),
    )

    with pytest.raises(ElevenLabsError):
        synthesize_with_timestamps("   ", "v1", api_key="sk-test")


def test_synthesize_with_timestamps_raises_on_missing_audio(monkeypatch):
    payload = {"alignment": None}
    monkeypatch.setattr(
        tts_module, "urlopen", _fake_urlopen(json.dumps(payload).encode("utf-8"))
    )

    with pytest.raises(ElevenLabsError):
        synthesize_with_timestamps("Hello", "v1", api_key="sk-test")


def test_synthesize_with_timestamps_raises_on_malformed_json(monkeypatch):
    monkeypatch.setattr(tts_module, "urlopen", _fake_urlopen(b"not json"))

    with pytest.raises(ElevenLabsError):
        synthesize_with_timestamps("Hello", "v1", api_key="sk-test")


def test_synthesize_with_timestamps_raises_on_http_error(monkeypatch):
    def urlopen(request, timeout=None):
        raise _http_error(422, b"not json body")

    monkeypatch.setattr(tts_module, "urlopen", urlopen)

    with pytest.raises(ElevenLabsError, match="not json body"):
        synthesize_with_timestamps("Hello", "v1", api_key="sk-test")


def test_synthesize_with_timestamps_raises_on_network_error(monkeypatch):
    def urlopen(request, timeout=None):
        raise URLError("timed out")

    monkeypatch.setattr(tts_module, "urlopen", urlopen)

    with pytest.raises(ElevenLabsError):
        synthesize_with_timestamps("Hello", "v1", api_key="sk-test")
