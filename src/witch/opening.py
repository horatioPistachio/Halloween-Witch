"""Chooses which opening cue the witch gets when a conversation starts."""

from __future__ import annotations

import random
from collections.abc import Sequence

from witch.bag import ShuffleBag
from witch.persona import OPENING_PROMPTS, Opening


class OpeningPicker(ShuffleBag[Opening]):
    """Draws every opening once before any repeats, and never repeats across a refill."""

    def __init__(
        self,
        openings: Sequence[Opening] = OPENING_PROMPTS,
        rng: random.Random | None = None,
    ) -> None:
        super().__init__(openings, rng)
