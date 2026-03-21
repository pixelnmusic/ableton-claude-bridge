"""Fixtures for ClaudeBridge MCP server tests — synthetic audio generation."""

import numpy as np
import pytest


@pytest.fixture
def sample_rate():
    return 48000


@pytest.fixture
def mono_sine_1k(sample_rate):
    """1 kHz sine at -6 dBFS (amplitude ~0.501), 2 seconds, mono."""
    duration = 2.0
    amplitude = 10 ** (-6.0 / 20.0)  # ~0.501
    t = np.linspace(0, duration, int(sample_rate * duration), endpoint=False)
    audio = (amplitude * np.sin(2 * np.pi * 1000 * t)).astype(np.float32)
    return audio


@pytest.fixture
def stereo_sine_1k(mono_sine_1k):
    """1 kHz sine at -6 dBFS, 2 seconds, stereo (identical L/R)."""
    return np.column_stack([mono_sine_1k, mono_sine_1k])


@pytest.fixture
def silence_mono(sample_rate):
    """1 second of silence, mono."""
    return np.zeros(sample_rate, dtype=np.float32)


@pytest.fixture
def full_scale_mono(sample_rate):
    """1 kHz sine at 0 dBFS (amplitude 1.0), 1 second, mono."""
    t = np.linspace(0, 1.0, sample_rate, endpoint=False)
    return np.sin(2 * np.pi * 1000 * t).astype(np.float32)


@pytest.fixture
def inter_sample_peak_mono(sample_rate):
    """Signal designed to produce inter-sample peaks above sample peak.
    Two adjacent samples at +/-1.0 at a frequency that causes overshoot."""
    n = sample_rate  # 1 second
    audio = np.zeros(n, dtype=np.float32)
    # Place a pattern that will produce ISP: alternating +1, -1
    # at Nyquist-adjacent frequency — guaranteed to produce ISP > 1.0
    mid = n // 2
    for i in range(100):
        audio[mid + i] = 1.0 if i % 2 == 0 else -1.0
    return audio


@pytest.fixture
def nan_audio(sample_rate):
    """Audio with NaN values for validation testing."""
    audio = np.zeros(sample_rate, dtype=np.float32)
    audio[100] = np.nan
    return audio


@pytest.fixture
def dc_offset_mono(sample_rate):
    """1 second of DC offset (0.05), mono."""
    return np.full(sample_rate, 0.05, dtype=np.float32)


@pytest.fixture
def long_stereo(sample_rate):
    """90-second stereo sine for truncation testing."""
    duration = 90.0
    n = int(sample_rate * duration)
    t = np.linspace(0, duration, n, endpoint=False)
    ch = (0.5 * np.sin(2 * np.pi * 440 * t)).astype(np.float32)
    return np.column_stack([ch, ch])


