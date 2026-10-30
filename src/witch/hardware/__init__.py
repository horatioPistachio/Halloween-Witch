"""Chooses the sensor and activation output for the configured trigger and hardware."""

from __future__ import annotations

from witch.config import Settings
from witch.hardware.base import ActivationOutput, MotionSensor
from witch.hardware.esp32 import Esp32Link, Esp32MotionSensor, Esp32Output, warn_strapping_pins
from witch.hardware.fake import KeyboardSensor, LoggingOutput
from witch.hardware.gpio import GpioMotionSensor, GpioOutput


def build_hardware(settings: Settings) -> tuple[MotionSensor, ActivationOutput]:
    if settings.trigger not in ("pir", "keyboard"):
        raise ValueError(f"No hardware for trigger {settings.trigger!r}")
    if settings.hardware == "esp32":
        return _build_esp32(settings)
    if settings.trigger == "pir":
        return (
            GpioMotionSensor(settings.pir_pin, settings.pir_power_pin),
            GpioOutput("witch", settings.activate_pin),
        )
    return KeyboardSensor(), LoggingOutput("witch")


def _build_esp32(settings: Settings) -> tuple[MotionSensor, ActivationOutput]:
    assert settings.esp32_port, "load_settings requires WITCH_ESP32_PORT for esp32 hardware"
    link = Esp32Link(settings.esp32_port, probe_pin=settings.activate_pin)
    output = Esp32Output("witch", link, settings.activate_pin)
    if settings.trigger == "keyboard":
        warn_strapping_pins(settings.activate_pin)
        # The keyboard stands in for the PIR, but the witch output is real.
        return KeyboardSensor(), output
    warn_strapping_pins(settings.pir_pin, settings.pir_power_pin, settings.activate_pin)
    return Esp32MotionSensor(link, settings.pir_pin, settings.pir_power_pin), output
