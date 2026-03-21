"""Tests for ClaudeBridge metering functions and analyse_audio/analyse_stem_set tools."""

import json
import math
import sys
import os

import numpy as np
import pyloudnorm as pyln
import pytest
import soundfile as sf

# Add parent to path so we can import the MCP server module
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from claude_mcp_server import (
    compute_lufs_v2,
    compute_true_peak_v2,
    compute_rms_v2,
    compute_sample_peak,
    _validate_audio,
    _safe_db,
)


def write_wav(audio, sample_rate, tmp_path, name="test.wav"):
    """Helper: write audio to a WAV file and return the path."""
    path = os.path.join(str(tmp_path), name)
    sf.write(path, audio, sample_rate)
    return path


# ── compute_lufs_v2 ──

class TestLUFS:
    def test_matches_pyloudnorm_directly(self, stereo_sine_1k, sample_rate):
        """LUFS should match pyloudnorm's own measurement."""
        meter = pyln.Meter(sample_rate)
        expected = round(meter.integrated_loudness(stereo_sine_1k.astype(np.float64)), 1)
        result = compute_lufs_v2(stereo_sine_1k, sample_rate)
        assert result == expected

    def test_mono(self, mono_sine_1k, sample_rate):
        """Mono input should produce a valid LUFS reading."""
        result = compute_lufs_v2(mono_sine_1k, sample_rate)
        assert -30.0 < result < 0.0

    def test_silence_returns_floor(self, silence_mono, sample_rate):
        result = compute_lufs_v2(silence_mono, sample_rate)
        assert result == -120.0


# ── compute_true_peak_v2 ──

class TestTruePeak:
    def test_returns_per_channel_and_bus(self, stereo_sine_1k):
        result = compute_true_peak_v2(stereo_sine_1k)
        assert "per_channel_dbfs" in result
        assert "bus_dbfs" in result
        assert len(result["per_channel_dbfs"]) == 2

    def test_true_peak_gte_sample_peak(self, mono_sine_1k):
        """True peak must always be >= sample peak (polyphase finds inter-sample peaks)."""
        tp = compute_true_peak_v2(mono_sine_1k)
        sp = compute_sample_peak(mono_sine_1k)
        assert tp["bus_dbfs"] >= sp["bus_dbfs"] - 0.1  # small tolerance for rounding

    def test_inter_sample_peak_detected(self, inter_sample_peak_mono):
        """Signal with ISP should show true peak > sample peak."""
        tp = compute_true_peak_v2(inter_sample_peak_mono)
        sp = compute_sample_peak(inter_sample_peak_mono)
        # The alternating +1/-1 pattern should produce ISP > 0 dBFS
        assert tp["bus_dbfs"] > sp["bus_dbfs"]

    def test_full_scale_sine(self, full_scale_mono):
        """Full-scale sine true peak should be ~0 dBFS or slightly above."""
        tp = compute_true_peak_v2(full_scale_mono)
        assert -0.5 <= tp["bus_dbfs"] <= 3.5  # sine can overshoot slightly


# ── compute_rms_v2 ──

class TestRMS:
    def test_sine_rms_known_value(self, sample_rate):
        """For a sine wave with amplitude A, RMS = A / sqrt(2) ≈ A * 0.7071."""
        amplitude = 0.5
        t = np.linspace(0, 1.0, sample_rate, endpoint=False)
        sine = (amplitude * np.sin(2 * np.pi * 1000 * t)).astype(np.float32)

        expected_rms = amplitude / math.sqrt(2)
        expected_db = round(20.0 * math.log10(expected_rms), 1)

        result = compute_rms_v2(sine)
        assert abs(result["bus_dbfs"] - expected_db) <= 0.2

    def test_stereo_bus_higher_than_channel(self, stereo_sine_1k):
        """Bus RMS should be ~3 dB above channel RMS for identical stereo channels."""
        result = compute_rms_v2(stereo_sine_1k)
        ch_rms = result["per_channel_dbfs"][0]
        bus_rms = result["bus_dbfs"]
        diff = bus_rms - ch_rms
        assert 2.8 <= diff <= 3.2  # Should be ~3.01 dB

    def test_returns_structure(self, mono_sine_1k):
        result = compute_rms_v2(mono_sine_1k)
        assert "per_channel_dbfs" in result
        assert "bus_dbfs" in result
        assert len(result["per_channel_dbfs"]) == 1

    def test_silence(self, silence_mono):
        result = compute_rms_v2(silence_mono)
        assert result["bus_dbfs"] == -120.0


