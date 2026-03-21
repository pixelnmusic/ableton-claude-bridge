"""
ClaudeBridge MCP Server — connects Claude Desktop to Ableton Live via ClaudeBridge Control Surface.
Implements all MCP tools over stdio. Uses aiohttp for async HTTP to the Control Surface on port 8765.
Audio analysis uses soundfile, numpy, and scipy.
"""

import asyncio
import json
import os
import subprocess
import time
from pathlib import Path

import aiohttp
import numpy as np
import pyloudnorm as pyln
import soundfile as sf
from scipy.signal import resample_poly, welch
from mcp.server.fastmcp import FastMCP

# ── Constants ──

BRIDGE_URL = "http://127.0.0.1:8765"
BRIDGE_TIMEOUT = 30

# Derive paths relative to the project root (one level up from mcp_server/)
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
EXPORTS_DIR = _PROJECT_ROOT / "exports"
REGISTRY_PATH = _PROJECT_ROOT / "plugin_registry.json"
CONFIG_PATH = _PROJECT_ROOT / "claude_bridge_config.json"

# Auto-detect Ableton app name from config written by installer
_APP_NAME_FILE = Path(__file__).resolve().parent / ".ableton_app_name"
if _APP_NAME_FILE.exists():
    ABLETON_APP_NAME = _APP_NAME_FILE.read_text().strip()
else:
    ABLETON_APP_NAME = "Ableton Live 12 Suite"

# ── Error Codes ──

ERR_FILE_READ_FAILED = "FILE_READ_FAILED"
ERR_INVALID_AUDIO = "INVALID_AUDIO"
ERR_UNSUPPORTED_FORMAT = "UNSUPPORTED_FORMAT"
ERR_SAMPLE_RATE_MISMATCH = "SAMPLE_RATE_MISMATCH"
ERR_CHANNEL_COUNT_UNSUPPORTED = "CHANNEL_COUNT_UNSUPPORTED"
ERR_LENGTH_MISMATCH = "LENGTH_MISMATCH"
ERR_OVERS_DETECTED = "OVERS_DETECTED"
ERR_TIMEOUT = "TIMEOUT"
ERR_INTERNAL = "INTERNAL"


def _make_error(code: str, message: str, severity: str = "error") -> dict:
    """Create a structured error/warning dict."""
    return {"code": code, "message": message, "severity": severity}


# Ensure exports directory exists
EXPORTS_DIR.mkdir(parents=True, exist_ok=True)


# ── V2 Metering Functions ──

def _safe_db(value: float, ref: float = 1.0, floor: float = -120.0) -> float:
    """Convert linear to dB with a floor."""
    if value <= 0 or ref <= 0:
        return floor
    return max(floor, round(20.0 * np.log10(value / ref), 1))


def compute_lufs_v2(audio: np.ndarray, sample_rate: int) -> float:
    """Compute integrated LUFS using pyloudnorm (ITU-R BS.1770-4 compliant)."""
    try:
        meter = pyln.Meter(sample_rate)
        loudness = meter.integrated_loudness(audio.astype(np.float64))
        if np.isinf(loudness) or np.isnan(loudness):
            return -120.0
        return round(float(loudness), 1)
    except Exception:
        return -120.0


def compute_true_peak_v2(audio: np.ndarray) -> dict:
    """Compute true peak via 4x polyphase FIR oversampling per channel."""
    if audio.ndim == 1:
        channels = [audio]
    else:
        channels = [audio[:, ch] for ch in range(audio.shape[1])]

    per_channel = []
    for ch in channels:
        upsampled = resample_poly(ch.astype(np.float64), up=4, down=1)
        peak = float(np.max(np.abs(upsampled)))
        per_channel.append(_safe_db(peak))

    bus = max(per_channel)
    return {"per_channel_dbfs": per_channel, "bus_dbfs": bus}


def compute_rms_v2(audio: np.ndarray) -> dict:
    """Compute RMS per channel and bus. Bus = sqrt(mean(sum of squared channels))."""
    if audio.ndim == 1:
        channels = [audio.astype(np.float64)]
    else:
        channels = [audio[:, ch].astype(np.float64) for ch in range(audio.shape[1])]

    per_channel = []
    for ch in channels:
        rms = float(np.sqrt(np.mean(ch ** 2)))
        per_channel.append(_safe_db(rms))

    # Bus RMS: energy sum across channels
    if len(channels) == 1:
        bus = per_channel[0]
    else:
        total_energy = sum(np.mean(ch ** 2) for ch in channels)
        bus_rms = float(np.sqrt(total_energy))
        bus = _safe_db(bus_rms)

    return {"per_channel_dbfs": per_channel, "bus_dbfs": bus}


def compute_sample_peak(audio: np.ndarray) -> dict:
    """Compute sample peak (no oversampling) per channel and bus."""
    if audio.ndim == 1:
        channels = [audio]
    else:
        channels = [audio[:, ch] for ch in range(audio.shape[1])]

    per_channel = []
    for ch in channels:
        peak = float(np.max(np.abs(ch)))
        per_channel.append(_safe_db(peak))

    bus = max(per_channel)
    return {"per_channel_dbfs": per_channel, "bus_dbfs": bus}


def _validate_audio(audio: np.ndarray, sample_rate: int, path: str,
                     expected_sr: int | None = None) -> list:
    """Validate audio data. Returns list of error dicts (empty = valid)."""
    errors = []

    if audio.size == 0:
        errors.append(_make_error(ERR_INVALID_AUDIO, f"Empty audio file: {path}"))
        return errors

    if np.any(np.isnan(audio)):
        errors.append(_make_error(ERR_INVALID_AUDIO, f"Audio contains NaN values: {path}"))

    if np.any(np.isinf(audio)):
        errors.append(_make_error(ERR_INVALID_AUDIO, f"Audio contains Inf values: {path}"))

    if expected_sr is not None and sample_rate != expected_sr:
        errors.append(_make_error(
            ERR_SAMPLE_RATE_MISMATCH,
            f"Expected {expected_sr} Hz, got {sample_rate} Hz: {path}",
        ))

    if audio.ndim > 1 and audio.shape[1] > 2:
        errors.append(_make_error(
            ERR_CHANNEL_COUNT_UNSUPPORTED,
            f"Unsupported channel count ({audio.shape[1]}): {path}",
        ))

    return errors


