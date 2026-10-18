"""ElevenLabs text-to-speech over the streaming HTTP endpoint."""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from typing import Any

import httpx

from witch.audio.base import SAMPLE_RATE
from witch.config import ConfigError, Settings
from witch.tts.base import TTSError

log = logging.getLogger(__name__)

API_BASE_URL = "https://api.elevenlabs.io"
MAX_ERROR_CHARS = 200


class ElevenLabsTTS:
    def __init__(self, settings: Settings, transport: httpx.AsyncBaseTransport | None = None) -> None:
        self._settings = settings
        self._transport = transport
        self._client: httpx.AsyncClient | None = None

    async def __aenter__(self) -> ElevenLabsTTS:
        if not self._settings.eleven_api_key:
            raise ConfigError("ELEVEN_LABS_API_KEY not set. Add it to .env or run with --no-tts")
        self._client = httpx.AsyncClient(
            base_url=API_BASE_URL,
            headers={"xi-api-key": self._settings.eleven_api_key},
            timeout=httpx.Timeout(connect=5.0, read=10.0, write=5.0, pool=5.0),
            transport=self._transport,
        )
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    async def synthesize(self, text: str) -> AsyncIterator[bytes]:
        if self._client is None:
            raise RuntimeError("ElevenLabsTTS used outside its context manager")
        path = f"/v1/text-to-speech/{self._settings.voice_id}/stream"
        log.debug("TTS request: %d chars, model %s", len(text), self._settings.tts_model)
        try:
            async with self._client.stream(
                "POST",
                path,
                params={"output_format": f"pcm_{SAMPLE_RATE}"},
                json=self._body(text),
            ) as response:
                if not response.is_success:
                    await response.aread()
                    raise TTSError(f"ElevenLabs HTTP {response.status_code}: {_error_message(response)}")
                carry = b""
                async for chunk in response.aiter_bytes():
                    data = carry + chunk
                    usable = len(data) - len(data) % 2
                    carry = data[usable:]
                    if usable:
                        yield data[:usable]
                if carry:
                    log.debug("Dropped trailing odd byte from TTS stream")
        except httpx.HTTPError as exc:
            raise TTSError(f"ElevenLabs request failed: {type(exc).__name__}") from None

    def _body(self, text: str) -> dict[str, Any]:
        body: dict[str, Any] = {"text": text, "model_id": self._settings.tts_model}
        if self._settings.tts_stability is not None:
            body["voice_settings"] = {"stability": self._settings.tts_stability}
        return body


def _error_message(response: httpx.Response) -> str:
    try:
        body = response.json()
    except ValueError:
        return "no details"
    detail = body.get("detail") if isinstance(body, dict) else None
    if isinstance(detail, dict):
        message = detail.get("message") or detail.get("status")
    elif isinstance(detail, str):
        message = detail
    elif isinstance(detail, list):
        message = "request validation failed"
    else:
        message = None
    return str(message or "no details")[:MAX_ERROR_CHARS]
