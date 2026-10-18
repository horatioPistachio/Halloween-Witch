"""Text-to-speech contract shared by all providers."""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Protocol


class TTSError(RuntimeError):
    """Synthesis failed. The message never contains an API key."""


class TextToSpeech(Protocol):
    async def __aenter__(self) -> TextToSpeech: ...

    async def __aexit__(self, *exc_info: object) -> None: ...

    def synthesize(self, text: str) -> AsyncIterator[bytes]:
        """Yield mono 16-bit little-endian PCM at SAMPLE_RATE, even byte lengths. Raises TTSError."""
        ...
