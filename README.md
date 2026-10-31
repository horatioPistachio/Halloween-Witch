# Halloween-Witch

MVP voice loop for an animatronic witch: microphone -> OpenAI Realtime (`gpt-realtime-2.1-mini`, text out) -> tag parser -> console, with cost per interaction.

See [.prompts/design-document-witch-MVP.prompt.md](.prompts/design-document-witch-MVP.prompt.md) for the design.

## Setup (Windows)

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -e ".[dev]"
copy .env.example .env   # then set OPENAI_API_KEY
```

## Setup (Linux / Raspberry Pi)

Use Raspberry Pi OS (64-bit recommended) with Python 3.11 or newer. From the project
checkout, install PortAudio and Python's virtual-environment support:

```bash
sudo apt update
sudo apt install -y python3 python3-venv python3-pip libportaudio2
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e ".[dev]"
cp .env.example .env
```

Edit `.env` and set `OPENAI_API_KEY` and `ELEVEN_LABS_API_KEY`. The ElevenLabs key
is only needed for spoken replies; run `python -m witch --no-tts` to use console
replies without it. Connect your microphone and speaker, then use
`python -m witch --list-devices` to check that Linux detects them.

## Run

```powershell
python -m witch --list-devices
python -m witch                 # default microphone
python -m witch --device "USB" # select by name substring
python -m witch --help
```

Stop with Ctrl+C. Each reply prints its cost and the run total. Every interaction is also appended to `usage.jsonl` (set `WITCH_USAGE_LOG=` to disable).

## Configuration

| Variable | Default | Purpose |
|---|---|---|
| `OPENAI_API_KEY` | required | API key (read from `.env` or environment) |
| `WITCH_MODEL` | `gpt-realtime-2.1-mini` | Realtime model |
| `WITCH_INPUT_DEVICE` | system default | Device index or name substring |
| `WITCH_VAD` | `semantic_vad` | `semantic_vad` or `server_vad` |
| `WITCH_REASONING_EFFORT` | `low` | `minimal` to `xhigh`; lower is faster and cheaper |
| `WITCH_MAX_OUTPUT_TOKENS` | `600` | Per-reply cap on output tokens |
| `WITCH_USAGE_LOG` | `usage.jsonl` | Per-interaction cost log |
| `WITCH_LOG_LEVEL` | `INFO` | Logging level |
| `WITCH_TRIGGER` | `none` | `none` (always listening), `pir` (motion sensor), or `keyboard` (Enter = instant activation, for laptop tests) |
| `WITCH_HARDWARE` | `gpio` | `gpio` (Raspberry Pi pins) or `esp32` (ESP32 Feather over USB serial, see below). Also `--hardware` |
| `WITCH_ESP32_PORT` | required for `esp32` | Serial port of the Feather, for example `COM7` |
| `WITCH_PIR_PIN` | `27` | PIR signal pin (BCM on `gpio`, ESP32 GPIO on `esp32`; same for the next two) |
| `WITCH_PIR_POWER_PIN` | `17` (`33` on `esp32`) | Pin that powers the PIR; empty disables it |
| `WITCH_ACTIVATE_PIN` | `22` (`32` on `esp32`) | Pin that triggers the witch (3.3 V) |
| `WITCH_PIR_WARMUP_S` | `60` | Seconds to ignore the PIR after start-up |
| `WITCH_ACTIVATE_MS` | `1000` | Length of the witch trigger pulse |
| `WITCH_IDLE_TIMEOUT_S` | `30` | Silence that ends a conversation (the witch's speech doesn't count) |
| `WITCH_MAX_CONVERSATION_S` | `120` | Hard cap per activation, counted from the start of the pulse |
| `WITCH_COOLDOWN_S` | `30` | Wait after a conversation before the sensor re-arms |
| `WITCH_SETUP_TIMEOUT_S` | `20` | Maximum time to open the session, mic, speaker and TTS |
| `WITCH_OPENING` | `on` | With `pir` or `keyboard`, the witch speaks first when a conversation starts. `off` (or `--no-opening`) makes her wait for the visitor |
| `WITCH_OPENING_DELAY_MS` | `0` | Wait this long after activation before her opening line (0 to 60000), so she can speak once the jump lands |
| `WITCH_PRESENCE_THRESHOLD_S` | `10` | Seconds of PIR-high needed before she activates. Each activation uses this x a random 0.5 to 1.5. `0` activates on the first blip |
| `WITCH_PRESENCE_DECAY` | `0.5` | Confidence lost per second the PIR is low, so brief visits fade away. `0` never decays |
| `WITCH_AMBIENT_INTERVAL_S` | `300` | Quiet time before she speaks on her own, x a random 0.5 to 1.5 each time. `0` turns it off |
| `WITCH_PHRASE_CACHE_DIR` | `.cache/phrases` | Where her ambient phrases are stored once synthesised. Empty disables the cache |
| `WITCH_EYES` | `on` | Drive the eye LEDs (`pir` or `keyboard` trigger only). `off` or `--no-eyes` disables them |
| `WITCH_EYES_PIN` | `12` | Eye LED pin (GPIO12 on `esp32`, BCM12 on `gpio`). Must differ from the other pins |
| `WITCH_EYES_MAX_DUTY` | `60` | Hard brightness ceiling, percent of full PWM duty (1 to 100) |
| `WITCH_EYES_SPEAK_LEVEL` | `0.6` | Brightness while she speaks, as a fraction of the ceiling |
| `WITCH_EYES_FLICKER_LEVEL` | `0.15` | Brightness of the motion flicker, as a fraction of the ceiling |
| `WITCH_EYES_FLICKER_COOLDOWN_S` | `5` | Minimum time between flickers |
| `WITCH_EYES_PCB_HOLDOFF_S` | `15` | Time from the start of the pulse that her own PCB drives the eyes; our pin is high impedance for it |

## Raspberry Pi run control

With `WITCH_TRIGGER=pir`, the app waits for presence, opens the conversation, pulses the witch, and ends the conversation on silence or the time cap. Then it cools down and re-arms once the PIR goes low.

### Presence confidence

She does not activate on the first PIR blip. While waiting, the app samples the PIR every 0.1 s: each second it is high adds a second of confidence, each second it is low removes `WITCH_PRESENCE_DECAY` of one. She activates when confidence reaches the threshold (`WITCH_PRESENCE_THRESHOLD_S` x a random 0.5 to 1.5, chosen afresh for each activation). Confidence starts from zero after every conversation. The log shows `presence>` lines as it changes. With `--trigger keyboard`, pressing Enter activates her at once.

### Ambient sounds

After `WITCH_AMBIENT_INTERVAL_S` (x 0.5 to 1.5) with no real activity she says one phrase by herself, which keeps a Bluetooth speaker awake and shows she is alive. Audio only: she does not jump. "Real activity" means confidence at 50% of the threshold or more, an Enter press, or an activation; small PIR blips do not reset the timer. The PIR is ignored while she speaks. She does nothing here with `--no-tts`, `WITCH_AMBIENT_INTERVAL_S=0`, or `WITCH_TRIGGER=none`.

The phrases are the `PERIODIC_PHRASES` list in `src/witch/phrases.py`; to add one, add a string to it. Tags such as `[cackle]` are spoken as performance cues with the v3/v4 voice models and removed for others. Each phrase is drawn from a shuffled bag (all before any repeat). The first time a phrase plays it is synthesised by ElevenLabs and saved under `.cache/phrases`, so later plays are free, offline and sound identical. Changing the voice, model or stability makes new audio; delete the folder to start over.

### Opening line

When a conversation starts, the witch speaks first. The app sends one of ten stage cues (`OPENING_PROMPTS` in `src/witch/persona.py`) as a user message, so she opens with a line such as a toll demand or a riddle without waiting for a visitor. Each opening is drawn from a shuffled bag, so all ten appear before any repeats. The log shows each one as `opening> cue N/10 <id>`.

Set `WITCH_OPENING=off` (or pass `--no-opening`) to keep the old behaviour. Set `WITCH_OPENING_DELAY_MS` if she speaks before the jump has landed.

The persona is identical on every activation, so OpenAI's prompt cache can reuse it. The `cached` figure in each cost line shows whether it did. Tune the cue wording by trying all ten with both personas and rewording any that read badly.

Wiring (BCM numbering):

| Signal | GPIO | Notes |
|---|---|---|
| PIR signal | 27 | Must be 3.3 V logic. Set the module's hold time to the minimum. |
| PIR power | 17 | Drives the PIR high at start-up. Leave `WITCH_PIR_POWER_PIN` empty if the module needs 5 V. |
| Witch trigger | 22 | 3.3 V output. Drive the witch's trigger input through a transistor or MOSFET. Don't connect a coil directly. |

Install on the Pi. `lgpio` comes from apt, so the venv needs system site packages:

```bash
sudo apt install -y python3-lgpio
python3 -m venv --system-site-packages .venv
source .venv/bin/activate
python -m pip install -e ".[dev,pi]"
```

Bench test before connecting the witch, with the witch **disconnected**:

```bash
python -m witch --hw-test   # prints PIR level for 10 s, then pulses GPIO22 once
```

Run as a service (edit the user and paths in the unit file first):

```bash
sudo usermod -aG gpio,audio $USER   # then log in again
sudo cp deploy/witch.service /etc/systemd/system/
sudo systemctl enable --now witch
journalctl -u witch -f
```

Troubleshooting:
- `GPIO unavailable`: add the user to the `gpio` group, and check the venv was made with `--system-site-packages`.
- PIR triggers at start-up: raise `WITCH_PIR_WARMUP_S`.
- PIR retriggers constantly or never re-arms: shorten the module's hold time and check the 3.3 V level and GPIO27 wiring.
- No audio under systemd: add the service user to the `audio` group and set `WITCH_INPUT_DEVICE` / `WITCH_OUTPUT_DEVICE`.
- Witch speaks over the jump or before it lands: set `WITCH_OPENING_DELAY_MS` (for example `800`).
- She triggers too easily: set the PIR's hold time to the minimum, then raise `WITCH_PRESENCE_THRESHOLD_S`. A PIR that stays high for several seconds after each blip counts as that much presence. If she never triggers in a busy room, lower the threshold or `WITCH_PRESENCE_DECAY`.
- Ambient sound never plays: check TTS is on, `WITCH_AMBIENT_INTERVAL_S` is not `0`, and look for `ambient>` warnings in the log. The first syllable may be clipped if the Bluetooth speaker was asleep.
- First opening after a quiet spell is slow but quick afterwards: the prompt cache was cold. This is expected.
- `cached 0` on every activation: something changed the prefix. Check that `WITCH_PERSONA` and the prompt text are unchanged between runs.
- Witch says "stage cue" aloud or describes the cue: tighten the rule in `_build_prompt` in `src/witch/persona.py`.

## Laptop + ESP32 Feather V2

When the Pi's input devices are a problem, run the app on a laptop and let an Adafruit ESP32 Feather V2 provide the GPIO over USB serial. The Feather runs `mcu-firmware/ESP32-USB-GPIO-ESPIDF` and The app talks to it through its own serial client (`witch/hardware/esp32_client.py`, built on `pyserial`) using the firmware's line protocol v2, documented in the firmware README. The firmware and the app must be upgraded together: the app refuses an older firmware and says to reflash. The state machine is identical to the Pi's.

Wiring (raw ESP32 GPIO numbers, not BCM):

| Signal | Feather pin | GPIO | Notes |
|---|---|---|---|
| PIR signal | D27 | 27 | Must be 3.3 V logic. |
| PIR power | D33 | 33 | Set `WITCH_PIR_POWER_PIN=` (empty) if the module needs 5 V and feed it from the USB pin. |
| Witch trigger | D32 | 32 | 3.3 V output. Drive the witch through a transistor or MOSFET with a pull-down, so it stays off while the board resets. |

Avoid D14 and D15 (boot strapping or PWM at boot, which can fire the witch on reset), and A2, A3, A4 and D37 (input-only). The app rejects flash pins and input-only output pins. D12 is the exception: it is the eye pin by design (see Eyes below).

```powershell
python -m pip install -e ".[dev,esp32]"
```

`.env`:

```
WITCH_TRIGGER=pir
WITCH_HARDWARE=esp32
WITCH_ESP32_PORT=COM7
```

Close any serial monitor first; only one program can hold the port. Opening the port resets the board, so start-up takes a couple of seconds. Bench test with the witch **disconnected**:

```powershell
python -m witch --hw-test   # prints PIR level for 10 s, ramps the eyes, then pulses GPIO32 once
```

If the PIR misbehaves, `python -m witch --trigger keyboard --hardware esp32` lets Enter stand in for motion while still firing the real witch. To restart after a crash: `while ($true) { python -m witch; Start-Sleep 5 }`.

Troubleshooting:
- `Could not open COM7`: another program holds the port, or the port number changed (see Device Manager).
- `ESP32 ... is not responding` or `stopped answering`: the Feather is not running the firmware, or the cable was pulled. The app exits so a supervisor can restart it.
- `ESP32 firmware speaks protocol N` or `is not responding (is the protocol v2 firmware flashed?)`: reflash `mcu-firmware/ESP32-USB-GPIO-ESPIDF` (`idf.py flash`).
- `ESP32 deadman fired`: the board heard nothing from the app for 15 s and released its outputs; the app exits so a supervisor can restart it.
- Bench-test the pulse on an LED or multimeter before connecting the witch.

## Eyes

The eyes are two LEDs on one pin (GPIO12 on the ESP32, active high), driven with PWM. The witch's own PCB drives the same LEDs, at up to about 2 V, to light them for the jump. So the app never drives the pin low: "off" is **high impedance** (an input with no pulls), and it stays high impedance for `WITCH_EYES_PCB_HOLDOFF_S` from the start of every activation pulse, so the two drivers never fight.

- **Motion:** when the PIR goes high while she waits, the eyes flicker briefly and dimly (about 1.5 s at `WITCH_EYES_FLICKER_LEVEL`), at most once per `WITCH_EYES_FLICKER_COOLDOWN_S`.
- **Speaking:** the eyes are on at `WITCH_EYES_SPEAK_LEVEL` from the first audio of any reply, opening line or ambient phrase until it ends. Speech that starts inside the PCB window waits for the window to end.
- **Ceiling:** every level is a fraction of `WITCH_EYES_MAX_DUTY`, and nothing is ever driven above it. Levels are squared so mid levels look mid. Measure the pin with a meter while running `python -m witch --hw-test` and set the ceiling so the LEDs see what the witch's PCB gives them (about 2 V).
- **Errors:** an eye error is logged once and the eyes switch themselves off for the rest of the run; it never ends a conversation.
- **Raspberry Pi:** `gpiozero.PWMLED` on BCM12 (software PWM through `lgpio`). Going high impedance closes the device, which returns the pin to an input.

GPIO12 is an ESP32 strapping pin (it selects the flash voltage if it is high at reset). The eye pin is high impedance at boot, so the LEDs to ground are safe, but check on the bench that nothing holds GPIO12 high while the board resets, for example the witch's PCB driving the LEDs while the Feather boots. Add a series resistor if the board ever fails to boot with the witch connected.

`--hw-test` ramps the eyes to the ceiling, holds the speaking level, flickers, then releases the pin.

## Tests

```powershell
pytest
```