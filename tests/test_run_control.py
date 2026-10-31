"""Run-control config, idle timer, and the activation state machine (hardware faked)."""

from __future__ import annotations

import asyncio
import random
from collections.abc import Callable

import pytest

from witch.config import (
    DEFAULT_ACTIVATE_PIN,
    DEFAULT_IDLE_TIMEOUT_S,
    DEFAULT_MAX_CONVERSATION_S,
    DEFAULT_PHRASE_CACHE_DIR,
    DEFAULT_PIR_PIN,
    DEFAULT_PIR_POWER_PIN,
    DEFAULT_PIR_WARMUP_S,
    ConfigError,
    load_settings,
)
from witch.control import EndReason, IdleTimer, RunController
from witch.hardware.base import ActivationOutput

BASE_ENV = {"OPENAI_API_KEY": "sk-test-123", "WITCH_TTS": "off"}


def _env(**extra: str) -> dict[str, str]:
    return {**BASE_ENV, **extra}


# --- config -----------------------------------------------------------------


def test_trigger_defaults_to_none_and_skips_run_control_validation():
    settings = load_settings(environ=_env(WITCH_PIR_PIN="not-a-pin", WITCH_MAX_CONVERSATION_S="1"))
    assert settings.trigger == "none"


def test_pir_defaults():
    settings = load_settings(environ=_env(WITCH_TRIGGER="pir"))
    assert settings.pir_pin == DEFAULT_PIR_PIN
    assert settings.pir_power_pin == DEFAULT_PIR_POWER_PIN
    assert settings.activate_pin == DEFAULT_ACTIVATE_PIN
    assert settings.pir_warmup_s == DEFAULT_PIR_WARMUP_S
    assert settings.idle_timeout_s == DEFAULT_IDLE_TIMEOUT_S
    assert settings.max_conversation_s == DEFAULT_MAX_CONVERSATION_S


def test_empty_power_pin_disables_it():
    settings = load_settings(environ=_env(WITCH_TRIGGER="pir", WITCH_PIR_POWER_PIN=""))
    assert settings.pir_power_pin is None


def test_cli_trigger_overrides_env():
    settings = load_settings(trigger="keyboard", environ=_env(WITCH_TRIGGER="pir"))
    assert settings.trigger == "keyboard"


@pytest.mark.parametrize(
    "extra",
    [
        {"WITCH_TRIGGER": "bogus"},
        {"WITCH_TRIGGER": "pir", "WITCH_PIR_PIN": "28"},
        {"WITCH_TRIGGER": "pir", "WITCH_ACTIVATE_PIN": "27"},
        {"WITCH_TRIGGER": "pir", "WITCH_PIR_POWER_PIN": "17", "WITCH_ACTIVATE_PIN": "17"},
        {"WITCH_TRIGGER": "pir", "WITCH_IDLE_TIMEOUT_S": "0"},
        {"WITCH_TRIGGER": "pir", "WITCH_IDLE_TIMEOUT_S": "60", "WITCH_MAX_CONVERSATION_S": "30"},
        {"WITCH_TRIGGER": "pir", "WITCH_COOLDOWN_S": "-1"},
        {"WITCH_TRIGGER": "pir", "WITCH_ACTIVATE_MS": "0"},
    ],
)
def test_invalid_run_control_rejected(extra):
    with pytest.raises(ConfigError):
        load_settings(environ=_env(**extra))


# --- idle timer -------------------------------------------------------------


def test_idle_timer_expires_after_timeout_and_touch_resets_it():
    now = [0.0]
    timer = IdleTimer(10, clock=lambda: now[0])
    timer.start()
    now[0] = 9.0
    assert not timer.expired()
    timer.touch()
    now[0] = 18.0
    assert not timer.expired()
    now[0] = 19.5
    assert timer.expired()
    assert timer.seconds_until_deadline() == 0.0


