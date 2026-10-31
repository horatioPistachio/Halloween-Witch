"""Serial client for the ESP32 firmware's line protocol v2 (see mcu-firmware/ESP32-USB-GPIO-ESPIDF/README.md).

Every request carries a sequence number and is answered by `OK:<seq>[:value]` or `ERR:<seq>:<NAME>`, so
replies are matched exactly and several requests may be in flight. A reader thread turns serial lines into
loop callbacks. A serial error, a missing reply or the firmware's deadman marks the link lost instead of
leaving cached state to freeze silently.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import re
import threading
from collections.abc import Callable
from typing import Any, Protocol

from witch.hardware.base import HardwareError

log = logging.getLogger("witch.hardware")

PROTOCOL_VERSION = 2
BAUD_RATE = 115200
READ_TIMEOUT_S = 0.1
WRITE_TIMEOUT_S = 1.0
MAX_SEQ = 65535

_REPLY = re.compile(r"(OK|ERR):(\d+)(?::(.*))?")
_READY = re.compile(r"READY:(\d+)")
_PORT = re.compile(r"PORT:(?:0[xX])?([0-9a-fA-F]+)")


class SerialLike(Protocol):
    def write(self, data: bytes) -> Any: ...

    def read(self, size: int) -> bytes:
        """Return available bytes, or b"" after a short timeout."""

    def close(self) -> None: ...

    def reset_board(self) -> None:
        """Pulse the line that resets the ESP32."""


SerialFactory = Callable[[str], SerialLike]


class CommandError(HardwareError):
    """The firmware answered a request with an error."""

    def __init__(self, command: str, name: str) -> None:
        super().__init__(f"ESP32 {command} failed: {name}")
        self.name = name


class LinkLost(OSError):
    """The serial connection broke or the firmware stopped answering."""


class _PySerialPort:
    def __init__(self, port: str) -> None:
        import serial

        self._serial = serial.Serial(port, BAUD_RATE, timeout=READ_TIMEOUT_S, write_timeout=WRITE_TIMEOUT_S)

    def write(self, data: bytes) -> int:
        written = self._serial.write(data)
        self._serial.flush()
        return written

    def read(self, size: int) -> bytes:
        return self._serial.read(size)

    def close(self) -> None:
        self._serial.close()

    def reset_board(self) -> None:
        import time

        self._serial.setDTR(False)
        time.sleep(0.1)
        self._serial.setDTR(True)


def default_serial_factory(port: str) -> SerialLike:
    try:
        import serial  # noqa: F401
    except ImportError as exc:
        raise HardwareError("pyserial is not installed; run: pip install -e '.[esp32]' (see README)") from exc
    return _PySerialPort(port)


class Esp32Client:
    def __init__(
        self,
        port: str,
        on_lost: Callable[[str], None],
        *,
        serial_factory: SerialFactory = default_serial_factory,
        boot_wait_s: float = 3.0,
        call_timeout_s: float = 2.0,
    ) -> None:
        self._port_name = port
        self._on_lost = on_lost
        self._factory = serial_factory
        self._boot_wait_s = boot_wait_s
        self._call_timeout_s = call_timeout_s
        self._serial: SerialLike | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._reader: threading.Thread | None = None
        self._stopping = threading.Event()
        self._pending: dict[int, tuple[str, asyncio.Future[str | None]]] = {}
        self._seq = 0
        self._ready: asyncio.Event | None = None
        self._ready_version: int | None = None
        self._port_state = 0
        self._lost = False
        self._established = False

    @property
    def port_state(self) -> int:
        """Levels of every pin the firmware has configured, as last streamed; no serial round trip."""
        return self._port_state

    @property
    def is_open(self) -> bool:
        return self._serial is not None

    async def open(self) -> None:
        self._loop = asyncio.get_running_loop()
        self._ready = asyncio.Event()
        self._lost = False
        self._established = False
        self._stopping.clear()
        try:
            self._serial = await asyncio.to_thread(self._factory, self._port_name)
        except HardwareError:
            raise
        except Exception as exc:
            raise HardwareError(
                f"Could not open {self._port_name}: {exc} (close any serial monitor using it)"
            ) from exc
        self._reader = threading.Thread(target=self._read_loop, name="esp32-reader", daemon=True)
        self._reader.start()
        try:
            await self._handshake()
        except BaseException:
            await self.close()
            raise
        self._established = True

    async def _handshake(self) -> None:
        """Wait for the boot banner; if the board stays silent, probe it, then hard-reset it once."""
        assert self._ready is not None
        for attempt in (1, 2):
            with contextlib.suppress(TimeoutError):
                # Opening the port resets the board, so a banner normally follows.
                await asyncio.wait_for(self._ready.wait(), self._boot_wait_s)
                self._check_version(self._ready_version)
                return
            try:
                self._check_version(await self._request_version())
                return
            except (TimeoutError, CommandError):
                if attempt == 2:
                    raise HardwareError(
                        f"ESP32 on {self._port_name} is not responding "
                        f"(is the protocol v{PROTOCOL_VERSION} firmware flashed?)"
                    ) from None
                log.warning("ESP32 did not answer; resetting it and retrying")
                await self._reset_board()
            except LinkLost as exc:
                raise HardwareError(str(exc)) from exc

    async def _request_version(self) -> int | None:
        reply = await self.request("VER")
        return int(reply) if reply is not None and reply.isdigit() else None

    def _check_version(self, version: int | None) -> None:
        if version != PROTOCOL_VERSION:
            raise HardwareError(
                f"ESP32 firmware speaks protocol {version}, expected {PROTOCOL_VERSION}; reflash mcu-firmware"
            )

    async def _reset_board(self) -> None:
        assert self._serial is not None and self._ready is not None
        self._ready.clear()
        with contextlib.suppress(Exception):
            await asyncio.to_thread(self._serial.reset_board)

    async def request(self, command: str, *args: int) -> str | None:
        """Send one command and return the reply value. Raises TimeoutError, LinkLost or CommandError."""
        if self._serial is None:
            raise HardwareError("ESP32 link is not open")
        if self._lost:
            raise LinkLost(f"ESP32 on {self._port_name} lost")
        assert self._loop is not None
        self._seq = self._seq % MAX_SEQ + 1
        seq = self._seq
        future: asyncio.Future[str | None] = self._loop.create_future()
        self._pending[seq] = (command, future)
        line = ";".join([str(seq), command, *(str(arg) for arg in args)]) + "\n"
        try:
            try:
                self._serial.write(line.encode())
            except Exception as exc:
                self._lose(f"ESP32 on {self._port_name} lost: {exc}")
                raise LinkLost(str(exc)) from exc
            return await asyncio.wait_for(future, self._call_timeout_s)
        finally:
            self._pending.pop(seq, None)

    async def close(self) -> None:
        serial, self._serial = self._serial, None
        reader, self._reader = self._reader, None
        self._stopping.set()
        self._fail_pending(LinkLost("ESP32 link closed"))
        if serial is not None:
            await asyncio.to_thread(self._shutdown, serial, reader)

    def _shutdown(self, serial: SerialLike, reader: threading.Thread | None) -> None:
        if reader is not None:
            reader.join(1.0)
        with contextlib.suppress(Exception):
            serial.close()

    def _read_loop(self) -> None:
        serial = self._serial
        assert serial is not None
        buffer = b""
        while not self._stopping.is_set():
            try:
                chunk = serial.read(256)
            except Exception as exc:
                if not self._stopping.is_set():
                    self._post(self._lose, f"ESP32 on {self._port_name} lost: {exc}")
                return
            if not chunk:
                continue
            buffer += chunk
            *lines, buffer = buffer.split(b"\n")
            for raw in lines:
                self._post(self._on_line, raw.decode("ascii", "replace").strip())
            # A line that never ends is boot noise, not protocol.
            buffer = buffer[-512:]

    def _post(self, callback: Callable[..., None], *args: Any) -> None:
        loop = self._loop
        if loop is not None and not loop.is_closed():
            with contextlib.suppress(RuntimeError):
                loop.call_soon_threadsafe(callback, *args)

    def _on_line(self, line: str) -> None:
        if (match := _PORT.fullmatch(line)) is not None:
            self._port_state = int(match.group(1), 16)
        elif (match := _REPLY.fullmatch(line)) is not None:
            self._on_reply(match.group(1), int(match.group(2)), match.group(3))
        elif (match := _READY.fullmatch(line)) is not None:
            if self._established:
                # The banner is printed only at boot: the board restarted and forgot its pins and deadman.
                self._lose("ESP32 restarted unexpectedly (brownout or crash); its pin setup was lost")
                return
            self._ready_version = int(match.group(1))
            assert self._ready is not None
            self._ready.set()
        elif line == "DEADMAN":
            self._lose("ESP32 deadman fired: the board stopped hearing from the host and released its outputs")

    def _on_reply(self, kind: str, seq: int, value: str | None) -> None:
        entry = self._pending.get(seq)
        if entry is None:
            if kind == "ERR":
                log.warning("ESP32 rejected a request it could not attribute: %s", value)
            return
        command, future = entry
        if future.done():
            return
        if kind == "OK":
            future.set_result(value)
        else:
            future.set_exception(CommandError(command, value or "unknown error"))

    def _lose(self, message: str) -> None:
        if self._lost:
            return
        self._lost = True
        self._fail_pending(LinkLost(message))
        self._on_lost(message)

    def _fail_pending(self, error: Exception) -> None:
        for _, future in list(self._pending.values()):
            if not future.done():
                future.set_exception(error)
