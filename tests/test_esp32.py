"""ESP32 backend: command sequences, polling sensor, failure handling and hardware selection (no serial)."""

from __future__ import annotations

import asyncio
import time
from typing import Any

import pytest

from witch.config import ConfigError, load_settings
from witch.hardware import build_hardware
from witch.hardware.base import HardwareError
from witch.hardware.esp32 import Esp32Link, Esp32MotionSensor, Esp32Output
from witch.hardware.fake import KeyboardSensor, LoggingOutput
from witch.hardware.gpio import GpioMotionSensor, GpioOutput

PIR, POWER, ACTIVATE = 27, 33, 32
BASE_ENV = {"OPENAI_API_KEY": "sk-test-123", "WITCH_TTS": "off"}


class FakeGpio:
    """Stands in for ESP32USBGPIO: records commands, holds a settable port state, can hang or fail."""

    def __init__(self) -> None:
        self.commands: list[tuple[Any, ...]] = []
        self.state = 0
        self.hang: set[str] = set()
        self.closed = False
        self.responseRecv = False

    def _run(self, name: str, *args: Any) -> None:
        self.commands.append((name, *args))
        if name in self.hang:
            self.responseRecv = False
            while not self.responseRecv:
                time.sleep(0.005)

    def setup(self, pin: int, mode: int, pull_up: int = 0, pull_down: int = 0, intr: int = 0) -> None:
        self._run("setup", pin, mode, pull_up, pull_down)

    def set(self, pin: int) -> None:
        self._run("set", pin)

    def reset(self, pin: int) -> None:
        self._run("reset", pin)

    def pinState(self, pin: int) -> int:
        self._run("pinState", pin)
        return 0

    def hardReset(self) -> None:
        self._run("hardReset")

    def portState(self) -> list[int]:
        return [40, self.state]

    def close(self) -> None:
        self.closed = True


def _link(fake: FakeGpio, **kwargs: Any) -> Esp32Link:
    options: dict[str, Any] = {"boot_wait_s": 0.0, "call_timeout_s": 0.2, "keepalive_s": 60.0}
    options.update(kwargs)
    return Esp32Link("COM-TEST", ACTIVATE, gpio_factory=lambda port: fake, **options)


def _sensor(link: Esp32Link, **kwargs: Any) -> Esp32MotionSensor:
    return Esp32MotionSensor(link, PIR, POWER, poll_s=0.005, **kwargs)


def _commands(fake: FakeGpio) -> list[tuple[Any, ...]]:
    return [c for c in fake.commands if c[0] != "pinState"]


# --- output -----------------------------------------------------------------


def test_output_open_pulse_close_sequence():
    async def scenario() -> None:
        fake = FakeGpio()
        link = _link(fake)
        output = Esp32Output("witch", link, ACTIVATE)
        async with output:
            await output.pulse(0.01)
        assert _commands(fake) == [
            ("setup", ACTIVATE, 2, 0, 0),  # link handshake
            ("reset", ACTIVATE),
            ("setup", ACTIVATE, 2, 0, 0),  # output open
            ("reset", ACTIVATE),
            ("reset", ACTIVATE),  # __aenter__ drives low
            ("set", ACTIVATE),
            ("reset", ACTIVATE),
            ("reset", ACTIVATE),  # __aexit__
        ]
        assert fake.closed

    asyncio.run(scenario())


def test_cancelled_pulse_drives_output_low():
    async def scenario() -> None:
        fake = FakeGpio()
        output = Esp32Output("witch", _link(fake), ACTIVATE)
        async with output:
            task = asyncio.create_task(output.pulse(30))
            await asyncio.sleep(0.05)
            assert _commands(fake)[-1] == ("set", ACTIVATE)
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            assert _commands(fake)[-1] == ("reset", ACTIVATE)

    asyncio.run(scenario())


def test_command_timeout_becomes_hardware_error_and_fails_the_link():
    async def scenario() -> None:
        fake = FakeGpio()
        link = _link(fake)
        output = Esp32Output("witch", link, ACTIVATE)
        async with output:
            fake.hang.add("set")
            with pytest.raises(HardwareError, match="did not answer"):
                await output.pulse(1)
            assert link.failed
            fake.hang.clear()
            with pytest.raises(HardwareError):
                await link.call("set", ACTIVATE)
            # A failed link still lets a low go out, so the witch is never left driven.
            await link.call("reset", ACTIVATE, force=True)
            assert _commands(fake)[-1] == ("reset", ACTIVATE)

    asyncio.run(scenario())


