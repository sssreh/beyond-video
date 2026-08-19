"""
ElevenLabs text-to-speech client for bv-web's "Read aloud" feature on
the archive recording detail page (see archive_recording_detail.html
and app.py's /api/tts/* routes).

Replaces the original browser-native implementation (Web Speech API's
`speechSynthesis`, task #1018) entirely - Christer, after trying that
one: "The voices of Susan and hazel where better, but i woild
probably le elevenlabs do the work if i implement it in full" (logged
as a future-improvement note in WORKING_CONTEXT.md at the time), then
later: "implement elevenlabs text to speech, i have an api key. I
want to be able to select speaker among all speakers including my own
voices." He picked "replace entirely" (not "add alongside browser
voices") when asked, since the whole point was moving off whatever
voices happen to be installed on a given browser/OS - a real network
dependency and per-character cost, but a consistent voice list
regardless of which device or browser opens bv-web.

Deliberately stdlib `urllib.request` rather than adding `requests`/
`httpx`/the `elevenlabs` SDK as a dependency - same choice
export/geocoding.py and export/osm_roads.py already made for their
own external HTTP calls, and the `web` extra in pyproject.toml is
kept deliberately light (shared with bv-live) so this avoids growing
it for what's ultimately two small JSON/binary HTTP calls.

The API key is read from the ELEVENLABS_API_KEY environment variable
(see api_key() below) - never hardcoded, never passed on a command
line where it could leak into shell history or process listings, and
never accepted from a request body/form field (the web UI has no
"paste your key here" input at all, unlike a per-request key some
other tools use, since a self-hosted single-account app has no reason
to accept one from the browser side). Set it in docker-compose.yml's
`environment:` block for the bv-web service, matching every other
env-var-configured secret/path in this codebase (see
core/camera_config.py's BEYOND_VIDEO_CONFIG_DIR, web/users.py's
BEYOND_VIDEO_USERS_FILE) - there is no central settings/config module
in this app, each feature reads its own env var where it's needed.

Copyright (C) 2026 Christer R. (sssreh)

SPDX-License-Identifier: GPL-3.0-or-later
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from urllib.error import HTTPError
from urllib.error import URLError
from urllib.request import Request
from urllib.request import urlopen

API_BASE = "https://api.elevenlabs.io/v1"

# Same naming convention as BEYOND_VIDEO_CONFIG_DIR/BEYOND_VIDEO_USERS_FILE
# elsewhere in this codebase, except this one isn't Beyond-Video-
# specific - it's ElevenLabs' own account API key, so it gets their
# name rather than a BEYOND_VIDEO_ prefix (nothing else in this app
# would plausibly want a differently-scoped ELEVENLABS_API_KEY).
ELEVENLABS_API_KEY_ENV_VAR = "ELEVENLABS_API_KEY"

# eleven_multilingual_v2: Christer's pick when asked "highest quality/
# most natural" vs "faster and cheaper" - matches his stated
# preference for the better-sounding voices in the browser-TTS era
# ("The voices of Susan and hazel where better") over speed or cost.
DEFAULT_MODEL_ID = "eleven_multilingual_v2"

DEFAULT_TIMEOUT = 30.0


class ElevenLabsError(Exception):
    """Raised for anything that stops a call to the ElevenLabs API
    from succeeding - no API key configured, a network error, an API-
    level error response (bad key, quota exceeded, unknown voice
    id, ...), or a response bv-web can't parse. Callers (the /api/tts/*
    routes in app.py) catch this and turn it into a user-facing error
    message instead of a bare 500."""


@dataclass(frozen=True)
class Voice:
    """One voice available to the configured account - either one of
    ElevenLabs' own premade voices, or one Christer has cloned/added
    himself ("my own voices"). The API's own /v1/voices response
    doesn't distinguish access levels beyond a `category` field; every
    voice it returns is one this account can actually use, so there's
    nothing further to filter here - "all speakers including my own
    voices" is just the whole list, unfiltered."""

    voice_id: str
    name: str
    category: str


def api_key() -> str | None:
    """The configured ElevenLabs API key, or None if
    ELEVENLABS_API_KEY isn't set. Every route/function below that
    needs a key takes it as an explicit parameter rather than calling
    this internally - keeps the actual network calls easily testable
    with a fake key, and keeps "is a key configured at all" a single
    visible check at the call site (app.py) rather than buried."""

    return os.environ.get(ELEVENLABS_API_KEY_ENV_VAR) or None


def list_voices(*, api_key: str, timeout: float = DEFAULT_TIMEOUT) -> list[Voice]:
    """Every voice the account behind `api_key` can use - ElevenLabs'
    premade voices plus any cloned/custom ones. Sorted with cloned
    voices first (Christer's own voices are the ones he's most likely
    to want to jump straight to), then alphabetically by name within
    each group.

    Raises ElevenLabsError on any network/API/parsing failure.
    """

    request = Request(
        f"{API_BASE}/voices",
        headers={"xi-api-key": api_key, "Accept": "application/json"},
    )
    raw = _open(request, timeout)

    try:
        payload = json.loads(raw)
    except ValueError as exc:
        raise ElevenLabsError(
            f"could not parse ElevenLabs' voice list: {exc}"
        ) from exc

    voices = []
    for entry in payload.get("voices", []):
        voice_id = entry.get("voice_id")
        name = entry.get("name")
        if not voice_id or not name:
            continue
        voices.append(
            Voice(
                voice_id=voice_id,
                name=name,
                category=entry.get("category") or "premade",
            )
        )

    voices.sort(key=lambda v: (v.category != "cloned", v.name.lower()))
    return voices


