"""Configuration loading tests."""

import os
from pathlib import Path

import pytest

from witch.config import ConfigError, load_settings

KEY_ENV = {"OPENAI_API_KEY": "sk-test-123"}


def test_missing_key_raises_clear_error():
    with pytest.raises(ConfigError, match="OPENAI_API_KEY not set"):
        load_settings(environ={})


def test_api_key_is_hidden_in_repr():
    settings = load_settings(environ=KEY_ENV)
    assert settings.api_key == "sk-test-123"
    assert "sk-test-123" not in repr(settings)


def test_defaults():
    settings = load_settings(environ=KEY_ENV)
    assert settings.model == "gpt-realtime-2.1-mini"
    assert settings.vad == "semantic_vad"
    assert settings.reasoning_effort == "low"
    assert settings.device is None
    assert settings.usage_log == Path("usage.jsonl")


def test_device_index_or_name():
    assert load_settings(environ={**KEY_ENV, "WITCH_INPUT_DEVICE": "2"}).device == 2
    assert load_settings(environ={**KEY_ENV, "WITCH_INPUT_DEVICE": "USB Mic"}).device == "USB Mic"


def test_cli_overrides_env():
    settings = load_settings(
        model="cli-model",
        environ={**KEY_ENV, "WITCH_MODEL": "env-model"},
    )
    assert settings.model == "cli-model"


def test_empty_usage_log_disables_tracking():
    assert load_settings(environ={**KEY_ENV, "WITCH_USAGE_LOG": ""}).usage_log is None


def test_invalid_vad_rejected():
    with pytest.raises(ConfigError, match="WITCH_VAD"):
        load_settings(environ={**KEY_ENV, "WITCH_VAD": "bogus"})


def test_dotenv_fills_gaps_but_real_env_wins(monkeypatch, tmp_path):
    dotenv = tmp_path / ".env"
    dotenv.write_text("OPENAI_API_KEY=from-file\nWITCH_MODEL=file-model\n", encoding="utf-8")
    fake_environ = {"OPENAI_API_KEY": "from-env"}
    monkeypatch.setattr(os, "environ", fake_environ)

    settings = load_settings(dotenv_path=dotenv)

    assert settings.api_key == "from-env"
    assert settings.model == "file-model"
