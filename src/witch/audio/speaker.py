"""Speaker playback via sounddevice (PortAudio)."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import threading
from collections.abc import AsyncIterator

import sounddevice as sd

from witch.audio.base import SAMPLE_RATE, PlaybackError
from witch.audio.resample import resample
from witch.config import ConfigError, Settings

log = logging.getLogger(__name__)

PREBUFFER_MS = 100
PREBUFFER_BYTES = SAMPLE_RATE * PREBUFFER_MS // 1000 * 2
SILENCE_TAIL = bytes(PREBUFFER_BYTES)


def list_output_devices() -> list[tuple[int, str, int]]:
    """Return (index, name, default sample rate) for every output-capable device."""
    rows = []
    for index, device in enumerate(sd.query_devices()):
        if device["max_output_channels"] > 0:
            rows.append((index, device["name"], int(device["default_samplerate"])))
    return rows


def resolve_output_device(device: int | str | None) -> int | None:
    if device is None or isinstance(device, int):
        return device
    needle = device.lower()
    for index, name, _rate in list_output_devices():
        if needle in name.lower():
            return index
    raise ConfigError(f"No output device name contains '{device}'. Run --list-devices.")


class SpeakerSink:
    """Plays 24 kHz mono PCM16 on one persistent output stream."""

    def __init__(self, settings: Settings) -> None:
        self._device = resolve_output_device(settings.output_device)
        self._device_rate = SAMPLE_RATE
        self._stream: sd.RawOutputStream | None = None
        self._lock = threading.Lock()

    async def __aenter__(self) -> SpeakerSink:
        self._stream = self._open()
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        self._abort()

    async def play(self, chunks: AsyncIterator[bytes]) -> None:
        if self._stream is None:
            self._stream = self._open()
        stream = self._stream
        pending = bytearray()
        started = False
        try:
            async for chunk in chunks:
                if started:
                    await self._write(stream, chunk)
                    continue
                pending += chunk
                if len(pending) >= PREBUFFER_BYTES:
                    started = True
                    await self._write(stream, bytes(pending))
                    pending.clear()
            if pending:
                await self._write(stream, bytes(pending))
            await self._write(stream, SILENCE_TAIL)
        except BaseException:
            self._abort()
            raise

    async def _write(self, stream: sd.RawOutputStream, pcm: bytes) -> None:
        data = pcm if self._device_rate == SAMPLE_RATE else resample(pcm, SAMPLE_RATE, self._device_rate)
        try:
            await asyncio.to_thread(self._locked_write, stream, data)
        except sd.PortAudioError as exc:
            raise PlaybackError(f"speaker write failed: {exc}") from exc

    def _locked_write(self, stream: sd.RawOutputStream, data: bytes) -> None:
        with self._lock:
            stream.write(data)

    def _open(self) -> sd.RawOutputStream:
        try:
            try:
                stream = self._make_stream(SAMPLE_RATE)
                self._device_rate = SAMPLE_RATE
            except sd.PortAudioError as exc:
                native = int(sd.query_devices(self._device, "output")["default_samplerate"])
                log.warning(
                    "Output device rejected %d Hz (%s); playing at %d Hz and resampling",
                    SAMPLE_RATE,
                    exc,
                    native,
                )
                self._device_rate = native
                stream = self._make_stream(native)
            stream.start()
        except sd.PortAudioError as exc:
            raise PlaybackError(f"cannot open output device: {exc}") from exc
        log.info("Speaker open (device=%s, playback rate=%s Hz)", self._device, self._device_rate)
        return stream

    def _make_stream(self, rate: int) -> sd.RawOutputStream:
        return sd.RawOutputStream(samplerate=rate, device=self._device, channels=1, dtype="int16")

    def _abort(self) -> None:
        stream, self._stream = self._stream, None
        if stream is None:
            return
        with contextlib.suppress(sd.PortAudioError):
            stream.abort()
        with self._lock, contextlib.suppress(sd.PortAudioError):
            stream.close()
