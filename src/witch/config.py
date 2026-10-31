"""Settings from environment variables, the repo .env file and CLI overrides."""

from __future__ import annotations

import logging
import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

from witch.persona import PERSONA_PROMPTS

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_MODEL = "gpt-realtime-2.1-mini"
DEFAULT_PERSONA = "kids"
DEFAULT_USAGE_LOG = "usage.jsonl"
DEFAULT_MAX_OUTPUT_TOKENS = 600
VAD_TYPES = ("semantic_vad", "server_vad")
REASONING_EFFORTS = ("minimal", "low", "medium", "high", "xhigh")
VOICES = {
    "seer": "7NsaqHdLuKNFvEfjpUno",
    "witch": "xBIfEFAROTtux2FvqTLj",
    "lilith": "mLw8kuDeVGqVstOYjRII",
    "selna": "2aL479c8D3QMIPExj0tw",
    "gork": "PRfCKe8kdrG3nuXOAnoH",
    "morinia":"xSw6WlNtwj2sWByfcnB9",
}
DEFAULT_VOICE = "witch"
DEFAULT_VOICE_ID = VOICES[DEFAULT_VOICE]
DEFAULT_TTS_MODEL = "eleven_v4_turbo"
DEFAULT_GATE_TAIL_MS = 400
TAG_CAPABLE_TTS_PREFIXES = ("eleven_v3", "eleven_v4")
TRIGGERS = ("none", "pir", "keyboard")
HARDWARE = ("gpio", "esp32")
MAX_BCM_PIN = 27
DEFAULT_PIR_PIN = 27
DEFAULT_PIR_POWER_PIN = 17
DEFAULT_ACTIVATE_PIN = 22
# Raw ESP32 GPIO numbers on the Feather V2 (D27, D33, D32): none are strapping, input-only or flash pins.
DEFAULT_ESP32_PIR_PIN = 27
DEFAULT_ESP32_PIR_POWER_PIN = 33
DEFAULT_ESP32_ACTIVATE_PIN = 32
MAX_ESP32_PIN = 39
ESP32_FLASH_PINS = range(6, 12)
ESP32_INPUT_ONLY_PINS = range(34, 40)
# GPIO12 on the ESP32 Feather (D12) and BCM12 on the Pi: both are output capable.
DEFAULT_EYES_PIN = 12
DEFAULT_EYES_MAX_DUTY = 60.0
DEFAULT_EYES_SPEAK_LEVEL = 0.6
DEFAULT_EYES_FLICKER_LEVEL = 0.15
DEFAULT_EYES_FLICKER_COOLDOWN_S = 5.0
DEFAULT_EYES_PCB_HOLDOFF_S = 15.0
DEFAULT_PIR_WARMUP_S = 60.0
DEFAULT_ACTIVATE_MS = 1000
DEFAULT_IDLE_TIMEOUT_S = 30.0
DEFAULT_MAX_CONVERSATION_S = 120.0
DEFAULT_COOLDOWN_S = 30.0
DEFAULT_SETUP_TIMEOUT_S = 20.0
DEFAULT_OPENING_DELAY_MS = 0
MAX_OPENING_DELAY_MS = 60000
DEFAULT_PRESENCE_THRESHOLD_S = 10.0
DEFAULT_PRESENCE_DECAY = 0.5
DEFAULT_AMBIENT_INTERVAL_S = 300.0
DEFAULT_PHRASE_CACHE_DIR = REPO_ROOT / ".cache" / "phrases"
PRESENCE_JITTER = (0.5, 1.5)
AMBIENT_JITTER = (0.5, 1.5)
# Confidence (as a fraction of the threshold) that counts as real activity for the ambient timer.
AMBIENT_RESET_FRACTION = 0.5


class ConfigError(RuntimeError):
    """Required configuration is missing or invalid."""


