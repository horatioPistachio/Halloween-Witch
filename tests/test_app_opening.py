"""How the witch's opening cue hands the microphone gate to the reply that follows."""

import asyncio
import random

import pytest

from witch.app import Conversation
from witch.config import Settings, load_settings
from witch.cost import CostTracker, TokenUsage
from witch.opening import OpeningPicker
from witch.realtime import ResponseDone

KEY_ENV = {
    "OPENAI_API_KEY": "sk-test-123",
    "ELEVEN_LABS_API_KEY": "el-test-456",
    "WITCH_TRIGGER": "keyboard",
    "WITCH_GATE_TAIL_MS": "0",
}


class FakeRealtime:
    def __init__(self, events=()) -> None:
        self._events = list(events)
        self.prompts: list[str] = []

    async def prompt(self, text: str) -> None:
        self.prompts.append(text)

    async def events(self):
        for event in self._events:
            yield event


def _done(text: str) -> ResponseDone:
    return ResponseDone(response_id="resp-1", status="completed", text=text, tokens=TokenUsage(text_in=5, text_out=9))


def _conversation(settings: Settings, *, openings: bool) -> Conversation:
    tracker = CostTracker(settings.model, None)
    picker = OpeningPicker(rng=random.Random(0)) if openings else None
    return Conversation(settings, tracker, picker)


def test_opening_sends_one_cue_and_closes_the_mic():
    settings = load_settings(environ=KEY_ENV)
    conversation = _conversation(settings, openings=True)
    rt = FakeRealtime()

    asyncio.run(conversation._send_opening(rt, conversation._openings))

    assert len(rt.prompts) == 1
    assert rt.prompts[0].startswith("Stage cue:")
    assert not conversation._gate.is_open
    assert conversation._opening_gate


def test_spoken_opening_keeps_mic_closed_until_playback_releases_it():
    settings = load_settings(environ=KEY_ENV)
    conversation = _conversation(settings, openings=True)
    rt = FakeRealtime(events=[_done("[calm] Boo!")])

    asyncio.run(conversation._send_opening(rt, conversation._openings))
    with pytest.raises(ConnectionError):
        asyncio.run(conversation._handle_events(rt))

    assert conversation._queue.qsize() == 1
    assert not conversation._opening_gate
    assert not conversation._gate.is_open
    # One release, matching the single close from the opening, is enough to reopen the mic.
    conversation._gate.release(0.0)
    assert conversation._gate.is_open


def test_silent_opening_reopens_the_mic_straight_away():
    settings = load_settings(environ={**KEY_ENV, "WITCH_TTS": "off"})
    conversation = _conversation(settings, openings=True)
    rt = FakeRealtime(events=[_done("[calm] Boo!")])

    asyncio.run(conversation._send_opening(rt, conversation._openings))
    with pytest.raises(ConnectionError):
        asyncio.run(conversation._handle_events(rt))

    assert conversation._queue.qsize() == 0
    assert not conversation._opening_gate
    assert conversation._gate.is_open


def test_ordinary_reply_still_closes_and_releases_the_mic():
    settings = load_settings(environ=KEY_ENV)
    conversation = _conversation(settings, openings=True)
    rt = FakeRealtime(events=[_done("[calm] Boo!")])

    with pytest.raises(ConnectionError):
        asyncio.run(conversation._handle_events(rt))

    assert conversation._queue.qsize() == 1
    assert not conversation._gate.is_open
    conversation._gate.release(0.0)
    assert conversation._gate.is_open
