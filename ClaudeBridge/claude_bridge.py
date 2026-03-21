"""
ClaudeBridge Control Surface for Ableton Live 12.
Runs an HTTP server on 127.0.0.1:8765 exposing the Live Object Model as JSON.
Uses only Python standard library — no pip packages.
"""

import Live
import http.server
import queue
import threading
import json
import math
import re
import traceback
import time


def lin_to_db(v):
    """Standard linear amplitude to dB — used for metering only."""
    return round(20 * math.log10(v), 2) if v > 0 else -120.0


def db_to_lin(db):
    """Standard dB to linear amplitude — used for metering only."""
    return math.pow(10, db / 20.0) if db > -120 else 0.0


# ── Ableton fader curve ──
# Ableton's mixer faders use a proprietary non-linear curve.
# Instead of approximating, we use Ableton's own str_for_value() to read dB,
# and binary search with str_for_value() to write dB values accurately.


def _parse_db_display(display_str):
    """Parse a dB string from Ableton's str_for_value (e.g. '-6.0 dB' or '-inf dB')."""
    text = display_str.replace("dB", "").strip()
    if text == "-inf" or text == "":
        return -70.0
    return round(float(text), 1)


def read_fader_db(param):
    """Read the current dB value from a fader parameter using Ableton's own display."""
    try:
        display = param.str_for_value(param.value)
        return _parse_db_display(display)
    except Exception:
        return -70.0


def write_fader_db(param, target_db):
    """Set a fader parameter to a target dB using binary search with str_for_value.
    Returns the actual dB value that was set."""
    if target_db <= -70.0:
        param.value = param.min
        return -70.0
    if target_db >= 6.0:
        param.value = param.max
        return 6.0

    low = param.min
    high = param.max
    best = (low + high) / 2.0

    for _ in range(40):
        mid = (low + high) / 2.0
        try:
            display = param.str_for_value(mid)
            current_db = _parse_db_display(display)
        except Exception:
            break

        best = mid
        if abs(current_db - target_db) < 0.1:
            break
        if current_db < target_db:
            low = mid
        else:
            high = mid

    param.value = best
    return read_fader_db(param)


# ── Native device parameter helpers ──
# Ableton native devices use non-linear internal mappings (e.g. Utility Gain:
# raw -1.0..1.0 maps to -inf..+35 dB). 3rd-party plugins (VST/AU/Max for Live)
# use normalized 0.0–1.0 values and handle their own mapping internally.
# For native devices, we use binary search with str_for_value() to find the
# correct raw value. For 3rd-party plugins, we pass the raw value directly.

_THIRD_PARTY_CLASSES = frozenset([
    "PluginDevice", "AuPluginDevice",
    "MxDeviceAudioEffect", "MxDeviceInstrument", "MxDeviceMidi",
])


def is_native_device(device):
    """Check if a device is an Ableton native device."""
    return device.class_name not in _THIRD_PARTY_CLASSES


def _parse_display_value(display_str):
    """Parse a display string into (numeric_value, unit).
    Handles: '-12.0 dB', '120 Hz', '2.8k Hz', '14.0k Hz', '3.2:1', 'On', 'Off'.
    Returns (None, None) if unparseable."""
    s = display_str.strip()

    # Boolean/toggle
    if s.lower() == "on":
        return 1.0, "bool"
    if s.lower() == "off":
        return 0.0, "bool"

    # Ratio format: "3.2:1"
    m = re.match(r"([+-]?\d+\.?\d*):1", s)
    if m:
        return float(m.group(1)), "ratio"

    # "k" multiplier: "14.0k Hz", "2.8k Hz"
    m = re.match(r"([+-]?\d+\.?\d*)k\s*(Hz)", s, re.IGNORECASE)
    if m:
        return float(m.group(1)) * 1000.0, m.group(2)

    # Standard numeric with optional unit: "-3 dB", "80 Hz", "100.0 %"
    m = re.match(r"([+-]?\d+\.?\d*)\s*([a-zA-Z%]*)", s)
    if m and m.group(1):
        unit = m.group(2) if m.group(2) else ""
        return float(m.group(1)), unit

    return None, None


def _parse_numeric_from_display(display_str):
    """Extract just the numeric value from a display string (backward compat)."""
    val, _ = _parse_display_value(display_str)
    return val


def _extract_unit(display_str):
    """Extract just the unit from a display string (backward compat)."""
    _, unit = _parse_display_value(display_str)
    return unit