@dataclass(frozen=True)
class Alignment:
    """Character-level timing for a synthesized clip, as returned by
    ElevenLabs' `/with-timestamps` endpoint - one entry per character
    of the *original* input text (not the "normalized" text ElevenLabs
    may internally expand numbers/abbreviations into - that's a
    separate `normalized_alignment` field this app has no use for and
    doesn't parse), so index i here lines up directly with `text[i]`
    for whatever string was passed to synthesize_with_timestamps().
    Used client-side to build SRT subtitle cues without a second API
    call - see app.py's /api/tts/speak route and
    archive_recording_detail.html/trip_detail.html's own JS."""

    characters: tuple[str, ...]
    character_start_times_seconds: tuple[float, ...]
    character_end_times_seconds: tuple[float, ...]


@dataclass(frozen=True)
class SpeechWithTimestamps:
    """One synthesized clip plus its character alignment.
    `audio_base64` is passed through as-is (not decoded to bytes) -
    the only consumer is the browser, which decodes it client-side via
    atob(), so decoding and re-encoding it here would just be wasted
    work for a bv-web server that's often CPU-constrained already
    (scene description / Whisper jobs running alongside it)."""

    audio_base64: str
    alignment: Alignment | None


def synthesize_with_timestamps(
    text: str,
    voice_id: str,
    *,
    api_key: str,
    model_id: str = DEFAULT_MODEL_ID,
    timeout: float = DEFAULT_TIMEOUT,
) -> SpeechWithTimestamps:
    """The same audio the old plain `synthesize()` used to return,
    plus character-level timing alongside it in one call - added so
    the "Download SRT" link (app.py's /api/tts/speak route) can build
    subtitle cues from the exact same ElevenLabs request that already
    generates the MP3 for playback/download, rather than a second
    request that could itself drift out of sync with the first.
    Christer, after finding out a stray Download link had given him a
    non-audio file: "With knowing the description and having the mp3,
    could you also give me a srt file matching the timestamps in the
    mp3" - replaced the plain `synthesize()` entirely rather than
    adding this alongside it, since the with-timestamps endpoint
    returns identical audio at no extra cost, just wrapped in JSON
    with alignment data attached.

    Raises ElevenLabsError on any network/API failure, or if `text`
    is empty/whitespace-only (ElevenLabs itself rejects that with a
    422 - checked here first for a clearer message, and to avoid a
    round-trip for a request that can never succeed).
    """

    if not text.strip():
        raise ElevenLabsError("no text to speak")

    body = json.dumps({"text": text, "model_id": model_id}).encode("utf-8")
    request = Request(
        f"{API_BASE}/text-to-speech/{voice_id}/with-timestamps",
        data=body,
        headers={
            "xi-api-key": api_key,
            "Content-Type": "application/json",
            "Accept": "application/json",
        },
    )
    raw = _open(request, timeout)

    try:
        payload = json.loads(raw)
    except ValueError as exc:
        raise ElevenLabsError(
            f"could not parse ElevenLabs' speech response: {exc}"
        ) from exc

    audio_base64 = payload.get("audio_base64")
    if not audio_base64:
        raise ElevenLabsError("ElevenLabs response had no audio")

    # `alignment` is documented as nullable - degrade gracefully (no
    # SRT offered) rather than raise, since the audio itself is still
    # perfectly usable without it.
    alignment = None
    alignment_payload = payload.get("alignment")
    if alignment_payload:
        alignment = Alignment(
            characters=tuple(alignment_payload.get("characters") or ()),
            character_start_times_seconds=tuple(
                alignment_payload.get("character_start_times_seconds") or ()
            ),
            character_end_times_seconds=tuple(
                alignment_payload.get("character_end_times_seconds") or ()
            ),
        )

    return SpeechWithTimestamps(audio_base64=audio_base64, alignment=alignment)


def _open(request: Request, timeout: float) -> bytes:
    try:
        with urlopen(request, timeout=timeout) as response:
            return response.read()
    except HTTPError as exc:
        raise ElevenLabsError(
            f"ElevenLabs API error ({exc.code}): {_error_detail(exc)}"
        ) from exc
    except URLError as exc:
        raise ElevenLabsError(f"could not reach ElevenLabs: {exc}") from exc


def _error_detail(exc: HTTPError) -> str:
    """ElevenLabs' error responses are JSON with a `detail` field -
    sometimes a plain string, sometimes {"status": ..., "message":
    ...}. Falls back to the raw response body (or the exception's own
    text) for anything that doesn't match that shape, rather than
    raising a second error while trying to explain the first one."""

    try:
        raw = exc.read()
    except Exception:
        return str(exc)

    try:
        payload = json.loads(raw)
    except ValueError:
        return raw.decode("utf-8", errors="replace") or str(exc)

    detail = payload.get("detail")
    if isinstance(detail, dict):
        detail = detail.get("message")
    return str(detail) if detail else (raw.decode("utf-8", errors="replace") or str(exc))
