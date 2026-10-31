"""Bench check for the wiring: prints PIR transitions, ramps the eyes, then fires one activation pulse."""

from __future__ import annotations

import asyncio
import logging
import time
from contextlib import AsyncExitStack

from witch.config import Settings
from witch.hardware import build_hardware
from witch.hardware.base import EyeLight

log = logging.getLogger("witch.hwtest")

WATCH_S = 10.0
PULSE_S = 1.0
EYE_RAMP_STEPS = 10
EYE_STEP_S = 0.2
EYE_HOLD_S = 2.0


async def _test_eyes(eyes: EyeLight, settings: Settings) -> None:
    log.info("Ramping the eyes up to their ceiling (%.0f%% duty) on GPIO%d", settings.eyes_max_duty, settings.eyes_pin)
    for step in range(EYE_RAMP_STEPS + 1):
        await eyes.set(step / EYE_RAMP_STEPS)
        await asyncio.sleep(EYE_STEP_S)
    log.info("Holding the speaking level (%.2f)", settings.eyes_speak_level)
    await eyes.set(settings.eyes_speak_level)
    await asyncio.sleep(EYE_HOLD_S)
    log.info("Flickering at the dim level (%.2f)", settings.eyes_flicker_level)
    for _ in range(20):
        await eyes.set(settings.eyes_flicker_level)
        await asyncio.sleep(0.05)
        await eyes.set(0.0)
        await asyncio.sleep(0.05)
    await eyes.release()
    log.info("Eyes released (high impedance): the pin should now measure as undriven")


async def run_selftest(settings: Settings) -> int:
    sensor, output, eyes = build_hardware(settings)
    async with AsyncExitStack() as stack:
        await stack.enter_async_context(sensor)
        await stack.enter_async_context(output)
        if eyes is not None:
            await stack.enter_async_context(eyes)
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
        if eyes is not None:
            await _test_eyes(eyes, settings)
        log.info("Pulsing witch activation for %.1f s", PULSE_S)
        await output.pulse(PULSE_S)
        log.info("Self-test finished")
    return 0
