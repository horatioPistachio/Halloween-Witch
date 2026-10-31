"""ESP32 backend: serial protocol client, command sequences, polling sensor, eyes, failures and hardware selection.

The board is a fake serial port that speaks the firmware's line protocol, so no hardware is needed.
"""

from __future__ import annotations

import asyncio
import queue
from typing import Any

import pytest

from witch.config import ConfigError, load_settings
from witch.hardware import build_hardware
from witch.hardware.base import HardwareError
from witch.hardware.esp32 import Esp32Eyes, Esp32Link, Esp32MotionSensor, Esp32Output
from witch.hardware.esp32_client import Esp32Client
from witch.hardware.fake import KeyboardSensor, LoggingEyes, LoggingOutput
from witch.hardware.gpio import GpioEyes, GpioMotionSensor, GpioOutput

PIR, POWER, ACTIVATE, EYES = 27, 33, 32, 12
BASE_ENV = {"OPENAI_API_KEY": "sk-test-123", "WITCH_TTS": "off"}


class FakeBoard:
    """A serial port with the protocol v2 firmware behind it: records commands, can hang, fail or vanish."""

    def __init__(self, *, banner: bool = True, version: int = 2) -> None:
        self.commands: list[tuple[Any, ...]] = []
        self.errors: dict[str, str] = {}
        self.hang: set[str] = set()
        self.mute = False
        self.closed = False
        self.resets = 0
        self.broken = False
        self.version = version
        self._rx: queue.Queue[bytes] = queue.Queue()
        if banner:
            self._rx.put(f"READY:{version}\n".encode())

    # SerialLike
    def write(self, data: bytes) -> int:
        if self.broken:
            raise OSError("device disconnected")
        for line in data.decode().splitlines():
            seq, command, *args = line.split(";")
            self.commands.append((command, *(int(a) for a in args)))
            if self.mute or command in self.hang:
                continue
            if command in self.errors:
                self._rx.put(f"ERR:{seq}:{self.errors[command]}\n".encode())
            elif command == "VER":
                self._rx.put(f"OK:{seq}:{self.version}\n".encode())
            else:
                self._rx.put(f"OK:{seq}\n".encode())
        return len(data)

    def read(self, size: int) -> bytes:
        try:
            return self._rx.get(timeout=0.01)
        except queue.Empty:
            if self.broken:
                raise OSError("device disconnected") from None
            return b""

    def close(self) -> None:
        self.closed = True

    def reset_board(self) -> None:
        self.resets += 1

    # Test controls
    def set_state(self, mask: int) -> None:
        self._rx.put(f"PORT:{mask:#x}\n".encode())

    def say(self, line: str) -> None:
        self._rx.put(f"{line}\n".encode())

    def sent(self, *, keepalive: bool = False) -> list[tuple[Any, ...]]:
        return [c for c in self.commands if keepalive or c[0] != "PING"]


def _link(board: FakeBoard, **kwargs: Any) -> Esp32Link:
    options: dict[str, Any] = {"boot_wait_s": 0.3, "call_timeout_s": 0.2, "keepalive_s": 60.0}
    options.update(kwargs)
    return Esp32Link("COM-TEST", serial_factory=lambda port: board, **options)


def _sensor(link: Esp32Link, **kwargs: Any) -> Esp32MotionSensor:
    return Esp32MotionSensor(link, PIR, POWER, poll_s=0.005, **kwargs)


# --- output -----------------------------------------------------------------


def test_output_open_pulse_close_sequence():
    async def scenario() -> None:
        board = FakeBoard()
        output = Esp32Output("witch", _link(board), ACTIVATE)
        async with output:
            await output.pulse(0.01)
        assert board.sent() == [
            ("DEADMAN", 15000),
            ("INIT", ACTIVATE, 2),
            ("RESET", ACTIVATE),
            ("RESET", ACTIVATE),  # __aenter__ drives low
            ("SET", ACTIVATE),
            ("RESET", ACTIVATE),
            ("RESET", ACTIVATE),  # __aexit__
        ]
        assert board.closed

    asyncio.run(scenario())


def test_cancelled_pulse_drives_output_low():
    async def scenario() -> None:
        board = FakeBoard()
        output = Esp32Output("witch", _link(board), ACTIVATE)
        async with output:
            task = asyncio.create_task(output.pulse(30))
            await asyncio.sleep(0.05)
            assert board.sent()[-1] == ("SET", ACTIVATE)
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            assert board.sent()[-1] == ("RESET", ACTIVATE)

    asyncio.run(scenario())


