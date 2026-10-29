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
from witch.control import EndReason, IdleTimer, RunController
from witch.cost import CostTracker, InteractionCost, format_interaction
from witch.gate import MicGate
from witch.hardware import build_hardware
from witch.opening import OpeningPicker
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
    gpt_first_token_ms: int | None = None
    gpt_done_ms: int | None = None
    queued_at: float | None = None


@dataclass(frozen=True)
class _Spoken:
    started_at: float
    first_audio_at: float | None
    finished_at: float
    tts_requested_at: float | None = None

    @property
    def played_s(self) -> float:
        return self.finished_at - self.started_at


class Conversation:
    """One conversation: microphone, speech output, and Realtime sessions.

    Trigger-none mode keeps one conversation open and reconnects (serve()). Controlled
    mode creates one per activation and calls run(timer) once.
    """

    def __init__(
        self, settings: Settings, tracker: CostTracker, openings: OpeningPicker | None = None
    ) -> None:
        self._settings = settings
        self._tracker = tracker
        self._openings = openings
        self._gate = MicGate()
        # True while the gate is closed for the opening cue rather than for a reply.
        self._opening_gate = False
        self._opening_sent_at: float | None = None
        self._queue: asyncio.Queue[SpeechJob] = asyncio.Queue()
        self._stack = AsyncExitStack()
        self._mic: AudioSource | None = None
        self._speaker: asyncio.Task[None] | None = None
        self._timer: IdleTimer | None = None
        self._cost_before: tuple[int, float | None] = (0, None)

    async def __aenter__(self) -> Conversation:
        self._cost_before = (self._tracker.interactions, self._tracker.total_usd)
        try:
            self._mic = await self._stack.enter_async_context(MicSource(self._settings))
            if self._settings.tts_enabled:
                tts = await self._stack.enter_async_context(ElevenLabsTTS(self._settings))
                sink = await self._stack.enter_async_context(SpeakerSink(self._settings))
                self._speaker = asyncio.create_task(self._speak_loop(tts, sink), name="speak")
                self._stack.push_async_callback(_stop_task, self._speaker)
        except BaseException:
            await self._stack.aclose()
            raise
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        await self._stack.aclose()

    async def serve(self) -> None:
        """Keep a Realtime session open, reconnecting with backoff. Returns only by raising."""
        backoff = 1.0
        while True:
            try:
                async with RealtimeClient(self._settings) as rt:
                    backoff = 1.0
                    log.info("Connected to %s. Speak to the witch.", self._settings.model)
                    await self._session(rt, timer=None)
            except RETRYABLE_ERRORS as exc:
                log.warning("Connection lost (%s). Reconnecting in %.0f s", exc, backoff)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, MAX_BACKOFF_S)

    async def run(self, timer: IdleTimer) -> EndReason:
        """One Realtime session that ends on silence, the time cap, or a stream failure."""
        async with RealtimeClient(self._settings) as rt:
            log.info("Connected to %s.", self._settings.model)
            return await self._session(rt, timer, self._openings)

    async def _session(
        self, rt: RealtimeClient, timer: IdleTimer | None, openings: OpeningPicker | None = None
    ) -> EndReason:
        assert self._mic is not None, "Conversation used before __aenter__"
        self._timer = timer
        started = asyncio.get_running_loop().time()
        if openings is not None:
            await self._send_opening(rt, openings)
        pump = asyncio.create_task(_pump_audio(self._mic, rt, self._gate), name="pump-audio")
        events = asyncio.create_task(self._handle_events(rt), name="handle-events")
        watchdog: asyncio.Task[EndReason] | None = None
        if timer is not None:
            watchdog = asyncio.create_task(self._watch(timer, started), name="watchdog")
        running = [t for t in (pump, events, watchdog) if t is not None]
        try:
            done, _pending = await asyncio.wait(running, return_when=asyncio.FIRST_COMPLETED)
            if watchdog is not None and watchdog in done:
                reason = watchdog.result()
                if reason is EndReason.MAX_DURATION and self._speaker is not None:
                    self._speaker.cancel()
                return reason
            for task in done:
                task.result()
            raise ConnectionError("Realtime session ended")
        finally:
            for task in running:
                task.cancel()
            await asyncio.gather(*running, return_exceptions=True)

    async def _send_opening(self, rt: RealtimeClient, openings: OpeningPicker) -> None:
        # Closed before the audio pump starts, so nothing said during the jump is sent.
        self._gate.close()
        self._opening_gate = True
        if self._settings.opening_delay_ms:
            await asyncio.sleep(self._settings.opening_delay_s)
        cue = openings.next()
        log.info(
            "opening> cue %d/%d %s (delay %d ms)",
            openings.position,
            openings.size,
            cue.id,
            self._settings.opening_delay_ms,
        )
        self._opening_sent_at = asyncio.get_running_loop().time()
        await rt.prompt(cue.text)

    async def _watch(self, timer: IdleTimer, started: float) -> EndReason:
        loop = asyncio.get_running_loop()
        cap = self._settings.max_conversation_s
        while True:
            if timer.expired():
                return EndReason.SILENCE
            elapsed = loop.time() - started
            if elapsed >= cap:
                return EndReason.MAX_DURATION
            remaining = min(timer.seconds_until_deadline(), cap - elapsed)
            await asyncio.sleep(max(0.05, min(remaining, 1.0)))

    def _touch(self) -> None:
        if self._timer is not None:
            self._timer.touch()

    def summary(self) -> str:
        count_before, cost_before = self._cost_before
        interactions = self._tracker.interactions - count_before
        if self._tracker.total_usd is None or cost_before is None:
            cost = "cost n/a"
        else:
            cost = f"${self._tracker.total_usd - cost_before:.4f}"
        return f"Conversation: {interactions} interactions, {cost}"

    async def _handle_events(self, rt: RealtimeClient) -> None:
        loop = asyncio.get_running_loop()
        # The opening's latency is measured from the moment its cue was sent.
        speech_ended_at: float | None = self._opening_sent_at
        self._opening_sent_at = None
        streaming = False
        latency_ms: int | None = None

        async for event in rt.events():
            match event:
                case SpeechStarted():
                    if streaming:
                        print("\n[interrupted]", flush=True)
                    speech_ended_at = None
                    self._touch()
                case SpeechStopped():
                    speech_ended_at = loop.time()
                    self._touch()
                case TextDelta(delta=delta):
                    if not streaming:
                        streaming = True
                        if speech_ended_at is not None:
                            latency_ms = round((loop.time() - speech_ended_at) * 1000)
                        print("witch> ", end="", flush=True)
                        self._touch()
                    print(delta, end="", flush=True)
                case ResponseDone() as done:
                    parsed = tags.parse(done.text) if done.status == "completed" else None
                    speech = _speech_text(parsed, self._settings)
                    entry = self._tracker.record(
                        done.response_id, done.status, done.tokens, tts_chars=len(speech)
                    )
                    _report(done, entry, latency_ms, parsed)
                    now = loop.time()
                    done_ms = _ms_between(speech_ended_at, now)
                    log.info(
                        "timing> gpt: first text %s, response complete %s (after end of speech)",
                        _fmt_ms(latency_ms),
                        _fmt_ms(done_ms),
                    )
                    if speech:
                        # A cue's close is released by this job's playback, so it must not close again.
                        if not self._opening_gate:
                            self._gate.close()
                        if self._timer is not None:
                            self._timer.hold()
                        self._queue.put_nowait(
                            SpeechJob(
                                text=speech,
                                speech_ended_at=speech_ended_at,
                                gpt_first_token_ms=latency_ms,
                                gpt_done_ms=done_ms,
                                queued_at=now,
                            )
                        )
                    elif self._opening_gate:
                        # No speech will release the cue's close, so reopen the mic now.
                        self._gate.release(self._settings.gate_tail_s)
                    self._opening_gate = False
                    streaming = False
                    latency_ms = None
                    speech_ended_at = None
                case ServerError(message=message, code=code):
                    log.warning("Realtime error (%s): %s", code, message)

        raise ConnectionError("Realtime event stream closed")

    async def _speak_loop(self, tts: TextToSpeech, sink: AudioSink) -> None:
        while True:
            job = await self._queue.get()
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
                self._gate.release(self._settings.gate_tail_s)
                if self._timer is not None:
                    self._timer.release()


