"""Presence confidence accumulator (pure, no clock)."""

import random

import pytest

from witch.presence import PresenceTracker


class FixedRng:
    def uniform(self, low: float, high: float) -> float:
        return 1.0


def _tracker(base: float = 10.0, decay: float = 0.5) -> PresenceTracker:
    return PresenceTracker(base, decay, FixedRng())  # type: ignore[arg-type]


def test_continuous_presence_triggers_at_the_threshold_and_not_before():
    tracker = _tracker()
    results = [tracker.update(True, 1.0) for _ in range(10)]
    assert results == [False] * 9 + [True]


def test_absence_drains_confidence_at_the_decay_rate_and_clamps_at_zero():
    tracker = _tracker(decay=0.5)
    tracker.update(True, 4.0)
    tracker.update(False, 4.0)
    assert tracker.confidence == pytest.approx(2.0)
    tracker.update(False, 100.0)
    assert tracker.confidence == 0.0


def test_a_brief_burst_followed_by_silence_never_triggers():
    tracker = _tracker()
    assert not tracker.update(True, 3.0)
    assert not any(tracker.update(False, 1.0) for _ in range(30))
    assert tracker.confidence == 0.0


def test_intermittent_presence_takes_longer_than_continuous():
    tracker = _tracker()
    samples = 0
    while True:
        samples += 1
        if tracker.update(samples % 2 == 1, 1.0):
            break
    assert samples > 10


def test_progress_is_a_fraction_of_the_threshold():
    tracker = _tracker()
    assert tracker.progress == 0.0
    tracker.update(True, 5.0)
    assert tracker.progress == pytest.approx(0.5)


def test_reset_clears_confidence():
    tracker = _tracker()
    tracker.update(True, 6.0)
    tracker.reset()
    assert tracker.confidence == 0.0
    assert tracker.progress == 0.0


def test_a_zero_threshold_triggers_on_the_first_high_sample_only():
    tracker = _tracker(base=0.0)
    assert not tracker.update(False, 1.0)
    assert tracker.update(True, 0.1)
    assert tracker.progress == 1.0


@pytest.mark.parametrize("seed", range(50))
def test_threshold_stays_within_the_jitter_range(seed):
    tracker = PresenceTracker(10.0, 0.5, random.Random(seed))
    assert 5.0 <= tracker.threshold <= 15.0


def test_the_same_seed_gives_the_same_threshold_and_reset_rolls_a_new_one():
    first = PresenceTracker(10.0, 0.5, random.Random(7))
    second = PresenceTracker(10.0, 0.5, random.Random(7))
    assert first.threshold == second.threshold
    before = first.threshold
    first.reset()
    assert first.threshold != before