def test_command_timeout_becomes_hardware_error_and_fails_the_link():
    async def scenario() -> None:
        board = FakeBoard()
        link = _link(board)
        output = Esp32Output("witch", link, ACTIVATE)
        async with output:
            board.hang.add("SET")
            with pytest.raises(HardwareError, match="did not answer"):
                await output.pulse(1)
            assert link.failed
            board.hang.clear()
            with pytest.raises(HardwareError):
                await link.call("SET", ACTIVATE)
            # A failed link still lets a low go out, so the witch is never left driven.
            await link.call("RESET", ACTIVATE, force=True)
            assert board.sent()[-1] == ("RESET", ACTIVATE)

    asyncio.run(scenario())


def test_error_reply_is_a_hardware_error_but_not_a_link_failure():
    async def scenario() -> None:
        board = FakeBoard()
        link = _link(board)
        async with Esp32Output("witch", link, ACTIVATE) as output:
            board.errors["SET"] = "NOT_CONFIGURED"
            with pytest.raises(HardwareError, match="NOT_CONFIGURED"):
                await output.pulse(0.01)
            assert not link.failed

    asyncio.run(scenario())


def test_silent_board_is_probed_and_reset_once_then_reported():
    async def scenario() -> None:
        board = FakeBoard(banner=False)
        board.mute = True
        link = _link(board, boot_wait_s=0.02)
        with pytest.raises(HardwareError, match="not responding.*firmware"):
            await Esp32Output("witch", link, ACTIVATE).__aenter__()
        assert [c[0] for c in board.commands] == ["VER", "VER"]
        assert board.resets == 1
        assert board.closed

    asyncio.run(scenario())


def test_board_that_is_already_running_is_accepted_without_a_banner():
    async def scenario() -> None:
        board = FakeBoard(banner=False)
        link = _link(board, boot_wait_s=0.02)
        async with Esp32Output("witch", link, ACTIVATE):
            pass
        assert board.resets == 0
        assert board.commands[0] == ("VER",)

    asyncio.run(scenario())


def test_old_firmware_is_rejected_with_a_reflash_hint():
    async def scenario() -> None:
        board = FakeBoard(version=1)
        with pytest.raises(HardwareError, match="protocol 1.*reflash"):
            await Esp32Output("witch", _link(board), ACTIVATE).__aenter__()
        assert board.closed

    asyncio.run(scenario())


def test_open_failure_explains_busy_port():
    async def scenario() -> None:
        def busy(port: str) -> Any:
            raise OSError("Access is denied")

        link = Esp32Link("COM7", serial_factory=busy, boot_wait_s=0.0)
        with pytest.raises(HardwareError, match="COM7.*serial monitor"):
            await Esp32Output("witch", link, ACTIVATE).__aenter__()

    asyncio.run(scenario())


# --- client and link failure ------------------------------------------------


def test_serial_loss_fails_the_link_at_once():
    async def scenario() -> None:
        board = FakeBoard()
        link = _link(board)
        async with _sensor(link) as sensor:
            waiter = asyncio.create_task(sensor.wait_for_motion())
            await asyncio.sleep(0.02)
            board.broken = True
            with pytest.raises(HardwareError, match="lost"):
                await asyncio.wait_for(waiter, 1)
            assert link.failed

    asyncio.run(scenario())


def test_deadman_event_fails_the_link():
    async def scenario() -> None:
        board = FakeBoard()
        link = _link(board)
        async with _sensor(link) as sensor:
            board.say("DEADMAN")
            with pytest.raises(HardwareError, match="deadman"):
                await asyncio.wait_for(sensor.wait_for_motion(), 1)

    asyncio.run(scenario())


def test_a_board_restart_mid_run_fails_the_link():
    async def scenario() -> None:
        board = FakeBoard()
        link = _link(board)
        async with _sensor(link) as sensor:
            board.say("READY:2")  # the banner is only printed at boot
            with pytest.raises(HardwareError, match="restarted"):
                await asyncio.wait_for(sensor.wait_for_motion(), 1)
            assert link.failed

    asyncio.run(scenario())


def test_keepalive_pings_and_a_silent_board_fails_the_link():
    async def scenario() -> None:
        board = FakeBoard()
        link = _link(board, keepalive_s=0.03)
        async with _sensor(link) as sensor:
            await asyncio.sleep(0.15)
            assert ("PING",) in board.sent(keepalive=True)
            board.hang.add("PING")
            with pytest.raises(HardwareError, match="did not answer"):
                await asyncio.wait_for(sensor.wait_for_motion(), 2)
            with pytest.raises(HardwareError):
                await sensor.wait_for_motion()

    asyncio.run(scenario())