def _tolerance_for_unit(unit, target_value):
    """Return an appropriate tolerance for a given unit type."""
    if unit == "dB":
        return 0.3
    if unit in ("Hz", "kHz"):
        return abs(target_value) * 0.05 if target_value != 0 else 1.0
    if unit == "%":
        return 1.0
    if unit in ("s", "ms"):
        return abs(target_value) * 0.05 if target_value != 0 else 0.001
    return abs(target_value) * 0.05 if target_value != 0 else 0.01


def set_native_param(param, target_value):
    """Set a native device parameter using binary search with str_for_value().
    Returns dict with result details."""
    # Quantized/boolean: set directly
    if param.is_quantized:
        clamped = max(param.min, min(param.max, target_value))
        param.value = clamped
        try:
            display = param.str_for_value(param.value)
        except Exception:
            display = str(param.value)
        return {
            "status": "applied",
            "display_value": display,
            "raw_value": param.value,
        }

    # Determine unit and tolerance from current display
    try:
        current_display = param.str_for_value(param.value)
        unit = _extract_unit(current_display)
    except Exception:
        unit = None
    tolerance = _tolerance_for_unit(unit, target_value)

    low = param.min
    high = param.max
    best_raw = (low + high) / 2.0
    best_error = float("inf")
    best_display = ""

    for _ in range(40):
        mid = (low + high) / 2.0
        try:
            display = param.str_for_value(mid)
            actual = _parse_numeric_from_display(display)
        except Exception:
            break

        if actual is None:
            break

        error = abs(actual - target_value)
        if error < best_error:
            best_error = error
            best_raw = mid
            best_display = display

        if error <= tolerance:
            break

        if actual < target_value:
            low = mid
        else:
            high = mid

        if (high - low) < 1e-10:
            break

    param.value = best_raw

    # Verify
    try:
        verified_display = param.str_for_value(param.value)
        verified_value = _parse_numeric_from_display(verified_display)
    except Exception:
        verified_display = best_display
        verified_value = None

    return {
        "status": "applied",
        "target_value": target_value,
        "display_value": verified_display,
        "raw_value": best_raw,
        "is_native": True,
        "verified": verified_value is not None and abs(verified_value - target_value) <= tolerance,
    }


# ── 3rd-party plugin parameter convergence ──
# VST/AU plugins handle their own internal mapping. str_for_value() may return
# stale data, so we must actually set param.value and read back the display.
# Only triggered when the change includes a "target_display_value" field.

def converge_vst_param(param, initial_raw, target_value, target_unit):
    """Iteratively converge a 3rd-party plugin parameter to a target display value.
    Actually sets param.value on each iteration and reads back the display."""
    tolerance = _tolerance_for_unit(target_unit, target_value)

    # Boolean/toggle: try common raw values
    if target_unit == "bool":
        for raw in ([1.0, 0.5] if target_value == 1.0 else [0.0]):
            clamped = max(param.min, min(param.max, raw))
            param.value = clamped
            try:
                display = param.str_for_value(param.value)
                actual, _ = _parse_display_value(display)
            except Exception:
                continue
            if actual == target_value:
                return {
                    "status": "applied",
                    "target_value": target_value,
                    "display_value": display,
                    "raw_value": clamped,
                    "converged": True,
                }
        # Fallback to initial guess
        param.value = max(param.min, min(param.max, initial_raw))
        try:
            display = param.str_for_value(param.value)
        except Exception:
            display = str(param.value)
        return {
            "status": "applied",
            "target_value": target_value,
            "display_value": display,
            "raw_value": param.value,
            "converged": False,
        }

    # Set initial guess and read back to determine search direction
    low = param.min
    high = param.max
    param.value = max(low, min(high, initial_raw))
    try:
        display = param.str_for_value(param.value)
        actual, _ = _parse_display_value(display)
    except Exception:
        actual = None
        display = ""

    if actual is None:
        return {
            "status": "applied",
            "target_value": target_value,
            "display_value": display,
            "raw_value": initial_raw,
            "converged": False,
            "note": "Cannot parse display value — set raw value directly",
        }

    # Already good enough?
    if abs(actual - target_value) <= tolerance:
        return {
            "status": "applied",
            "target_value": target_value,
            "display_value": display,
            "raw_value": initial_raw,
            "converged": True,
        }

    # Probe direction: is higher raw = higher display, or inverted?
    probe_raw = min(initial_raw + 0.05, high)
    if probe_raw == initial_raw:
        probe_raw = max(initial_raw - 0.05, low)
    param.value = probe_raw
    try:
        probe_display = param.str_for_value(param.value)
        probe_actual, _ = _parse_display_value(probe_display)
    except Exception:
        probe_actual = None
    is_inverted = (probe_actual is not None and probe_actual < actual
                   and probe_raw > initial_raw)

    best_raw = initial_raw
    best_error = abs(actual - target_value)
    best_display = display
    recent_actuals = []

    for iteration in range(25):
        mid = (low + high) / 2.0
        param.value = mid
        try:
            display = param.str_for_value(param.value)
            actual, _ = _parse_display_value(display)
        except Exception:
            break
        if actual is None:
            break

        error = abs(actual - target_value)
        if error < best_error:
            best_error = error
            best_raw = mid
            best_display = display

        if error <= tolerance:
            return {
                "status": "applied",
                "target_value": target_value,
                "display_value": display,
                "raw_value": mid,
                "converged": True,
            }

        # Oscillation detection
        recent_actuals.append(actual)
        if len(recent_actuals) > 4:
            recent_actuals.pop(0)
            if len(set(str(a) for a in recent_actuals)) <= 2:
                break

        # Binary search direction
        if is_inverted:
            if actual < target_value:
                high = mid
            else:
                low = mid
        else:
            if actual < target_value:
                low = mid
            else:
                high = mid

        if (high - low) < 1e-7:
            break

    # Set best result
    param.value = best_raw
    return {
        "status": "applied",
        "target_value": target_value,
        "display_value": best_display,
        "raw_value": best_raw,
        "converged": best_error <= tolerance,
    }


