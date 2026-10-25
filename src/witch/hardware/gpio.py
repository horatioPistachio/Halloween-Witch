"""Raspberry Pi GPIO implementations, built on gpiozero (imported lazily so tests run anywhere)."""

from __future__ import annotations

import asyncio
from typing import Any, Self

from witch.hardware.base import ActivationOutput, EdgeLatch, HardwareError

_STILL_POLL_S = 0.05


def _gpiozero() -> Any:
    try:
        import gpiozero
    except ImportError as exc:
        raise HardwareError(
            "gpiozero is not installed; on the Pi run: pip install -e '.[pi]' (see README)"
        ) from exc
    return gpiozero


class GpioMotionSensor:
    """A PIR sensor on a digital input, optionally powered from a GPIO pin."""

    def __init__(self, pin: int, power_pin: int | None = None) -> None:
        self._pin = pin
        self._power_pin = power_pin
        self._latch = EdgeLatch()
        self._power: Any = None
        self._device: Any = None

    async def __aenter__(self) -> Self:
        gz = _gpiozero()
        self._latch.bind(asyncio.get_running_loop())
        try:
            if self._power_pin is not None:
                self._power = gz.DigitalOutputDevice(self._power_pin, initial_value=True)
            self._device = gz.DigitalInputDevice(self._pin, pull_up=False, bounce_time=0.05)
        except Exception as exc:
            self._close_devices()
            raise HardwareError(f"Could not open PIR on GPIO{self._pin}: {exc}") from exc
        self._device.when_activated = self._latch.signal
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        self._close_devices()

    def _close_devices(self) -> None:
        if self._device is not None:
            self._device.close()
            self._device = None
        if self._power is not None:
            self._power.off()
            self._power.close()
            self._power = None

    @property
    def is_active(self) -> bool:
        return bool(self._device and self._device.is_active)

    async def wait_for_motion(self) -> None:
        await self._latch.wait_fresh()

    async def wait_for_still(self) -> None:
        while self.is_active:
            await asyncio.sleep(_STILL_POLL_S)


class GpioOutput(ActivationOutput):
    """A GPIO output that drives the witch's activation input."""

    def __init__(self, name: str, pin: int) -> None:
        super().__init__(name)
        self._pin = pin
        self._device: Any = None

    def _open(self) -> None:
        gz = _gpiozero()
        try:
            self._device = gz.DigitalOutputDevice(self._pin, initial_value=False)
        except Exception as exc:
            raise HardwareError(f"Could not open {self.name} on GPIO{self._pin}: {exc}") from exc

    def _set(self, high: bool) -> None:
        if self._device is None:
            return
        if high:
            self._device.on()
        else:
            self._device.off()

    def _close(self) -> None:
        if self._device is not None:
            self._device.close()
            self._device = None
