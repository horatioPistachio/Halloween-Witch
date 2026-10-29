"""ElevenLabs text-to-speech using the official `elevenlabs` SDK (streaming)."""

from __future__ import annotations

import logging
import time
from collections.abc import AsyncIterator

import httpx
from elevenlabs import VoiceSettings
from elevenlabs.client import AsyncElevenLabs
from elevenlabs.core.api_error import ApiError

from witch.audio.base import SAMPLE_RATE
from witch.config import ConfigError, Settings
from witch.tts.base import TTSError

log = logging.getLogger(__name__)

REQUEST_TIMEOUT_S = 10.0
MAX_ERROR_CHARS = 200


class ElevenLabsTTS:
    def __init__(self, settings: Settings, transport: httpx.AsyncBaseTransport | None = None) -> None:
        self._settings = settings
        self._transport = transport
        self._http: httpx.AsyncClient | None = None
        self._client: AsyncElevenLabs | None = None

    async def __aenter__(self) -> ElevenLabsTTS:
        if not self._settings.eleven_api_key:
            raise ConfigError("ELEVEN_LABS_API_KEY not set. Add it to .env or run with --no-tts")
        self._http = httpx.AsyncClient(timeout=REQUEST_TIMEOUT_S, transport=self._transport)
        self._client = AsyncElevenLabs(api_key=self._settings.eleven_api_key, httpx_client=self._http)
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        if self._http is not None:
            await self._http.aclose()
        self._http = None
        self._client = None

    async def synthesize(self, text: str) -> AsyncIterator[bytes]:
        if self._client is None:
            raise RuntimeError("ElevenLabsTTS used outside its context manager")
        log.debug("TTS request: %d chars, model %s", len(text), self._settings.tts_model)
        requested_at = time.perf_counter()
        first_chunk_logged = False
        extra: dict[str, VoiceSettings] = {}
        if self._settings.tts_stability is not None:
            extra["voice_settings"] = VoiceSettings(stability=self._settings.tts_stability)
        try:
            carry = b""
            # No SDK retries: a retry would add seconds of silence to a live reply.
            async for chunk in self._client.text_to_speech.stream(
                voice_id=self._settings.voice_id,
                text=text,
                model_id=self._settings.tts_model,
                output_format=f"pcm_{SAMPLE_RATE}",
                request_options={"max_retries": 0},
                **extra,
            ):
                if not first_chunk_logged:
                    first_chunk_logged = True
                    log.info(
                        "timing> elevenlabs: first byte %.0f ms (%d chars, %s)",
                        (time.perf_counter() - requested_at) * 1000,
                        len(text),
                        self._settings.tts_model,
                    )
                data = carry + chunk
                usable = len(data) - len(data) % 2
                carry = data[usable:]
                if usable:
                    yield data[:usable]
            if carry:
                log.debug("Dropped trailing odd byte from TTS stream")
        except ApiError as exc:
            raise TTSError(f"ElevenLabs HTTP {exc.status_code}: {_error_message(exc.body)}") from None
        except httpx.HTTPError as exc:
            raise TTSError(f"ElevenLabs request failed: {type(exc).__name__}") from None


def _error_message(body: object) -> str:
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
