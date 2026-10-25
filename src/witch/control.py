"""Run control: waits for motion, runs one conversation per activation, and rearms.

State flow: WARMUP -> WAITING -> ACTIVE -> COOLDOWN -> REARM -> WAITING.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections.abc import Callable
from enum import Enum
from typing import Protocol, Self

from witch.config import Settings
from witch.hardware.base import ActivationOutput, MotionSensor

log = logging.getLogger("witch.control")


class EndReason(Enum):
    SILENCE = "silence"
    MAX_DURATION = "max duration"
    ERROR = "error"
    SETUP_FAILED = "setup failed"


class IdleTimer:
    """Counts idle time since the last activity. Held time (the witch is speaking) does not count."""

    def __init__(self, timeout_s: float, clock: Callable[[], float] = time.monotonic) -> None:
        self._timeout = timeout_s
        self._clock = clock
        self._started = False
        self._held = False
        self._accumulated = 0.0
        self._idle_since = 0.0

    def start(self) -> None:
        self._started = True
        self._held = False
        self._accumulated = 0.0
        self._idle_since = self._clock()

    def touch(self) -> None:
        """Activity happened: restart the idle count."""
        self._accumulated = 0.0
        if self._started and not self._held:
            self._idle_since = self._clock()

    def hold(self) -> None:
        if self._started and not self._held:
            self._accumulated = self._elapsed()
            self._held = True

    def release(self) -> None:
        if self._started and self._held:
            self._held = False
            self._idle_since = self._clock()

    def _elapsed(self) -> float:
        if not self._started:
            return 0.0
        if self._held:
            return self._accumulated
        return self._accumulated + (self._clock() - self._idle_since)

    def expired(self) -> bool:
        return self._elapsed() >= self._timeout

    def seconds_until_deadline(self) -> float:
        return max(0.0, self._timeout - self._elapsed())


class ConversationLike(Protocol):
    async def __aenter__(self) -> Self: ...

    async def __aexit__(self, *exc_info: object) -> None: ...

    async def run(self, timer: IdleTimer | None) -> EndReason: ...

    def summary(self) -> str: ...


ConversationFactory = Callable[[], ConversationLike]


class RunController:
    def __init__(
        self,
        settings: Settings,
        sensor: MotionSensor,
        output: ActivationOutput,
        conversation_factory: ConversationFactory,
        fatal_errors: tuple[type[BaseException], ...],
    ) -> None:
        self._settings = settings
        self._sensor = sensor
        self._output = output
        self._new_conversation = conversation_factory
        self._fatal = fatal_errors

    async def run(self) -> None:
        async with self._output, self._sensor:
            await self._warm_up()
            while True:
                log.info("state> WAITING for motion")
                await self._sensor.wait_for_motion()
                await self._activate()
                log.info("state> COOLDOWN %.0f s", self._settings.cooldown_s)
                await asyncio.sleep(self._settings.cooldown_s)
                log.info("state> REARM: waiting for PIR to go low, then high")
                await self._sensor.wait_for_still()
                # Anything that happened before this call is discarded by wait_for_motion.

    async def _warm_up(self) -> None:
        if self._settings.pir_warmup_s <= 0:
            return
        log.info("state> WARMUP %.0f s (PIR settling; motion ignored)", self._settings.pir_warmup_s)
        await asyncio.sleep(self._settings.pir_warmup_s)

    async def _activate(self) -> None:
        s = self._settings
        conversation = self._new_conversation()
        log.info("state> ACTIVE")
        try:
            async with asyncio.timeout(s.setup_timeout_s):
                await conversation.__aenter__()
        except TimeoutError:
            log.error("Setup took longer than %.0f s; skipping this activation", s.setup_timeout_s)
            return
        except self._fatal:
            raise
        except Exception:
            log.exception("Setup failed; skipping this activation")
            return

        try:
            await self._converse(conversation)
        finally:
            await conversation.__aexit__(None, None, None)
            log.info("%s", conversation.summary())

    async def _converse(self, conversation: ConversationLike) -> None:
        s = self._settings
        timer = IdleTimer(s.idle_timeout_s)
        timer.start()
        pulse = asyncio.create_task(self._output.pulse(s.activate_ms / 1000))
        try:
            reason = await conversation.run(timer)
            log.info("Conversation ended: %s", reason.value)
            await pulse
        except self._fatal:
            raise
        except Exception:
            log.exception("Conversation failed")
        finally:
            pulse.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await pulse
