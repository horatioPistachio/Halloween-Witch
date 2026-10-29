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
DEFAULT_MODEL = "gpt-realtime-2.1-mini" # or "gpt-realtime-2.1-mini"
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
}
DEFAULT_VOICE = "witch"
DEFAULT_VOICE_ID = VOICES[DEFAULT_VOICE]
DEFAULT_TTS_MODEL = "eleven_v4_turbo"
DEFAULT_GATE_TAIL_MS = 400
TAG_CAPABLE_TTS_PREFIXES = ("eleven_v3", "eleven_v4")
TRIGGERS = ("none", "pir", "keyboard")
MAX_BCM_PIN = 27
DEFAULT_PIR_PIN = 27
DEFAULT_PIR_POWER_PIN = 17
DEFAULT_ACTIVATE_PIN = 22
DEFAULT_PIR_WARMUP_S = 60.0
DEFAULT_ACTIVATE_MS = 1000
DEFAULT_IDLE_TIMEOUT_S = 30.0
DEFAULT_MAX_CONVERSATION_S = 120.0
DEFAULT_COOLDOWN_S = 30.0
DEFAULT_SETUP_TIMEOUT_S = 20.0


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
    pir_pin: int = DEFAULT_PIR_PIN
    pir_power_pin: int | None = DEFAULT_PIR_POWER_PIN
    activate_pin: int = DEFAULT_ACTIVATE_PIN
    pir_warmup_s: float = DEFAULT_PIR_WARMUP_S
    activate_ms: int = DEFAULT_ACTIVATE_MS
    idle_timeout_s: float = DEFAULT_IDLE_TIMEOUT_S
    max_conversation_s: float = DEFAULT_MAX_CONVERSATION_S
    cooldown_s: float = DEFAULT_COOLDOWN_S
    setup_timeout_s: float = DEFAULT_SETUP_TIMEOUT_S

    @property
    def tts_keep_tags(self) -> bool:
        return tts_supports_tags(self.tts_model)

    @property
    def gate_tail_s(self) -> float:
        return self.gate_tail_ms / 1000


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
    run_control = _parse_run_control(environ, trigger_name) if trigger_name != "none" else {}

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


def _parse_run_control(environ: Mapping[str, str], trigger: str) -> dict[str, object]:
    values: dict[str, object] = {
        "pir_pin": _parse_pin(environ, "WITCH_PIR_PIN", DEFAULT_PIR_PIN),
        "pir_power_pin": _parse_optional_pin(environ, "WITCH_PIR_POWER_PIN", DEFAULT_PIR_POWER_PIN),
        "activate_pin": _parse_pin(environ, "WITCH_ACTIVATE_PIN", DEFAULT_ACTIVATE_PIN),
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
    }
    if values["max_conversation_s"] < values["idle_timeout_s"]:  # type: ignore[operator]
        raise ConfigError("WITCH_MAX_CONVERSATION_S must be at least WITCH_IDLE_TIMEOUT_S")
    if trigger == "pir":
        # Only the PIR path drives real pins; the keyboard trigger uses a logging output.
        pins = [values["pir_pin"], values["pir_power_pin"], values["activate_pin"]]
        used = [pin for pin in pins if pin is not None]
        if len(set(used)) != len(used):
            raise ConfigError("WITCH_PIR_PIN, WITCH_PIR_POWER_PIN and WITCH_ACTIVATE_PIN must be different")
    return values


def _parse_pin(environ: Mapping[str, str], name: str, default: int) -> int:
    value = _parse_int(environ, name, default, minimum=0)
    if value > MAX_BCM_PIN:
        raise ConfigError(f"{name} must be between 0 and {MAX_BCM_PIN}")
    return value


def _parse_optional_pin(environ: Mapping[str, str], name: str, default: int) -> int | None:
    if environ.get(name, str(default)).strip() == "":
        return None
    return _parse_pin(environ, name, default)


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


def _parse_int(environ: Mapping[str, str], name: str, default: int, *, minimum: int = 1) -> int:
    raw = _get(environ, name, str(default))
    try:
        value = int(raw)
    except ValueError as exc:
        raise ConfigError(f"{name} must be an integer") from exc
    if value < minimum:
        raise ConfigError(f"{name} must be at least {minimum}")
    return value
