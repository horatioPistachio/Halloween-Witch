"""Chooses which opening cue the witch gets when a conversation starts."""

from __future__ import annotations

import random
from collections.abc import Sequence

from witch.persona import OPENING_PROMPTS, Opening


class OpeningPicker:
    """Draws every opening once before any repeats, and never repeats across a refill."""

    def __init__(
        self,
        openings: Sequence[Opening] = OPENING_PROMPTS,
        rng: random.Random | None = None,
    ) -> None:
        self._openings = tuple(openings)
        self._rng = rng if rng is not None else random.Random()
        self._bag: list[Opening] = []
        self._last: Opening | None = None
        self._position = 0

    @property
    def size(self) -> int:
        return len(self._openings)

    @property
    def position(self) -> int:
        """1-based number of the latest draw within the current bag."""
        return self._position

    def next(self) -> Opening:
        if not self._bag:
            self._refill()
        opening = self._bag.pop()
        self._last = opening
        self._position += 1
        return opening

    def _refill(self) -> None:
        self._bag = list(self._openings)
        self._rng.shuffle(self._bag)
        self._position = 0
        # The bag is drawn from the end, so the last element is the next draw.
        if len(self._bag) > 1 and self._bag[-1] == self._last:
            self._bag[-1], self._bag[-2] = self._bag[-2], self._bag[-1]
