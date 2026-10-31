"""Chooses the sensor, activation output and eyes for the configured trigger and hardware."""

from __future__ import annotations

from typing import NamedTuple

from witch.config import Settings
from witch.hardware.base import ActivationOutput, EyeLight, MotionSensor
from witch.hardware.esp32 import Esp32Eyes, Esp32Link, Esp32MotionSensor, Esp32Output, warn_strapping_pins
from witch.hardware.fake import KeyboardSensor, LoggingEyes, LoggingOutput
from witch.hardware.gpio import GpioEyes, GpioMotionSensor, GpioOutput


class Hardware(NamedTuple):
    sensor: MotionSensor
    output: ActivationOutput
    eyes: EyeLight | None


def build_hardware(settings: Settings) -> Hardware:
    if settings.trigger not in ("pir", "keyboard"):
        raise ValueError(f"No hardware for trigger {settings.trigger!r}")
    if settings.hardware == "esp32":
        return _build_esp32(settings)
    max_duty = settings.eyes_max_duty / 100
    if settings.trigger == "pir":
        return Hardware(
            GpioMotionSensor(settings.pir_pin, settings.pir_power_pin),
            GpioOutput("witch", settings.activate_pin),
            GpioEyes("eyes", settings.eyes_pin, max_duty) if settings.eyes else None,
        )
    return Hardware(
        KeyboardSensor(),
        LoggingOutput("witch"),
        LoggingEyes("eyes", max_duty) if settings.eyes else None,
    )


def _build_esp32(settings: Settings) -> Hardware:
    assert settings.esp32_port, "load_settings requires WITCH_ESP32_PORT for esp32 hardware"
    link = Esp32Link(settings.esp32_port)
    output = Esp32Output("witch", link, settings.activate_pin)
    # The eye pin is deliberately GPIO12 (a strapping pin) and is high impedance at boot, so it is not warned about.
    eyes = Esp32Eyes("eyes", link, settings.eyes_pin, settings.eyes_max_duty / 100) if settings.eyes else None
    if settings.trigger == "keyboard":
        warn_strapping_pins(settings.activate_pin)
        # The keyboard stands in for the PIR, but the witch output is real.
        return Hardware(KeyboardSensor(), output, eyes)
    warn_strapping_pins(settings.pir_pin, settings.pir_power_pin, settings.activate_pin)
    return Hardware(Esp32MotionSensor(link, settings.pir_pin, settings.pir_power_pin), output, eyes)
