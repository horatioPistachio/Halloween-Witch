"""Shuffle-bag selection: every item once before any repeats, never the same item twice in a row."""

from __future__ import annotations

import random
from collections.abc import Sequence
from typing import Generic, TypeVar

T = TypeVar("T")


class ShuffleBag(Generic[T]):
    def __init__(self, items: Sequence[T], rng: random.Random | None = None) -> None:
        self._items = tuple(items)
        self._rng = rng if rng is not None else random.Random()
        self._bag: list[T] = []
        self._last: T | None = None
        self._position = 0

    @property
    def size(self) -> int:
        return len(self._items)

    @property
    def position(self) -> int:
        """1-based number of the latest draw within the current bag."""
        return self._position

    def next(self) -> T:
        if not self._bag:
            self._refill()
        item = self._bag.pop()
        self._last = item
        self._position += 1
        return item

    def _refill(self) -> None:
        self._bag = list(self._items)
        self._rng.shuffle(self._bag)
        self._position = 0
        # The bag is drawn from the end, so the last element is the next draw.
        if len(self._bag) > 1 and self._bag[-1] == self._last:
            self._bag[-1], self._bag[-2] = self._bag[-2], self._bag[-1]
