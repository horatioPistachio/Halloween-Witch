"""Opening cue prompts, the picker that chooses them, and the persona rule that makes them work."""

import random

import pytest

from witch.opening import OpeningPicker
from witch.persona import OPENING_PROMPTS, PERSONA_PROMPTS


def test_there_are_ten_unique_stage_cues():
    assert len(OPENING_PROMPTS) == 10
    assert len({opening.id for opening in OPENING_PROMPTS}) == 10
    assert len({opening.text for opening in OPENING_PROMPTS}) == 10


@pytest.mark.parametrize("opening", OPENING_PROMPTS, ids=lambda o: o.id)
def test_each_cue_is_a_short_stage_direction(opening):
    assert opening.text.startswith("Stage cue:")
    assert len(opening.text.split()) < 30
    # Square brackets would be read as witch tags, so cues must not use them.
    assert "[" not in opening.text and "]" not in opening.text


@pytest.mark.parametrize("persona", sorted(PERSONA_PROMPTS))
def test_persona_tells_the_witch_that_stage_cues_are_direction(persona):
    assert 'starts with "Stage cue:"' in PERSONA_PROMPTS[persona]


def test_a_full_bag_contains_every_opening_once():
    picker = OpeningPicker(rng=random.Random(1))
    drawn = [picker.next().id for _ in range(picker.size)]
    assert sorted(drawn) == sorted(opening.id for opening in OPENING_PROMPTS)


def test_no_repeat_across_refills_over_many_seeds():
    for seed in range(200):
        picker = OpeningPicker(rng=random.Random(seed))
        draws = [picker.next().id for _ in range(3 * picker.size)]
        for start in (0, picker.size, 2 * picker.size):
            assert len(set(draws[start : start + picker.size])) == picker.size
        assert draws[picker.size - 1] != draws[picker.size]
        assert draws[2 * picker.size - 1] != draws[2 * picker.size]


def test_same_seed_gives_same_sequence():
    first = OpeningPicker(rng=random.Random(7))
    second = OpeningPicker(rng=random.Random(7))
    assert [first.next().id for _ in range(25)] == [second.next().id for _ in range(25)]


def test_position_counts_draws_within_the_bag():
    picker = OpeningPicker(rng=random.Random(3))
    positions = []
    for _ in range(picker.size + 2):
        picker.next()
        positions.append(picker.position)
    assert positions == [*range(1, picker.size + 1), 1, 2]