@dataclass(frozen=True)
class Settings:
    api_key: str = field(default="", repr=False)
    model: str = DEFAULT_MODEL
    persona: str = DEFAULT_PERSONA
    device: int | str | None = None
    vad: str = "semantic_vad"
    reasoning_effort: str = "low"
    max_output_tokens: int = DEFAULT_MAX_OUTPUT_TOKENS
    log_level: str = "INFO"
    usage_log: Path | None = Path(DEFAULT_USAGE_LOG)
    tts_enabled: bool = True
    eleven_api_key: str | None = field(default=None, repr=False)
    voice_id: str = DEFAULT_VOICE_ID
    tts_model: str = DEFAULT_TTS_MODEL
    tts_stability: float | None = None
    output_device: int | str | None = None
    gate_tail_ms: int = DEFAULT_GATE_TAIL_MS
    trigger: str = "none"
    hardware: str = "gpio"
    esp32_port: str | None = None
    pir_pin: int = DEFAULT_PIR_PIN
    pir_power_pin: int | None = DEFAULT_PIR_POWER_PIN
    activate_pin: int = DEFAULT_ACTIVATE_PIN
    eyes: bool = True
    eyes_pin: int = DEFAULT_EYES_PIN
    # Hardware ceiling as a percentage of full PWM duty; the levels below are fractions of it.
    eyes_max_duty: float = DEFAULT_EYES_MAX_DUTY
    eyes_speak_level: float = DEFAULT_EYES_SPEAK_LEVEL
    eyes_flicker_level: float = DEFAULT_EYES_FLICKER_LEVEL
    eyes_flicker_cooldown_s: float = DEFAULT_EYES_FLICKER_COOLDOWN_S
    eyes_pcb_holdoff_s: float = DEFAULT_EYES_PCB_HOLDOFF_S
    pir_warmup_s: float = DEFAULT_PIR_WARMUP_S
    activate_ms: int = DEFAULT_ACTIVATE_MS
    idle_timeout_s: float = DEFAULT_IDLE_TIMEOUT_S
    max_conversation_s: float = DEFAULT_MAX_CONVERSATION_S
    cooldown_s: float = DEFAULT_COOLDOWN_S
    setup_timeout_s: float = DEFAULT_SETUP_TIMEOUT_S
    opening: bool = True
    opening_delay_ms: int = DEFAULT_OPENING_DELAY_MS
    presence_threshold_s: float = DEFAULT_PRESENCE_THRESHOLD_S
    presence_decay: float = DEFAULT_PRESENCE_DECAY
    ambient_interval_s: float = DEFAULT_AMBIENT_INTERVAL_S
    phrase_cache_dir: Path | None = DEFAULT_PHRASE_CACHE_DIR
    @property
    def tts_keep_tags(self) -> bool:
        return tts_supports_tags(self.tts_model)

    @property
    def gate_tail_s(self) -> float:
        return self.gate_tail_ms / 1000

    @property
    def opening_delay_s(self) -> float:
        return self.opening_delay_ms / 1000


def tts_supports_tags(model_id: str) -> bool:
    return model_id.startswith(TAG_CAPABLE_TTS_PREFIXES)


