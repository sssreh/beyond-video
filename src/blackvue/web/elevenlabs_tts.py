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


def synthesize(
    text: str,
    voice_id: str,
    *,
    api_key: str,
    model_id: str = DEFAULT_MODEL_ID,
    timeout: float = DEFAULT_TIMEOUT,
) -> bytes:
    """MP3 audio bytes of `text` spoken by `voice_id`.

    Raises ElevenLabsError on any network/API failure, or if `text`
    is empty/whitespace-only (ElevenLabs itself rejects that with a
    422 - checked here first for a clearer message, and to avoid a
    round-trip for a request that can never succeed).
    """

    if not text.strip():
        raise ElevenLabsError("no text to speak")

    body = json.dumps({"text": text, "model_id": model_id}).encode("utf-8")
    request = Request(
        f"{API_BASE}/text-to-speech/{voice_id}",
        data=body,
        headers={
            "xi-api-key": api_key,
            "Content-Type": "application/json",
            "Accept": "audio/mpeg",
        },
    )
    return _open(request, timeout)


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