async def run(settings: Settings) -> int:
    """Trigger-none mode: listen continuously, reconnecting on dropped connections."""
    tracker = CostTracker(settings.model, settings.usage_log)
    try:
        async with Conversation(settings, tracker) as conversation:
            await conversation.serve()
    except FATAL_ERRORS as exc:
        log.error("Stopping: %s", exc)
        return 1
    finally:
        _print_run_total(tracker)
    return 0


async def run_controlled(settings: Settings) -> int:
    """PIR/keyboard mode: one conversation per activation, governed by RunController."""
    tracker = CostTracker(settings.model, settings.usage_log)
    sensor, output = build_hardware(settings)
    openings = OpeningPicker() if settings.opening else None
    controller = RunController(
        settings, sensor, output, lambda: Conversation(settings, tracker, openings), FATAL_ERRORS
    )
    try:
        await controller.run()
    except FATAL_ERRORS as exc:
        log.error("Stopping: %s", exc)
        return 1
    finally:
        _print_run_total(tracker)
    return 0


async def _stop_task(task: asyncio.Task[None]) -> None:
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)


async def _pump_audio(mic: AudioSource, rt: RealtimeClient, gate: MicGate) -> None:
    async for chunk in mic.chunks():
        if gate.is_open:
            await rt.send_audio(chunk)


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


