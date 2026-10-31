"""Ambient sounds: short phrases the witch says by herself after a long quiet spell.

Audio only. Each phrase is synthesised once by ElevenLabs and cached on disk, so it sounds the
same every time and replays cost nothing.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import logging
import os
from collections.abc import AsyncIterator, Callable, Sequence
from pathlib import Path
from typing import Any, Protocol

from witch import tags
from witch.audio.base import SAMPLE_RATE, PlaybackError
from witch.audio.speaker import SpeakerSink
from witch.bag import ShuffleBag
from witch.config import Settings
from witch.phrases import PERIODIC_PHRASES
from witch.tts.base import TTSError
from witch.tts.elevenlabs import ElevenLabsTTS

log = logging.getLogger("witch.ambient")

AMBIENT_TIMEOUT_S = 60.0
CHUNK_BYTES = 8192


class AmbientSound(Protocol):
    async def play_next(self) -> None:
        """Play one phrase. Never raises for TTS, playback or cache failures."""


class PhraseCache:
    """One raw PCM file per phrase, named by a hash of everything that affects the audio."""

    def __init__(self, directory: Path | None) -> None:
        self._directory = directory
        self._warned = False

    @staticmethod
    def key(settings: Settings, speech: str) -> str:
        parts = [
            settings.tts_model,
            settings.voice_id,
            str(settings.tts_stability),
            f"pcm_{SAMPLE_RATE}",
            speech,
        ]
        return hashlib.sha256("\x1f".join(parts).encode()).hexdigest()[:32]

    def _path(self, key: str) -> Path | None:
        return None if self._directory is None else self._directory / f"{key}.pcm"

    async def read(self, key: str) -> bytes | None:
        path = self._path(key)
        if path is None:
            return None
        return await asyncio.to_thread(self._read, path)

    def _read(self, path: Path) -> bytes | None:
        try:
            data = path.read_bytes()
        except FileNotFoundError:
            return None
        except OSError as exc:
            self._warn(f"cannot read {path}: {exc}")
            return None
        if not data or len(data) % 2:
            log.warning("Discarding corrupt cached phrase %s", path.name)
            with contextlib.suppress(OSError):
                path.unlink()
            return None
        return data

    async def write(self, key: str, pcm: bytes) -> None:
        path = self._path(key)
        if path is not None:
            await asyncio.to_thread(self._write, path, pcm)

    def _write(self, path: Path, pcm: bytes) -> None:
        temp = path.with_suffix(".tmp")
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            temp.write_bytes(pcm)
            os.replace(temp, path)
        except OSError as exc:
            self._warn(f"cannot write {path}: {exc}")
            with contextlib.suppress(OSError):
                temp.unlink()

    def _warn(self, message: str) -> None:
        if not self._warned:
            self._warned = True
            log.warning("Phrase cache problem: %s", message)


class AmbientPlayer:
    def __init__(
        self,
        settings: Settings,
        bag: ShuffleBag[str],
        cache: PhraseCache,
        tts_factory: Callable[[Settings], Any] = ElevenLabsTTS,
        sink_factory: Callable[[Settings], Any] = SpeakerSink,
    ) -> None:
        self._settings = settings
        self._bag = bag
        self._cache = cache
        self._tts_factory = tts_factory
        self._sink_factory = sink_factory

    async def play_next(self) -> None:
        phrase = self._bag.next()
        speech = tags.trusted_speech_text(phrase, keep_tags=self._settings.tts_keep_tags)
        if not speech:
            return
        try:
            async with asyncio.timeout(AMBIENT_TIMEOUT_S):
                await self._play(phrase, speech)
        except TTSError as exc:
            log.warning("ambient> TTS failed: %s", exc)
        except PlaybackError as exc:
            log.warning("ambient> playback failed: %s", exc)
        except TimeoutError:
            log.warning("ambient> gave up after %.0f s", AMBIENT_TIMEOUT_S)
        except Exception:
            log.warning("ambient> failed", exc_info=True)

    async def _play(self, phrase: str, speech: str) -> None:
        key = PhraseCache.key(self._settings, speech)
        pcm = await self._cache.read(key)
        async with self._sink_factory(self._settings) as sink:
            if pcm is not None:
                log.info('ambient> "%s" (cached)', phrase)
                await sink.play(_chunks(pcm))
                return
            recorded = bytearray()
            async with self._tts_factory(self._settings) as tts:

                async def audio() -> AsyncIterator[bytes]:
                    async for chunk in tts.synthesize(speech):
                        recorded.extend(chunk)
                        yield chunk

                await sink.play(audio())
        # Reached only when playback finished normally, so a partial clip is never cached.
        if recorded and len(recorded) % 2 == 0:
            await self._cache.write(key, bytes(recorded))
            log.info('ambient> "%s" (synthesised, %d KB cached)', phrase, len(recorded) // 1024)


async def _chunks(pcm: bytes) -> AsyncIterator[bytes]:
    for start in range(0, len(pcm), CHUNK_BYTES):
        yield pcm[start : start + CHUNK_BYTES]


def build_ambient(
    settings: Settings, phrases: Sequence[str] = PERIODIC_PHRASES
) -> AmbientSound | None:
    if settings.ambient_interval_s <= 0:
        log.info("Ambient sounds off (WITCH_AMBIENT_INTERVAL_S=0)")
        return None
    if not settings.tts_enabled:
        log.info("Ambient sounds off (TTS is disabled)")
        return None
    if not phrases:
        log.info("Ambient sounds off (no phrases)")
        return None
    return AmbientPlayer(settings, ShuffleBag(phrases), PhraseCache(settings.phrase_cache_dir))
