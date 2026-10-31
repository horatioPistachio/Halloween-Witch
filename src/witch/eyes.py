"""The witch's eyes: a dim flicker when motion is seen, steady light while she speaks.

The eye LEDs are shared with the witch's own PCB, which lights them for the jump. So "off" means high
impedance, and during that window (`hold_off`) nothing is driven. All state changes from the rest of the
app are cheap and non-blocking: a driver task does the serial I/O, so a slow board never stalls audio or
the presence loop.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import random
import time
from collections.abc import AsyncIterator, Callable
from contextlib import AsyncExitStack, asynccontextmanager
from enum import Enum
from typing import Protocol, Self

from witch.config import Settings
from witch.hardware.base import EyeLight

log = logging.getLogger("witch.eyes")

FLICKER_DURATION_S = 1.5
FLICKER_STEP_S = (0.04, 0.09)


class Eyes(Protocol):
    async def __aenter__(self) -> Self: ...

    async def __aexit__(self, *exc_info: object) -> None: ...

    def flicker(self) -> None:
        """Motion was seen: flicker briefly and dimly, if nothing else is using the eyes."""

    async def hold_off(self, seconds: float) -> None:
        """The witch's PCB is about to drive the eyes: release the pin and stay off it for `seconds`."""

    def track(self, chunks: AsyncIterator[bytes]) -> contextlib.AbstractAsyncContextManager[AsyncIterator[bytes]]:
        """Wrap audio so the eyes are lit from its first chunk until it ends, however it ends."""


class NullEyes:
    """Eyes that are off: every call is a no-op."""

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        return None

    def flicker(self) -> None:
        return None

    async def hold_off(self, seconds: float) -> None:
        return None

    @asynccontextmanager
    async def track(self, chunks: AsyncIterator[bytes]) -> AsyncIterator[AsyncIterator[bytes]]:
        yield chunks


def build_eyes(settings: Settings, light: EyeLight | None) -> Eyes:
    if light is None:
        return NullEyes()
    return EyeController(
        light,
        speak_level=settings.eyes_speak_level,
        flicker_level=settings.eyes_flicker_level,
        flicker_cooldown_s=settings.eyes_flicker_cooldown_s,
    )


class _Mode(Enum):
    RELEASED = "released"
    SPEAKING = "speaking"
    FLICKER = "flicker"


class EyeController:
    def __init__(
        self,
        light: EyeLight,
        *,
        speak_level: float,
        flicker_level: float,
        flicker_cooldown_s: float,
        flicker_duration_s: float = FLICKER_DURATION_S,
        flicker_step_s: tuple[float, float] = FLICKER_STEP_S,
        clock: Callable[[], float] = time.monotonic,
        rng: random.Random | None = None,
    ) -> None:
        self._light = light
        self._speak_level = speak_level
        self._flicker_level = flicker_level
        self._cooldown_s = flicker_cooldown_s
        self._flicker_duration_s = flicker_duration_s
        self._flicker_step_s = flicker_step_s
        self._clock = clock
        self._rng = rng if rng is not None else random.Random()
        self._stack = AsyncExitStack()
        self._task: asyncio.Task[None] | None = None
        self._wake = asyncio.Event()
        self._io = asyncio.Lock()
        self._speakers = 0
        self._holdoff_until = float("-inf")
        self._flicker_until = float("-inf")
        self._last_flicker = float("-inf")
        # The mode the pin is actually in; the pin starts released when the light opens.
        self._applied = _Mode.RELEASED
        self._disabled = False

    async def __aenter__(self) -> Self:
        await self._stack.enter_async_context(self._light)
        self._task = asyncio.create_task(self._drive(), name="eyes")
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        task, self._task = self._task, None
        if task is not None:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        try:
            await self._stack.aclose()
        except Exception as exc:
            log.warning("eyes> could not release the eye pin cleanly: %s", exc)

    def flicker(self) -> None:
        now = self._clock()
        if (
            self._disabled
            or self._speakers > 0
            or now < self._holdoff_until
            or now < self._flicker_until
            or now - self._last_flicker < self._cooldown_s
        ):
            return
        self._last_flicker = now
        self._flicker_until = now + self._flicker_duration_s
        log.debug("eyes> flicker")
        self._wake.set()

    async def hold_off(self, seconds: float) -> None:
        self._holdoff_until = max(self._holdoff_until, self._clock() + seconds)
        self._flicker_until = float("-inf")
        if not self._disabled:
            # Released here rather than by the driver so the pin is free before the caller starts the pulse.
            try:
                async with self._io:
                    await self._set_mode(_Mode.RELEASED)
            except Exception as exc:
                self._disable(exc)
        if self._disabled:
            await self._release_quietly()
        self._wake.set()

    @asynccontextmanager
    async def track(self, chunks: AsyncIterator[bytes]) -> AsyncIterator[AsyncIterator[bytes]]:
        stream = self._tracked(chunks)
        try:
            yield stream
        finally:
            await stream.aclose()

    async def _tracked(self, chunks: AsyncIterator[bytes]) -> AsyncIterator[bytes]:
        started = False
        try:
            async for chunk in chunks:
                if not started:
                    started = True
                    self._speakers += 1
                    self._flicker_until = float("-inf")
                    self._wake.set()
                yield chunk
        finally:
            if started:
                self._speakers -= 1
                self._wake.set()

    def _mode(self, now: float) -> _Mode:
        if now < self._holdoff_until:
            return _Mode.RELEASED
        if self._speakers > 0:
            return _Mode.SPEAKING
        if now < self._flicker_until:
            return _Mode.FLICKER
        return _Mode.RELEASED

    async def _drive(self) -> None:
        while True:
            self._wake.clear()
            try:
                async with self._io:
                    timeout = await self._step()
            except Exception as exc:
                self._disable(exc)
                await self._release_quietly()
                return
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._wake.wait(), timeout)

    async def _step(self) -> float | None:
        """Bring the pin to the wanted mode. Returns how long to wait before it must be looked at again."""
        now = self._clock()
        mode = self._mode(now)
        if mode is _Mode.FLICKER:
            await self._light.set(self._rng.uniform(0.0, self._flicker_level))
            self._applied = _Mode.FLICKER
            return min(self._rng.uniform(*self._flicker_step_s), max(self._flicker_until - now, 0.0))
        await self._set_mode(mode)
        if mode is _Mode.RELEASED and self._speakers > 0:
            return max(self._holdoff_until - now, 0.0)
        return None

    async def _set_mode(self, mode: _Mode) -> None:
        if mode is self._applied:
            return
        if mode is _Mode.SPEAKING:
            await self._light.set(self._speak_level)
        else:
            await self._light.release()
        self._applied = mode

    async def _release_quietly(self) -> None:
        """Best effort: a pin left driven would fight the witch's PCB, so try even after the eyes gave up."""
        with contextlib.suppress(Exception):
            await self._light.release()

    def _disable(self, exc: Exception) -> None:
        if not self._disabled:
            self._disabled = True
            log.warning("eyes> disabled after a hardware error: %s", exc)