def _load_audio_directory() -> Path:
    """Load audio_directory from config, defaulting to ~/Downloads."""
    default = Path.home() / "Downloads"
    if CONFIG_PATH.exists():
        try:
            with open(CONFIG_PATH, "r") as f:
                config = json.load(f)
            path = Path(config.get("audio_directory", str(default))).expanduser()
            if path.is_dir():
                return path
        except Exception:
            pass
    return default

# ── MCP Server ──

mcp = FastMCP("ableton-claude-bridge")


# ── HTTP Helper ──

async def bridge_call(method: str, params: dict | None = None) -> dict:
    """Make an async HTTP POST to the ClaudeBridge Control Surface."""
    payload = {"method": method, "params": params or {}}
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(
                BRIDGE_URL,
                json=payload,
                timeout=aiohttp.ClientTimeout(total=BRIDGE_TIMEOUT),
            ) as resp:
                data = await resp.json()
                if data.get("error"):
                    raise Exception(data["error"])
                return data.get("result", {})
    except aiohttp.ClientError:
        raise Exception(
            "Cannot connect to Ableton Live. Open Live and select ClaudeBridge "
            "as a Control Surface in Preferences > MIDI."
        )
    except asyncio.TimeoutError:
        raise Exception(
            "Ableton did not respond within 30 seconds. "
            "The session may be loading or processing."
        )


async def bridge_ping() -> bool:
    """Check if ClaudeBridge is running."""
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(
                BRIDGE_URL + "/ping",
                timeout=aiohttp.ClientTimeout(total=2),
            ) as resp:
                return resp.status == 200
    except Exception:
        return False


# ── MCP Tools: Connection ──

@mcp.tool()
async def check_connection() -> str:
    """Check if ClaudeBridge is running in Ableton Live."""
    if await bridge_ping():
        return json.dumps({
            "connected": True,
            "message": "ClaudeBridge is running in Ableton Live.",
        })
    return json.dumps({
        "connected": False,
        "message": (
            "ClaudeBridge not found. Open Ableton Live and go to "
            "Preferences > MIDI > Control Surface and select ClaudeBridge."
        ),
    })


# ── MCP Tools: Session Reading (Tier 1) ──

@mcp.tool()
async def get_session_overview() -> str:
    """Get a lightweight overview of the entire Ableton session — track names, types, volumes, device names. No parameter values."""
    result = await bridge_call("get_session_overview")
    return json.dumps(result, indent=2)


@mcp.tool()
async def get_track_detail(track_name: str) -> str:
    """Get full structural detail for a named track — devices, plugin formats, parameter counts, sends, metering. No parameter values."""
    result = await bridge_call("get_track_detail", {"track_name": track_name})
    return json.dumps(result, indent=2)


@mcp.tool()
async def get_all_tracks() -> str:
    """Get structural detail for every track simultaneously — device names and counts, no parameter values."""
    result = await bridge_call("get_all_tracks")
    return json.dumps(result, indent=2)


@mcp.tool()
async def get_return_tracks() -> str:
    """Get all return tracks with their device chains and which tracks are sending to each return at what level."""
    result = await bridge_call("get_return_tracks")
    return json.dumps(result, indent=2)


@mcp.tool()
async def get_master_bus_state() -> str:
    """Get the master track with its full device chain and current metering."""
    result = await bridge_call("get_master_bus_state")
    return json.dumps(result, indent=2)


# ── MCP Tools: Session Reading (Tier 2) ──

@mcp.tool()
async def get_device_params(track_name: str, device_name: str) -> str:
    """Get the complete parameter list for a single device on a track. Returns all parameter names, values, display values, and ranges."""
    result = await bridge_call("get_device_params", {
        "track_name": track_name,
        "device_name": device_name,
    })
    return json.dumps(result, indent=2)


# ── MCP Tools: Metering ──

@mcp.tool()
async def get_metering(track_name: str = "") -> str:
    """Get current instantaneous peak metering from Ableton Live. Omit track_name for all tracks. Note: Live API provides peak meters only, not true RMS or LUFS. Use analyse_audio on exported WAV files for accurate loudness measurement."""
    params = {}
    if track_name:
        params["track_name"] = track_name
    result = await bridge_call("get_metering", params)
    return json.dumps(result, indent=2)


@mcp.tool()
async def get_metering_snapshot(
    track_name: str = "",
    duration_seconds: float = 15.0,
    readings: int = 10,
) -> str:
    """Poll peak metering repeatedly over a duration and return statistical summary (peak max, peak avg). Session must be playing. Note: Live API provides peak meters only. Use analyse_audio on exported WAV files for accurate RMS/LUFS."""
    params = {
        "duration_seconds": duration_seconds,
        "readings": readings,
    }
    if track_name:
        params["track_name"] = track_name
    result = await bridge_call("get_metering_snapshot", params)
    return json.dumps(result, indent=2)


@mcp.tool()
async def get_alerts() -> str:
    """Scan all tracks for clipping (peak >= 0 dBFS), hot levels (peak > -6 dBFS), and master ceiling breaches."""
    result = await bridge_call("get_alerts")
    return json.dumps(result, indent=2)


# ── MCP Tools: Transport ──

@mcp.tool()
async def get_transport_state() -> str:
    """Get current transport state — position (beats, bars, seconds), tempo, time signature, loop settings."""
    result = await bridge_call("get_transport_state")
    return json.dumps(result, indent=2)


@mcp.tool()
async def set_playhead_position(position: str, unit: str = "beats") -> str:
    """Move the playhead. Position can be beats (float), bars_beats string ('5.1.1'), or seconds (float with unit='seconds')."""
    result = await bridge_call("set_playhead_position", {
        "position": position,
        "unit": unit,
    })
    return json.dumps(result, indent=2)


@mcp.tool()
async def transport_play() -> str:
    """Start playback from current position."""
    result = await bridge_call("transport_play")
    return json.dumps(result, indent=2)


@mcp.tool()
async def transport_stop() -> str:
    """Stop playback."""
    result = await bridge_call("transport_stop")
    return json.dumps(result, indent=2)


