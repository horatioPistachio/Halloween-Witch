"""Realtime client messages for opening cues and the session config that the prompt cache depends on."""

import asyncio
from types import SimpleNamespace

from witch.config import load_settings
from witch.persona import PERSONA_PROMPTS
from witch.realtime import RealtimeClient, session_config

KEY_ENV = {"OPENAI_API_KEY": "sk-test-123", "ELEVEN_LABS_API_KEY": "el-test-456"}


class FakeConnection:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []
        self.conversation = SimpleNamespace(item=SimpleNamespace(create=self._item_create))
        self.response = SimpleNamespace(create=self._response_create)

    async def _item_create(self, **kwargs):
        self.calls.append(("conversation.item.create", kwargs))

    async def _response_create(self, **kwargs):
        self.calls.append(("response.create", kwargs))


def test_prompt_adds_a_user_message_then_asks_for_a_response():
    client = RealtimeClient(load_settings(environ=KEY_ENV))
    fake = FakeConnection()
    client._conn = fake

    asyncio.run(client.prompt("Stage cue: Say hello."))

    assert fake.calls == [
        (
            "conversation.item.create",
            {
                "item": {
                    "type": "message",
                    "role": "user",
                    "content": [{"type": "input_text", "text": "Stage cue: Say hello."}],
                }
            },
        ),
        ("response.create", {}),
    ]


def test_session_config_is_identical_every_time_for_prompt_cache():
    settings = load_settings(environ=KEY_ENV)
    assert session_config(settings) == session_config(settings)


def test_session_config_carries_the_persona_as_instructions():
    settings = load_settings(persona="evil", environ=KEY_ENV)
    assert session_config(settings)["instructions"] == PERSONA_PROMPTS["evil"]
