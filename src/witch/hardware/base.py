"""Hardware seams for the PIR motion sensor and the witch's activation output."""

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
