"""Bench check for the Pi wiring: prints PIR transitions, then fires one activation pulse."""

from __future__ import annotations

import asyncio
import logging
import time

from witch.config import Settings
from witch.hardware import build_hardware

log = logging.getLogger("witch.hwtest")

WATCH_S = 10.0
PULSE_S = 1.0


async def run_selftest(settings: Settings) -> int:
    sensor, output = build_hardware(settings)
    async with sensor, output:
        log.info("Watching PIR on GPIO%d for %.0f s; move in front of it", settings.pir_pin, WATCH_S)
        last = sensor.is_active
        log.info("PIR starts %s", "HIGH" if last else "low")
        deadline = time.monotonic() + WATCH_S
        while time.monotonic() < deadline:
            await asyncio.sleep(0.05)
            now = sensor.is_active
            if now != last:
                log.info("PIR -> %s", "HIGH" if now else "low")
                last = now
        log.info("Pulsing witch activation for %.1f s", PULSE_S)
        await output.pulse(PULSE_S)
        log.info("Self-test finished")
    return 0
