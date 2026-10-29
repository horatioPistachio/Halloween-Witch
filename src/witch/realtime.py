"""Thin wrapper over the OpenAI Realtime WebSocket that yields app-level events."""

from __future__ import annotations

import base64
import logging
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any

from openai import AsyncOpenAI

from witch.audio.base import SAMPLE_RATE
from witch.config import Settings
from witch.cost import TokenUsage, tokens_from_usage
from witch.persona import PERSONA_PROMPTS

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class SpeechStarted:
    pass


@dataclass(frozen=True)
class SpeechStopped:
    pass


@dataclass(frozen=True)
class TextDelta:
    response_id: str
    delta: str


@dataclass(frozen=True)
class ResponseDone:
    response_id: str
    status: str
    text: str
    tokens: TokenUsage


@dataclass(frozen=True)
class ServerError:
    message: str
    code: str | None


ClientEvent = SpeechStarted | SpeechStopped | TextDelta | ResponseDone | ServerError


def session_config(settings: Settings) -> dict[str, Any]:
    if settings.vad == "semantic_vad":
        turn_detection: dict[str, Any] = {"type": "semantic_vad"}
    else:
        turn_detection = {"type": "server_vad", "silence_duration_ms": 500, "threshold": 0.5}
    return {
        "type": "realtime",
        "model": settings.model,
        "output_modalities": ["text"],
        "instructions": PERSONA_PROMPTS[settings.persona],
        "reasoning": {"effort": settings.reasoning_effort},
        "max_output_tokens": settings.max_output_tokens,
        "audio": {
            "input": {
                "format": {"type": "audio/pcm", "rate": SAMPLE_RATE},
                "turn_detection": turn_detection,
            }
        },
    }


class RealtimeClient:
    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._client = AsyncOpenAI(api_key=settings.api_key)
        self._manager: Any = None
        self._conn: Any = None

    async def __aenter__(self) -> RealtimeClient:
        # max_retries=0: reconnects are handled by the app loop so session config is re-sent.
        self._manager = self._client.realtime.connect(model=self._settings.model, max_retries=0)
        self._conn = await self._manager.__aenter__()
        await self._conn.session.update(session=session_config(self._settings))
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        if self._manager is not None:
            await self._manager.__aexit__(*exc_info)
            self._manager = None
            self._conn = None

    async def send_audio(self, pcm: bytes) -> None:
        await self._connection().input_audio_buffer.append(
            audio=base64.b64encode(pcm).decode("ascii")
        )

    async def prompt(self, text: str) -> None:
        """Add a user message and ask for a reply in the session's persona."""
        conn = self._connection()
        await conn.conversation.item.create(
            item={"type": "message", "role": "user", "content": [{"type": "input_text", "text": text}]}
        )
        # No response-level instructions: they would replace the persona and break the prompt cache.
        await conn.response.create()

    async def events(self) -> AsyncIterator[ClientEvent]:
        async for event in self._connection():
            mapped = _map_event(event)
            if mapped is not None:
                yield mapped

    def _connection(self) -> Any:
        if self._conn is None:
            raise RuntimeError("RealtimeClient used outside its context manager")
        return self._conn


def _map_event(event: Any) -> ClientEvent | None:
    kind = event.type
    if kind == "input_audio_buffer.speech_started":
        return SpeechStarted()
    if kind == "input_audio_buffer.speech_stopped":
        return SpeechStopped()
    if kind == "response.output_text.delta":
        return TextDelta(response_id=event.response_id, delta=event.delta)
    if kind == "response.done":
        response = event.response
        return ResponseDone(
            response_id=response.id or "",
            status=response.status or "unknown",
            text=_output_text(response),
            tokens=tokens_from_usage(response.usage),
        )
    if kind == "error":
        error = getattr(event, "error", None)
        return ServerError(
            message=getattr(error, "message", "unknown error"),
            code=getattr(error, "code", None),
        )
    log.debug("realtime event: %s", kind)
    return None


def _output_text(response: Any) -> str:
    parts: list[str] = []
    for item in response.output or []:
        for part in getattr(item, "content", None) or []:
            text = getattr(part, "text", None)
            if getattr(part, "type", None) == "output_text" and text:
                parts.append(text)
    return "".join(parts)
