"""Witch loop: microphone -> Realtime API -> tag parser -> console, with per-interaction cost."""

from __future__ import annotations

import asyncio
import logging
import sys
from collections.abc import AsyncIterator
from contextlib import AsyncExitStack
from dataclasses import dataclass

import openai
from websockets.exceptions import WebSocketException

from witch import tags
from witch.audio.base import AudioSink, AudioSource, PlaybackError
from witch.audio.mic import MicSource
from witch.audio.speaker import SpeakerSink
from witch.config import Settings
from witch.cost import CostTracker, InteractionCost, format_interaction
from witch.gate import MicGate
from witch.realtime import (
    RealtimeClient,
    ResponseDone,
    ServerError,
    SpeechStarted,
    SpeechStopped,
    TextDelta,
)
from witch.tts.base import TextToSpeech, TTSError
from witch.tts.elevenlabs import ElevenLabsTTS

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


@dataclass(frozen=True)
class SpeechJob:
    text: str
    speech_ended_at: float | None


@dataclass(frozen=True)
class _Spoken:
    started_at: float
    first_audio_at: float | None
    finished_at: float

    @property
    def played_s(self) -> float:
        return self.finished_at - self.started_at


async def run(settings: Settings) -> int:
    tracker = CostTracker(settings.model, settings.usage_log)
    gate = MicGate()
    queue: asyncio.Queue[SpeechJob] = asyncio.Queue()
    backoff = 1.0
    try:
        async with MicSource(settings) as mic, AsyncExitStack() as stack:
            if settings.tts_enabled:
                tts = await stack.enter_async_context(ElevenLabsTTS(settings))
                sink = await stack.enter_async_context(SpeakerSink(settings))
                speaker = asyncio.create_task(_speak_loop(queue, tts, sink, gate, settings), name="speak")
                stack.push_async_callback(_stop_task, speaker)
            while True:
                try:
                    async with RealtimeClient(settings) as rt:
                        backoff = 1.0
                        log.info("Connected to %s. Speak to the witch.", settings.model)
                        await _run_session(mic, rt, tracker, settings, gate, queue)
                except FATAL_ERRORS as exc:
                    log.error("Stopping: %s", exc)
                    return 1
                except RETRYABLE_ERRORS as exc:
                    log.warning("Connection lost (%s). Reconnecting in %.0f s", exc, backoff)
                    await asyncio.sleep(backoff)
                    backoff = min(backoff * 2, MAX_BACKOFF_S)
    finally:
        _print_run_total(tracker)


async def _stop_task(task: asyncio.Task[None]) -> None:
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)


async def _run_session(
    mic: AudioSource,
    rt: RealtimeClient,
    tracker: CostTracker,
    settings: Settings,
    gate: MicGate,
    queue: asyncio.Queue[SpeechJob],
) -> None:
    pump = asyncio.create_task(_pump_audio(mic, rt, gate), name="pump-audio")
    events = asyncio.create_task(
        _handle_events(rt, tracker, settings, gate, queue), name="handle-events"
    )
    try:
        done, _pending = await asyncio.wait({pump, events}, return_when=asyncio.FIRST_COMPLETED)
        for task in done:
            task.result()
        raise ConnectionError("Realtime session ended")
    finally:
        for task in (pump, events):
            task.cancel()
        await asyncio.gather(pump, events, return_exceptions=True)


async def _pump_audio(mic: AudioSource, rt: RealtimeClient, gate: MicGate) -> None:
    async for chunk in mic.chunks():
        if gate.is_open:
            await rt.send_audio(chunk)