def get_plugin_format(device):
    class_name = device.class_name
    format_map = {
        "AuPluginDevice": "AU",
        "PluginDevice": "VST",
        "MxDeviceAudioEffect": "Max for Live",
        "MxDeviceInstrument": "Max for Live",
        "MxDeviceMidi": "Max for Live",
        "AudioEffectGroupDevice": "Audio Effect Rack",
        "InstrumentGroupDevice": "Instrument Rack",
        "MidiEffectGroupDevice": "MIDI Effect Rack",
        "DrumGroupDevice": "Drum Rack",
    }
    return format_map.get(class_name, "Native")


def get_track_type(track):
    try:
        if track.has_audio_input:
            return "Audio"
    except AttributeError:
        pass
    try:
        if track.has_midi_input:
            return "MIDI"
    except AttributeError:
        pass
    return "Return/Master"


def build_device_summary(device):
    """Build device info without parameter values (Tier 1)."""
    result = {
        "name": device.name,
        "plugin_format": get_plugin_format(device),
        "class_name": device.class_name,
        "active": device.is_active,
        "param_count": len(device.parameters),
    }
    if hasattr(device, "chains"):
        chains = []
        for chain in device.chains:
            chain_devices = []
            for inner_device in chain.devices:
                chain_devices.append(build_device_summary(inner_device))
            chains.append({
                "name": chain.name,
                "devices": chain_devices,
            })
        result["rack_chains"] = chains
    return result


def build_device_params(device):
    """Build full parameter data for a device (Tier 2)."""
    params = []
    for i, param in enumerate(device.parameters):
        try:
            display = param.str_for_value(param.value)
        except Exception:
            display = str(param.value)
        params.append({
            "index": i,
            "name": param.name,
            "value": param.value,
            "display_value": display,
            "min": param.min,
            "max": param.max,
            "is_quantized": param.is_quantized,
        })
    return params