# ── compute_sample_peak ──

class TestSamplePeak:
    def test_full_scale(self, full_scale_mono):
        result = compute_sample_peak(full_scale_mono)
        assert abs(result["bus_dbfs"] - 0.0) <= 0.1

    def test_known_amplitude(self, sample_rate):
        amplitude = 10 ** (-6.0 / 20.0)
        t = np.linspace(0, 1.0, sample_rate, endpoint=False)
        sine = (amplitude * np.sin(2 * np.pi * 1000 * t)).astype(np.float32)
        result = compute_sample_peak(sine)
        assert abs(result["bus_dbfs"] - (-6.0)) <= 0.2


# ── _validate_audio ──

class TestValidation:
    def test_valid_audio(self, mono_sine_1k, sample_rate):
        errors = _validate_audio(mono_sine_1k, sample_rate, "test.wav")
        assert errors == []

    def test_empty_audio(self, sample_rate):
        empty = np.array([], dtype=np.float32)
        errors = _validate_audio(empty, sample_rate, "empty.wav")
        assert len(errors) == 1
        assert errors[0]["code"] == "INVALID_AUDIO"

    def test_nan_detected(self, nan_audio, sample_rate):
        errors = _validate_audio(nan_audio, sample_rate, "nan.wav")
        assert any(e["code"] == "INVALID_AUDIO" for e in errors)

    def test_sample_rate_mismatch(self, mono_sine_1k):
        errors = _validate_audio(mono_sine_1k, 44100, "test.wav", expected_sr=48000)
        assert any(e["code"] == "SAMPLE_RATE_MISMATCH" for e in errors)

    def test_too_many_channels(self, sample_rate):
        audio = np.zeros((sample_rate, 4), dtype=np.float32)
        errors = _validate_audio(audio, sample_rate, "quad.wav")
        assert any(e["code"] == "CHANNEL_COUNT_UNSUPPORTED" for e in errors)


# ── analyse_audio (integration) ──

class TestAnalyseAudio:
    @pytest.mark.asyncio
    async def test_backward_compat_fields(self, stereo_sine_1k, sample_rate, tmp_path):
        """Old flat field names must still be present."""
        from claude_mcp_server import analyse_audio
        path = write_wav(stereo_sine_1k, sample_rate, tmp_path)
        result_json = await analyse_audio(path)
        result = json.loads(result_json)

        levels = result["levels"]
        assert "true_peak_dbfs" in levels
        assert "rms_dbfs" in levels
        assert "crest_factor_db" in levels
        assert "integrated_lufs" in levels

    @pytest.mark.asyncio
    async def test_new_fields(self, stereo_sine_1k, sample_rate, tmp_path):
        """New structured fields must be present."""
        from claude_mcp_server import analyse_audio
        path = write_wav(stereo_sine_1k, sample_rate, tmp_path)
        result_json = await analyse_audio(path)
        result = json.loads(result_json)

        assert result["schema_version"] == "2.2.0"
        assert "loudness_notes" in result
        assert "errors" in result

        levels = result["levels"]
        assert "sample_peak" in levels
        assert "true_peak" in levels
        assert "rms" in levels
        assert "crest_factor_per_channel_db" in levels

        # Structured fields have per_channel + bus
        assert "per_channel_dbfs" in levels["true_peak"]
        assert "bus_dbfs" in levels["true_peak"]

    @pytest.mark.asyncio
    async def test_file_not_found(self):
        from claude_mcp_server import analyse_audio
        result_json = await analyse_audio("/nonexistent/file.wav")
        result = json.loads(result_json)
        assert "error" in result


