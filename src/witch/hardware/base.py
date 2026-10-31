"""Hardware seams for the PIR motion sensor, the witch's activation output and her eyes."""

from __future__ import annotations

import asyncio
from abc import ABC, abstractmethod
from typing import Protocol, Self


class HardwareError(RuntimeError):
    """A GPIO device could not be opened or used."""


class MotionSensor(Protocol):
    async def __aenter__(self) -> Self: ...

    async def __aexit__(self, *exc_info: object) -> None: ...

    @property
    def is_active(self) -> bool: ...

    async def wait_for_motion(self) -> None:
        """Return on the next rising edge. Edges that happened before the call are discarded."""

    async def wait_for_still(self) -> None:
        """Return once the sensor level is low (immediately if it already is)."""

    def take_manual_trigger(self) -> bool:
        """True once for each operator-forced activation since the last call. Real sensors return False."""


class EdgeLatch:
    """Carries rising edges from a hardware thread into the event loop."""

    def __init__(self) -> None:
        self._loop: asyncio.AbstractEventLoop | None = None
        self._event: asyncio.Event | None = None

    def bind(self, loop: asyncio.AbstractEventLoop) -> None:
        self._loop = loop
        self._event = asyncio.Event()

    def signal(self) -> None:
        """Safe to call from any thread."""
        if self._loop is not None and self._event is not None:
            self._loop.call_soon_threadsafe(self._event.set)

    async def wait_fresh(self) -> None:
        assert self._event is not None, "EdgeLatch used before bind()"
        self._event.clear()
        await self._event.wait()


class ActivationOutput(ABC):
    """A pin that drives the witch. Always low when not pulsing."""

    def __init__(self, name: str) -> None:
        self.name = name
        self._pulsing = False

    async def __aenter__(self) -> ActivationOutput:
        await self._open()
        try:
            await self._set(False)
        except BaseException:
            await self._close()
            raise
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        try:
            await self._set(False)
        finally:
            await self._close()

    async def pulse(self, duration_s: float) -> None:
        if self._pulsing:
            raise RuntimeError(f"{self.name}: pulse already in progress")
        self._pulsing = True
        try:
            await self._set(True)
            await asyncio.sleep(duration_s)
        finally:
            try:
                # Shielded so a second cancel still lets the output be driven low.
                await asyncio.shield(self._set(False))
            finally:
                self._pulsing = False

    @abstractmethod
    async def _open(self) -> None: ...

    @abstractmethod
    async def _set(self, high: bool) -> None: ...

    @abstractmethod
    async def _close(self) -> None: ...


class EyeLight(ABC):
    """The witch's eyes: LEDs shared with the witch's own PCB, so "off" is high impedance, never low.

    `set` takes a brightness level from 0 to 1 and never drives harder than the ceiling `max_duty`.
    """

    def __init__(self, name: str, max_duty: float) -> None:
        self.name = name
        self._max_duty = max(0.0, min(max_duty, 1.0))

    async def __aenter__(self) -> EyeLight:
        await self._open()
        try:
            await self._release()
        except BaseException:
            await self._close()
            raise
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        try:
            await self._release()
        finally:
            await self._close()

    async def set(self, level: float) -> None:
        # Squared so that mid levels look mid to the eye.
        duty = max(0.0, min(level, 1.0)) ** 2 * self._max_duty
        await self._drive(duty)

    async def release(self) -> None:
        """Stop driving the pin (high impedance) so the witch's PCB can drive the eyes."""
        await self._release()

    @abstractmethod
    async def _open(self) -> None: ...

    @abstractmethod
    async def _drive(self, duty: float) -> None:
        """Drive the pin with PWM; `duty` is a fraction of full scale, already capped."""

    @abstractmethod
    async def _release(self) -> None: ...

    @abstractmethod
    async def _close(self) -> None: ...
