"""Linear-interpolation resampling for mono 16-bit little-endian PCM."""

from __future__ import annotations

import numpy as np


def resample(pcm: bytes, src_rate: int, dst_rate: int) -> bytes:
    samples = np.frombuffer(pcm, dtype="<i2")
    if samples.size == 0:
        return b""
    n_out = round(samples.size * dst_rate / src_rate)
    x_old = np.arange(samples.size)
    x_new = np.linspace(0, samples.size - 1, n_out)
    return np.interp(x_new, x_old, samples).astype("<i2").tobytes()
