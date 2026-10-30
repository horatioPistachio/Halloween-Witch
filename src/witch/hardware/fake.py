"""Stand-ins for the GPIO devices so the state machine can run on a laptop."""

from __future__ import annotations

import asyncio
import logging
import sys
import threading
from typing import Self

from witch.hardware.base import ActivationOutput, EdgeLatch

log = logging.getLogger("witch.hardware")


class KeyboardSensor:
    """Treats each line typed on stdin as one motion event."""

    def __init__(self) -> None:
        self._latch = EdgeLatch()

    async def __aenter__(self) -> Self:
        self._latch.bind(asyncio.get_running_loop())
        threading.Thread(target=self._read_stdin, name="keyboard-sensor", daemon=True).start()
        log.info("Keyboard sensor: press Enter to simulate motion")
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        return None

    def _read_stdin(self) -> None:
        while True:
            line = sys.stdin.readline()
            if not line:
                return
            self._latch.signal()

    @property
    def is_active(self) -> bool:
        return False

    async def wait_for_motion(self) -> None:
        await self._latch.wait_fresh()

    async def wait_for_still(self) -> None:
        return None


class LoggingOutput(ActivationOutput):
    """Logs pulses instead of driving a pin."""

    async def _open(self) -> None:
        log.info("%s: (simulated) output ready", self.name)

    async def _set(self, high: bool) -> None:
        log.info("%s: %s", self.name, "HIGH" if high else "low")

    async def _close(self) -> None:
        log.info("%s: (simulated) output closed", self.name)
