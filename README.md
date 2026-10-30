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
| `WITCH_TRIGGER` | `none` | `none` (always listening), `pir` (motion sensor), or `keyboard` (Enter = motion, for laptop tests) |
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
| `WITCH_OPENING_DELAY_MS` | `0` | Wait this long after activation before her opening line (0 to 5000), so she can speak once the jump lands |

## Raspberry Pi run control

With `WITCH_TRIGGER=pir`, the app waits for motion, opens the conversation, pulses the witch, and ends the conversation on silence or the time cap. Then it cools down and re-arms only after the PIR goes low and high again.

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
- First opening after a quiet spell is slow but quick afterwards: the prompt cache was cold. This is expected.
- `cached 0` on every activation: something changed the prefix. Check that `WITCH_PERSONA` and the prompt text are unchanged between runs.
- Witch says "stage cue" aloud or describes the cue: tighten the rule in `_build_prompt` in `src/witch/persona.py`.

## Laptop + ESP32 Feather V2

When the Pi's input devices are a problem, run the app on a laptop and let an Adafruit ESP32 Feather V2 provide the GPIO over USB serial. The Feather runs `mcu-firmware/ESP32-USB-GPIO-ESPIDF` and the app talks to it through the `esp32-usb-gpio` library. The state machine is identical to the Pi's.

Wiring (raw ESP32 GPIO numbers, not BCM):

| Signal | Feather pin | GPIO | Notes |
|---|---|---|---|
| PIR signal | D27 | 27 | Must be 3.3 V logic. |
| PIR power | D33 | 33 | Set `WITCH_PIR_POWER_PIN=` (empty) if the module needs 5 V and feed it from the USB pin. |
| Witch trigger | D32 | 32 | 3.3 V output. Drive the witch through a transistor or MOSFET with a pull-down, so it stays off while the board resets. |

Avoid D12, D14 and D15 (boot strapping or PWM at boot, which can fire the witch on reset), and A2, A3, A4 and D37 (input-only). The app rejects flash pins and input-only output pins.

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
python -m witch --hw-test   # prints PIR level for 10 s, then pulses GPIO32 once
```

If the PIR misbehaves, `python -m witch --trigger keyboard --hardware esp32` lets Enter stand in for motion while still firing the real witch. To restart after a crash: `while ($true) { python -m witch; Start-Sleep 5 }`.

Troubleshooting:
- `Could not open COM7`: another program holds the port, or the port number changed (see Device Manager).
- `ESP32 ... is not responding` or `stopped answering`: the Feather is not running the firmware, or the cable was pulled. The app exits so a supervisor can restart it.
- Bench-test the pulse on an LED or multimeter before connecting the witch.

## Tests

```powershell
pytest
```