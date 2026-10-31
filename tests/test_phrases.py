"""The shuffle bag and the periodic phrase list."""

import random

import pytest

from witch.bag import ShuffleBag
from witch.persona import ALLOWED_TAGS
from witch.phrases import PERIODIC_PHRASES
from witch.tags import parse, trusted_speech_text


def test_a_bag_draws_every_item_once_per_round_and_never_repeats_across_rounds():
    for seed in range(50):
        bag = ShuffleBag(list(range(5)), random.Random(seed))
        draws = [bag.next() for _ in range(5 * 6)]
        for start in range(0, len(draws), 5):
            assert sorted(draws[start : start + 5]) == [0, 1, 2, 3, 4]
        assert all(a != b for a, b in zip(draws, draws[1:]))


def test_a_one_item_bag_repeats_that_item():
    bag = ShuffleBag(["only"])
    assert [bag.next() for _ in range(3)] == ["only"] * 3


def test_the_same_seed_gives_the_same_sequence():
    first = ShuffleBag(list("abcdef"), random.Random(3))
    second = ShuffleBag(list("abcdef"), random.Random(3))
    assert [first.next() for _ in range(12)] == [second.next() for _ in range(12)]


def test_position_and_size():
    bag = ShuffleBag([1, 2, 3])
    bag.next()
    bag.next()
    assert (bag.position, bag.size) == (2, 3)


def test_there_are_phrases_and_none_repeat():
    assert PERIODIC_PHRASES
    assert len(set(PERIODIC_PHRASES)) == len(PERIODIC_PHRASES)


@pytest.mark.parametrize("phrase", PERIODIC_PHRASES)
@pytest.mark.parametrize("keep_tags", [True, False])
def test_every_phrase_has_speech_text(phrase, keep_tags):
    assert isinstance(phrase, str)
    assert trusted_speech_text(phrase, keep_tags=keep_tags).strip()


def test_tags_are_kept_verbatim_or_stripped():
    phrase = "[cackle] [angry] who goes there?"
    assert trusted_speech_text(phrase, keep_tags=True) == phrase
    assert trusted_speech_text(phrase, keep_tags=False) == "who goes there?"


@pytest.mark.parametrize("phrase", PERIODIC_PHRASES)
def test_ambient_phrase_tags_are_allowed(phrase):
    response = parse(phrase)
    assert not response.unknown_tags
    assert all(tag in ALLOWED_TAGS for tag in response.tags)
