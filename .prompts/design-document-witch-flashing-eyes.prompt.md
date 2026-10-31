# Design Document: Witch Flashing Eyes
Continuing to evolve the life like behavior of the witch, we want to be able to control the eyes. The eyes are two Leds that are driven directly from GPIO 12 on the esp32. Active high.

Note that the witches own pcb drives the LEDs directly too however only upto 2v. We should try to mimic this via PWM.


## Eye behaviour
The eyes should be triggers in the following conditions:
- When the witch detects motion via the PIR sensor.
  - The eyes should flicker briefly and dimly.
- When the witch is speaking or reacting at any time  (either with the visitor or from the ambient sounds).
  - The eyes should be on moderately bright. In future implementations we may vary the brightness based on the intensity of the speech or reaction.

---

# Plan

## Decisions

| Topic | Decision |
|---|---|
| Brightness | PWM on the eye pin (ESP32 LEDC, Pi `gpiozero.PWMLED`). A configurable **ceiling** (`WITCH_EYES_MAX_DUTY`, default 60 %, roughly 2 V / 3.3 V) is applied in the hardware layer so nothing can exceed it. The real value is set after measuring with a meter, because it is not yet known whether the eyes have a series resistor. |
| Shared pin (**high-Z**) | The witch's own PCB also drives the eyes (for the jump). Whenever we are not driving, the pin must be **high-Z (input, no pulls), never low**, or our pin and the PCB's driver would fight. "Off" therefore means high-Z. |
| PCB window | The PCB drives the eyes from the start of the activation pulse for about 15 s (`WITCH_EYES_PCB_HOLDOFF_S`, default 15). The app stays high-Z for that whole window, including if speech or flicker is requested inside it. This matches `WITCH_OPENING_DELAY_MS=15000` in `.env`. |
| "Reacting" | The jump pulse is **not** a trigger. The PCB lights the eyes itself. Our triggers are PIR motion and speech only. |
| Speech cue | Eyes on steady and moderately bright while any audio plays: conversation replies, the opening line, ambient phrases. |
| Motion cue | A brief, dim flicker on a PIR rising edge, rate-limited to one per `WITCH_EYES_FLICKER_COOLDOWN_S` (default 5 s). |
| Flicker generation | In Python (testable). Firmware only sets a duty or releases the pin. |
| Backends | ESP32 and Raspberry Pi. The keyboard trigger with `gpio` uses a logging no-op, like `LoggingOutput`. |
| Firmware | Add PWM and high-Z commands, and harden the protocol (see below). The firmware is flashed by the operator (`idf.py flash`). |
| Library | Replace the third-party `esp32-usb-gpio` with an in-repo serial client. That is the only way to fix its quirks and to carry PWM. |

## Design

### Eye controller (`src/witch/eyes.py`, new)

One object owns the eyes and is the only code that touches the light. All its calls are **synchronous and non-blocking** (they set state and wake a driver task), so serial latency can never stall the presence loop or audio.

- `EyeLight` protocol (in `hardware/base.py`): `async open()`, `async set(level)` with `level` 0..1, `async release()` (high-Z), `async close()` (release, then free the pin). Levels are fractions of the ceiling.
- `EyeController(settings, light, clock, sleep, rng)`:
  - `flicker()`: ignored if speaking, inside the PCB window, still inside the cool-down, or already flickering. Otherwise runs about 1.5 s of random brightness steps every 40 to 90 ms, between 0 and `WITCH_EYES_FLICKER_LEVEL`, then releases.
  - `track(chunks)`: an async context manager that wraps the audio iterator given to `sink.play` (`async with eyes.track(audio()) as chunks: await sink.play(chunks)`). Eyes turn on at the **first chunk** (not at the TTS request) and release on exit, so cancel and errors are covered. Calls are reference-counted, so overlapping speakers cannot turn the eyes off early.
  - `hold_off(seconds)` is `async`: it releases the pin before returning, so the pin is free before `_converse` starts the pulse.
  - `hold_off(seconds)`: starts the PCB window. Releases the pin at once, cancels any flicker, and suppresses driving until the window ends. If speech is active when it ends, the speech level is applied then.
  - Priority: PCB window (high-Z), then speaking, then flicker, then high-Z.
  - A light failure is logged once and the eyes are disabled for the run. Eyes are cosmetic and must never end a conversation. A fatal link failure is still reported by the existing sensor and output paths.
  - Brightness uses a simple perceptual curve (`level ** 2`) so mid levels look mid.
- `NullEyes` (all methods no-ops) is used when `WITCH_EYES=off`, so call sites need no `if`.

### Hooks