def load_settings(
    *,
    model: str | None = None,
    persona: str | None = None,
    voice: str | None = None,
    device: str | None = None,
    log_level: str | None = None,
    output_device: str | None = None,
    tts: bool | None = None,
    trigger: str | None = None,
    hardware: str | None = None,
    opening: bool | None = None,
    eyes: bool | None = None,
    require_openai: bool = True,
    environ: Mapping[str, str] | None = None,
    dotenv_path: Path | None = None,
) -> Settings:
    """Real environment variables win over .env; CLI arguments win over both."""
    if environ is None:
        load_dotenv(dotenv_path or REPO_ROOT / ".env", override=False)
        environ = os.environ

    api_key = _get(environ, "OPENAI_API_KEY", "")
    if require_openai and not api_key:
        raise ConfigError("OPENAI_API_KEY not set. Add it to .env")

    persona_name = persona or _get(environ, "WITCH_PERSONA", DEFAULT_PERSONA)
    if persona_name not in PERSONA_PROMPTS:
        raise ConfigError(f"WITCH_PERSONA must be one of: {', '.join(PERSONA_PROMPTS)}")

    vad = _get(environ, "WITCH_VAD", "semantic_vad")
    if vad not in VAD_TYPES:
        raise ConfigError(f"WITCH_VAD must be one of: {', '.join(VAD_TYPES)}")

    effort = _get(environ, "WITCH_REASONING_EFFORT", "low")
    if effort not in REASONING_EFFORTS:
        raise ConfigError(f"WITCH_REASONING_EFFORT must be one of: {', '.join(REASONING_EFFORTS)}")

    level = (log_level or _get(environ, "WITCH_LOG_LEVEL", "INFO")).upper()
    if level not in logging.getLevelNamesMapping():
        raise ConfigError(f"Unknown log level: {level}")

    tts_enabled = tts if tts is not None else _parse_switch(environ, "WITCH_TTS", default=True)
    eleven_key = _get(environ, "ELEVEN_LABS_API_KEY", "")
    if tts_enabled and not eleven_key:
        raise ConfigError("ELEVEN_LABS_API_KEY not set. Add it to .env or run with --no-tts")

    if voice is not None:
        if voice not in VOICES:
            raise ConfigError(f"voice must be one of: {', '.join(VOICES)}")
        voice_id = VOICES[voice]
    else:
        voice_id = _get(environ, "WITCH_VOICE_ID", DEFAULT_VOICE_ID)
    if not voice_id.isalnum():
        raise ConfigError("WITCH_VOICE_ID must contain only letters and digits")

    device_raw = device if device is not None else _get(environ, "WITCH_INPUT_DEVICE", "")
    output_raw = output_device if output_device is not None else _get(environ, "WITCH_OUTPUT_DEVICE", "")
    usage_raw = environ.get("WITCH_USAGE_LOG", DEFAULT_USAGE_LOG).strip()

    trigger_name = (trigger or _get(environ, "WITCH_TRIGGER", "none")).lower()
    if trigger_name not in TRIGGERS:
        raise ConfigError(f"WITCH_TRIGGER must be one of: {', '.join(TRIGGERS)}")
    # Run-control values are only read when a trigger is in use, so a laptop .env needs none of them.
    hardware_name = (hardware or _get(environ, "WITCH_HARDWARE", "gpio")).lower()
    if trigger_name != "none" and hardware_name not in HARDWARE:
        raise ConfigError(f"WITCH_HARDWARE must be one of: {', '.join(HARDWARE)}")
    run_control = _parse_run_control(environ, trigger_name, hardware_name) if trigger_name != "none" else {}
    if opening is not None:
        run_control["opening"] = opening
    if eyes is not None:
        run_control["eyes"] = eyes

    return Settings(
        trigger=trigger_name,
        **run_control,
        api_key=api_key,
        model=model or _get(environ, "WITCH_MODEL", DEFAULT_MODEL),
        persona=persona_name,
        device=_parse_device(device_raw),
        vad=vad,
        reasoning_effort=effort,
        max_output_tokens=_parse_int(environ, "WITCH_MAX_OUTPUT_TOKENS", DEFAULT_MAX_OUTPUT_TOKENS),
        log_level=level,
        usage_log=Path(usage_raw) if usage_raw else None,
        tts_enabled=tts_enabled,
        eleven_api_key=eleven_key or None,
        voice_id=voice_id,
        tts_model=_get(environ, "WITCH_TTS_MODEL", DEFAULT_TTS_MODEL),
        tts_stability=_parse_stability(environ),
        output_device=_parse_device(output_raw),
        gate_tail_ms=_parse_int(environ, "WITCH_GATE_TAIL_MS", DEFAULT_GATE_TAIL_MS, minimum=0),
    )