class ClaudeBridge:
    """Ableton Live Control Surface that exposes the Live Object Model via HTTP."""

    def __init__(self, c_instance):
        self._c_instance = c_instance
        self._lock = threading.Lock()
        self._request_queue = queue.Queue()
        self._server = None
        self._server_thread = None
        try:
            self._start_server()
            self.log("ClaudeBridge ready on port 8765")
        except OSError as e:
            self.log("Could not start server (port 8765 may be in use): " + str(e))

    def log(self, msg):
        self._c_instance.log_message("[ClaudeBridge] " + str(msg))

    def disconnect(self):
        if self._server:
            self._server.shutdown()
            self.log("ClaudeBridge server stopped")

    def connect_script_instances(self, scripts):
        pass

    def request_rebuild_midi_map(self):
        pass

    def update_display(self):
        """Called by Live ~10x/sec on the main thread. Drains the request queue."""
        while True:
            try:
                method, params, result_event, result_holder = self._request_queue.get_nowait()
            except queue.Empty:
                break
            try:
                result, error = self._dispatch(method, params)
                result_holder["result"] = result
                result_holder["error"] = error
            except Exception as e:
                self.log("Queue dispatch error: " + traceback.format_exc())
                result_holder["result"] = None
                result_holder["error"] = str(e)
            finally:
                result_event.set()

    def build_midi_map(self, midi_map_handle):
        pass

    def refresh_state(self):
        pass

    def suggest_input_port(self):
        return ""

    def suggest_output_port(self):
        return ""

    def suggest_map_mode(self, cc_no, channel):
        return Live.MidiMap.MapMode.absolute

    def can_lock_to_devices(self):
        return False

    def toggle_lock(self):
        pass

    # ── HTTP Server ──

    def _start_server(self):
        bridge = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, format, *args):
                # Suppress default stderr logging
                pass

            def do_GET(self):
                if self.path == "/ping":
                    self.send_response(200)
                    self.send_header("Content-Type", "text/plain")
                    self.end_headers()
                    self.wfile.write(b"pong")
                else:
                    self.send_response(404)
                    self.end_headers()

            def do_POST(self):
                # Methods that sleep or do their own timing stay on the HTTP thread
                DIRECT_METHODS = {"get_metering_snapshot"}

                try:
                    length = int(self.headers.get("Content-Length", 0))
                    body = self.rfile.read(length).decode("utf-8")
                    request = json.loads(body)
                    method = request.get("method", "")
                    params = request.get("params", {})

                    if method in DIRECT_METHODS:
                        with bridge._lock:
                            result, error = bridge._dispatch(method, params)
                    else:
                        # Queue for Live's main thread via update_display()
                        result_event = threading.Event()
                        result_holder = {"result": None, "error": None}
                        bridge._request_queue.put((method, params, result_event, result_holder))
                        if not result_event.wait(timeout=30):
                            result, error = None, "Request timed out waiting for Live main thread"
                        else:
                            result = result_holder["result"]
                            error = result_holder["error"]

                    response = json.dumps({"result": result, "error": error})
                except Exception as e:
                    bridge.log("Handler error: " + traceback.format_exc())
                    response = json.dumps({"result": None, "error": str(e)})

                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(response.encode("utf-8"))

        self._server = http.server.HTTPServer(("127.0.0.1", 8765), Handler)
        self._server_thread = threading.Thread(target=self._server.serve_forever)
        self._server_thread.daemon = True
        self._server_thread.start()

    # ── Dispatch ──

    def _get_song(self):
        return Live.Application.get_application().get_document()

    def _dispatch(self, method, params):
        """Route a method call to the appropriate handler. Returns (result, error)."""
        try:
            handler = getattr(self, "_handle_" + method, None)
            if handler is None:
                return None, "Unknown method: " + method
            result = handler(params)
            return result, None
        except Exception as e:
            self.log("Dispatch error [" + method + "]: " + traceback.format_exc())
            return None, str(e)

    # ── Track Lookup Helpers ──

    def _all_tracks(self, song):
        """Return list of (track, category) tuples for all tracks."""
        tracks = []
        for t in song.tracks:
            tracks.append((t, "track"))
        for t in song.return_tracks:
            tracks.append((t, "return"))
        tracks.append((song.master_track, "master"))
        return tracks

    def _find_track(self, song, track_name):
        """Find a track by name. Returns (track, category) or raises ValueError."""
        for t, cat in self._all_tracks(song):
            if t.name == track_name:
                return t, cat
        available = [t.name for t, _ in self._all_tracks(song)]
        raise ValueError(
            "Track not found: " + track_name + ". Available tracks: " + ", ".join(available)
        )

    def _find_device(self, track, device_name):
        """Find a device by name on a track. Returns device or raises ValueError."""
        for device in track.devices:
            if device.name == device_name:
                return device
            # Search inside racks
            if hasattr(device, "chains"):
                for chain in device.chains:
                    for inner_device in chain.devices:
                        if inner_device.name == device_name:
                            return inner_device
        device_names = [d.name for d in track.devices]
        raise ValueError(
            "Device not found: " + device_name
            + ". Devices on " + track.name + ": " + ", ".join(device_names)
        )

    # ── Track Detail Builder ──

    def _build_track_detail(self, track, song):
        """Build full structural detail for a track (no parameter values)."""
        detail = {
            "name": track.name,
            "track_type": get_track_type(track),
            "volume_db": read_fader_db(track.mixer_device.volume),
            "pan": round(track.mixer_device.panning.value, 2),
            "mute": track.mute,
            "solo": track.solo,
        }

        # Sends
        sends = []
        return_tracks = song.return_tracks
        for i, send in enumerate(track.mixer_device.sends):
            return_name = return_tracks[i].name if i < len(return_tracks) else "?"
            sends.append({
                "return_name": chr(65 + i),  # A, B, C...
                "return_track": return_name,
                "level_db": read_fader_db(send),
            })
        detail["sends"] = sends

        # Device chain
        device_chain = []
        for idx, device in enumerate(track.devices):
            entry = build_device_summary(device)
            entry["index"] = idx
            device_chain.append(entry)
        detail["device_chain"] = device_chain

        # Metering
        peak_l = track.output_meter_left
        peak_r = track.output_meter_right
        peak = max(peak_l, peak_r)
        detail["metering"] = {
            "peak_dbfs": lin_to_db(peak),
            "metering_note": "Peak meters only; Live API does not expose true RMS/LUFS.",
        }

        return detail

    # ── Method Handlers ──

    def _handle_get_session_overview(self, params):
        song = self._get_song()

        tracks = []
        for t in song.tracks:
            tracks.append({
                "name": t.name,
                "track_type": get_track_type(t),
                "mute": t.mute,
                "volume_db": read_fader_db(t.mixer_device.volume),
                "device_names": [d.name for d in t.devices],
                "device_count": len(t.devices),
            })

        return_tracks = []
        for t in song.return_tracks:
            return_tracks.append({
                "name": t.name,
                "track_type": "Return/Master",
                "mute": t.mute,
                "volume_db": read_fader_db(t.mixer_device.volume),
                "device_names": [d.name for d in t.devices],
                "device_count": len(t.devices),
            })

        master = song.master_track
        master_info = {
            "name": master.name,
            "volume_db": read_fader_db(master.mixer_device.volume),
            "device_names": [d.name for d in master.devices],
            "device_count": len(master.devices),
        }

        sig = song.signature_numerator
        sig_den = song.signature_denominator

        return {
            "session_name": song.name if hasattr(song, "name") else "Untitled",
            "tempo": round(song.tempo, 1),
            "time_signature": str(sig) + "/" + str(sig_den),
            "track_count": len(song.tracks),
            "return_count": len(song.return_tracks),
            "tracks": tracks,
            "return_tracks": return_tracks,
            "master": master_info,
        }

    def _handle_get_track_detail(self, params):
        song = self._get_song()
        track_name = params.get("track_name", "")
        track, _ = self._find_track(song, track_name)
        return self._build_track_detail(track, song)

    def _handle_get_all_tracks(self, params):
        song = self._get_song()

        tracks = []
        for t in song.tracks:
            tracks.append(self._build_track_detail(t, song))

        return_tracks = []
        for t in song.return_tracks:
            return_tracks.append(self._build_track_detail(t, song))

        master = self._build_track_detail(song.master_track, song)

        return {
            "tracks": tracks,
            "return_tracks": return_tracks,
            "master": master,
        }

    def _handle_get_device_params(self, params):
        song = self._get_song()
        track_name = params.get("track_name", "")
        device_name = params.get("device_name", "")
        track, _ = self._find_track(song, track_name)
        device = self._find_device(track, device_name)

        return {
            "track": track.name,
            "device": device.name,
            "plugin_format": get_plugin_format(device),
            "class_name": device.class_name,
            "parameters": build_device_params(device),
        }

    def _handle_get_return_tracks(self, params):
        song = self._get_song()
        result = []

        for i, rt in enumerate(song.return_tracks):
            detail = self._build_track_detail(rt, song)

            # Build receiving_sends: which tracks send to this return and at what level
            receiving = []
            for t in song.tracks:
                sends = t.mixer_device.sends
                if i < len(sends):
                    send_param = sends[i]
                    if send_param.value > 0:
                        receiving.append({
                            "track_name": t.name,
                            "send_level_db": read_fader_db(send_param),
                        })
            detail["receiving_sends"] = receiving
            result.append(detail)

        return {"return_tracks": result}

    def _handle_get_master_bus_state(self, params):
        song = self._get_song()
        return self._build_track_detail(song.master_track, song)

    def _handle_get_metering(self, params):
        song = self._get_song()
        track_name = params.get("track_name", None)

        if track_name:
            track, _ = self._find_track(song, track_name)
            peak = max(track.output_meter_left, track.output_meter_right)
            peak_db = lin_to_db(peak)
            return {
                "peak_dbfs": peak_db,
                "metering_note": "Peak meters only; Live API does not expose true RMS/LUFS.",
            }

        tracks = []
        for t in song.tracks:
            peak = max(t.output_meter_left, t.output_meter_right)
            peak_db = lin_to_db(peak)
            tracks.append({
                "track_name": t.name,
                "peak_dbfs": peak_db,
            })

        master = song.master_track
        m_peak = max(master.output_meter_left, master.output_meter_right)
        m_peak_db = lin_to_db(m_peak)

        return {
            "tracks": tracks,
            "master": {
                "peak_dbfs": m_peak_db,
            },
            "metering_note": "Peak meters only; Live API does not expose true RMS/LUFS.",
        }

    def _handle_get_metering_snapshot(self, params):
        song = self._get_song()
        track_name = params.get("track_name", None)
        duration = float(params.get("duration_seconds", 15.0))
        readings = int(params.get("readings", 10))
        interval = duration / max(readings, 1)

        # Determine which tracks to monitor
        if track_name:
            track, _ = self._find_track(song, track_name)
            monitor_tracks = [(track, track_name)]
        else:
            monitor_tracks = [(t, t.name) for t in song.tracks]
            master = song.master_track
            monitor_tracks.append((master, "Master"))

        # Collect readings
        track_readings = {}
        for _, name in monitor_tracks:
            track_readings[name] = []

        for _ in range(readings):
            # Re-fetch song in case document changed
            song = self._get_song()
            for track_obj, name in monitor_tracks:
                peak = max(track_obj.output_meter_left, track_obj.output_meter_right)
                track_readings[name].append(peak)
            time.sleep(interval)

        # Compute statistics
        results = []
        for _, name in monitor_tracks:
            peaks = track_readings[name]
            non_zero = [p for p in peaks if p > 0]
            if non_zero:
                peak_max = max(non_zero)
                peak_avg = sum(non_zero) / len(non_zero)
                peak_max_db = lin_to_db(peak_max)
                peak_avg_db = lin_to_db(peak_avg)
            else:
                peak_max_db = -120.0
                peak_avg_db = -120.0

            results.append({
                "track_name": name,
                "peak_max_dbfs": peak_max_db,
                "peak_avg_dbfs": peak_avg_db,
                "readings_taken": readings,
                "duration_seconds": duration,
                "metering_note": "Peak meters only; Live API does not expose true RMS/LUFS.",
            })

        if track_name:
            return results[0]
        return {"tracks": results}

    def _handle_get_alerts(self, params):
        song = self._get_song()
        alerts = []

        for t in song.tracks:
            peak = max(t.output_meter_left, t.output_meter_right)
            peak_db = lin_to_db(peak)
            if peak_db >= 0.0:
                alerts.append({
                    "severity": "CRITICAL",
                    "type": "clipping",
                    "track": t.name,
                    "value": peak_db,
                })
            elif peak_db > -6.0:
                alerts.append({
                    "severity": "WARNING",
                    "type": "hot_track",
                    "track": t.name,
                    "value": peak_db,
                })

        master = song.master_track
        m_peak = max(master.output_meter_left, master.output_meter_right)
        m_peak_db = lin_to_db(m_peak)
        if m_peak_db > -6.0:
            alerts.append({
                "severity": "CRITICAL",
                "type": "master_ceiling_breach",
                "track": "Master",
                "value": m_peak_db,
            })

        return {"alerts": alerts, "count": len(alerts)}

    def _handle_get_transport_state(self, params):
        song = self._get_song()
        beats = song.current_song_time
        tempo = song.tempo
        sig_num = song.signature_numerator
        sig_den = song.signature_denominator

        # Convert beats to bars.beats.subdivisions
        beats_per_bar = sig_num * (4.0 / sig_den)
        bar = int(beats / beats_per_bar) + 1
        beat_in_bar = beats - (bar - 1) * beats_per_bar
        beat_num = int(beat_in_bar) + 1
        subdivision = int((beat_in_bar - int(beat_in_bar)) * 4) + 1
        bars_beats = str(bar) + "." + str(beat_num) + "." + str(subdivision)

        # Convert beats to seconds
        seconds = round(beats * 60.0 / tempo, 2)

        return {
            "is_playing": song.is_playing,
            "current_position_beats": round(beats, 4),
            "current_position_bars_beats": bars_beats,
            "current_position_seconds": seconds,
            "tempo": round(tempo, 1),
            "time_signature": str(sig_num) + "/" + str(sig_den),
            "loop_enabled": song.loop,
            "loop_start_beats": round(song.loop_start, 4),
            "loop_length_beats": round(song.loop_length, 4),
        }

    def _handle_set_playhead_position(self, params):
        song = self._get_song()
        position = params.get("position")
        unit = params.get("unit", "beats")

        if unit == "seconds":
            beats_value = float(position) * song.tempo / 60.0
        elif unit == "bars_beats":
            # Parse "bar.beat.subdivision" format
            parts = str(position).split(".")
            bar = int(parts[0]) - 1
            beat = int(parts[1]) - 1 if len(parts) > 1 else 0
            sub = int(parts[2]) - 1 if len(parts) > 2 else 0
            sig_num = song.signature_numerator
            sig_den = song.signature_denominator
            beats_per_bar = sig_num * (4.0 / sig_den)
            beats_value = bar * beats_per_bar + beat + sub / 4.0
        else:
            beats_value = float(position)

        song.current_song_time = max(0.0, beats_value)
        return {"position_beats": round(beats_value, 4)}

    def _handle_transport_play(self, params):
        song = self._get_song()
        song.start_playing()
        return {"is_playing": True}

    def _handle_transport_stop(self, params):
        song = self._get_song()
        song.stop_playing()
        return {"is_playing": False}

    def _handle_transport_play_stop(self, params):
        song = self._get_song()
        if song.is_playing:
            song.stop_playing()
            return {"is_playing": False}
        else:
            song.start_playing()
            return {"is_playing": True}

    def _handle_set_loop(self, params):
        song = self._get_song()
        start = float(params.get("start", 0.0))
        length = float(params.get("length", 16.0))
        enable = params.get("enable", True)

        song.loop_start = start
        song.loop_length = length
        song.loop = enable

        return {
            "loop_start_beats": start,
            "loop_length_beats": length,
            "loop_enabled": enable,
        }

    def _handle_enable_loop(self, params):
        song = self._get_song()
        song.loop = True
        return {"loop_enabled": True}

    def _handle_disable_loop(self, params):
        song = self._get_song()
        song.loop = False
        return {"loop_enabled": False}

    def _handle_apply_changes(self, params):
        song = self._get_song()
        changes = params.get("changes", [])
        applied = []
        errors = []

        for change in changes:
            try:
                track_name = change.get("track_name", "")
                track, cat = self._find_track(song, track_name)

                # ── Mixer property changes ──
                if "property" in change:
                    prop = change["property"]
                    value = change.get("value")

                    if prop == "volume":
                        vol_param = track.mixer_device.volume
                        actual_db = write_fader_db(vol_param, float(value))
                        applied.append({
                            "change": change,
                            "status": "applied",
                            "value_db": actual_db,
                        })
                    elif prop == "pan":
                        pan_param = track.mixer_device.panning
                        pan_value = max(-1.0, min(1.0, float(value)))
                        pan_param.value = pan_value
                        applied.append({
                            "change": change,
                            "status": "applied",
                            "value": pan_value,
                        })
                    elif prop == "mute":
                        track.mute = bool(value)
                        applied.append({
                            "change": change,
                            "status": "applied",
                            "value": bool(value),
                        })
                    elif prop == "solo":
                        track.solo = bool(value)
                        applied.append({
                            "change": change,
                            "status": "applied",
                            "value": bool(value),
                        })
                    elif prop == "arm":
                        if cat in ("return", "master"):
                            raise ValueError(
                                "Cannot arm " + track_name + " — return and master tracks do not support arming."
                            )
                        track.arm = bool(value)
                        applied.append({
                            "change": change,
                            "status": "applied",
                            "value": bool(value),
                        })
                    else:
                        raise ValueError("Unknown mixer property: " + prop)
                    continue

                device_name = change.get("device_name", "")
                param_name = change.get("param_name", "")
                proposed_value = float(change.get("proposed_value", 0))
                reason = change.get("reason", "")

                # Handle send level changes
                if device_name.startswith("Send"):
                    # Format: "Send -> ReturnName"
                    parts = device_name.split("->")
                    if len(parts) == 2:
                        return_name = parts[1].strip()
                        return_tracks = song.return_tracks
                        send_index = None
                        for i, rt in enumerate(return_tracks):
                            if rt.name == return_name:
                                send_index = i
                                break
                        if send_index is None:
                            raise ValueError("Return track not found: " + return_name)
                        sends = track.mixer_device.sends
                        if send_index >= len(sends):
                            raise ValueError("Send index out of range")
                        send = sends[send_index]
                        actual_db = write_fader_db(send, proposed_value)
                        applied.append({
                            "change": change,
                            "status": "applied",
                            "value_db": actual_db,
                        })
                        continue

                device = self._find_device(track, device_name)

                # Find parameter by name
                target_param = None
                for p in device.parameters:
                    if p.name == param_name:
                        target_param = p
                        break

                if target_param is None:
                    param_names = [p.name for p in device.parameters]
                    raise ValueError(
                        "Parameter not found: " + param_name
                        + ". Parameters: " + ", ".join(param_names)
                    )

                # Native devices: binary search with str_for_value (non-destructive)
                # 3rd-party + target_display_value: iterative set-read-converge
                # 3rd-party without target: direct raw value set (unchanged)
                if is_native_device(device):
                    result = set_native_param(target_param, proposed_value)
                    result["change"] = change
                    applied.append(result)
                else:
                    target_display = change.get("target_display_value")
                    if target_display is not None:
                        tv, tu = _parse_display_value(target_display)
                        if tv is not None:
                            result = converge_vst_param(
                                target_param, proposed_value, tv, tu,
                            )
                            result["change"] = change
                            result["is_native"] = False
                            applied.append(result)
                            continue
                    # No target or unparseable — direct raw set
                    clamped = max(target_param.min, min(target_param.max, proposed_value))
                    target_param.value = clamped
                    try:
                        display = target_param.str_for_value(target_param.value)
                    except Exception:
                        display = str(clamped)
                    applied.append({
                        "change": change,
                        "status": "applied",
                        "raw_value": clamped,
                        "display_value": display,
                        "is_native": False,
                    })

            except Exception as e:
                errors.append({
                    "change": change,
                    "status": "error",
                    "message": str(e),
                })

        return {"applied": applied, "errors": errors}

    def _handle_restore_snapshot(self, params):
        song = self._get_song()
        track_name = params.get("track_name", "")
        snapshot = params.get("snapshot", {})
        track, _ = self._find_track(song, track_name)

        restored = 0
        device_chain = snapshot.get("device_chain", [])

        for device_snap in device_chain:
            d_name = device_snap.get("name", "")
            try:
                device = self._find_device(track, d_name)
            except ValueError:
                continue

            for param_snap in device_snap.get("parameters", []):
                p_name = param_snap.get("name", "")
                p_value = param_snap.get("value", None)
                if p_value is None:
                    continue
                for p in device.parameters:
                    if p.name == p_name:
                        clamped = max(p.min, min(p.max, float(p_value)))
                        p.value = clamped
                        restored += 1
                        break

        return {"restored_params": restored, "track": track_name}

    def _handle_export_prepare(self, params):
        """Prepare for audio export — set playhead and return track index."""
        song = self._get_song()
        track_name = params.get("track_name", "")
        start_beats = params.get("start_position_beats", None)

        if start_beats is not None:
            song.current_song_time = float(start_beats)
        else:
            start_beats = song.current_song_time

        # Find track index
        track_index = None
        for i, t in enumerate(song.tracks):
            if t.name == track_name:
                track_index = i
                break

        if track_index is None:
            # Check return tracks
            for i, t in enumerate(song.return_tracks):
                if t.name == track_name:
                    track_index = len(song.tracks) + i
                    break

        if track_index is None and song.master_track.name == track_name:
            track_index = -1  # Master

        if track_index is None:
            available = [t.name for t, _ in self._all_tracks(song)]
            raise ValueError(
                "Track not found: " + track_name
                + ". Available: " + ", ".join(available)
            )

        return {
            "track_name": track_name,
            "track_index": track_index,
            "start_beats": round(float(start_beats), 4),
            "duration_seconds": float(params.get("duration_seconds", 15.0)),
            "sample_rate": 44100,
        }
