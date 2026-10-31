# Plan: Witch MCU Control (laptop + ESP32 Feather V2)

## Goal

Run the witch from the laptop tonight. The laptop supplies mic, speaker and the app. An Adafruit ESP32 Feather V2 on `COM7` supplies the GPIO the laptop lacks: PIR sense, PIR power and the witch activation pulse.

The state machine (`RunController`, `MotionSensor`, `ActivationOutput`) must not change. Only a new hardware backend and its selection are added.

## Decisions

| Topic | Decision |
|---|---|
| Backend selection | `WITCH_HARDWARE=gpio\|esp32` (default `gpio`, so the Pi is unchanged) plus a `--hardware` CLI flag. `WITCH_TRIGGER` keeps meaning what/who starts a conversation. |
| Firmware | Already flashed. **No firmware changes tonight.** Quirks are listed below for later. |
| Library | `esp32-usb-gpio` 0.1.6 (already in `.venv`). Optional extra `esp32`, imported lazily like `gpiozero`. |
| Serial port | `WITCH_ESP32_PORT` (set to `COM7` in `.env`). Required when `WITCH_HARDWARE=esp32`. |
| PIR power | Powered from a GPIO, as on the Pi. |
| Pins | Raw ESP32 GPIO numbers (not BCM). Defaults below. |

### Pin defaults for `esp32` (Feather V2)

| Signal | Feather label | ESP32 GPIO | Why |
|---|---|---|---|
| PIR signal (input) | D27 | 27 | Plain GPIO, no boot behaviour. |
| PIR power (output) | D33 | 33 | Output-capable, quiet at boot. |
| Witch activate (output) | D32 | 32 | Output-capable, quiet at boot. |

Avoided: D12/D15/D14 (boot strapping or PWM at boot, which could fire the witch on reset), A2/A3/A4/D37 (GPIO 34-39 are input-only), GPIO 6-11 (flash), GPIO 0/2 (strapping).

Wiring: the same three signals as the Pi. PIR output must be 3.3 V logic. If the module needs 5 V, leave `WITCH_PIR_POWER_PIN` empty and feed it from the Feather's USB pin. Drive the witch trigger through a transistor or MOSFET with a pull-down at its gate, so it stays off while the ESP32 resets.

## Design

### `hardware/base.py`

`ActivationOutput._open/_set/_close` become `async`. The ESP32 round trip is a blocking serial call, and the event loop streams audio, so it must not block. `GpioOutput`, `LoggingOutput` and `FakeOutput` in `tests/test_run_control.py` get trivial `async` updates. `pulse`, `__aenter__` and `__aexit__` keep their behaviour (always low when idle, low on cancel).

### `hardware/esp32.py` (new)

**`Esp32Link`** owns the single serial connection and wraps `ESP32USBGPIO`.
- Reference-counted `acquire()`/`release()`: the sensor and output share one link; the port opens on first use and closes after the last release (`RunController` exits the output first, then the sensor).
- Constructed with a `gpio_factory` so tests inject a fake (no serial needed).
- Open: build `ESP32USBGPIO(port)` in a worker thread. Opening the port resets the ESP32 (DTR/RTS), so wait about 2 s for boot, then run a handshake `INIT`. If the handshake times out, close, `hardReset()` and retry once, then raise `HardwareError`.
- All library calls run through `_call(fn, *args)`:
  - one `asyncio.Lock` (the library keeps shared per-instance state, so only one command may be in flight);
  - a single-thread `ThreadPoolExecutor`, so the loop never blocks;
  - `asyncio.wait_for(..., timeout=2 s)`. On timeout: set the library's `responseRecv = True` to release the thread spinning inside `_sendCommand`, mark the link failed, raise `HardwareError`. A failed link is never reused.