def _parse_run_control(environ: Mapping[str, str], trigger: str, hardware: str) -> dict[str, object]:
    esp32 = hardware == "esp32"
    esp32_port = _get(environ, "WITCH_ESP32_PORT", "")
    if esp32 and not esp32_port:
        raise ConfigError("WITCH_ESP32_PORT must be set (for example COM7) when WITCH_HARDWARE=esp32")
    max_pin = MAX_ESP32_PIN if esp32 else MAX_BCM_PIN
    values: dict[str, object] = {
        "hardware": hardware,
        "esp32_port": esp32_port or None,
        "pir_pin": _parse_pin(
            environ, "WITCH_PIR_PIN", DEFAULT_ESP32_PIR_PIN if esp32 else DEFAULT_PIR_PIN, maximum=max_pin
        ),
        "pir_power_pin": _parse_optional_pin(
            environ,
            "WITCH_PIR_POWER_PIN",
            DEFAULT_ESP32_PIR_POWER_PIN if esp32 else DEFAULT_PIR_POWER_PIN,
            maximum=max_pin,
        ),
        "activate_pin": _parse_pin(
            environ,
            "WITCH_ACTIVATE_PIN",
            DEFAULT_ESP32_ACTIVATE_PIN if esp32 else DEFAULT_ACTIVATE_PIN,
            maximum=max_pin,
        ),
        "eyes": _parse_switch(environ, "WITCH_EYES", default=True),
        "eyes_pin": _parse_pin(environ, "WITCH_EYES_PIN", DEFAULT_EYES_PIN, maximum=max_pin),
        "eyes_max_duty": _parse_ranged_float(
            environ, "WITCH_EYES_MAX_DUTY", DEFAULT_EYES_MAX_DUTY, low=0.0, high=100.0
        ),
        "eyes_speak_level": _parse_ranged_float(
            environ, "WITCH_EYES_SPEAK_LEVEL", DEFAULT_EYES_SPEAK_LEVEL, low=0.0, high=1.0
        ),
        "eyes_flicker_level": _parse_ranged_float(
            environ, "WITCH_EYES_FLICKER_LEVEL", DEFAULT_EYES_FLICKER_LEVEL, low=0.0, high=1.0
        ),
        "eyes_flicker_cooldown_s": _parse_float(
            environ, "WITCH_EYES_FLICKER_COOLDOWN_S", DEFAULT_EYES_FLICKER_COOLDOWN_S, minimum=0.0
        ),
        "eyes_pcb_holdoff_s": _parse_float(
            environ, "WITCH_EYES_PCB_HOLDOFF_S", DEFAULT_EYES_PCB_HOLDOFF_S, minimum=0.0
        ),
        "pir_warmup_s": _parse_float(environ, "WITCH_PIR_WARMUP_S", DEFAULT_PIR_WARMUP_S, minimum=0.0),
        "activate_ms": _parse_int(environ, "WITCH_ACTIVATE_MS", DEFAULT_ACTIVATE_MS),
        "idle_timeout_s": _parse_float(
            environ, "WITCH_IDLE_TIMEOUT_S", DEFAULT_IDLE_TIMEOUT_S, minimum=0.0, strict=True
        ),
        "max_conversation_s": _parse_float(
            environ, "WITCH_MAX_CONVERSATION_S", DEFAULT_MAX_CONVERSATION_S, minimum=0.0, strict=True
        ),
        "cooldown_s": _parse_float(environ, "WITCH_COOLDOWN_S", DEFAULT_COOLDOWN_S, minimum=0.0),
        "setup_timeout_s": _parse_float(
            environ, "WITCH_SETUP_TIMEOUT_S", DEFAULT_SETUP_TIMEOUT_S, minimum=0.0, strict=True
        ),
        "opening": _parse_switch(environ, "WITCH_OPENING", default=True),
        "opening_delay_ms": _parse_int(
            environ,
            "WITCH_OPENING_DELAY_MS",
            DEFAULT_OPENING_DELAY_MS,
            minimum=0,
            maximum=MAX_OPENING_DELAY_MS,
        ),
        "presence_threshold_s": _parse_float(
            environ, "WITCH_PRESENCE_THRESHOLD_S", DEFAULT_PRESENCE_THRESHOLD_S, minimum=0.0
        ),
        "presence_decay": _parse_float(environ, "WITCH_PRESENCE_DECAY", DEFAULT_PRESENCE_DECAY, minimum=0.0),
        "ambient_interval_s": _parse_float(
            environ, "WITCH_AMBIENT_INTERVAL_S", DEFAULT_AMBIENT_INTERVAL_S, minimum=0.0
        ),
        "phrase_cache_dir": _parse_cache_dir(environ),
    }
    if values["max_conversation_s"] < values["idle_timeout_s"]:  # type: ignore[operator]
        raise ConfigError("WITCH_MAX_CONVERSATION_S must be at least WITCH_IDLE_TIMEOUT_S")
    if esp32:
        _check_esp32_pins(values)
    if trigger == "pir":
        # Only the PIR path drives the sensor pins; the keyboard trigger only needs the activate pin.
        pins = [values["pir_pin"], values["pir_power_pin"], values["activate_pin"]]
        if values["eyes"]:
            pins.append(values["eyes_pin"])
        used = [pin for pin in pins if pin is not None]
        if len(set(used)) != len(used):
            raise ConfigError(
                "WITCH_PIR_PIN, WITCH_PIR_POWER_PIN, WITCH_ACTIVATE_PIN and WITCH_EYES_PIN must be different"
            )
    elif values["eyes"] and values["eyes_pin"] == values["activate_pin"]:
        raise ConfigError("WITCH_ACTIVATE_PIN and WITCH_EYES_PIN must be different")
    return values


