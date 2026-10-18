"""Witch loop: microphone -> Realtime API -> tag parser -> console, with per-interaction cost."""

from __future__ import annotations

import asyncio
import logging

import openai
from websockets.exceptions import WebSocketException

from witch import tags
from witch.audio.base import AudioSource
from witch.audio.mic import MicSource
from witch.config import Settings
from witch.cost import CostTracker, InteractionCost, format_interaction
from witch.realtime import (
    RealtimeClient,
    ResponseDone,
    ServerError,
    SpeechStarted,
    SpeechStopped,
    TextDelta,
)

log = logging.getLogger(__name__)

FATAL_ERRORS = (
    openai.AuthenticationError,
    openai.PermissionDeniedError,
    openai.NotFoundError,
    openai.BadRequestError,
)
RETRYABLE_ERRORS = (
    openai.APIConnectionError,
    openai.RateLimitError,
    openai.InternalServerError,
    WebSocketException,
    OSError,
)
MAX_BACKOFF_S = 15.0


async def run(settings: Settings) -> int:
    tracker = CostTracker(settings.model, settings.usage_log)
    backoff = 1.0
    try:
        async with MicSource(settings) as mic:
            while True:
                try:
                    async with RealtimeClient(settings) as rt:
                        backoff = 1.0
                        log.info("Connected to %s. Speak to the witch.", settings.model)
                        await _run_session(mic, rt, tracker)
                except FATAL_ERRORS as exc:
                    log.error("Stopping: %s", exc)
                    return 1
                except RETRYABLE_ERRORS as exc:
                    log.warning("Connection lost (%s). Reconnecting in %.0f s", exc, backoff)
                    await asyncio.sleep(backoff)
                    backoff = min(backoff * 2, MAX_BACKOFF_S)
    finally:
        _print_run_total(tracker)


async def _run_session(mic: AudioSource, rt: RealtimeClient, tracker: CostTracker) -> None:
    pump = asyncio.create_task(_pump_audio(mic, rt), name="pump-audio")
    events = asyncio.create_task(_handle_events(rt, tracker), name="handle-events")
    try:
        done, _pending = await asyncio.wait({pump, events}, return_when=asyncio.FIRST_COMPLETED)
        for task in done:
            task.result()
        raise ConnectionError("Realtime session ended")
    finally:
        for task in (pump, events):
            task.cancel()
        await asyncio.gather(pump, events, return_exceptions=True)


async def _pump_audio(mic: AudioSource, rt: RealtimeClient) -> None:
    async for chunk in mic.chunks():
        await rt.send_audio(chunk)


async def _handle_events(rt: RealtimeClient, tracker: CostTracker) -> None:
    loop = asyncio.get_running_loop()
    speech_ended_at: float | None = None
    streaming = False
    latency_ms: int | None = None

    async for event in rt.events():
        match event:
            case SpeechStarted():
                if streaming:
                    print("\n[interrupted]", flush=True)
                speech_ended_at = None
            case SpeechStopped():
                speech_ended_at = loop.time()
            case TextDelta(delta=delta):
                if not streaming:
                    streaming = True
                    if speech_ended_at is not None:
                        latency_ms = round((loop.time() - speech_ended_at) * 1000)
                    print("witch> ", end="", flush=True)
                print(delta, end="", flush=True)
            case ResponseDone() as done:
                _report(done, tracker, latency_ms)
                streaming = False
                latency_ms = None
                speech_ended_at = None
            case ServerError(message=message, code=code):
                log.warning("Realtime error (%s): %s", code, message)

    raise ConnectionError("Realtime event stream closed")


def _report(done: ResponseDone, tracker: CostTracker, latency_ms: int | None) -> None:
    entry = tracker.record(done.response_id, done.status, done.tokens)
    print()
    if done.status == "completed":
        parsed = tags.parse(done.text)
        print("--- response " + "-" * 40)
        print(f"raw:        {parsed.raw}")
        print(f"text:       {parsed.clean_text}")
        print(f"tags:       {', '.join(parsed.tags) or 'none'}")
        print(f"latency_ms: {latency_ms if latency_ms is not None else 'n/a'}")
        print(format_interaction(entry))
        print("-" * 56)
    else:
        print(f"[{done.status}] response not parsed")
        print(format_interaction(entry))


def _print_run_total(tracker: CostTracker) -> None:
    if tracker.interactions == 0:
        return
    total = "n/a" if tracker.total_usd is None else f"${tracker.total_usd:.4f}"
    print(f"Run total: {tracker.interactions} interactions, {total}")
