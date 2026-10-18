"""Resampler tests."""

import numpy as np

from witch.audio.resample import resample


def test_same_rate_is_identity():
    pcm = np.arange(480, dtype="<i2").tobytes()
    assert resample(pcm, 24_000, 24_000) == pcm


def test_downsampling_halves_sample_count():
    pcm = np.arange(480, dtype="<i2").tobytes()
    assert len(resample(pcm, 48_000, 24_000)) == 480


def test_empty_input():
    assert resample(b"", 48_000, 24_000) == b""
