"""Chooses the sensor and activation output for the configured trigger."""

from __future__ import annotations

from witch.config import Settings
from witch.hardware.base import ActivationOutput, MotionSensor
from witch.hardware.fake import KeyboardSensor, LoggingOutput
from witch.hardware.gpio import GpioMotionSensor, GpioOutput


def build_hardware(settings: Settings) -> tuple[MotionSensor, ActivationOutput]:
    if settings.trigger == "pir":
        return (
            GpioMotionSensor(settings.pir_pin, settings.pir_power_pin),
            GpioOutput("witch", settings.activate_pin),
        )
    if settings.trigger == "keyboard":
        return KeyboardSensor(), LoggingOutput("witch")
    raise ValueError(f"No hardware for trigger {settings.trigger!r}")
