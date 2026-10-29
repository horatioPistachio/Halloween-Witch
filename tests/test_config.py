"""Configuration loading tests."""

import os
from pathlib import Path

import pytest

from witch.config import ConfigError, load_settings, tts_supports_tags

KEY_ENV = {"OPENAI_API_KEY": "sk-test-123", "ELEVEN_LABS_API_KEY": "el-test-456"}


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


def test_persona_defaults_to_kids_and_cli_overrides_env():
    assert load_settings(environ=KEY_ENV).persona == "kids"
    assert load_settings(environ={**KEY_ENV, "WITCH_PERSONA": "evil"}).persona == "evil"
    assert load_settings(persona="kids", environ={**KEY_ENV, "WITCH_PERSONA": "evil"}).persona == "kids"


def test_invalid_persona_rejected():
    with pytest.raises(ConfigError, match="WITCH_PERSONA"):
        load_settings(environ={**KEY_ENV, "WITCH_PERSONA": "bogus"})


def test_dotenv_fills_gaps_but_real_env_wins(monkeypatch, tmp_path):
    dotenv = tmp_path / ".env"
    dotenv.write_text(
        "OPENAI_API_KEY=from-file\nELEVEN_LABS_API_KEY=el-file\nWITCH_MODEL=file-model\n",
        encoding="utf-8",
    )
    fake_environ = {"OPENAI_API_KEY": "from-env"}
    monkeypatch.setattr(os, "environ", fake_environ)

    settings = load_settings(dotenv_path=dotenv)

    assert settings.api_key == "from-env"
    assert settings.model == "file-model"


def test_tts_defaults():
    settings = load_settings(environ=KEY_ENV)
    assert settings.tts_enabled
    assert settings.voice_id == "xBIfEFAROTtux2FvqTLj"
    assert settings.tts_model == "eleven_v4_turbo"
    assert settings.tts_stability is None
    assert settings.gate_tail_s == pytest.approx(0.4)


def test_cli_voice_selects_named_voice_and_overrides_env():
    assert load_settings(voice="seer", environ=KEY_ENV).voice_id == "7NsaqHdLuKNFvEfjpUno"
    assert load_settings(voice="selna", environ={**KEY_ENV, "WITCH_VOICE_ID": "abc123"}).voice_id == (
        "2aL479c8D3QMIPExj0tw"
    )


def test_env_voice_id_used_without_cli_voice():
    assert load_settings(environ={**KEY_ENV, "WITCH_VOICE_ID": "abc123"}).voice_id == "abc123"


def test_unknown_cli_voice_rejected():
    with pytest.raises(ConfigError, match="voice must be one of"):
        load_settings(voice="bogus", environ=KEY_ENV)


def test_eleven_key_hidden_in_repr():
    assert "el-test-456" not in repr(load_settings(environ=KEY_ENV))


def test_missing_eleven_key_fails_when_tts_on():
    with pytest.raises(ConfigError, match="ELEVEN_LABS_API_KEY not set"):
        load_settings(environ={"OPENAI_API_KEY": "sk-test-123"})


def test_missing_eleven_key_allowed_when_tts_off():
    settings = load_settings(environ={"OPENAI_API_KEY": "sk-test-123", "WITCH_TTS": "off"})
    assert not settings.tts_enabled


def test_cli_no_tts_overrides_env():
    settings = load_settings(tts=False, environ={"OPENAI_API_KEY": "sk-test-123"})
    assert not settings.tts_enabled


def test_say_mode_needs_no_openai_key():
    settings = load_settings(
        tts=True,
        require_openai=False,
        environ={"ELEVEN_LABS_API_KEY": "el-test-456"},
    )
    assert settings.api_key == ""


def test_output_device_from_cli_or_env():
    assert load_settings(output_device="Speakers", environ=KEY_ENV).output_device == "Speakers"
    assert load_settings(environ={**KEY_ENV, "WITCH_OUTPUT_DEVICE": "3"}).output_device == 3


@pytest.mark.parametrize(
    ("env", "name"),
    [
        ({"WITCH_TTS": "maybe"}, "WITCH_TTS"),
        ({"WITCH_TTS_STABILITY": "1.5"}, "WITCH_TTS_STABILITY"),
        ({"WITCH_TTS_STABILITY": "abc"}, "WITCH_TTS_STABILITY"),
        ({"WITCH_GATE_TAIL_MS": "-1"}, "WITCH_GATE_TAIL_MS"),
        ({"WITCH_VOICE_ID": "abc/def"}, "WITCH_VOICE_ID"),
        ({"WITCH_VOICE_ID": "abc def"}, "WITCH_VOICE_ID"),
    ],
)
def test_invalid_tts_settings_rejected(env, name):
    with pytest.raises(ConfigError, match=name):
        load_settings(environ={**KEY_ENV, **env})


def test_tts_supports_tags_by_model_family():
    assert tts_supports_tags("eleven_v4_turbo")
    assert tts_supports_tags("eleven_v4")
    assert tts_supports_tags("eleven_v3")
    assert not tts_supports_tags("eleven_flash_v2_5")
    assert not tts_supports_tags("eleven_multilingual_v2")


KEYBOARD_ENV = {**KEY_ENV, "WITCH_TRIGGER": "keyboard"}


def test_opening_defaults_on_with_no_delay():
    settings = load_settings(environ=KEYBOARD_ENV)
    assert settings.opening is True
    assert settings.opening_delay_ms == 0
    assert settings.opening_delay_s == 0.0


def test_opening_can_be_turned_off_and_delay_set():
    settings = load_settings(environ={**KEYBOARD_ENV, "WITCH_OPENING": "off", "WITCH_OPENING_DELAY_MS": "1500"})
    assert settings.opening is False
    assert settings.opening_delay_s == pytest.approx(1.5)


def test_cli_no_opening_overrides_env():
    settings = load_settings(opening=False, environ={**KEYBOARD_ENV, "WITCH_OPENING": "on"})
    assert settings.opening is False


@pytest.mark.parametrize(
    ("env", "name"),
    [
        ({"WITCH_OPENING": "maybe"}, "WITCH_OPENING"),
        ({"WITCH_OPENING_DELAY_MS": "-1"}, "WITCH_OPENING_DELAY_MS"),
        ({"WITCH_OPENING_DELAY_MS": "5001"}, "WITCH_OPENING_DELAY_MS"),
        ({"WITCH_OPENING_DELAY_MS": "soon"}, "WITCH_OPENING_DELAY_MS"),
    ],
)
def test_invalid_opening_settings_rejected(env, name):
    with pytest.raises(ConfigError, match=name):
        load_settings(environ={**KEYBOARD_ENV, **env})


def test_opening_settings_not_read_when_trigger_is_none():
    settings = load_settings(environ={**KEY_ENV, "WITCH_OPENING_DELAY_MS": "bogus"})
    assert settings.opening is True
    assert settings.opening_delay_ms == 0