def test_library_error_reply_is_a_hardware_error_but_not_a_link_failure():
    async def scenario() -> None:
        fake = FakeGpio()

        def boom(pin: int) -> None:
            raise Exception("Error setting pin 32: 0x103")

        fake.set = boom  # type: ignore[method-assign]
        link = _link(fake)
        async with Esp32Output("witch", link, ACTIVATE) as output:
            with pytest.raises(HardwareError, match="0x103"):
                await output.pulse(0.01)
            assert not link.failed

    asyncio.run(scenario())


def test_unresponsive_board_is_reset_once_then_reported():
    async def scenario() -> None:
        fake = FakeGpio()
        fake.hang.add("setup")
        link = _link(fake)
        with pytest.raises(HardwareError, match="not responding"):
            await Esp32Output("witch", link, ACTIVATE).__aenter__()
        assert [c[0] for c in fake.commands] == ["setup", "hardReset", "setup"]
        assert fake.closed

    asyncio.run(scenario())


def test_open_failure_explains_busy_port():
    async def scenario() -> None:
        def busy(port: str) -> Any:
            raise OSError("Access is denied")

        link = Esp32Link("COM7", ACTIVATE, gpio_factory=busy, boot_wait_s=0.0)
        with pytest.raises(HardwareError, match="COM7.*serial monitor"):
            await Esp32Output("witch", link, ACTIVATE).__aenter__()

    asyncio.run(scenario())


# --- sensor -----------------------------------------------------------------


def test_sensor_configures_pins_and_powers_pir():
    async def scenario() -> None:
        fake = FakeGpio()
        link = _link(fake)
        async with _sensor(link):
            assert ("setup", POWER, 2, 0, 0) in fake.commands
            assert ("set", POWER) in fake.commands
            assert ("setup", PIR, 1, 0, 1) in fake.commands
        assert fake.commands[-1] == ("reset", POWER)
        assert fake.closed

    asyncio.run(scenario())


def test_motion_on_rising_edge_and_still_on_falling():
    async def scenario() -> None:
        fake = FakeGpio()
        async with _sensor(_link(fake)) as sensor:
            assert not sensor.is_active
            waiter = asyncio.create_task(sensor.wait_for_motion())
            await asyncio.sleep(0.05)
            assert not waiter.done()
            fake.state = 1 << PIR
            await asyncio.wait_for(waiter, 1)
            assert sensor.is_active
            still = asyncio.create_task(sensor.wait_for_still())
            await asyncio.sleep(0.05)
            assert not still.done()
            fake.state = 0
            await asyncio.wait_for(still, 1)

    asyncio.run(scenario())


def test_other_pins_do_not_trigger_motion():
    async def scenario() -> None:
        fake = FakeGpio()
        async with _sensor(_link(fake)) as sensor:
            fake.state = (1 << POWER) | (1 << ACTIVATE)
            with pytest.raises(TimeoutError):
                await asyncio.wait_for(sensor.wait_for_motion(), 0.1)

    asyncio.run(scenario())


def test_edges_before_the_call_are_discarded():
    async def scenario() -> None:
        fake = FakeGpio()
        async with _sensor(_link(fake)) as sensor:
            fake.state = 1 << PIR
            await asyncio.sleep(0.1)
            with pytest.raises(TimeoutError):
                await asyncio.wait_for(sensor.wait_for_motion(), 0.1)

    asyncio.run(scenario())


def test_single_sample_glitch_is_debounced():
    async def scenario() -> None:
        fake = FakeGpio()
        link = _link(fake)
        async with Esp32MotionSensor(link, PIR, None, poll_s=0.05, debounce_samples=2) as sensor:
            waiter = asyncio.create_task(sensor.wait_for_motion())
            await asyncio.sleep(0.01)
            fake.state = 1 << PIR
            await asyncio.sleep(0.06)  # about one poll high
            fake.state = 0
            await asyncio.sleep(0.2)
            assert not waiter.done()
            waiter.cancel()

    asyncio.run(scenario())