async def _speak(tts: TextToSpeech, sink: AudioSink, text: str) -> _Spoken:
    loop = asyncio.get_running_loop()
    started_at = loop.time()
    first_audio_at: float | None = None
    tts_requested_at: float | None = None

    async def audio() -> AsyncIterator[bytes]:
        nonlocal first_audio_at, tts_requested_at
        tts_requested_at = loop.time()
        async for chunk in tts.synthesize(text):
            if first_audio_at is None:
                first_audio_at = loop.time()
            yield chunk

    await sink.play(audio())
    return _Spoken(
        started_at=started_at,
        first_audio_at=first_audio_at,
        finished_at=loop.time(),
        tts_requested_at=tts_requested_at,
    )


def _ms_between(start: float | None, end: float | None) -> int | None:
    if start is None or end is None:
        return None
    return round((end - start) * 1000)


def _fmt_ms(ms: int | None) -> str:
    return "n/a" if ms is None else f"{ms} ms"


def _print_speech(job: SpeechJob, spoken: _Spoken) -> None:
    first = _fmt_ms(_ms_between(job.speech_ended_at, spoken.first_audio_at))
    print(
        f"speech> {len(job.text)} chars, first audio {first} after end of speech, "
        f"played {spoken.played_s:.1f} s",
        flush=True,
    )
    log.info(
        "timing> gpt first text %s, gpt complete %s | queue wait %s, "
        "elevenlabs first audio %s | end of speech to first audio %s, playback %.1f s",
        _fmt_ms(job.gpt_first_token_ms),
        _fmt_ms(job.gpt_done_ms),
        _fmt_ms(_ms_between(job.queued_at, spoken.tts_requested_at)),
        _fmt_ms(_ms_between(spoken.tts_requested_at, spoken.first_audio_at)),
        first,
        spoken.played_s,
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
    first = _fmt_ms(_ms_between(spoken.tts_requested_at, spoken.first_audio_at))
    print(f"first audio {first}, played {spoken.played_s:.1f} s")
    return 0


def _print_run_total(tracker: CostTracker) -> None:
    if tracker.interactions == 0:
        return
    total = "n/a" if tracker.total_usd is None else f"${tracker.total_usd:.4f}"
    print(f"Run total: {tracker.interactions} interactions, {total}")