@mcp.tool()
async def transport_play_stop() -> str:
    """Toggle play/stop."""
    result = await bridge_call("transport_play_stop")
    return json.dumps(result, indent=2)


@mcp.tool()
async def set_loop(start: float, length: float, enable: bool = True) -> str:
    """Set loop start (beats), length (beats), and enable/disable loop."""
    result = await bridge_call("set_loop", {
        "start": start,
        "length": length,
        "enable": enable,
    })
    return json.dumps(result, indent=2)


@mcp.tool()
async def enable_loop() -> str:
    """Enable loop without changing loop points."""
    result = await bridge_call("enable_loop")
    return json.dumps(result, indent=2)


@mcp.tool()
async def disable_loop() -> str:
    """Disable loop without changing loop points."""
    result = await bridge_call("disable_loop")
    return json.dumps(result, indent=2)


# ── MCP Tools: Audio Export ──

async def run_applescript(script: str) -> str:
    """Run an AppleScript via osascript subprocess."""
    proc = await asyncio.create_subprocess_exec(
        "osascript", "-e", script,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await proc.communicate()
    if proc.returncode != 0:
        error_msg = stderr.decode("utf-8", errors="replace").strip()
        if "not allowed assistive access" in error_msg.lower() or "accessibility" in error_msg.lower():
            raise Exception(
                "Audio export requires Accessibility permissions. "
                "Go to System Preferences > Privacy & Security > Accessibility "
                "and enable access for Claude Desktop / Terminal."
            )
        raise Exception("AppleScript error: " + error_msg)
    return stdout.decode("utf-8", errors="replace").strip()



@mcp.tool()
async def export_track_audio() -> str:
    """Open the Ableton Live Export Audio/Video dialog (Cmd+Shift+R). The user will save the file manually. After the user confirms the export is done, use list_audio_files to find it, then analyse_audio to analyse it."""
    script = f'''
tell application "{ABLETON_APP_NAME}" to activate
delay 0.5

tell application "System Events"
    tell process "{ABLETON_APP_NAME}"
        keystroke "r" using {{command down, shift down}}
    end tell
end tell
'''
    try:
        await run_applescript(script)
    except Exception as e:
        error_msg = str(e)
        if "Accessibility" in error_msg:
            return json.dumps({"error": error_msg})
        return json.dumps({
            "error": f"Could not open export dialog: {error_msg}",
        })

    return json.dumps({
        "status": "export_dialog_opened",
        "message": "The Export Audio/Video dialog is now open in Ableton Live. Save the file and let me know when done — I will then find and analyse it.",
    })


# ── MCP Tools: Audio Analysis ──

def _to_mono(audio: np.ndarray) -> np.ndarray:
    """Convert audio to mono float64."""
    if audio.ndim > 1:
        return np.mean(audio, axis=1).astype(np.float64)
    return audio.astype(np.float64)


# ── Spectrum Analysis ──

# Eight professional mixing bands
FREQ_BANDS = [
    ("sub_bass",    20,    60),
    ("bass",        60,   150),
    ("low_mid",    150,   400),
    ("mid",        400,  1000),
    ("upper_mid", 1000,  3000),
    ("presence",  3000,  6000),
    ("brilliance",6000, 10000),
    ("air",      10000, 20000),
]


def compute_band_energy(psd: np.ndarray, freqs: np.ndarray) -> dict:
    """Compute energy distribution across 8 mixing bands."""
    total_energy = 0.0
    band_energies = {}
    for name, lo, hi in FREQ_BANDS:
        mask = (freqs >= lo) & (freqs < hi)
        energy = float(np.sum(psd[mask]))
        band_energies[name] = energy
        total_energy += energy

    result = {}
    for name, lo, hi in FREQ_BANDS:
        energy = band_energies[name]
        pct = round(100.0 * energy / total_energy, 1) if total_energy > 0 else 0.0
        avg_db = round(10.0 * np.log10(energy / max(1, np.sum((freqs >= lo) & (freqs < hi)))) if energy > 0 else -120.0, 1)
        result[name] = {"energy_pct": pct, "avg_level_db": avg_db}
    return result


def compute_resonances(psd_db: np.ndarray, freqs: np.ndarray, threshold: float = 6.0) -> list:
    """Find frequencies protruding above smoothed spectral average."""
    # Smooth with moving average (~21 bins)
    kernel_size = 21
    kernel = np.ones(kernel_size) / kernel_size
    smoothed = np.convolve(psd_db, kernel, mode="same")

    diff = psd_db - smoothed
    # Find peaks above threshold
    peaks = []
    for i in range(1, len(diff) - 1):
        if diff[i] > threshold and diff[i] > diff[i - 1] and diff[i] > diff[i + 1]:
            if 20 <= freqs[i] <= 20000:
                severity = "strong" if diff[i] > 10 else "moderate"
                peaks.append({
                    "frequency_hz": round(float(freqs[i]), 1),
                    "prominence_db": round(float(diff[i]), 1),
                    "level_db": round(float(psd_db[i]), 1),
                    "severity": severity,
                })

    peaks.sort(key=lambda x: x["prominence_db"], reverse=True)
    return peaks[:10]


def compute_frequency_nulls(psd_db: np.ndarray, freqs: np.ndarray, threshold: float = 8.0) -> list:
    """Find suspicious frequency dips (possible phase cancellation)."""
    kernel_size = 21
    kernel = np.ones(kernel_size) / kernel_size
    smoothed = np.convolve(psd_db, kernel, mode="same")

    diff = smoothed - psd_db
    nulls = []
    for i in range(1, len(diff) - 1):
        if diff[i] > threshold and diff[i] > diff[i - 1] and diff[i] > diff[i + 1]:
            if 20 <= freqs[i] <= 20000:
                cause = "phase_cancellation" if diff[i] > 12 else "natural_dip"
                nulls.append({
                    "frequency_hz": round(float(freqs[i]), 1),
                    "depth_db": round(float(diff[i]), 1),
                    "level_db": round(float(psd_db[i]), 1),
                    "possible_cause": cause,
                })

    nulls.sort(key=lambda x: x["depth_db"], reverse=True)
    return nulls[:10]


def compute_dominant_frequencies(psd: np.ndarray, freqs: np.ndarray) -> dict:
    """Find dominant frequency and top 5 peaks."""
    mask = (freqs >= 20) & (freqs <= 20000)
    masked_psd = psd.copy()
    masked_psd[~mask] = 0

    dominant_idx = np.argmax(masked_psd)
    dominant_hz = float(freqs[dominant_idx])

    # Top 5: find peaks in masked PSD
    psd_db = 10.0 * np.log10(np.maximum(masked_psd, 1e-20))
    top_indices = np.argsort(masked_psd)[-50:][::-1]  # Get top 50 candidates

    # Deduplicate — skip peaks within 20 Hz of a stronger one
    top5 = []
    for idx in top_indices:
        if not mask[idx]:
            continue
        freq = float(freqs[idx])
        too_close = False
        for existing in top5:
            if abs(freq - existing["frequency_hz"]) < 20:
                too_close = True
                break
        if not too_close:
            top5.append({
                "frequency_hz": round(freq, 1),
                "magnitude_db": round(float(psd_db[idx]), 1),
            })
        if len(top5) >= 5:
            break

    return {
        "dominant_frequency_hz": round(dominant_hz, 1),
        "top_5_peaks": top5,
    }


def compute_spectral_slope(psd: np.ndarray, freqs: np.ndarray) -> dict:
    """Compute spectral slope (dB/octave) via linear regression on log-frequency."""
    mask = (freqs >= 100) & (freqs <= 10000)
    f = freqs[mask]
    p = psd[mask]

    if len(f) < 10 or np.all(p <= 0):
        return {"spectral_slope_db_per_octave": 0.0, "tonal_character": "balanced"}

    log_f = np.log2(f)
    p_db = 10.0 * np.log10(np.maximum(p, 1e-20))

    # Linear regression
    A = np.vstack([log_f, np.ones(len(log_f))]).T
    slope, _ = np.linalg.lstsq(A, p_db, rcond=None)[0]

    if slope < -5:
        character = "dark/warm"
    elif slope > -3:
        character = "bright"
    else:
        character = "balanced"

    return {
        "spectral_slope_db_per_octave": round(float(slope), 1),
        "tonal_character": character,
    }


def compute_spectrum_analysis(audio: np.ndarray, sample_rate: int) -> dict:
    """Full spectrum analysis: bands, resonances, nulls, peaks, slope."""
    mono = _to_mono(audio)

    # Limit to 60 seconds for performance
    max_samples = sample_rate * 60
    if len(mono) > max_samples:
        start = (len(mono) - max_samples) // 2
        mono = mono[start:start + max_samples]

    # Welch PSD
    freqs, psd = welch(mono, fs=sample_rate, nperseg=8192, noverlap=4096)

    psd_db = 10.0 * np.log10(np.maximum(psd, 1e-20))

    band_energy = compute_band_energy(psd, freqs)
    resonances = compute_resonances(psd_db, freqs)
    nulls = compute_frequency_nulls(psd_db, freqs)
    dominant = compute_dominant_frequencies(psd, freqs)
    slope = compute_spectral_slope(psd, freqs)

    return {
        "band_energy": band_energy,
        "resonances": resonances,
        "frequency_nulls": nulls,
        **dominant,
        **slope,
    }


# ── Stereo & Phase Analysis ──

def _bandpass_fft(signal: np.ndarray, sample_rate: int, lo: float, hi: float) -> np.ndarray:
    """Bandpass filter using FFT masking."""
    spectrum = np.fft.rfft(signal)
    freqs = np.fft.rfftfreq(len(signal), 1.0 / sample_rate)
    mask = (freqs >= lo) & (freqs <= hi)
    spectrum[~mask] = 0
    return np.fft.irfft(spectrum, n=len(signal))


def compute_stereo_analysis(audio: np.ndarray, sample_rate: int) -> dict:
    """Full stereo and phase analysis."""
    if audio.ndim == 1 or audio.shape[1] < 2:
        return {
            "correlation": 1.0,
            "stereo_width": 0.0,
            "balance_db": 0.0,
            "balance_direction": "center",
            "mid_side_ratio_db": 100.0,
            "mono_compatibility": "good",
            "band_correlation": {"low": 1.0, "mid": 1.0, "high": 1.0},
            "phase_issues": ["Mono file — no stereo analysis applicable"],
        }

    left = audio[:, 0].astype(np.float64)
    right = audio[:, 1].astype(np.float64)

    # Limit to 60 seconds
    max_samples = sample_rate * 60
    if len(left) > max_samples:
        start = (len(left) - max_samples) // 2
        left = left[start:start + max_samples]
        right = right[start:start + max_samples]

    # Overall correlation (Pearson)
    l_std = np.std(left)
    r_std = np.std(right)
    if l_std > 0 and r_std > 0:
        correlation = float(np.corrcoef(left, right)[0, 1])
    else:
        correlation = 1.0

    # Mid/Side
    mid = (left + right) / 2.0
    side = (left - right) / 2.0
    mid_rms = np.sqrt(np.mean(mid ** 2))
    side_rms = np.sqrt(np.mean(side ** 2))
    stereo_width = float(side_rms / mid_rms) if mid_rms > 0 else 0.0

    # Balance
    l_rms = np.sqrt(np.mean(left ** 2))
    r_rms = np.sqrt(np.mean(right ** 2))
    if r_rms > 0 and l_rms > 0:
        balance_db = round(20.0 * np.log10(l_rms / r_rms), 1)
    else:
        balance_db = 0.0

    if abs(balance_db) < 0.5:
        balance_direction = "center"
    elif balance_db > 0:
        balance_direction = "left"
    else:
        balance_direction = "right"

    # Mid/Side ratio
    if side_rms > 0 and mid_rms > 0:
        ms_ratio_db = round(20.0 * np.log10(mid_rms / side_rms), 1)
    elif side_rms == 0:
        ms_ratio_db = 100.0
    else:
        ms_ratio_db = -100.0

    # Mono compatibility
    if correlation > 0.7:
        mono_compat = "good"
    elif correlation > 0.3:
        mono_compat = "acceptable"
    else:
        mono_compat = "poor"

    # Band correlation
    band_ranges = [("low", 20, 200), ("mid", 200, 2000), ("high", 2000, 20000)]
    band_corr = {}
    for band_name, lo, hi in band_ranges:
        l_band = _bandpass_fft(left, sample_rate, lo, hi)
        r_band = _bandpass_fft(right, sample_rate, lo, hi)
        l_s = np.std(l_band)
        r_s = np.std(r_band)
        if l_s > 0 and r_s > 0:
            band_corr[band_name] = round(float(np.corrcoef(l_band, r_band)[0, 1]), 2)
        else:
            band_corr[band_name] = 1.0

    # Phase warnings
    warnings = []
    if correlation < 0:
        warnings.append(f"CRITICAL: Negative correlation ({correlation:.2f}), signal collapses in mono")
    elif correlation < 0.5:
        warnings.append(f"WARNING: Low correlation ({correlation:.2f}), significant phase issues")
    if band_corr["low"] < 0.8:
        warnings.append(f"Low-frequency phase issue (correlation: {band_corr['low']}). Bass cancels in mono.")
    if band_corr["mid"] < 0.3:
        warnings.append(f"Mid-frequency phase divergence (correlation: {band_corr['mid']}). Wide but risky.")
    if stereo_width > 1.2:
        warnings.append("Side-heavy signal: may lose energy in mono folddown")
    if not warnings:
        warnings.append("No phase issues detected")

    return {
        "correlation": round(correlation, 2),
        "stereo_width": round(stereo_width, 2),
        "balance_db": balance_db,
        "balance_direction": balance_direction,
        "mid_side_ratio_db": ms_ratio_db,
        "mono_compatibility": mono_compat,
        "band_correlation": band_corr,
        "phase_issues": warnings,
    }


# ── Dynamics Analysis ──

def compute_dynamics(audio: np.ndarray, sample_rate: int) -> dict:
    """Short-term dynamic range and low-frequency crest factor."""
    mono = _to_mono(audio)

    # Short-term RMS (400ms windows)
    window = int(sample_rate * 0.4)
    hop = window  # non-overlapping for speed
    rms_values = []
    for i in range(0, len(mono) - window, hop):
        block = mono[i:i + window]
        block_rms = np.sqrt(np.mean(block ** 2))
        db = _safe_db(block_rms)
        if db > -60.0:  # exclude silence
            rms_values.append(db)

    if rms_values:
        st_max = round(max(rms_values), 1)
        st_min = round(min(rms_values), 1)
        st_range = round(st_max - st_min, 1)
    else:
        st_max = st_min = st_range = 0.0

    # Low-frequency crest factor (20-200 Hz)
    lf = _bandpass_fft(mono, sample_rate, 20, 200)
    lf_peak = np.max(np.abs(lf))
    lf_rms = np.sqrt(np.mean(lf ** 2))
    if lf_rms > 0 and lf_peak > 0:
        lf_crest = round(20.0 * np.log10(lf_peak / lf_rms), 1)
    else:
        lf_crest = 0.0

    return {
        "short_term_rms_max_db": st_max,
        "short_term_rms_min_db": st_min,
        "short_term_dynamic_range_db": st_range,
        "low_freq_crest_factor_db": lf_crest,
    }


# ── Transient Analysis ──

def compute_transients(audio: np.ndarray, sample_rate: int) -> dict:
    """Detect transients and characterise attack/decay."""
    mono = _to_mono(audio)

    # Limit to 60 seconds
    max_samples = sample_rate * 60
    if len(mono) > max_samples:
        start = (len(mono) - max_samples) // 2
        mono = mono[start:start + max_samples]

    duration = len(mono) / sample_rate

    # Envelope: RMS with ~5ms window
    env_window = max(1, int(sample_rate * 0.005))
    # Compute envelope using strided trick for speed
    n_blocks = len(mono) // env_window
    if n_blocks < 2:
        return {
            "transient_count": 0,
            "transients_per_second": 0.0,
            "transient_character": "sparse",
            "avg_attack_ms": 0.0,
            "avg_decay_ms": 0.0,
            "attack_character": "soft",
        }

    trimmed = mono[:n_blocks * env_window]
    blocks = trimmed.reshape(n_blocks, env_window)
    envelope_db = np.zeros(n_blocks)
    for i in range(n_blocks):
        rms_val = np.sqrt(np.mean(blocks[i] ** 2))
        envelope_db[i] = _safe_db(rms_val)

    # Transient detection: rise > 6 dB between consecutive envelope blocks
    # (~5ms resolution, so consecutive blocks ≈ 10ms)
    threshold_db = 6.0
    transient_indices = []
    for i in range(1, len(envelope_db)):
        if envelope_db[i] - envelope_db[i - 1] > threshold_db:
            # Avoid double-counting: must be > 50ms from last transient
            min_gap = int(0.05 * sample_rate / env_window)
            if not transient_indices or (i - transient_indices[-1]) > min_gap:
                transient_indices.append(i)

    t_count = len(transient_indices)
    t_per_sec = round(t_count / duration, 2) if duration > 0 else 0.0

    if t_per_sec < 1:
        character = "sparse"
    elif t_per_sec <= 8:
        character = "rhythmic"
    else:
        character = "dense"

    # Measure attack and decay for each transient
    attack_times = []
    decay_times = []
    for idx in transient_indices:
        # Attack: find onset (look back for -6 dB from peak)
        peak_db = envelope_db[idx]
        onset_idx = idx
        for j in range(idx - 1, max(0, idx - 20), -1):
            if envelope_db[j] < peak_db - 6:
                onset_idx = j
                break
        attack_samples = (idx - onset_idx) * env_window
        attack_ms = 1000.0 * attack_samples / sample_rate
        attack_times.append(attack_ms)

        # Decay: find -20 dB from peak (look forward)
        decay_idx = min(idx + 1, len(envelope_db) - 1)
        for j in range(idx + 1, min(len(envelope_db), idx + 200)):
            if envelope_db[j] < peak_db - 20:
                decay_idx = j
                break
        decay_samples = (decay_idx - idx) * env_window
        decay_ms = 1000.0 * decay_samples / sample_rate
        decay_times.append(decay_ms)

    avg_attack = round(float(np.mean(attack_times)), 1) if attack_times else 0.0
    avg_decay = round(float(np.mean(decay_times)), 1) if decay_times else 0.0

    if avg_attack < 2:
        attack_char = "sharp"
    elif avg_attack <= 10:
        attack_char = "medium"
    else:
        attack_char = "soft"

    return {
        "transient_count": t_count,
        "transients_per_second": t_per_sec,
        "transient_character": character,
        "avg_attack_ms": avg_attack,
        "avg_decay_ms": avg_decay,
        "attack_character": attack_char,
    }


# ── Atmos / Mono Folddown ──

def compute_atmos_checks(stereo: dict) -> dict:
    """Compute Atmos-specific mono folddown safety."""
    corr = stereo["correlation"]
    low_corr = stereo["band_correlation"]["low"]
    width = stereo["stereo_width"]

    # Mono folddown score (0-100)
    # Correlation component (40%): map -1..1 to 0..100
    corr_score = (corr + 1.0) / 2.0 * 100.0
    # Low-band correlation (40%): map -1..1 to 0..100
    low_score = (low_corr + 1.0) / 2.0 * 100.0
    # Width component (20%): 0 width = 100, 1.5+ width = 0
    width_score = max(0.0, 100.0 - (width / 1.5) * 100.0)

    score = int(round(corr_score * 0.4 + low_score * 0.4 + width_score * 0.2))
    score = max(0, min(100, score))

    if score > 85:
        rating = "excellent"
    elif score >= 70:
        rating = "good"
    elif score >= 50:
        rating = "caution"
    else:
        rating = "poor"

    low_end_mono = low_corr > 0.9
    if low_end_mono:
        recommendation = "Bass is mono-safe"
    else:
        recommendation = "Apply mid/side EQ to mono frequencies below 120 Hz"

    return {
        "mono_folddown_score": score,
        "mono_folddown_rating": rating,
        "low_end_mono_compatible": low_end_mono,
        "low_end_correlation": round(low_corr, 2),
        "recommendation": recommendation,
    }


# ── Masking Analysis ──

def compute_masking(audio1: np.ndarray, audio2: np.ndarray, sample_rate: int) -> dict:
    """Compare two audio files for frequency masking/conflicts."""
    mono1 = _to_mono(audio1)
    mono2 = _to_mono(audio2)

    # Limit to 60 seconds (direct slicing — loop rebind was a no-op bug)
    max_samples = sample_rate * 60
    if len(mono1) > max_samples:
        start = (len(mono1) - max_samples) // 2
        mono1 = mono1[start:start + max_samples]
    if len(mono2) > max_samples:
        start = (len(mono2) - max_samples) // 2
        mono2 = mono2[start:start + max_samples]

    freqs1, psd1 = welch(mono1, fs=sample_rate, nperseg=8192, noverlap=4096)
    freqs2, psd2 = welch(mono2, fs=sample_rate, nperseg=8192, noverlap=4096)

    # Use the shorter frequency array for comparison
    min_len = min(len(freqs1), len(freqs2))
    freqs = freqs1[:min_len]
    psd1 = psd1[:min_len]
    psd2 = psd2[:min_len]

    psd1_db = 10.0 * np.log10(np.maximum(psd1, 1e-20))
    psd2_db = 10.0 * np.log10(np.maximum(psd2, 1e-20))

    # Per-band conflicts
    conflicts = []
    for name, lo, hi in FREQ_BANDS:
        mask = (freqs >= lo) & (freqs < hi)
        if not np.any(mask):
            continue
        avg1 = round(float(np.mean(psd1_db[mask])), 1)
        avg2 = round(float(np.mean(psd2_db[mask])), 1)

        # "Significant energy" = above floor + 40 dB
        floor1 = float(np.min(psd1_db[mask]))
        floor2 = float(np.min(psd2_db[mask]))
        strong1 = avg1 > floor1 + 40
        strong2 = avg2 > floor2 + 40

        if strong1 and strong2:
            severity = "high"
        elif strong1 or strong2:
            severity = "moderate"
        else:
            severity = "low"

        conflicts.append({
            "band": name,
            "range_hz": f"{lo}-{hi}",
            "file1_avg_db": avg1,
            "file2_avg_db": avg2,
            "severity": severity,
        })

    # Total overlap percentage
    threshold1 = np.percentile(psd1_db, 20)
    threshold2 = np.percentile(psd2_db, 20)
    significant1 = psd1_db > threshold1
    significant2 = psd2_db > threshold2
    both_significant = significant1 & significant2
    overlap_pct = round(100.0 * np.sum(both_significant) / max(1, len(freqs)), 1)

    return {
        "conflicts": conflicts,
        "total_overlap_pct": overlap_pct,
    }


# ── Main Analysis Tool ──

@mcp.tool()
async def analyse_audio(wav_path: str, wav_path_2: str = "",
                        analysis_profile: str = "reference_master",
                        include_estimates: bool = False) -> str:
    """Professional-grade audio analysis — spectrum (8-band energy, resonances, nulls, peaks, spectral slope), stereo/phase analysis (correlation, width, mono compatibility, band correlation), dynamics (LUFS, true peak, RMS, crest factor, short-term dynamic range, low-frequency crest), transient detection (density, attack/decay profile), Atmos mono-folddown safety, and optional masking analysis when two files are provided. LUFS via pyloudnorm (BS.1770-4), true peak via 4x polyphase FIR."""
    path = Path(wav_path).expanduser()
    if not path.exists():
        return json.dumps({"error": f"Audio file not found: {wav_path}"})

    try:
        audio, sample_rate = sf.read(str(path), dtype="float32")
    except Exception as e:
        return json.dumps({
            "error": f"Could not read audio file: {e}. Ensure the file is a valid WAV/AIFF format.",
        })

    # Validate
    validation_errors = _validate_audio(audio, sample_rate, str(path))
    fatal = [e for e in validation_errors if e["severity"] == "error"]
    if fatal:
        return json.dumps({
            "schema_version": "2.2.0",
            "error": fatal[0]["message"],
            "errors": validation_errors,
        })

    duration = len(audio) / sample_rate
    channels = 1 if audio.ndim == 1 else audio.shape[1]

    # V2 metering
    lufs = compute_lufs_v2(audio, sample_rate)
    true_peak = compute_true_peak_v2(audio)
    rms = compute_rms_v2(audio)
    sample_peak = compute_sample_peak(audio)
    crest = round(true_peak["bus_dbfs"] - rms["bus_dbfs"], 1)

    # Per-channel crest factor
    crest_per_channel = []
    for i in range(len(true_peak["per_channel_dbfs"])):
        tp = true_peak["per_channel_dbfs"][i]
        r = rms["per_channel_dbfs"][i]
        crest_per_channel.append(round(tp - r, 1))

    # Dynamics
    dynamics = compute_dynamics(audio, sample_rate)

    # Spectrum
    spectrum = compute_spectrum_analysis(audio, sample_rate)

    # Stereo
    stereo = compute_stereo_analysis(audio, sample_rate)

    # Transients
    transients = compute_transients(audio, sample_rate)

    # Atmos
    atmos = compute_atmos_checks(stereo)

    # Masking (optional)
    masking = None
    if wav_path_2:
        path2 = Path(wav_path_2).expanduser()
        if not path2.exists():
            masking = {"error": f"Second audio file not found: {wav_path_2}"}
        else:
            try:
                audio2, sr2 = sf.read(str(path2), dtype="float32")
                masking = compute_masking(audio, audio2, sample_rate)
                masking["file2"] = str(path2)
            except Exception as e:
                masking = {"error": f"Could not read second file: {e}"}

    result = {
        "schema_version": "2.2.0",
        "file": path.name,
        "sample_rate": sample_rate,
        "channels": channels,
        "duration_seconds": round(duration, 2),
        "levels": {
            # Backward-compatible flat fields (bus values)
            "true_peak_dbfs": true_peak["bus_dbfs"],
            "rms_dbfs": rms["bus_dbfs"],
            "crest_factor_db": crest,
            "integrated_lufs": lufs,
            # New structured per-channel fields
            "sample_peak": sample_peak,
            "true_peak": true_peak,
            "rms": rms,
            "crest_factor_per_channel_db": crest_per_channel,
            **dynamics,
        },
        "spectrum": spectrum,
        "stereo": stereo,
        "transients": transients,
        "atmos": atmos,
        "masking": masking,
        "loudness_notes": [
            "integrated_lufs: pyloudnorm (ITU-R BS.1770-4 compliant)",
            "true_peak: 4x polyphase FIR oversampling (scipy resample_poly)",
            "rms: per-channel sqrt(mean(x^2)); bus = sqrt(sum of channel energies)",
            "sample_peak: max(abs(x)) per channel, no oversampling",
        ],
        "errors": validation_errors,
    }

    return json.dumps(result, indent=2)


# ── Stem Set QC Tool ──

def _meter_stem(audio: np.ndarray, sample_rate: int, path: str) -> dict:
    """Compute full metering for a single stem file."""
    channels = 1 if audio.ndim == 1 else audio.shape[1]
    duration = len(audio) / sample_rate

    sp = compute_sample_peak(audio)
    tp = compute_true_peak_v2(audio)
    rms = compute_rms_v2(audio)
    lufs = compute_lufs_v2(audio, sample_rate)
    crest = round(tp["bus_dbfs"] - rms["bus_dbfs"], 1)

    # DC offset detection
    if audio.ndim == 1:
        dc_offset = float(np.mean(audio))
    else:
        dc_offset = float(np.max(np.abs(np.mean(audio, axis=0))))

    return {
        "file": Path(path).name,
        "path": path,
        "channels": channels,
        "duration_seconds": round(duration, 2),
        "sample_rate": sample_rate,
        "sample_peak": sp,
        "true_peak": tp,
        "rms": rms,
        "integrated_lufs": lufs,
        "crest_factor_db": crest,
        "dc_offset": round(dc_offset, 6),
    }


@mcp.tool()
async def analyse_stem_set(
    stem_files: list,
    reference_master: str = "",
    expected_sample_rate: int = 48000,
    expected_format: str = "float32",
    strict_length_match: bool = True,
) -> str:
    """QC a set of stem files for delivery. Validates sample rate, channel count, length consistency, overs, headroom, and DC offset. Optionally compares against a reference master. Returns PASS/WARN/FAIL verdict."""
    errors = []
    warnings = []
    stems = []
    lengths = []

    for stem_path in stem_files:
        p = Path(stem_path).expanduser()
        if not p.exists():
            errors.append(_make_error(ERR_FILE_READ_FAILED, f"File not found: {stem_path}"))
            continue

        try:
            audio, sr = sf.read(str(p), dtype="float32")
        except Exception as e:
            errors.append(_make_error(ERR_FILE_READ_FAILED, f"Could not read {p.name}: {e}"))
            continue

        # Validate
        v_errors = _validate_audio(audio, sr, str(p), expected_sr=expected_sample_rate)
        for ve in v_errors:
            if ve["severity"] == "error":
                errors.append(ve)
            else:
                warnings.append(ve)

        # Skip metering if fatal validation errors for this file
        fatal_for_file = [e for e in v_errors if e["severity"] == "error"]
        if fatal_for_file:
            continue

        metering = _meter_stem(audio, sr, str(p))
        stems.append(metering)
        lengths.append(len(audio))

        # Overs check
        if metering["sample_peak"]["bus_dbfs"] > 0.0:
            errors.append(_make_error(
                ERR_OVERS_DETECTED,
                f"Sample peak over 0 dBFS ({metering['sample_peak']['bus_dbfs']} dB): {p.name}",
            ))
        elif metering["true_peak"]["bus_dbfs"] > 0.0:
            errors.append(_make_error(
                ERR_OVERS_DETECTED,
                f"True peak over 0 dBFS ({metering['true_peak']['bus_dbfs']} dB): {p.name}",
            ))

        # Headroom warning
        if metering["true_peak"]["bus_dbfs"] > -1.0 and metering["true_peak"]["bus_dbfs"] <= 0.0:
            warnings.append(_make_error(
                ERR_OVERS_DETECTED,
                f"Low headroom — true peak {metering['true_peak']['bus_dbfs']} dB: {p.name}",
                severity="warning",
            ))

        # DC offset warning
        if abs(metering["dc_offset"]) > 0.001:
            warnings.append(_make_error(
                ERR_INVALID_AUDIO,
                f"DC offset detected ({metering['dc_offset']}): {p.name}",
                severity="warning",
            ))

    # Length consistency check
    if strict_length_match and len(lengths) > 1:
        max_len = max(lengths)
        min_len = min(lengths)
        if max_len != min_len:
            diff_samples = max_len - min_len
            diff_ms = round(1000.0 * diff_samples / expected_sample_rate, 1)
            errors.append(_make_error(
                ERR_LENGTH_MISMATCH,
                f"Stem lengths differ by {diff_samples} samples ({diff_ms} ms)",
            ))

    # Leading/trailing silence differences
    for stem in stems:
        # Detect leading silence (first sample above -60 dBFS threshold)
        p = Path(stem["path"]).expanduser()
        try:
            audio_check, sr_check = sf.read(str(p), dtype="float32")
            mono = _to_mono(audio_check)
            threshold = 10 ** (-60.0 / 20.0)
            above = np.where(np.abs(mono) > threshold)[0]
            if len(above) > 0:
                leading_ms = round(1000.0 * above[0] / sr_check, 1)
                trailing_ms = round(1000.0 * (len(mono) - above[-1] - 1) / sr_check, 1)
                stem["leading_silence_ms"] = leading_ms
                stem["trailing_silence_ms"] = trailing_ms
        except Exception:
            pass

    # Optional reference master
    ref_result = None
    if reference_master:
        ref_path = Path(reference_master).expanduser()
        if not ref_path.exists():
            warnings.append(_make_error(
                ERR_FILE_READ_FAILED,
                f"Reference master not found: {reference_master}",
                severity="warning",
            ))
        else:
            try:
                ref_audio, ref_sr = sf.read(str(ref_path), dtype="float32")
                ref_result = _meter_stem(ref_audio, ref_sr, str(ref_path))
            except Exception as e:
                warnings.append(_make_error(
                    ERR_FILE_READ_FAILED,
                    f"Could not read reference master: {e}",
                    severity="warning",
                ))

    # Verdict
    if errors:
        verdict = "FAIL"
    elif warnings:
        verdict = "WARN"
    else:
        verdict = "PASS"

    result = {
        "schema_version": "2.2.0",
        "verdict": verdict,
        "errors": errors,
        "warnings": warnings,
        "stems": stems,
        "reference_master": ref_result,
    }

    return json.dumps(result, indent=2)


# ── MCP Tools: Audio File Discovery ──

@mcp.tool()
async def list_audio_files(limit: int = 20) -> str:
    """List recent WAV and AIFF files in the configured audio directory (default: ~/Downloads). Returns files sorted by modification time, newest first. Use this to find exported audio files for analysis. IMPORTANT: Always tell the user which file you picked (name and path) before passing it to analyse_audio."""
    audio_dir = _load_audio_directory()
    if not audio_dir.is_dir():
        return json.dumps({"error": f"Audio directory not found: {audio_dir}"})

    extensions = {".wav", ".aiff", ".aif"}
    files = []
    for f in audio_dir.iterdir():
        if f.suffix.lower() in extensions and f.is_file():
            stat = f.stat()
            files.append({
                "path": str(f),
                "name": f.name,
                "size_mb": round(stat.st_size / (1024 * 1024), 2),
                "modified": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(stat.st_mtime)),
                "modified_timestamp": stat.st_mtime,
            })

    files.sort(key=lambda x: x["modified_timestamp"], reverse=True)
    files = files[:limit]

    # Remove raw timestamp from output
    for f in files:
        del f["modified_timestamp"]

    return json.dumps({
        "audio_directory": str(audio_dir),
        "files": files,
        "count": len(files),
    }, indent=2)



