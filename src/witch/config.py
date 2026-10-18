"""Settings from environment variables, the repo .env file and CLI overrides."""

from __future__ import annotations

import logging
import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_MODEL = "gpt-realtime-2.1-mini"
DEFAULT_USAGE_LOG = "usage.jsonl"
DEFAULT_MAX_OUTPUT_TOKENS = 600
VAD_TYPES = ("semantic_vad", "server_vad")
REASONING_EFFORTS = ("minimal", "low", "medium", "high", "xhigh")
DEFAULT_VOICE_ID = "xBIfEFAROTtux2FvqTLj"
DEFAULT_TTS_MODEL = "eleven_v4_turbo"
DEFAULT_GATE_TAIL_MS = 400
TAG_CAPABLE_TTS_PREFIXES = ("eleven_v3", "eleven_v4")


class ConfigError(RuntimeError):
    """Required configuration is missing or invalid."""


@dataclass(frozen=True)
class Settings:
    api_key: str = field(default="", repr=False)
    model: str = DEFAULT_MODEL
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
    device: str | None = None,
    log_level: str | None = None,
    output_device: str | None = None,
    tts: bool | None = None,
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

    voice_id = _get(environ, "WITCH_VOICE_ID", DEFAULT_VOICE_ID)
    if not voice_id.isalnum():
        raise ConfigError("WITCH_VOICE_ID must contain only letters and digits")

    device_raw = device if device is not None else _get(environ, "WITCH_INPUT_DEVICE", "")
    output_raw = output_device if output_device is not None else _get(environ, "WITCH_OUTPUT_DEVICE", "")
    usage_raw = environ.get("WITCH_USAGE_LOG", DEFAULT_USAGE_LOG).strip()

    return Settings(
        api_key=api_key,
        model=model or _get(environ, "WITCH_MODEL", DEFAULT_MODEL),
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