| Where | Change |
|---|---|
| `control.py` `RunController.run` | Takes `eyes`. Opens it with `async with self._output, self._sensor, self._eyes`, so it is released before the link closes. |
| `control.py` `_await_presence` | On a PIR low to high transition (already detected as `active != was_active`), call `eyes.flicker()`. |
| `control.py` `_converse` | Call `eyes.hold_off(s.eyes_pcb_holdoff_s)` in the same step that starts the pulse. |
| `app.py` `_speak` | Pass the generator through `eyes.track(...)` before `sink.play`. `Conversation` receives `eyes` from `run_controlled`. |
| `ambient.py` `AmbientPlayer._play` | Same wrapper on both the cached and the synthesised path. `build_ambient(settings, eyes)`. |
| `hardware/__init__.py` | `build_hardware` returns a `Hardware(sensor, output, eyes)` named tuple instead of a 2-tuple. Update `app.py`, `selftest.py` and `tests/test_esp32.py`. |
| Trigger `none` | No eyes (no hardware). `Conversation` takes `NullEyes`. |

### Config (`config.py`, `.env.example`, README table)

| Variable | Default | Notes |
|---|---|---|
| `WITCH_EYES` | `on` | `off` or `--no-eyes` disables. Only read when `trigger != none`. |
| `WITCH_EYES_PIN` | `12` | GPIO12 on the ESP32; BCM 12 on the Pi. Must differ from the other pins. Output-capable pins only. |
| `WITCH_EYES_MAX_DUTY` | `60` | Percent ceiling, 1 to 100. Set after measuring. |
| `WITCH_EYES_SPEAK_LEVEL` | `0.6` | Fraction of the ceiling. |
| `WITCH_EYES_FLICKER_LEVEL` | `0.15` | Fraction of the ceiling. |
| `WITCH_EYES_FLICKER_COOLDOWN_S` | `5` | Minimum time between flickers. |
| `WITCH_EYES_PCB_HOLDOFF_S` | `15` | Time from the start of the pulse that the pin stays high-Z. |

The ESP32 strapping-pin warning is not raised for the eye pin: it is deliberate, and the pin is high-Z at boot. The warning stays for other pins. README text that says to avoid D12 changes to "D12 is the eyes pin; the LED path must not hold it high while the board resets".

### Raspberry Pi (`hardware/gpio.py`)

`GpioEyes` uses `gz.PWMLED(pin)` with `value = duty * ceiling`. It cannot go high-Z while a device exists, so `release()` **closes** the device, which returns the pin to an input, and the next `set()` recreates it. `close()` does the same.

### ESP32 serial client (`hardware/esp32_client.py`, new; replaces the library)

Built on `pyserial` (already a dependency of the library). It replaces the executor, lock, timeout hack and keepalive workaround in `Esp32Link`. `Esp32Link` keeps its public API (`acquire`, `release`, `call`, `port_state`, `wait_failed`, `raise_if_failed`), so `Esp32MotionSensor` and `Esp32Output` change very little.

| Library quirk | Fix in the client |
|---|---|
| `_sendCommand` spins forever | Every request waits on a per-sequence future with a timeout. No shared flag. |
| The reader swallows all exceptions | A reader thread posts lines to the loop. A serial error or EOF marks the link failed at once, so a pulled cable is detected without polling. |
| Not safe for concurrent commands | Requests carry a sequence number and are matched by it, so replies cannot be confused. |
| Errors raised only if the pin matches; `pinState` can return `None` | Success and error replies are matched by sequence. Errors always raise `HardwareError`. |
| Fixed 2 s boot sleep | Wait for the firmware's `READY` line (with timeout), then `PING`. |

New methods: `pwm(pin, duty)` and `hiz(pin)`.

### Firmware protocol v2 (`mcu-firmware/ESP32-USB-GPIO-ESPIDF/main`)

Line-based ASCII, `\n` terminated. Request: `<seq>;<CMD>[;args]`. Reply: `OK:<seq>[:value]` or `ERR:<seq>:<NAME>`. Unsolicited: `READY:<version>` at boot, `PORT:<hex>` and `DEADMAN` events.

| Command | Effect |
|---|---|
| `INIT;pin;mode[;pull_up;pull_down]` | As today. The interrupt argument is removed. |
| `SET`, `RESET`, `TOGGLE`, `GET` | As today. `GET` on an unconfigured pin returns `ERR:<seq>:NOT_CONFIGURED`. |
| `PWM;pin;duty` | LEDC at 5 kHz, 10-bit (0 to 1023). Routes the pin to LEDC and drives it. |
| `HIZ;pin` | `ledc_stop` and the pin set to input with no pulls. Used for "off" on the eye pin. |
| `PING` | Resets the deadman. |
| `DEADMAN;ms` | Arms a host watchdog (0 disables). |