- Keepalive: every 5 s a `GET` on the PIR pin through `_call`. This detects a yanked cable, because the library's reader thread swallows serial errors and the cached port state would otherwise freeze silently. Failure marks the link failed.
- `raise_if_failed()` is checked by the sensor loop and the output.
- Pin sanity at construction: reject GPIO 6-11, and 34-39 for outputs. Log a warning for strapping pins (0, 2, 12, 15).
- `port_state()` reads the library's cached `IN_GPIO_LEVEL` (no round trip).

**`Esp32MotionSensor`** implements `MotionSensor`.
- Open: acquire the link; if a power pin is set, `INIT` it as output and `SET` it; `INIT` the PIR pin as input with pull-down (same as gpiozero's `pull_up=False`), interrupts disabled.
- The firmware has no usable edge events (its ISR only prints a line the library ignores), so the sensor **polls** the cached port bitmask (`(state >> pin) & 1`) every 20 ms.
- Debounce like gpiozero's 50 ms: two consecutive high samples make a rising edge. The edge signals the existing `EdgeLatch`, so `wait_for_motion` and `wait_for_still` keep their exact semantics (edges before the call are discarded; `wait_for_still` returns once low).
- `is_active` reads the latest sample.
- Close: stop the poll task, `RESET` the power pin, release the link.

**`Esp32Output(ActivationOutput)`**
- `_open`: acquire the link, `INIT` the activate pin as output, `RESET` it.
- `_set(True/False)`: `SET`/`RESET`. `_set(False)` retries once on failure, because leaving the witch latched on is the worst failure.
- `_close`: `RESET`, release the link.

### `hardware/__init__.py`

`build_hardware(settings)`:

| `trigger` | `hardware` | Sensor | Output |
|---|---|---|---|
| `pir` | `gpio` | `GpioMotionSensor` | `GpioOutput` |
| `pir` | `esp32` | `Esp32MotionSensor` | `Esp32Output` |
| `keyboard` | `gpio` | `KeyboardSensor` | `LoggingOutput` (unchanged) |
| `keyboard` | `esp32` | `KeyboardSensor` | `Esp32Output` (bench or fallback: Enter fires the real witch) |

The two ESP32 devices are built around one shared `Esp32Link`.

### `config.py`

- `TRIGGERS` unchanged. Add `HARDWARE = ("gpio", "esp32")`.
- `Settings` gains `hardware: str = "gpio"` and `esp32_port: str | None = None`.
- `load_settings(..., hardware=None)`. Like the other run-control values it is read only when `trigger != none`.
- `WITCH_HARDWARE` and `WITCH_ESP32_PORT` are read in `_parse_run_control`. The port is required when `hardware == esp32` and trigger is `pir` or `keyboard` (clear `ConfigError`).
- Pin defaults and limits depend on the backend: `gpio` stays 27/17/22 with max 27 (BCM); `esp32` uses 27/33/32, with the ESP32 range and output rules above. The duplicate-pin check applies to both.
- The existing `trigger == "pir"` pin checks stay; for `esp32` the pin validation also runs with `keyboard` (the activate pin is used).

### CLI (`__main__.py`)

- `--hardware {gpio,esp32}` passed to `load_settings`. `--hw-test` works with either (`run_selftest` already goes through `build_hardware`).
- Catch `HardwareError` and print `error: ...` with exit code 1, instead of a traceback.
- The `keyboard` trigger's `pir_warmup_s = 0` override stays.

### Packaging and docs

- `pyproject.toml`: `esp32 = ["esp32-usb-gpio>=0.1.6"]`. `requirements.txt` gets the same dependency (the laptop venv installs from it).
- `.env.example`: add `WITCH_HARDWARE`, `WITCH_ESP32_PORT`, and ESP32 pin defaults in comments.
- `README.md`: new "Laptop + ESP32 Feather V2" section (wiring table, `.env` example, `--hw-test`, restart loop, troubleshooting) and two rows in the config table. The existing pin rows say "BCM on gpio, ESP32 GPIO on esp32".

## Tests (no hardware needed)

New `tests/test_esp32.py` with a fake `ESP32USBGPIO` (records commands, holds a settable port state, can hang or fail):
- the command sequence for open, pulse and close (`INIT` args, `SET`/`RESET` order, `RESET` on cancel and on exit);
- bit decoding of the port state, rising-edge synthesis and the 2-sample debounce, `wait_for_motion` discarding old edges, `wait_for_still`;
- timeout becomes `HardwareError`, the stuck waiter is released and the link is marked failed;
- the keepalive failure surfaces from `wait_for_motion`;
- the shared link opens and closes once;
- `build_hardware` matrix.

Update `tests/test_config.py` / `tests/test_run_control.py`: new settings, esp32 pin defaults, port required, pin range rejection, existing `gpio` defaults unchanged. Update the existing `FakeOutput` to `async`. Run the whole `pytest` suite.

## Rollout tonight

1. Install: `.\.venv\Scripts\python.exe -m pip install -e ".[dev,esp32]"` (the library is already in the venv).
2. Close anything holding COM7 (`idf.py monitor`, serial terminals). Only one process can open the port.
3. `.env`: `WITCH_TRIGGER=pir`, `WITCH_HARDWARE=esp32`, `WITCH_ESP32_PORT=COM7`, optionally `WITCH_PIR_WARMUP_S=15` if the PIR is already warm.
4. With the witch **disconnected**: `python -m witch --hw-test` (prints PIR level for 10 s, then pulses GPIO 32). Check the pulse with an LED or multimeter.
5. Fallback if the PIR misbehaves: `--trigger keyboard --hardware esp32` (Enter fires the real witch).
6. Supervise against a crash: `while ($true) { python -m witch; Start-Sleep 5 }`.

## Known quirks (deferred, not fixed tonight)

> **Resolved** by the eyes work ([design-document-witch-flashing-eyes.prompt.md](design-document-witch-flashing-eyes.prompt.md)): firmware protocol v2 (sequence numbers and acks, NULL-safe line parser, host deadman, no ISR, `PORT` only on change) and an in-repo serial client replacing `esp32-usb-gpio`. The list below is kept as history.

Firmware (`mcu-firmware/ESP32-USB-GPIO-ESPIDF`):
- Malformed or truncated commands can crash it: `strtok` results are not NULL-checked.
- A command split across two UART reads is parsed as two bad commands. The 1 ms read timeout makes this possible.
- Outputs stay latched if the host dies between `SET` and `RESET` (the pulse is host-timed). Add a firmware deadman or max-on time.
- The GPIO interrupt path only `printf`s from the ISR, which is unsafe, and emits no parseable event. Hence the polling.
- Every command re-reads and re-prints state on a roughly 1 ms loop, with no sequence or ack IDs. Responses are matched only by pin number.
- `GET` on an uninitialized pin returns error `0x103`.
- `uint8_t recv_len` and `UART_MAX_READ_LEN` leave no room for long commands.
- The port-state line only includes initialized pins.

Library (`esp32_usb_gpio` 0.1.6):
- `_sendCommand` waits forever if no response arrives (for example a command sent during boot). Mitigated here with a timeout and a release hack.
- Its reader thread swallows every exception, so a disconnect looks like a frozen state. Mitigated by the keepalive.
- Shared mutable state means it is not safe for concurrent commands. Mitigated by one lock and one worker thread.
- Errors are only raised if the response pin matches the command pin; a mismatch passes silently. `pinState` can then return `None`.
- There are no edge callbacks. The sensor polls `portState()`.
- Opening the port resets the ESP32 (DTR/RTS), so a 2 s boot wait is needed.
- Possible later work: a purpose-built async client replacing the library; firmware command IDs, ack, deadman and edge-event lines.

## Risks

- `COM7` already open elsewhere gives `HardwareError: could not open COM7`. The message says to close serial monitors.
- Boot or reset glitches on GPIO 32/33: use a pull-down on the witch trigger input.
- Polling latency (20 ms poll plus debounce plus serial) is far below the PIR's own jitter, so it is irrelevant here.