# ── MCP Tools: Parameter Modification ──

@mcp.tool()
async def apply_changes(changes: list) -> str:
    """Apply changes across tracks. Supports three formats:

    1. Device parameter changes: {track_name, device_name, param_name, proposed_value, reason(optional)}
       For native Ableton devices, proposed_value is in display units (e.g. -12.0 for "-12.0 dB").
       The bridge auto-detects native vs 3rd-party and uses the correct conversion.

    2. Device parameter changes with target (3rd-party plugins):
       {track_name, device_name, param_name, proposed_value, target_display_value, reason(optional)}
       Add target_display_value (e.g. "80 Hz", "-3 dB", "14.0k Hz") for precise convergence.
       The bridge will iteratively adjust the raw value until the plugin displays the target.
       Without target_display_value, the raw proposed_value is set directly (existing behavior).

    3. Send level changes: {track_name, device_name: "Send -> ReturnName", proposed_value (dB)}

    4. Mixer property changes: {track_name, property, value, reason(optional)}
       Supported properties:
         - volume: float dB (-inf to +6), e.g. {"track_name": "Vocals", "property": "volume", "value": -6.0}
         - pan: float -1.0 (L) to 1.0 (R), e.g. {"track_name": "Vocals", "property": "pan", "value": -0.25}
         - mute: bool, e.g. {"track_name": "Vocals", "property": "mute", "value": true}
         - solo: bool, e.g. {"track_name": "Vocals", "property": "solo", "value": true}
         - arm: bool (audio/MIDI tracks only), e.g. {"track_name": "Vocals", "property": "arm", "value": true}
    """
    result = await bridge_call("apply_changes", {"changes": changes})
    return json.dumps(result, indent=2)


@mcp.tool()
async def restore_snapshot(track_name: str, snapshot: dict) -> str:
    """Restore a parameter snapshot to revert all parameters to their previous state."""
    result = await bridge_call("restore_snapshot", {
        "track_name": track_name,
        "snapshot": snapshot,
    })
    return json.dumps(result, indent=2)


# ── MCP Tools: Plugin Registry ──

@mcp.tool()
async def get_plugin_registry() -> str:
    """Read the plugin registry (generated by scan_plugins.command) for suggesting processing chains."""
    if not REGISTRY_PATH.exists():
        return json.dumps({
            "error": "plugin_registry.json not found. Run scan_plugins.command to generate it.",
        })
    try:
        with open(REGISTRY_PATH, "r") as f:
            registry = json.load(f)
        return json.dumps(registry, indent=2)
    except Exception as e:
        return json.dumps({"error": f"Could not read plugin registry: {e}"})


# ── Entry Point ──

if __name__ == "__main__":
    mcp.run(transport="stdio")