Firmware quirks fixed at the same time:

| Quirk | Fix |
|---|---|
| `strtok` results not NULL-checked | One tokenising helper checks every field and range. Bad input gives `ERR:<seq>:BAD_ARG`. |
| Command split across UART reads; `uint8_t recv_len`; 256-byte limit | Accumulate into a line buffer across reads (`size_t`), then parse on `\n`. A line over the limit is discarded with `ERR:...:TOO_LONG`. |
| Outputs latch if the host dies | Deadman: with no valid command (the host `PING`s every 5 s) for `DEADMAN` ms (host sets 15 000), all outputs go low, every PWM pin goes high-Z, and `DEADMAN` is printed. |
| ISR only `printf`s | Interrupt support is removed. The host polls, so nothing depended on it. |
| State re-read and printed about every 1 ms, no acks | `PORT:` is printed only on change and once a second. Every command has a sequence number and an ack. |
| Port state omits uninitialised pins | The line covers every pin the firmware has configured as input or output. |

### Self test (`hardware/selftest.py`)

`--hw-test` gains an eyes step: ramp 0 to the ceiling, hold at the speak level, flicker once, then release. Bench test with a meter on the pin and the witch **disconnected**: check the ceiling voltage and that the pin reads high-Z (no drive) when released.

## Tests (no hardware)

- `tests/test_eyes.py` (new): flicker is rate-limited and dim; flicker is dropped while speaking or inside the PCB window; speech wins over and cancels a flicker; speech started inside the window waits for it to end; reference-counted speakers; `track` releases on cancel and error; a light failure disables the eyes without raising; the pin is released on exit; the ceiling and curve are applied.
- `tests/test_esp32.py`: rewrite the fake as a fake serial transport. Cover: framing and sequence matching, timeout fails the link, serial EOF fails the link, deadman and `PING` traffic, `pwm` and `hiz` command text, and that eye commands never delay a pulse `SET`.
- `tests/test_run_control.py`: flicker on a PIR rising edge, `hold_off` called with the pulse; `FakeEyes` records calls.
- `tests/test_ambient.py`: ambient audio goes through `track`.
- `tests/test_config.py`: new variables, defaults, ranges, duplicate-pin rejection, eyes off.
- Pi: `GpioEyes` against a fake gpiozero (PWM value, close on release).
- Firmware: the parser (tokeniser, line buffer) goes in a pure C file and is covered by a host test if a C compiler is available. Otherwise it is covered by the bench test. `idf.py build` must pass.

Run the full `pytest` suite at the end.

## Docs

- README: config rows, an "Eyes" section (wiring, high-Z reasoning, ceiling tuning, flicker behaviour), the updated D12 note, and the new troubleshooting lines.
- `.env.example`: the new variables.
- `mcu-firmware/.../README.md`: the new protocol and commands.
- `design-document-witch-mcu-control.prompt.md`: mark the "Known quirks" items resolved.
- `pyproject.toml`, `requirements.txt`: replace `esp32-usb-gpio` with `pyserial`.

## Build order

1. Firmware protocol v2 (parser, deadman, PWM, HIZ, `PORT` on change). Build it, then flash.
2. `esp32_client.py`, then port `Esp32Link`, the sensor and the output onto it. The existing behaviour tests pass again.
3. `EyeLight` implementations: `Esp32Eyes`, `GpioEyes`, `LoggingEyes`.
4. `EyeController` and `NullEyes`, with its tests.
5. Config, `Hardware` tuple, and hooks in `control.py`, `app.py` and `ambient.py`.
6. Self test, docs, full test run, then bench test on the real eyes.

## Risks and open items

- **GPIO12 strapping.** If the LED path holds GPIO12 high while the ESP32 resets, flash voltage selection can break boot. The eye pin is high-Z during reset, but the witch PCB driving the eyes through the LED during a reset could pull it up. Check on the bench, and add a series resistor if needed.
- **Ceiling.** 60 % is an assumption until the eyes are measured.
- **PCB window.** The 15 s figure is from memory; time it on the real witch and adjust `WITCH_EYES_PCB_HOLDOFF_S`.
- **Pi PWM.** `gpiozero` with `lgpio` does software PWM. That is fine for dim eyes, but check for visible flicker. The Pi default pin is 12 for symmetry; change it if it clashes with the wiring.
- **Firmware reflash.** The ESP32 firmware and the Python client must be upgraded together. An old firmware will not answer the new protocol, and the client fails clearly on a missing `READY` line.
