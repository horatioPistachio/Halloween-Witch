"""EyeController: flicker, speech, the witch-PCB window and failure isolation (fake light, real short timings)."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import Any

import pytest

from witch.eyes import EyeController, NullEyes
from witch.hardware.base import EyeLight, HardwareError
from witch.hardware.fake import LoggingEyes

SPEAK, FLICKER = 0.6, 0.15
SPEAK_DUTY = SPEAK**2
FLICKER_MAX_DUTY = FLICKER**2


class FakeLight(EyeLight):
    def __init__(self, max_duty: float = 1.0) -> None:
        super().__init__("fake", max_duty)
        self.log: list[tuple[str, float]] = []
        self.fail_on_drive = False
        self.closed = False

    async def _open(self) -> None:
        return None

    async def _drive(self, duty: float) -> None:
        if self.fail_on_drive:
            raise HardwareError("link lost")
        self.log.append(("drive", round(duty, 4)))

    async def _release(self) -> None:
        self.log.append(("release", 0.0))

    async def _close(self) -> None:
        self.closed = True

    def clear(self) -> None:
        self.log.clear()

    @property
    def last(self) -> tuple[str, float] | None:
        return self.log[-1] if self.log else None

    @property
    def drives(self) -> list[float]:
        return [value for kind, value in self.log if kind == "drive"]


def _eyes(light: FakeLight, **kwargs: Any) -> EyeController:
    options: dict[str, Any] = {
        "speak_level": SPEAK,
        "flicker_level": FLICKER,
        "flicker_cooldown_s": 0.3,
        "flicker_duration_s": 0.12,
        "flicker_step_s": (0.01, 0.02),
    }
    options.update(kwargs)
    return EyeController(light, **options)


class Speech:
    """Audio that stays 'playing' until released, so a test can look at the eyes meanwhile."""

    def __init__(self, eyes: Any, chunks: int = 1) -> None:
        self._eyes = eyes
        self._chunks = chunks
        self.finish = asyncio.Event()
        self.first_chunk = asyncio.Event()
        self.task: asyncio.Task[None] | None = None

    async def _audio(self) -> AsyncIterator[bytes]:
        for _ in range(self._chunks):
            yield b"\x00\x00"
            self.first_chunk.set()
        await self.finish.wait()

    async def _play(self) -> None:
        async with self._eyes.track(self._audio()) as chunks:
            async for _ in chunks:
                pass

    def start(self) -> None:
        self.task = asyncio.create_task(self._play())

    async def end(self) -> None:
        self.finish.set()
        assert self.task is not None
        await asyncio.wait_for(self.task, 1)


async def _settle(seconds: float = 0.05) -> None:
    await asyncio.sleep(seconds)


def test_perceptual_curve_and_ceiling_are_applied_by_the_light():
    async def scenario() -> None:
        light = FakeLight(max_duty=0.6)
        async with light:
            await light.set(1.0)
            await light.set(0.5)
            await light.set(9.0)
            await light.set(-1.0)
        assert light.drives == [0.6, 0.15, 0.6, 0.0]

    asyncio.run(scenario())


def test_the_eyes_come_on_at_the_first_audio_and_off_when_it_ends():
    async def scenario() -> None:
        light = FakeLight()
        async with _eyes(light) as eyes:
            light.clear()
            gate = asyncio.Event()
            end = asyncio.Event()

            async def late_audio() -> AsyncIterator[bytes]:
                await gate.wait()
                yield b"\x00\x00"
                await end.wait()

            async def play() -> None:
                async with eyes.track(late_audio()) as chunks:
                    async for _ in chunks:
                        pass

            task = asyncio.create_task(play())
            await _settle()
            assert light.log == []  # nothing before the first chunk (TTS latency)
            gate.set()
            await _settle()
            assert light.log == [("drive", SPEAK_DUTY)]
            end.set()
            await asyncio.wait_for(task, 1)
            await _settle()
            assert light.last == ("release", 0.0)

    asyncio.run(scenario())


def test_cancelled_speech_releases_the_eyes():
    async def scenario() -> None:
        light = FakeLight()
        async with _eyes(light) as eyes:
            speech = Speech(eyes)
            speech.start()
            await asyncio.wait_for(speech.first_chunk.wait(), 1)
            await _settle()
            assert light.last == ("drive", SPEAK_DUTY)
            speech.task.cancel()
            await asyncio.gather(speech.task, return_exceptions=True)
            await _settle()
            assert light.last == ("release", 0.0)

    asyncio.run(scenario())


def test_overlapping_speakers_keep_the_eyes_on_until_the_last_ends():
    async def scenario() -> None:
        light = FakeLight()
        async with _eyes(light) as eyes:
            first, second = Speech(eyes), Speech(eyes)
            first.start()
            second.start()
            await asyncio.wait_for(asyncio.gather(first.first_chunk.wait(), second.first_chunk.wait()), 1)
            await first.end()
            await _settle()
            assert light.last == ("drive", SPEAK_DUTY)
            await second.end()
            await _settle()
            assert light.last == ("release", 0.0)

    asyncio.run(scenario())


def test_a_flicker_is_dim_brief_and_ends_released():
    async def scenario() -> None:
        light = FakeLight()
        async with _eyes(light) as eyes:
            light.clear()
            eyes.flicker()
            await _settle(0.3)
            assert len(light.drives) >= 3
            assert max(light.drives) <= FLICKER_MAX_DUTY + 1e-9
            assert light.last == ("release", 0.0)

    asyncio.run(scenario())


def test_flickers_are_rate_limited():
    async def scenario() -> None:
        light = FakeLight()
        async with _eyes(light, flicker_cooldown_s=0.4) as eyes:
            eyes.flicker()
            await _settle(0.2)  # first flicker is over
            light.clear()
            eyes.flicker()  # inside the cool-down
            await _settle(0.1)
            assert light.log == []
            await _settle(0.25)  # cool-down over
            eyes.flicker()
            await _settle(0.05)
            assert light.drives

    asyncio.run(scenario())


def test_no_flicker_while_speaking():
    async def scenario() -> None:
        light = FakeLight()
        async with _eyes(light) as eyes:
            speech = Speech(eyes)
            speech.start()
            await asyncio.wait_for(speech.first_chunk.wait(), 1)
            await _settle()
            light.clear()
            eyes.flicker()
            await _settle(0.2)
            assert light.log == []
            await speech.end()

    asyncio.run(scenario())


def test_speech_takes_over_from_a_flicker():
    async def scenario() -> None:
        light = FakeLight()
        async with _eyes(light, flicker_duration_s=1.0) as eyes:
            eyes.flicker()
            await _settle(0.1)
            speech = Speech(eyes)
            speech.start()
            await asyncio.wait_for(speech.first_chunk.wait(), 1)
            await _settle(0.1)
            light.clear()
            await _settle(0.15)
            assert light.log == []  # steady: no more flicker steps
            assert light.drives == []
            await speech.end()
            await _settle()
            assert light.last == ("release", 0.0)

    asyncio.run(scenario())


def test_hold_off_releases_the_pin_before_returning_and_blocks_flicker():
    async def scenario() -> None:
        light = FakeLight()
        async with _eyes(light, flicker_cooldown_s=0.0) as eyes:
            speech = Speech(eyes)
            speech.start()
            await asyncio.wait_for(speech.first_chunk.wait(), 1)
            await _settle()
            assert light.last == ("drive", SPEAK_DUTY)
            await eyes.hold_off(0.25)
            assert light.last == ("release", 0.0)  # already free when hold_off returns
            await speech.end()
            light.clear()
            eyes.flicker()
            await _settle(0.1)
            assert light.log == []

    asyncio.run(scenario())


def test_speech_inside_the_pcb_window_waits_for_the_window_to_end():
    async def scenario() -> None:
        light = FakeLight()
        async with _eyes(light) as eyes:
            await eyes.hold_off(0.2)
            light.clear()
            speech = Speech(eyes)
            speech.start()
            await asyncio.wait_for(speech.first_chunk.wait(), 1)
            await _settle(0.08)
            assert light.drives == []  # the PCB may be driving the eyes
            await _settle(0.2)
            assert light.drives == [SPEAK_DUTY]
            await speech.end()

    asyncio.run(scenario())


def test_a_hardware_error_disables_the_eyes_without_raising(caplog):
    async def scenario() -> None:
        light = FakeLight()
        async with _eyes(light) as eyes:
            light.fail_on_drive = True
            eyes.flicker()
            await _settle(0.1)
            speech = Speech(eyes)
            speech.start()
            await asyncio.wait_for(speech.first_chunk.wait(), 1)
            await speech.end()  # the audio itself is unaffected
            eyes.flicker()
            light.clear()
            # Even when disabled, the pin is freed before the pulse.
            await eyes.hold_off(0.1)
            assert light.last == ("release", 0.0)
        assert "disabled" in caplog.text

    asyncio.run(scenario())


def test_the_pin_is_released_and_closed_on_exit():
    async def scenario() -> None:
        light = FakeLight()
        async with _eyes(light) as eyes:
            speech = Speech(eyes)
            speech.start()
            await asyncio.wait_for(speech.first_chunk.wait(), 1)
            await _settle()
            speech.task.cancel()
            await asyncio.gather(speech.task, return_exceptions=True)
        assert light.last == ("release", 0.0)
        assert light.closed

    asyncio.run(scenario())


def test_a_failing_release_on_exit_is_logged_not_raised(caplog):
    async def scenario() -> None:
        light = FakeLight()

        async def broken() -> None:
            raise HardwareError("link lost")

        async with _eyes(light):
            light._release = broken  # type: ignore[method-assign]

    asyncio.run(scenario())
    assert "could not release" in caplog.text


def test_null_eyes_pass_audio_through_and_do_nothing():
    async def scenario() -> None:
        eyes = NullEyes()
        async with eyes:
            eyes.flicker()
            await eyes.hold_off(5)

            async def audio() -> AsyncIterator[bytes]:
                yield b"a"
                yield b"b"

            async with eyes.track(audio()) as chunks:
                assert [c async for c in chunks] == [b"a", b"b"]

    asyncio.run(scenario())


def test_logging_eyes_open_released():
    async def scenario() -> None:
        async with LoggingEyes("eyes", 0.6) as eyes:
            await eyes.set(1.0)
            await eyes.release()

    asyncio.run(scenario())


@pytest.mark.parametrize("level", [0.0, 0.5, 1.0])
def test_levels_never_exceed_the_ceiling(level):
    async def scenario() -> None:
        light = FakeLight(max_duty=0.3)
        async with light:
            await light.set(level)
        assert all(duty <= 0.3 for duty in light.drives)

    asyncio.run(scenario())