def test_held_time_does_not_count_toward_idle():
    now = [0.0]
    timer = IdleTimer(10, clock=lambda: now[0])
    timer.start()
    now[0] = 6.0
    timer.hold()
    now[0] = 100.0
    assert not timer.expired()
    timer.release()
    now[0] = 103.0
    assert timer.seconds_until_deadline() == pytest.approx(1.0)
    now[0] = 110.0
    assert timer.expired()


# --- presence config ---------------------------------------------------------


def test_presence_and_ambient_defaults():
    settings = load_settings(environ=_env(WITCH_TRIGGER="pir"))
    assert settings.presence_threshold_s == 10.0
    assert settings.presence_decay == 0.5
    assert settings.ambient_interval_s == 300.0
    assert settings.phrase_cache_dir == DEFAULT_PHRASE_CACHE_DIR


def test_presence_and_ambient_overrides():
    settings = load_settings(
        environ=_env(
            WITCH_TRIGGER="keyboard",
            WITCH_PRESENCE_THRESHOLD_S="0",
            WITCH_PRESENCE_DECAY="0",
            WITCH_AMBIENT_INTERVAL_S="0",
            WITCH_PHRASE_CACHE_DIR="",
        )
    )
    assert settings.presence_threshold_s == 0
    assert settings.presence_decay == 0
    assert settings.ambient_interval_s == 0
    assert settings.phrase_cache_dir is None


@pytest.mark.parametrize(
    "name", ["WITCH_PRESENCE_THRESHOLD_S", "WITCH_PRESENCE_DECAY", "WITCH_AMBIENT_INTERVAL_S"]
)
@pytest.mark.parametrize("value", ["-1", "soon"])
def test_invalid_presence_settings_rejected(name, value):
    with pytest.raises(ConfigError, match=name):
        load_settings(environ=_env(WITCH_TRIGGER="pir", **{name: value}))


def test_presence_settings_are_not_validated_without_a_trigger():
    settings = load_settings(environ=_env(WITCH_PRESENCE_THRESHOLD_S="soon"))
    assert settings.trigger == "none"


# --- controller state machine -----------------------------------------------


class FatalTestError(Exception):
    pass


