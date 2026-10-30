"""ESP32 Feather backend: the PIR sensor and witch output over USB serial.

The firmware (mcu-firmware/ESP32-USB-GPIO-ESPIDF) is driven through the `esp32-usb-gpio` library, which
blocks on every command and gives up on nothing, so every call here runs on one worker thread under a
timeout. The firmware emits no usable edge events, so the sensor polls the port state it streams.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Self

from witch.hardware.base import ActivationOutput, EdgeLatch, HardwareError

log = logging.getLogger("witch.hardware")

# Values of the library's GPIOPinMode and pull enums; the library formats them straight into commands.
_MODE_INPUT = 1
_MODE_OUTPUT = 2
_ENABLE = 1

BOOT_WAIT_S = 2.0
CALL_TIMEOUT_S = 2.0
KEEPALIVE_S = 5.0
POLL_S = 0.02
DEBOUNCE_SAMPLES = 2
STILL_POLL_S = 0.05
STRAPPING_PINS = (0, 2, 12, 15)

GpioFactory = Callable[[str], Any]


def _default_factory(port: str) -> Any:
    try:
        from esp32_usb_gpio import ESP32USBGPIO
    except ImportError as exc:
        raise HardwareError(
            "esp32-usb-gpio is not installed; run: pip install -e '.[esp32]' (see README)"
        ) from exc
    return ESP32USBGPIO(port)


class Esp32Link:
    """One serial connection shared by the sensor and the output; opened by the first user, closed by the last."""

    def __init__(
        self,
        port: str,
        probe_pin: int,
        *,
        gpio_factory: GpioFactory = _default_factory,
        boot_wait_s: float = BOOT_WAIT_S,
        call_timeout_s: float = CALL_TIMEOUT_S,
        keepalive_s: float = KEEPALIVE_S,
    ) -> None:
        self._port = port
        self._probe_pin = probe_pin
        self._factory = gpio_factory
        self._boot_wait_s = boot_wait_s
        self._call_timeout_s = call_timeout_s
        self._keepalive_s = keepalive_s
        self._users = 0
        self._gpio: Any = None
        self._executor: ThreadPoolExecutor | None = None
        self._lock: asyncio.Lock | None = None
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
        """Bitmask of the streamed pin levels (initialised pins only); no serial round trip."""
        if self._gpio is None:
            return 0
        return int(self._gpio.portState()[1])

    async def acquire(self) -> None:
        if self._users == 0:
            await self._open()
        self._users += 1

    async def release(self) -> None:
        self._users -= 1
        if self._users == 0:
            await self._close()

    async def call(self, method: str, *args: int, force: bool = False) -> Any:
        """Run one library command. `force` still tries after a failure, for driving outputs low."""
        if not force:
            self.raise_if_failed()
        if self._gpio is None:
            raise HardwareError("ESP32 link is not open")
        try:
            return await self._invoke(method, *args)
        except TimeoutError:
            raise self._fail(f"ESP32 on {self._port} did not answer {method}{args}") from None
        except OSError as exc:
            raise self._fail(f"ESP32 on {self._port} lost: {exc}") from exc
        except Exception as exc:
            raise HardwareError(f"ESP32 {method}{args} failed: {exc}") from exc

    async def _invoke(self, method: str, *args: int) -> Any:
        assert self._lock is not None and self._executor is not None
        gpio = self._gpio
        loop = asyncio.get_running_loop()
        async with self._lock:
            future = loop.run_in_executor(self._executor, getattr(gpio, method), *args)
            try:
                return await asyncio.wait_for(future, self._call_timeout_s)
            except TimeoutError:
                # The library spins on this flag; setting it frees the worker thread.
                gpio.responseRecv = True
                raise

    def _fail(self, message: str) -> HardwareError:
        if self._error is None:
            self._error = HardwareError(message)
            log.error("%s", message)
            if self._failed is not None:
                self._failed.set()
        return self._error

    async def _open(self) -> None:
        self._lock = asyncio.Lock()
        self._failed = asyncio.Event()
        self._error = None
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="esp32")
        loop = asyncio.get_running_loop()
        try:
            self._gpio = await loop.run_in_executor(self._executor, self._factory, self._port)
        except HardwareError:
            self._shutdown_executor()
            raise
        except Exception as exc:
            self._shutdown_executor()
            raise HardwareError(
                f"Could not open {self._port}: {exc} (close any serial monitor using it)"
            ) from exc
        try:
            # Opening the port resets the ESP32, so give it time to boot before talking to it.
            await asyncio.sleep(self._boot_wait_s)
            await self._handshake()
        except BaseException:
            await self._close()
            raise
        self._keepalive_task = asyncio.create_task(self._keepalive(), name="esp32-keepalive")
        log.info("ESP32 connected on %s", self._port)

    async def _handshake(self) -> None:
        """Configure the probe pin low; if the board stays silent, hard-reset it and retry once."""
        for attempt in (1, 2):
            try:
                await self._invoke("setup", self._probe_pin, _MODE_OUTPUT)
                await self._invoke("reset", self._probe_pin)
                return
            except TimeoutError:
                if attempt == 2:
                    raise HardwareError(f"ESP32 on {self._port} is not responding") from None
                log.warning("ESP32 did not answer; resetting it and retrying")
                with contextlib.suppress(Exception):
                    await self._invoke("hardReset")
                await asyncio.sleep(self._boot_wait_s)
            except OSError as exc:
                raise HardwareError(f"ESP32 on {self._port} lost: {exc}") from exc
            except Exception as exc:
                raise HardwareError(f"ESP32 handshake failed: {exc}") from exc

    async def _keepalive(self) -> None:
        # The library swallows serial errors, so a pulled cable would otherwise freeze the cached state.
        while True:
            await asyncio.sleep(self._keepalive_s)
            try:
                await self._invoke("pinState", self._probe_pin)
            except TimeoutError:
                self._fail(f"ESP32 on {self._port} stopped answering")
                return
            except OSError as exc:
                self._fail(f"ESP32 on {self._port} lost: {exc}")
                return
            except Exception:
                # An error reply still proves the board is alive.
                pass

    async def _close(self) -> None:
        task, self._keepalive_task = self._keepalive_task, None
        if task is not None:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        gpio, self._gpio = self._gpio, None
        if gpio is not None:
            with contextlib.suppress(Exception):
                gpio.close()
        self._shutdown_executor()

    def _shutdown_executor(self) -> None:
        if self._executor is not None:
            self._executor.shutdown(wait=False)
            self._executor = None


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
                await self._link.call("setup", self._power_pin, _MODE_OUTPUT)
                await self._link.call("set", self._power_pin)
            await self._link.call("setup", self._pin, _MODE_INPUT, 0, _ENABLE)
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
                    await self._link.call("reset", self._power_pin, force=True)
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
            await self._link.call("setup", self._pin, _MODE_OUTPUT)
            await self._link.call("reset", self._pin)
        except BaseException:
            self._acquired = False
            await self._link.release()
            raise

    async def _set(self, high: bool) -> None:
        if high:
            await self._link.call("set", self._pin)
            return
        # Leaving the witch driven is the worst failure, so a low gets a second try even on a failed link.
        try:
            await self._link.call("reset", self._pin, force=True)
        except HardwareError:
            log.warning("%s: could not drive the output low; retrying", self.name)
            await self._link.call("reset", self._pin, force=True)

    async def _close(self) -> None:
        if not self._acquired:
            return
        self._acquired = False
        await self._link.release()


def warn_strapping_pins(*pins: int | None) -> None:
    for pin in pins:
        if pin in STRAPPING_PINS:
            log.warning("GPIO%d is an ESP32 strapping pin; the witch may twitch when the board resets", pin)
