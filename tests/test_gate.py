"""Microphone gate tests using a fake clock."""

import pytest

from witch.gate import MicGate


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


def test_open_by_default():
    assert MicGate(FakeClock()).is_open


def test_release_starts_tail():
    clock = FakeClock()
    gate = MicGate(clock)
    gate.close()
    assert not gate.is_open
    gate.release(0.4)
    clock.now = 0.39
    assert not gate.is_open
    clock.now = 0.4
    assert gate.is_open


def test_two_closes_need_two_releases():
    clock = FakeClock()
    gate = MicGate(clock)
    gate.close()
    gate.close()
    gate.release(0.4)
    clock.now = 1.0
    assert not gate.is_open
    gate.release(0.4)
    clock.now = 1.4
    assert gate.is_open


def test_release_without_close_raises():
    with pytest.raises(RuntimeError):
        MicGate(FakeClock()).release(0.4)