async def _handle_events(
    rt: RealtimeClient,
    tracker: CostTracker,
    settings: Settings,
    gate: MicGate,
    queue: asyncio.Queue[SpeechJob],
) -> None:
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
                parsed = tags.parse(done.text) if done.status == "completed" else None
                speech = _speech_text(parsed, settings)
                entry = tracker.record(done.response_id, done.status, done.tokens, tts_chars=len(speech))
                _report(done, entry, latency_ms, parsed)
                if speech:
                    gate.close()
                    queue.put_nowait(SpeechJob(text=speech, speech_ended_at=speech_ended_at))
                streaming = False
                latency_ms = None
                speech_ended_at = None
            case ServerError(message=message, code=code):
                log.warning("Realtime error (%s): %s", code, message)

    raise ConnectionError("Realtime event stream closed")


def _report(
    done: ResponseDone,
    entry: InteractionCost,
    latency_ms: int | None,
    parsed: tags.WitchResponse | None,
) -> None:
    print()
    if parsed is None:
        print(f"[{done.status}] response not parsed")
        print(format_interaction(entry))
        return
    print("--- response " + "-" * 40)
    print(f"raw:        {parsed.raw}")
    print(f"text:       {parsed.clean_text}")
    print(f"tags:       {', '.join(parsed.tags) or 'none'}")
    print(f"latency_ms: {latency_ms if latency_ms is not None else 'n/a'}")
    print(format_interaction(entry))
    print("-" * 56)


def _speech_text(parsed: tags.WitchResponse | None, settings: Settings) -> str:
    if parsed is None or not settings.tts_enabled:
        return ""
    return tags.speech_text(parsed, keep_tags=settings.tts_keep_tags)


async def _speak_loop(
    queue: asyncio.Queue[SpeechJob],
    tts: TextToSpeech,
    sink: AudioSink,
    gate: MicGate,
    settings: Settings,
) -> None:
    while True:
        job = await queue.get()
        try:
            spoken = await _speak(tts, sink, job.text)
            _print_speech(job, spoken)
        except TTSError as exc:
            log.error("TTS failed: %s", exc)
        except PlaybackError as exc:
            log.error("Playback failed: %s", exc)
        except Exception:
            log.exception("Speech worker error; continuing")
        finally:
            gate.release(settings.gate_tail_s)


async def _speak(tts: TextToSpeech, sink: AudioSink, text: str) -> _Spoken:
    loop = asyncio.get_running_loop()
    started_at = loop.time()
    first_audio_at: float | None = None

    async def audio() -> AsyncIterator[bytes]:
        nonlocal first_audio_at
        async for chunk in tts.synthesize(text):
            if first_audio_at is None:
                first_audio_at = loop.time()
            yield chunk

    await sink.play(audio())
    return _Spoken(started_at=started_at, first_audio_at=first_audio_at, finished_at=loop.time())


def _print_speech(job: SpeechJob, spoken: _Spoken) -> None:
    if job.speech_ended_at is None or spoken.first_audio_at is None:
        first = "n/a"
    else:
        first = f"{round((spoken.first_audio_at - job.speech_ended_at) * 1000)} ms"
    print(
        f"speech> {len(job.text)} chars, first audio {first} after end of speech, "
        f"played {spoken.played_s:.1f} s",
        flush=True,
    )


async def say(settings: Settings, text: str) -> int:
    speech = tags.speech_text(tags.parse(text), keep_tags=settings.tts_keep_tags)
    if not speech:
        print("error: nothing to say", file=sys.stderr)
        return 2
    print(f"sending: {speech}", flush=True)
    try:
        async with ElevenLabsTTS(settings) as tts, SpeakerSink(settings) as sink:
            spoken = await _speak(tts, sink, speech)
    except (TTSError, PlaybackError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    if spoken.first_audio_at is None:
        first = "n/a"
    else:
        first = f"{round((spoken.first_audio_at - spoken.started_at) * 1000)} ms"
    print(f"first audio {first}, played {spoken.played_s:.1f} s")
    return 0


def _print_run_total(tracker: CostTracker) -> None:
    if tracker.interactions == 0:
        return
    total = "n/a" if tracker.total_usd is None else f"${tracker.total_usd:.4f}"
    print(f"Run total: {tracker.interactions} interactions, {total}")
