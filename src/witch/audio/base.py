"""Audio source contract shared by all platforms."""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Protocol

SAMPLE_RATE = 24_000
CHUNK_MS = 20
CHUNK_SAMPLES = SAMPLE_RATE * CHUNK_MS // 1000


class AudioSource(Protocol):
    async def __aenter__(self) -> AudioSource: ...

    async def __aexit__(self, *exc_info: object) -> None: ...

    def chunks(self) -> AsyncIterator[bytes]:
        """Yield mono 16-bit little-endian PCM at SAMPLE_RATE, even byte lengths."""
        ...
