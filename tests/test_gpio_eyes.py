"""Pi eyes: PWM through gpiozero, with the pin released (device closed) whenever it is not driven."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from witch.hardware import gpio
from witch.hardware.base import HardwareError
from witch.hardware.gpio import GpioEyes


class FakePwmLed:
    instances: list[FakePwmLed] = []

    def __init__(self, pin: int, initial_value: float = 0.0, frequency: int = 100) -> None:
        self.pin = pin
        self.value = initial_value
        self.frequency = frequency
        self.closed = False
        FakePwmLed.instances.append(self)

    def close(self) -> None:
        self.closed = True


class FakeGpiozero:
    PWMLED = FakePwmLed


@pytest.fixture(autouse=True)
def fake_gpiozero(monkeypatch: pytest.MonkeyPatch) -> None:
    FakePwmLed.instances = []
    monkeypatch.setattr(gpio, "_gpiozero", lambda: FakeGpiozero)


def test_the_pin_is_not_claimed_until_the_eyes_are_driven():
    async def scenario() -> None:
        async with GpioEyes("eyes", 12, 0.6) as eyes:
            assert FakePwmLed.instances == []
            await eyes.set(1.0)
            (device,) = FakePwmLed.instances
            assert (device.pin, device.value) == (12, pytest.approx(0.6))
            await eyes.set(0.5)
            assert device.value == pytest.approx(0.15)
            assert len(FakePwmLed.instances) == 1

    asyncio.run(scenario())


def test_release_closes_the_device_so_the_pin_goes_high_impedance():
    async def scenario() -> None:
        async with GpioEyes("eyes", 12, 0.6) as eyes:
            await eyes.set(1.0)
            await eyes.release()
            assert FakePwmLed.instances[0].closed
            await eyes.set(1.0)
            assert len(FakePwmLed.instances) == 2
        assert FakePwmLed.instances[1].closed

    asyncio.run(scenario())


def test_open_failure_is_a_hardware_error(monkeypatch: pytest.MonkeyPatch):
    class Boom(FakeGpiozero):
        @staticmethod
        def PWMLED(*args: Any, **kwargs: Any) -> Any:  # noqa: N802
            raise OSError("pin busy")

    monkeypatch.setattr(gpio, "_gpiozero", lambda: Boom)

    async def scenario() -> None:
        async with GpioEyes("eyes", 12, 0.6) as eyes:
            with pytest.raises(HardwareError, match="GPIO12"):
                await eyes.set(1.0)

    asyncio.run(scenario())