def test_keepalive_failure_surfaces_from_wait_for_motion():
    async def scenario() -> None:
        fake = FakeGpio()
        link = _link(fake, keepalive_s=0.05)
        async with _sensor(link) as sensor:
            fake.hang.add("pinState")
            with pytest.raises(HardwareError, match="stopped answering"):
                await asyncio.wait_for(sensor.wait_for_motion(), 2)
            with pytest.raises(HardwareError):
                await sensor.wait_for_motion()

    asyncio.run(scenario())


def test_sensor_and_output_share_one_connection():
    async def scenario() -> None:
        fake = FakeGpio()
        opened: list[str] = []

        def factory(port: str) -> FakeGpio:
            opened.append(port)
            return fake

        link = Esp32Link("COM-TEST", ACTIVATE, gpio_factory=factory, boot_wait_s=0.0)
        output = Esp32Output("witch", link, ACTIVATE)
        sensor = _sensor(link)
        async with output, sensor:
            assert not fake.closed
        assert opened == ["COM-TEST"]
        assert fake.closed

    asyncio.run(scenario())


# --- hardware selection and config ------------------------------------------


def _settings(**env: str):
    return load_settings(environ={**BASE_ENV, **env})


def test_gpio_hardware_is_unchanged_by_default():
    sensor, output = build_hardware(_settings(WITCH_TRIGGER="pir"))
    assert isinstance(sensor, GpioMotionSensor)
    assert isinstance(output, GpioOutput)
    sensor, output = build_hardware(_settings(WITCH_TRIGGER="keyboard"))
    assert isinstance(sensor, KeyboardSensor)
    assert isinstance(output, LoggingOutput)


def test_esp32_pir_builds_polling_sensor_and_output():
    sensor, output = build_hardware(
        _settings(WITCH_TRIGGER="pir", WITCH_HARDWARE="esp32", WITCH_ESP32_PORT="COM7")
    )
    assert isinstance(sensor, Esp32MotionSensor)
    assert isinstance(output, Esp32Output)


def test_esp32_keyboard_fires_the_real_output():
    sensor, output = build_hardware(
        _settings(WITCH_TRIGGER="keyboard", WITCH_HARDWARE="esp32", WITCH_ESP32_PORT="COM7")
    )
    assert isinstance(sensor, KeyboardSensor)
    assert isinstance(output, Esp32Output)


def test_esp32_defaults_and_cli_override():
    settings = load_settings(
        trigger="pir", hardware="esp32", environ={**BASE_ENV, "WITCH_ESP32_PORT": "COM7"}
    )
    assert (settings.hardware, settings.esp32_port) == ("esp32", "COM7")
    assert (settings.pir_pin, settings.pir_power_pin, settings.activate_pin) == (27, 33, 32)


def test_esp32_requires_a_port():
    with pytest.raises(ConfigError, match="WITCH_ESP32_PORT"):
        _settings(WITCH_TRIGGER="pir", WITCH_HARDWARE="esp32")


@pytest.mark.parametrize(
    "extra",
    [
        {"WITCH_HARDWARE": "bogus"},
        {"WITCH_PIR_PIN": "40"},
        {"WITCH_PIR_PIN": "8"},
        {"WITCH_ACTIVATE_PIN": "34"},
        {"WITCH_PIR_POWER_PIN": "36"},
        {"WITCH_ACTIVATE_PIN": "27"},
    ],
)
def test_esp32_invalid_pins_rejected(extra):
    env = {"WITCH_TRIGGER": "pir", "WITCH_HARDWARE": "esp32", "WITCH_ESP32_PORT": "COM7", **extra}
    with pytest.raises(ConfigError):
        _settings(**env)


def test_input_only_pin_is_fine_for_the_pir_signal():
    settings = _settings(
        WITCH_TRIGGER="pir", WITCH_HARDWARE="esp32", WITCH_ESP32_PORT="COM7", WITCH_PIR_PIN="34"
    )
    assert settings.pir_pin == 34


def test_hardware_ignored_when_trigger_is_none():
    assert _settings(WITCH_HARDWARE="bogus").hardware == "gpio"