def test_replies_are_matched_by_sequence_number():
    async def scenario() -> None:
        board = FakeBoard()
        lost: list[str] = []
        client = Esp32Client("COM-TEST", lost.append, serial_factory=lambda port: board, call_timeout_s=0.2)
        await client.open()
        board.hang.add("GET")
        slow = asyncio.create_task(client.request("GET", 5))
        await asyncio.sleep(0.02)
        assert await client.request("VER") == "2"
        # A reply for a sequence nobody waits on is ignored, and does not complete the pending request.
        board.say("OK:9999:1")
        await asyncio.sleep(0.03)
        assert not slow.done()
        with pytest.raises(TimeoutError):
            await slow
        await client.close()
        assert not lost

    asyncio.run(scenario())


def test_sequence_numbers_wrap_within_the_protocol_range():
    async def scenario() -> None:
        board = FakeBoard()
        client = Esp32Client("COM-TEST", lambda message: None, serial_factory=lambda port: board)
        await client.open()
        client._seq = 65534
        await client.request("PING")
        await client.request("PING")
        await client.request("PING")
        assert client._seq == 2
        await client.close()

    asyncio.run(scenario())


def test_port_state_is_parsed_from_the_stream():
    async def scenario() -> None:
        board = FakeBoard()
        link = _link(board)
        async with Esp32Output("witch", link, ACTIVATE):
            assert link.port_state() == 0
            board.set_state(0x1A00)
            board.say("garbage that is not protocol")
            await asyncio.sleep(0.05)
            assert link.port_state() == 0x1A00

    asyncio.run(scenario())


# --- sensor -----------------------------------------------------------------


def test_sensor_configures_pins_and_powers_pir():
    async def scenario() -> None:
        board = FakeBoard()
        async with _sensor(_link(board)):
            assert ("INIT", POWER, 2) in board.commands
            assert ("SET", POWER) in board.commands
            assert ("INIT", PIR, 1, 0, 1) in board.commands
        assert board.commands[-1] == ("RESET", POWER)
        assert board.closed

    asyncio.run(scenario())


def test_motion_on_rising_edge_and_still_on_falling():
    async def scenario() -> None:
        board = FakeBoard()
        async with _sensor(_link(board)) as sensor:
            assert not sensor.is_active
            waiter = asyncio.create_task(sensor.wait_for_motion())
            await asyncio.sleep(0.05)
            assert not waiter.done()
            board.set_state(1 << PIR)
            await asyncio.wait_for(waiter, 1)
            assert sensor.is_active
            still = asyncio.create_task(sensor.wait_for_still())
            await asyncio.sleep(0.05)
            assert not still.done()
            board.set_state(0)
            await asyncio.wait_for(still, 1)

    asyncio.run(scenario())


def test_other_pins_do_not_trigger_motion():
    async def scenario() -> None:
        board = FakeBoard()
        async with _sensor(_link(board)) as sensor:
            board.set_state((1 << POWER) | (1 << ACTIVATE))
            with pytest.raises(TimeoutError):
                await asyncio.wait_for(sensor.wait_for_motion(), 0.1)

    asyncio.run(scenario())


def test_edges_before_the_call_are_discarded():
    async def scenario() -> None:
        board = FakeBoard()
        async with _sensor(_link(board)) as sensor:
            board.set_state(1 << PIR)
            await asyncio.sleep(0.1)
            with pytest.raises(TimeoutError):
                await asyncio.wait_for(sensor.wait_for_motion(), 0.1)

    asyncio.run(scenario())


def test_single_sample_glitch_is_debounced():
    async def scenario() -> None:
        board = FakeBoard()
        async with Esp32MotionSensor(_link(board), PIR, None, poll_s=0.05, debounce_samples=2) as sensor:
            waiter = asyncio.create_task(sensor.wait_for_motion())
            await asyncio.sleep(0.01)
            board.set_state(1 << PIR)
            await asyncio.sleep(0.06)  # about one poll high
            board.set_state(0)
            await asyncio.sleep(0.2)
            assert not waiter.done()
            waiter.cancel()

    asyncio.run(scenario())


