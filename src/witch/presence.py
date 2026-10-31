"""Presence confidence: PIR-high time builds it, PIR-low time drains it."""

from __future__ import annotations

import random

from witch.config import PRESENCE_JITTER


class PresenceTracker:
    """Pure accumulator with no clock; the caller supplies the elapsed time of each sample."""

    def __init__(
        self,
        base_threshold_s: float,
        decay: float,
        rng: random.Random | None = None,
        jitter: tuple[float, float] = PRESENCE_JITTER,
    ) -> None:
        self._base = base_threshold_s
        self._decay = decay
        self._rng = rng if rng is not None else random.Random()
        self._jitter = jitter
        self._confidence = 0.0
        self._threshold = 0.0
        self.reset()

    @property
    def confidence(self) -> float:
        return self._confidence

    @property
    def threshold(self) -> float:
        return self._threshold

    @property
    def progress(self) -> float:
        """Confidence as a fraction of the threshold (1.0 when the threshold is 0 and confidence is above 0)."""
        if self._confidence <= 0:
            return 0.0
        if self._threshold <= 0:
            return 1.0
        return self._confidence / self._threshold

    def reset(self) -> None:
        """Confidence back to 0, with a new random threshold."""
        self._confidence = 0.0
        self._threshold = self._base * self._rng.uniform(*self._jitter)

    def update(self, active: bool, dt: float) -> bool:
        """Apply one sample. True once confidence has reached the threshold."""
        if active:
            self._confidence += dt
        else:
            self._confidence = max(0.0, self._confidence - dt * self._decay)
        return self._confidence > 0 and self._confidence >= self._threshold
