"""Half-duplex gate that keeps the microphone muted while the witch is speaking."""

from __future__ import annotations

import time
from collections.abc import Callable


class MicGate:
    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self._pending = 0
        self._reopen_at = 0.0

    @property
    def is_open(self) -> bool:
        return self._pending == 0 and self._clock() >= self._reopen_at

    def close(self) -> None:
        self._pending += 1

    def release(self, tail_s: float) -> None:
        if self._pending == 0:
            raise RuntimeError("MicGate.release() called without a matching close()")
        self._pending -= 1
        if self._pending == 0:
            self._reopen_at = self._clock() + tail_s