def test_sensor_output_and_eyes_share_one_connection():
    async def scenario() -> None:
        board = FakeBoard()
        opened: list[str] = []

        def factory(port: str) -> FakeBoard:
            opened.append(port)
            return board

        link = Esp32Link("COM-TEST", serial_factory=factory, boot_wait_s=0.3)
        output = Esp32Output("witch", link, ACTIVATE)
        eyes = Esp32Eyes("eyes", link, EYES, 0.6)
        async with output, _sensor(link), eyes:
            assert not board.closed
        assert opened == ["COM-TEST"]
        assert board.closed

    asyncio.run(scenario())


# --- eyes -------------------------------------------------------------------


def test_eyes_are_high_impedance_until_driven_and_after():
    async def scenario() -> None:
        board = FakeBoard()
        async with Esp32Eyes("eyes", _link(board), EYES, 0.6) as eyes:
            assert board.sent() == [("DEADMAN", 15000), ("HIZ", EYES), ("HIZ", EYES)]
            await eyes.set(1.0)
            await eyes.release()
        assert board.sent()[3:] == [("PWM", EYES, 614), ("HIZ", EYES), ("HIZ", EYES)]

    asyncio.run(scenario())


def test_eye_brightness_is_capped_and_perceptually_curved():
    async def scenario() -> None:
        board = FakeBoard()
        async with Esp32Eyes("eyes", _link(board), EYES, 0.6) as eyes:
            await eyes.set(5.0)  # above the range
            await eyes.set(0.5)
            await eyes.set(-1.0)
        duties = [c[2] for c in board.sent() if c[0] == "PWM"]
        assert duties == [round(0.6 * 1023), round(0.25 * 0.6 * 1023), 0]

    asyncio.run(scenario())


def test_eyes_are_released_even_when_the_link_has_failed():
    async def scenario() -> None:
        board = FakeBoard()
        link = _link(board)
        async with Esp32Eyes("eyes", link, EYES, 0.6) as eyes:
            board.hang.add("PWM")
            with pytest.raises(HardwareError, match="did not answer"):
                await eyes.set(0.5)
            assert link.failed
            board.hang.clear()
            await eyes.release()
            assert board.sent()[-1] == ("HIZ", EYES)

    asyncio.run(scenario())


# --- hardware selection and config ------------------------------------------


def _settings(**env: str):
    return load_settings(environ={**BASE_ENV, **env})


def test_gpio_hardware_is_unchanged_by_default():
    sensor, output, eyes = build_hardware(_settings(WITCH_TRIGGER="pir"))
    assert isinstance(sensor, GpioMotionSensor)
    assert isinstance(output, GpioOutput)
    assert isinstance(eyes, GpioEyes)
    sensor, output, eyes = build_hardware(_settings(WITCH_TRIGGER="keyboard"))
    assert isinstance(sensor, KeyboardSensor)
    assert isinstance(output, LoggingOutput)
    assert isinstance(eyes, LoggingEyes)


def test_esp32_pir_builds_polling_sensor_output_and_eyes():
    sensor, output, eyes = build_hardware(
        _settings(WITCH_TRIGGER="pir", WITCH_HARDWARE="esp32", WITCH_ESP32_PORT="COM7")
    )
    assert isinstance(sensor, Esp32MotionSensor)
    assert isinstance(output, Esp32Output)
    assert isinstance(eyes, Esp32Eyes)


def test_esp32_keyboard_fires_the_real_output_and_eyes():
    sensor, output, eyes = build_hardware(
        _settings(WITCH_TRIGGER="keyboard", WITCH_HARDWARE="esp32", WITCH_ESP32_PORT="COM7")
    )
    assert isinstance(sensor, KeyboardSensor)
    assert isinstance(output, Esp32Output)
    assert isinstance(eyes, Esp32Eyes)


def test_eyes_can_be_switched_off():
    *_, eyes = build_hardware(
        _settings(WITCH_TRIGGER="pir", WITCH_HARDWARE="esp32", WITCH_ESP32_PORT="COM7", WITCH_EYES="off")
    )
    assert eyes is None
    *_, eyes = build_hardware(_settings(WITCH_TRIGGER="pir", WITCH_EYES="off"))
    assert eyes is None


def test_esp32_defaults_and_cli_override():
    settings = load_settings(
        trigger="pir", hardware="esp32", environ={**BASE_ENV, "WITCH_ESP32_PORT": "COM7"}
    )
    assert (settings.hardware, settings.esp32_port) == ("esp32", "COM7")
    assert (settings.pir_pin, settings.pir_power_pin, settings.activate_pin) == (27, 33, 32)
    assert settings.eyes_pin == 12


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
        {"WITCH_EYES_PIN": "34"},
        {"WITCH_EYES_PIN": "9"},
        {"WITCH_EYES_PIN": "32"},
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
