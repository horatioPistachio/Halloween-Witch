"""Run-control config, idle timer, and the activation state machine (hardware faked)."""

from __future__ import annotations

import asyncio

import pytest

from witch.config import (
    DEFAULT_ACTIVATE_PIN,
    DEFAULT_IDLE_TIMEOUT_S,
    DEFAULT_MAX_CONVERSATION_S,
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


# --- controller state machine -----------------------------------------------


class FatalTestError(Exception):
    pass


class FakeSensor:
    def __init__(self, motions: int) -> None:
        self._motions = motions
        self.idle = asyncio.Event()

    async def __aenter__(self) -> FakeSensor:
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        return None

    @property
    def is_active(self) -> bool:
        return False

    async def wait_for_motion(self) -> None:
        if self._motions > 0:
            self._motions -= 1
            return
        self.idle.set()
        await asyncio.Event().wait()

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


def _controller_settings(**extra: str):
    return load_settings(
        environ=_env(
            WITCH_TRIGGER="pir",
            WITCH_PIR_WARMUP_S="0",
            WITCH_COOLDOWN_S="0",
            WITCH_ACTIVATE_MS="10",
            **extra,
        )
    )


def test_one_motion_runs_one_conversation_and_pulses_then_rearms():
    async def scenario() -> None:
        settings = _controller_settings()
        sensor = FakeSensor(motions=1)
        output = FakeOutput()
        conversations: list[FakeConversation] = []

        def factory() -> FakeConversation:
            conversations.append(FakeConversation())
            return conversations[-1]

        controller = RunController(settings, sensor, output, factory, (FatalTestError,))
        task = asyncio.create_task(controller.run())
        await asyncio.wait_for(sensor.idle.wait(), 2)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

        assert len(conversations) == 1
        assert conversations[0].exited
        assert output.levels == [False, True, False, False]
        assert not output.open

    asyncio.run(scenario())


def test_setup_failure_skips_pulse_and_keeps_running():
    async def scenario() -> None:
        settings = _controller_settings()
        sensor = FakeSensor(motions=1)
        output = FakeOutput()
        controller = RunController(
            settings,
            sensor,
            output,
            lambda: FakeConversation(setup_error=OSError("no mic")),
            (FatalTestError,),
        )
        task = asyncio.create_task(controller.run())
        await asyncio.wait_for(sensor.idle.wait(), 2)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert True not in output.levels

    asyncio.run(scenario())


def test_fatal_error_stops_the_controller():
    async def scenario() -> None:
        settings = _controller_settings()
        controller = RunController(
            settings,
            FakeSensor(motions=1),
            FakeOutput(),
            lambda: FakeConversation(run_error=FatalTestError("bad key")),
            (FatalTestError,),
        )
        with pytest.raises(FatalTestError):
            await asyncio.wait_for(controller.run(), 2)

    asyncio.run(scenario())