def _parse_cache_dir(environ: Mapping[str, str]) -> Path | None:
    if "WITCH_PHRASE_CACHE_DIR" not in environ:
        return DEFAULT_PHRASE_CACHE_DIR
    raw = environ["WITCH_PHRASE_CACHE_DIR"].strip()
    return Path(raw) if raw else None


def _check_esp32_pins(values: dict[str, object]) -> None:
    names = {
        "pir_pin": "WITCH_PIR_PIN",
        "pir_power_pin": "WITCH_PIR_POWER_PIN",
        "activate_pin": "WITCH_ACTIVATE_PIN",
        "eyes_pin": "WITCH_EYES_PIN",
    }
    for key, name in names.items():
        pin = values[key]
        if pin is None or (key == "eyes_pin" and not values["eyes"]):
            continue
        if pin in ESP32_FLASH_PINS:
            raise ConfigError(f"{name}: GPIO{pin} is connected to the ESP32's flash and cannot be used")
        if key != "pir_pin" and pin in ESP32_INPUT_ONLY_PINS:
            raise ConfigError(f"{name}: GPIO{pin} is input-only on the ESP32 and cannot drive an output")


def _parse_pin(environ: Mapping[str, str], name: str, default: int, *, maximum: int = MAX_BCM_PIN) -> int:
    value = _parse_int(environ, name, default, minimum=0)
    if value > maximum:
        raise ConfigError(f"{name} must be between 0 and {maximum}")
    return value


def _parse_optional_pin(
    environ: Mapping[str, str], name: str, default: int, *, maximum: int = MAX_BCM_PIN
) -> int | None:
    if environ.get(name, str(default)).strip() == "":
        return None
    return _parse_pin(environ, name, default, maximum=maximum)


def _parse_float(
    environ: Mapping[str, str],
    name: str,
    default: float,
    *,
    minimum: float,
    strict: bool = False,
) -> float:
    raw = _get(environ, name, str(default))
    try:
        value = float(raw)
    except ValueError as exc:
        raise ConfigError(f"{name} must be a number") from exc
    if value < minimum or (strict and value == minimum):
        bound = "greater than" if strict else "at least"
        raise ConfigError(f"{name} must be {bound} {minimum:g}")
    return value


def _parse_ranged_float(
    environ: Mapping[str, str], name: str, default: float, *, low: float, high: float
) -> float:
    """A number in (low, high]."""
    value = _parse_float(environ, name, default, minimum=low, strict=True)
    if value > high:
        raise ConfigError(f"{name} must be at most {high:g}")
    return value


def _get(environ: Mapping[str, str], name: str, default: str) -> str:
    return environ.get(name, "").strip() or default


def _parse_device(raw: str) -> int | str | None:
    raw = raw.strip()
    if not raw:
        return None
    return int(raw) if raw.isdigit() else raw


def _parse_switch(environ: Mapping[str, str], name: str, *, default: bool) -> bool:
    raw = _get(environ, name, "on" if default else "off").lower()
    if raw not in ("on", "off"):
        raise ConfigError(f"{name} must be on or off")
    return raw == "on"


def _parse_stability(environ: Mapping[str, str]) -> float | None:
    raw = _get(environ, "WITCH_TTS_STABILITY", "")
    if not raw:
        return None
    try:
        value = float(raw)
    except ValueError as exc:
        raise ConfigError("WITCH_TTS_STABILITY must be a number") from exc
    if not 0.0 <= value <= 1.0:
        raise ConfigError("WITCH_TTS_STABILITY must be between 0 and 1")
    return value


def _parse_int(
    environ: Mapping[str, str],
    name: str,
    default: int,
    *,
    minimum: int = 1,
    maximum: int | None = None,
) -> int:
    raw = _get(environ, name, str(default))
    try:
        value = int(raw)
    except ValueError as exc:
        raise ConfigError(f"{name} must be an integer") from exc
    if value < minimum:
        raise ConfigError(f"{name} must be at least {minimum}")
    if maximum is not None and value > maximum:
        raise ConfigError(f"{name} must be at most {maximum}")
    return value
