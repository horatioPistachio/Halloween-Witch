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


class ConfigError(RuntimeError):
    """Required configuration is missing or invalid."""


@dataclass(frozen=True)
class Settings:
    api_key: str = field(repr=False)
    model: str = DEFAULT_MODEL
    device: int | str | None = None
    vad: str = "semantic_vad"
    reasoning_effort: str = "low"
    max_output_tokens: int = DEFAULT_MAX_OUTPUT_TOKENS
    log_level: str = "INFO"
    usage_log: Path | None = Path(DEFAULT_USAGE_LOG)


def load_settings(
    *,
    model: str | None = None,
    device: str | None = None,
    log_level: str | None = None,
    environ: Mapping[str, str] | None = None,
    dotenv_path: Path | None = None,
) -> Settings:
    """Real environment variables win over .env; CLI arguments win over both."""
    if environ is None:
        load_dotenv(dotenv_path or REPO_ROOT / ".env", override=False)
        environ = os.environ

    api_key = _get(environ, "OPENAI_API_KEY", "")
    if not api_key:
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

    device_raw = device if device is not None else _get(environ, "WITCH_INPUT_DEVICE", "")
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
    )


def _get(environ: Mapping[str, str], name: str, default: str) -> str:
    return environ.get(name, "").strip() or default


def _parse_device(raw: str) -> int | str | None:
    raw = raw.strip()
    if not raw:
        return None
    return int(raw) if raw.isdigit() else raw


def _parse_int(environ: Mapping[str, str], name: str, default: int) -> int:
    raw = _get(environ, name, str(default))
    try:
        value = int(raw)
    except ValueError as exc:
        raise ConfigError(f"{name} must be an integer") from exc
    if value < 1:
        raise ConfigError(f"{name} must be positive")
    return value
