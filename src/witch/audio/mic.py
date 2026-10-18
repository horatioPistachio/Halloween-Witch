"""Microphone capture via sounddevice (PortAudio)."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from typing import Any

import sounddevice as sd

from witch.audio.base import CHUNK_MS, CHUNK_SAMPLES, SAMPLE_RATE
from witch.audio.resample import resample
from witch.config import ConfigError, Settings

log = logging.getLogger(__name__)

QUEUE_CHUNKS = 100
OVERFLOW_LOG_INTERVAL_S = 1.0


def list_input_devices() -> list[tuple[int, str, int]]:
    """Return (index, name, default sample rate) for every input-capable device."""
    rows = []
    for index, device in enumerate(sd.query_devices()):
        if device["max_input_channels"] > 0:
            rows.append((index, device["name"], int(device["default_samplerate"])))
    return rows


def resolve_device(device: int | str | None) -> int | None:
    if device is None or isinstance(device, int):
        return device
    needle = device.lower()
    for index, name, _rate in list_input_devices():
        if needle in name.lower():
            return index
    raise ConfigError(f"No input device name contains '{device}'. Run --list-devices.")


class MicSource:
    """Captures the default (or configured) microphone as 24 kHz mono PCM16 chunks."""

    def __init__(self, settings: Settings) -> None:
        self._device = resolve_device(settings.device)
        self._loop: asyncio.AbstractEventLoop | None = None
        self._queue: asyncio.Queue[bytes] | None = None
        self._stream: sd.RawInputStream | None = None
        self._resample_from: int | None = None
        self._last_overflow_log = 0.0

    async def __aenter__(self) -> MicSource:
        self._loop = asyncio.get_running_loop()
        self._queue = asyncio.Queue(maxsize=QUEUE_CHUNKS)
        try:
            self._stream = self._open(SAMPLE_RATE, CHUNK_SAMPLES)
        except sd.PortAudioError as exc:
            native = int(sd.query_devices(self._device, "input")["default_samplerate"])
            log.warning("Device rejected %d Hz (%s); capturing at %d Hz and resampling", SAMPLE_RATE, exc, native)
            self._resample_from = native
            self._stream = self._open(native, native * CHUNK_MS // 1000)
        self._stream.start()
        log.info("Microphone open (device=%s, capture rate=%s Hz)", self._device, self._resample_from or SAMPLE_RATE)
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        if self._stream is not None:
            try:
                self._stream.stop()
            finally:
                self._stream.close()
                self._stream = None

    async def chunks(self) -> AsyncIterator[bytes]:
        assert self._queue is not None, "MicSource used outside its context manager"
        while True:
            yield await self._queue.get()

    def _open(self, rate: int, blocksize: int) -> sd.RawInputStream:
        return sd.RawInputStream(
            samplerate=rate,
            blocksize=blocksize,
            device=self._device,
            channels=1,
            dtype="int16",
            callback=self._on_audio,
        )

    def _on_audio(self, indata: Any, _frames: int, _time: Any, status: sd.CallbackFlags) -> None:
        # Runs on the PortAudio thread: copy the buffer and hand off to the event loop.
        data = bytes(indata)
        if self._resample_from is not None:
            data = resample(data, self._resample_from, SAMPLE_RATE)
        if status and self._loop is not None:
            self._loop.call_soon_threadsafe(log.debug, "audio callback status: %s", status)
        if self._loop is not None:
            self._loop.call_soon_threadsafe(self._enqueue, data)

    def _enqueue(self, data: bytes) -> None:
        assert self._queue is not None and self._loop is not None
        if self._queue.full():
            self._queue.get_nowait()
            now = self._loop.time()
            if now - self._last_overflow_log >= OVERFLOW_LOG_INTERVAL_S:
                log.warning("Audio queue full; dropping oldest chunk")
                self._last_overflow_log = now
        self._queue.put_nowait(data)
