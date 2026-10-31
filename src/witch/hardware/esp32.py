"""ESP32 Feather backend: the PIR sensor, witch output and eyes over USB serial.

The firmware (mcu-firmware/ESP32-USB-GPIO-ESPIDF) speaks a line protocol that `Esp32Client` implements. The
firmware emits no usable edge events, so the sensor polls the port state it streams.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Self

from witch.hardware.base import ActivationOutput, EdgeLatch, EyeLight, HardwareError
from witch.hardware.esp32_client import (
    Esp32Client,
    LinkLost,
    SerialFactory,
    default_serial_factory,
)

log = logging.getLogger("witch.hardware")

# Values of the firmware's gpio_mode_t.
_MODE_INPUT = 1
_MODE_OUTPUT = 2
_ENABLE = 1

BOOT_WAIT_S = 3.0
CALL_TIMEOUT_S = 2.0
KEEPALIVE_S = 5.0
# The board releases every output if it hears nothing for this long, for example if the host dies.
DEADMAN_MS = 15000
POLL_S = 0.02
DEBOUNCE_SAMPLES = 2
STILL_POLL_S = 0.05
STRAPPING_PINS = (0, 2, 12, 15)
PWM_FULL_SCALE = 1023


class Esp32Link:
    """One serial connection shared by the sensor, output and eyes; opened by the first user, closed by the last."""

    def __init__(
        self,
        port: str,
        *,
        serial_factory: SerialFactory = default_serial_factory,
        boot_wait_s: float = BOOT_WAIT_S,
        call_timeout_s: float = CALL_TIMEOUT_S,
        keepalive_s: float = KEEPALIVE_S,
        deadman_ms: int = DEADMAN_MS,
    ) -> None:
        self._port = port
        self._serial_factory = serial_factory
        self._boot_wait_s = boot_wait_s
        self._call_timeout_s = call_timeout_s
        self._keepalive_s = keepalive_s
        self._deadman_ms = deadman_ms
        self._users = 0
        self._client: Esp32Client | None = None
        self._failed: asyncio.Event | None = None
        self._error: HardwareError | None = None
        self._keepalive_task: asyncio.Task[None] | None = None

    @property
    def failed(self) -> bool:
        return self._error is not None

    def raise_if_failed(self) -> None:
        if self._error is not None:
            raise self._error

    async def wait_failed(self) -> None:
        assert self._failed is not None, "Esp32Link used before acquire()"
        await self._failed.wait()

    def port_state(self) -> int:
        """Bitmask of the streamed pin levels (configured pins only); no serial round trip."""
        return 0 if self._client is None else self._client.port_state

    async def acquire(self) -> None:
        if self._users == 0:
            await self._open()
        self._users += 1

    async def release(self) -> None:
        self._users -= 1
        if self._users == 0:
            await self._close()

    async def call(self, command: str, *args: int, force: bool = False) -> str | None:
        """Run one firmware command. `force` still tries after a failure, for driving outputs low."""
        if not force:
            self.raise_if_failed()
        if self._client is None:
            raise HardwareError("ESP32 link is not open")
        try:
            return await self._client.request(command, *args)
        except TimeoutError:
            raise self._fail(f"ESP32 on {self._port} did not answer {command}{args}") from None
        except LinkLost as exc:
            raise self._fail(f"ESP32 on {self._port} lost: {exc}") from exc

    def _fail(self, message: str) -> HardwareError:
        if self._error is None:
            self._error = HardwareError(message)
            log.error("%s", message)
            if self._failed is not None:
                self._failed.set()
        return self._error

    async def _open(self) -> None:
        self._failed = asyncio.Event()
        self._error = None
        client = Esp32Client(
            self._port,
            self._fail,
            serial_factory=self._serial_factory,
            boot_wait_s=self._boot_wait_s,
            call_timeout_s=self._call_timeout_s,
        )
        await client.open()
        self._client = client
        try:
            await self.call("DEADMAN", self._deadman_ms)
        except BaseException:
            await self._close()
            raise
        self._keepalive_task = asyncio.create_task(self._keepalive(), name="esp32-keepalive")
        log.info("ESP32 connected on %s", self._port)

    async def _keepalive(self) -> None:
        # Feeds the firmware's deadman and notices a board that has stopped answering.
        while True:
            await asyncio.sleep(self._keepalive_s)
            try:
                await self.call("PING")
            except HardwareError:
                return

    async def _close(self) -> None:
        task, self._keepalive_task = self._keepalive_task, None
        if task is not None:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        client, self._client = self._client, None
        if client is not None:
            await client.close()


class Esp32MotionSensor:
    """A PIR sensor on an ESP32 input, optionally powered from an ESP32 pin."""

    def __init__(
        self,
        link: Esp32Link,
        pin: int,
        power_pin: int | None = None,
        *,
        poll_s: float = POLL_S,
        debounce_samples: int = DEBOUNCE_SAMPLES,
    ) -> None:
        self._link = link
        self._pin = pin
        self._power_pin = power_pin
        self._poll_s = poll_s
        self._debounce_samples = debounce_samples
        self._latch = EdgeLatch()
        self._level = False
        self._task: asyncio.Task[None] | None = None
        self._acquired = False

    async def __aenter__(self) -> Self:
        self._latch.bind(asyncio.get_running_loop())
        await self._link.acquire()
        self._acquired = True
        try:
            if self._power_pin is not None:
                await self._link.call("INIT", self._power_pin, _MODE_OUTPUT)
                await self._link.call("SET", self._power_pin)
            await self._link.call("INIT", self._pin, _MODE_INPUT, 0, _ENABLE)
        except BaseException:
            await self._shutdown()
            raise
        self._task = asyncio.create_task(self._poll(), name="esp32-pir-poll")
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        await self._shutdown()

    async def _shutdown(self) -> None:
        task, self._task = self._task, None
        if task is not None:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        if not self._acquired:
            return
        self._acquired = False
        try:
            if self._power_pin is not None:
                try:
                    await self._link.call("RESET", self._power_pin, force=True)
                except HardwareError as exc:
                    log.warning("Could not switch the PIR power off: %s", exc)
        finally:
            await self._link.release()

    async def _poll(self) -> None:
        high_samples = 0
        while True:
            await asyncio.sleep(self._poll_s)
            self._level = bool((self._link.port_state() >> self._pin) & 1)
            if not self._level:
                high_samples = 0
                continue
            high_samples += 1
            if high_samples == self._debounce_samples:
                self._latch.signal()

    @property
    def is_active(self) -> bool:
        return self._level

    async def wait_for_motion(self) -> None:
        waiter = asyncio.ensure_future(self._latch.wait_fresh())
        failure = asyncio.ensure_future(self._link.wait_failed())
        try:
            await asyncio.wait({waiter, failure}, return_when=asyncio.FIRST_COMPLETED)
        finally:
            for task in (waiter, failure):
                task.cancel()
            await asyncio.gather(waiter, failure, return_exceptions=True)
        self._link.raise_if_failed()

    async def wait_for_still(self) -> None:
        while self.is_active:
            self._link.raise_if_failed()
            await asyncio.sleep(STILL_POLL_S)

    def take_manual_trigger(self) -> bool:
        return False


class Esp32Output(ActivationOutput):
    """An ESP32 output that drives the witch's activation input."""

    def __init__(self, name: str, link: Esp32Link, pin: int) -> None:
        super().__init__(name)
        self._link = link
        self._pin = pin
        self._acquired = False

    async def _open(self) -> None:
        await self._link.acquire()
        self._acquired = True
        try:
            await self._link.call("INIT", self._pin, _MODE_OUTPUT)
            await self._link.call("RESET", self._pin)
        except BaseException:
            self._acquired = False
            await self._link.release()
            raise

    async def _set(self, high: bool) -> None:
        if high:
            await self._link.call("SET", self._pin)
            return
        # Leaving the witch driven is the worst failure, so a low gets a second try even on a failed link.
        try:
            await self._link.call("RESET", self._pin, force=True)
        except HardwareError:
            log.warning("%s: could not drive the output low; retrying", self.name)
            await self._link.call("RESET", self._pin, force=True)

    async def _close(self) -> None:
        if not self._acquired:
            return
        self._acquired = False
        await self._link.release()


class Esp32Eyes(EyeLight):
    """The eye LEDs on an ESP32 pin, driven by hardware PWM and released to high impedance when idle."""

    def __init__(self, name: str, link: Esp32Link, pin: int, max_duty: float) -> None:
        super().__init__(name, max_duty)
        self._link = link
        self._pin = pin
        self._acquired = False

    async def _open(self) -> None:
        await self._link.acquire()
        self._acquired = True
        try:
            await self._link.call("HIZ", self._pin)
        except BaseException:
            self._acquired = False
            await self._link.release()
            raise

    async def _drive(self, duty: float) -> None:
        await self._link.call("PWM", self._pin, round(duty * PWM_FULL_SCALE))

    async def _release(self) -> None:
        # Driving against the witch's PCB is the failure to avoid, so a release is attempted even on a failed link.
        await self._link.call("HIZ", self._pin, force=True)

    async def _close(self) -> None:
        if not self._acquired:
            return
        self._acquired = False
        await self._link.release()


def warn_strapping_pins(*pins: int | None) -> None:
    for pin in pins:
        if pin in STRAPPING_PINS:
            log.warning("GPIO%d is an ESP32 strapping pin; the witch may twitch when the board resets", pin)