# ── analyse_stem_set (integration) ──

class TestAnalyseStemSet:
    @pytest.mark.asyncio
    async def test_pass_verdict(self, stereo_sine_1k, sample_rate, tmp_path):
        """Identical stems at safe levels should PASS."""
        from claude_mcp_server import analyse_stem_set
        p1 = write_wav(stereo_sine_1k, sample_rate, tmp_path, "stem1.wav")
        p2 = write_wav(stereo_sine_1k, sample_rate, tmp_path, "stem2.wav")
        result_json = await analyse_stem_set([p1, p2], expected_sample_rate=sample_rate)
        result = json.loads(result_json)

        assert result["schema_version"] == "2.2.0"
        assert result["verdict"] in ("PASS", "WARN")
        assert len(result["stems"]) == 2

    @pytest.mark.asyncio
    async def test_fail_on_missing_file(self, sample_rate, tmp_path):
        from claude_mcp_server import analyse_stem_set
        result_json = await analyse_stem_set(
            ["/nonexistent/stem.wav"],
            expected_sample_rate=sample_rate,
        )
        result = json.loads(result_json)
        assert result["verdict"] == "FAIL"
        assert len(result["errors"]) > 0

    @pytest.mark.asyncio
    async def test_fail_on_length_mismatch(self, sample_rate, tmp_path):
        """Stems with different lengths should fail when strict."""
        from claude_mcp_server import analyse_stem_set
        t1 = np.zeros(sample_rate * 2, dtype=np.float32)
        t2 = np.zeros(sample_rate * 3, dtype=np.float32)
        p1 = write_wav(t1, sample_rate, tmp_path, "short.wav")
        p2 = write_wav(t2, sample_rate, tmp_path, "long.wav")
        result_json = await analyse_stem_set(
            [p1, p2],
            expected_sample_rate=sample_rate,
            strict_length_match=True,
        )
        result = json.loads(result_json)
        assert result["verdict"] == "FAIL"
        assert any(e["code"] == "LENGTH_MISMATCH" for e in result["errors"])

    @pytest.mark.asyncio
    async def test_warn_on_dc_offset(self, dc_offset_mono, sample_rate, tmp_path):
        from claude_mcp_server import analyse_stem_set
        p = write_wav(dc_offset_mono, sample_rate, tmp_path, "dc.wav")
        result_json = await analyse_stem_set([p], expected_sample_rate=sample_rate)
        result = json.loads(result_json)
        assert result["verdict"] in ("WARN", "FAIL")

    @pytest.mark.asyncio
    async def test_reference_master(self, stereo_sine_1k, sample_rate, tmp_path):
        from claude_mcp_server import analyse_stem_set
        p1 = write_wav(stereo_sine_1k, sample_rate, tmp_path, "stem.wav")
        ref = write_wav(stereo_sine_1k, sample_rate, tmp_path, "master.wav")
        result_json = await analyse_stem_set(
            [p1], reference_master=ref, expected_sample_rate=sample_rate,
        )
        result = json.loads(result_json)
        assert result["reference_master"] is not None
        assert "integrated_lufs" in result["reference_master"]


# ── Masking truncation ──

class TestMaskingTruncation:
    def test_truncation_on_long_files(self, long_stereo, sample_rate):
        """Masking analysis on >60s files should not crash and should produce results."""
        from claude_mcp_server import compute_masking
        result = compute_masking(long_stereo, long_stereo, sample_rate)
        assert "conflicts" in result
        assert "total_overlap_pct" in result


# ── _safe_db ──

class TestSafeDb:
    def test_zero_returns_floor(self):
        assert _safe_db(0.0) == -120.0

    def test_negative_returns_floor(self):
        assert _safe_db(-1.0) == -120.0

    def test_unity(self):
        assert _safe_db(1.0) == 0.0

    def test_known_value(self):
        assert _safe_db(0.5) == round(20.0 * np.log10(0.5), 1)