class FakeTime:
    """A clock that only moves when the controller sleeps, so minutes pass in milliseconds."""

    def __init__(self, stop_at: float = 1e9) -> None:
        self.now = 0.0
        self.stop_at = stop_at
        self.done = asyncio.Event()

    def clock(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.now += seconds
        if self.now >= self.stop_at:
            self.done.set()
        await asyncio.sleep(0)


class FixedRng:
    """Makes every jitter factor 1.0 so thresholds and intervals are exact."""

    def uniform(self, low: float, high: float) -> float:
        return 1.0


class FakeSensor:
    def __init__(self, time: FakeTime, level: Callable[[float], bool] = lambda t: False) -> None:
        self._time = time
        self._level = level
        self.presses = 0

    async def __aenter__(self) -> FakeSensor:
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        return None

    @property
    def is_active(self) -> bool:
        return self._level(self._time.now)

    def take_manual_trigger(self) -> bool:
        pressed, self.presses = self.presses > 0, 0
        return pressed

    async def wait_for_still(self) -> None:
        return None


class FakeOutput(ActivationOutput):
    def __init__(self) -> None:
        super().__init__("fake")
        self.levels: list[bool] = []
        self.open = False

    async def _open(self) -> None:
        self.open = True

    async def _set(self, high: bool) -> None:
        self.levels.append(high)

    async def _close(self) -> None:
        self.open = False


class FakeConversation:
    def __init__(self, setup_error: Exception | None = None, run_error: Exception | None = None) -> None:
        self._setup_error = setup_error
        self._run_error = run_error
        self.exited = False

    async def __aenter__(self) -> FakeConversation:
        if self._setup_error is not None:
            raise self._setup_error
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        self.exited = True

    async def run(self, timer: IdleTimer | None) -> EndReason:
        if self._run_error is not None:
            raise self._run_error
        return EndReason.SILENCE

    def summary(self) -> str:
        return "fake summary"


class FakeAmbient:
    def __init__(self, time: FakeTime, duration_s: float = 5.0, on_play: Callable[[], None] | None = None) -> None:
        self._time = time
        self._duration = duration_s
        self._on_play = on_play
        self.played_at: list[float] = []

    async def play_next(self) -> None:
        self.played_at.append(self._time.now)
        if self._on_play is not None:
            self._on_play()
        self._time.now += self._duration


def _controller_settings(**extra: str):
    return load_settings(
        environ=_env(
            WITCH_TRIGGER="pir",
            WITCH_PIR_WARMUP_S="0",
            WITCH_COOLDOWN_S="0",
            WITCH_ACTIVATE_MS="10",
            **{"WITCH_PRESENCE_THRESHOLD_S": "0", **extra},
        )
    )


async def _run_controller(
    time: FakeTime,
    settings,
    sensor: FakeSensor,
    factory: Callable[[], FakeConversation],
    ambient: FakeAmbient | None = None,
    output: FakeOutput | None = None,
    rng: random.Random | FixedRng | None = None,
) -> FakeOutput:
    output = output or FakeOutput()
    controller = RunController(
        settings,
        sensor,
        output,
        factory,
        (FatalTestError,),
        ambient=ambient,
        clock=time.clock,
        sleep=time.sleep,
        rng=rng if rng is not None else random.Random(1),  # type: ignore[arg-type]
    )
    task = asyncio.create_task(controller.run())
    await asyncio.wait_for(time.done.wait(), 5)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    return output


class _Conversations:
    def __init__(self) -> None:
        self.made: list[FakeConversation] = []

    def __call__(self) -> FakeConversation:
        self.made.append(FakeConversation())
        return self.made[-1]


def test_one_motion_runs_one_conversation_and_pulses_then_rearms():
    async def scenario() -> None:
        time = FakeTime(stop_at=5)
        conversations = _Conversations()
        sensor = FakeSensor(time, level=lambda t: not conversations.made)
        output = await _run_controller(time, _controller_settings(), sensor, conversations)

        assert len(conversations.made) == 1
        assert conversations.made[0].exited
        assert output.levels == [False, True, False, False]
        assert not output.open

    asyncio.run(scenario())


def test_setup_failure_skips_pulse_and_keeps_running():
    async def scenario() -> None:
        time = FakeTime(stop_at=5)
        attempts: list[int] = []

        def failing() -> FakeConversation:
            attempts.append(1)
            return FakeConversation(setup_error=OSError("no mic"))

        sensor = FakeSensor(time, level=lambda t: not attempts)
        output = await _run_controller(time, _controller_settings(), sensor, failing)
        assert attempts == [1]
        assert True not in output.levels

    asyncio.run(scenario())


def test_fatal_error_stops_the_controller():
    async def scenario() -> None:
        time = FakeTime()
        controller = RunController(
            _controller_settings(),
            FakeSensor(time, level=lambda t: True),
            FakeOutput(),
            lambda: FakeConversation(run_error=FatalTestError("bad key")),
            (FatalTestError,),
            clock=time.clock,
            sleep=time.sleep,
        )
        with pytest.raises(FatalTestError):
            await asyncio.wait_for(controller.run(), 2)

    asyncio.run(scenario())


# --- presence confidence ----------------------------------------------------


def test_a_brief_burst_does_not_activate():
    async def scenario() -> None:
        time = FakeTime(stop_at=120)
        conversations = _Conversations()
        sensor = FakeSensor(time, level=lambda t: 1 <= t < 3)
        await _run_controller(
            time, _controller_settings(WITCH_PRESENCE_THRESHOLD_S="10"), sensor, conversations
        )
        assert conversations.made == []

    asyncio.run(scenario())


def test_sustained_presence_activates_after_the_threshold():
    async def scenario() -> None:
        time = FakeTime(stop_at=40)
        conversations = _Conversations()
        activated_at: list[float] = []

        def factory() -> FakeConversation:
            activated_at.append(time.now)
            return conversations()

        sensor = FakeSensor(time, level=lambda t: t >= 1)
        settings = _controller_settings(WITCH_PRESENCE_THRESHOLD_S="10")
        await _run_controller(time, settings, sensor, factory, rng=FixedRng())
        assert activated_at[0] == pytest.approx(11.0, abs=0.3)

    asyncio.run(scenario())


def test_presence_must_be_rebuilt_after_a_conversation():
    async def scenario() -> None:
        time = FakeTime(stop_at=30)
        activated_at: list[float] = []
        conversations = _Conversations()

        def factory() -> FakeConversation:
            activated_at.append(time.now)
            return conversations()

        sensor = FakeSensor(time, level=lambda t: True)
        settings = _controller_settings(WITCH_PRESENCE_THRESHOLD_S="10")
        await _run_controller(time, settings, sensor, factory, rng=FixedRng())
        assert len(activated_at) >= 2
        assert activated_at[1] - activated_at[0] == pytest.approx(10.0, abs=0.3)

    asyncio.run(scenario())


def test_a_manual_trigger_bypasses_the_threshold():
    async def scenario() -> None:
        time = FakeTime(stop_at=5)
        conversations = _Conversations()
        sensor = FakeSensor(time)
        original_sleep = time.sleep

        async def sleep(seconds: float) -> None:
            if time.now >= 1.0 and not conversations.made:
                sensor.presses += 1
            await original_sleep(seconds)

        time.sleep = sleep  # type: ignore[method-assign]
        await _run_controller(
            time, _controller_settings(WITCH_PRESENCE_THRESHOLD_S="100"), sensor, conversations
        )
        assert len(conversations.made) == 1

    asyncio.run(scenario())


# --- ambient sounds ---------------------------------------------------------


def _ambient_settings(**extra: str):
    return _controller_settings(WITCH_PRESENCE_THRESHOLD_S="20", WITCH_AMBIENT_INTERVAL_S="300", **extra)


def test_ambient_sound_plays_on_schedule_when_the_pir_only_blips():
    async def scenario() -> None:
        time = FakeTime(stop_at=400)
        conversations = _Conversations()
        ambient = FakeAmbient(time)
        # One second of PIR every 20 s never lifts confidence to 50% of the threshold.
        sensor = FakeSensor(time, level=lambda t: t % 20 < 1)
        await _run_controller(time, _ambient_settings(), sensor, conversations, ambient, rng=FixedRng())
        assert conversations.made == []
        assert ambient.played_at[0] == pytest.approx(300.0, abs=0.5)

    asyncio.run(scenario())


def test_ambient_sound_is_postponed_while_confidence_is_above_half_the_threshold():
    async def scenario() -> None:
        time = FakeTime(stop_at=450)
        conversations = _Conversations()
        ambient = FakeAmbient(time)
        # 15 s of presence against a threshold of 20 s: above 50% for about 15 s, never activates.
        sensor = FakeSensor(time, level=lambda t: 100 <= t < 115)
        await _run_controller(time, _ambient_settings(), sensor, conversations, ambient, rng=FixedRng())
        assert conversations.made == []
        assert ambient.played_at[0] == pytest.approx(425.0, abs=1.0)

    asyncio.run(scenario())


def test_a_manual_trigger_pressed_during_a_sound_activates_afterwards():
    async def scenario() -> None:
        time = FakeTime(stop_at=320)
        conversations = _Conversations()
        sensor = FakeSensor(time)
        ambient = FakeAmbient(time, on_play=lambda: setattr(sensor, "presses", 1))
        await _run_controller(time, _ambient_settings(), sensor, conversations, ambient, rng=FixedRng())
        assert len(ambient.played_at) == 1
        assert len(conversations.made) == 1

    asyncio.run(scenario())


def test_no_ambient_player_means_no_timer():
    async def scenario() -> None:
        time = FakeTime(stop_at=1000)
        conversations = _Conversations()
        await _run_controller(time, _ambient_settings(), FakeSensor(time), conversations, None)
        assert conversations.made == []

    asyncio.run(scenario())

