# Ableton-Claude-Bridge

**v2.2.0** | [Apache 2.0 License](LICENSE)

Ableton-Claude-Bridge gives Claude Desktop full read/write access to any Ableton Live 12 session. It runs as an invisible Control Surface — no Max for Live, no devices on any track.

## What Claude Can Do

- **Read** every plugin parameter on every track (Native, AU, VST, VST3, Racks)
- **Modify** any parameter — EQ, compression, gain, sends, and more
- **Mixer control** — volume, pan, mute, solo, arm on any track
- **Metering** — real-time peak levels, statistical snapshots, clipping alerts
- **Transport** — play, stop, play/stop toggle, set position, loop control
- **Export** — opens Ableton's Export Audio dialog; user saves the file manually
- **Audio file discovery** — list audio files in a configured directory
- **Analyse** — professional audio analysis: ITU BS.1770-4 LUFS, EBU R 128 true peak, per-channel RMS, 8-band spectrum, resonance/null detection, stereo/phase correlation, dynamics, transient profiling, Atmos mono-folddown safety, masking analysis
- **Stem QC** — validate a set of stems for sample rate, length, overs, headroom, and DC offset
- **Undo** — snapshot and restore parameter states

## Requirements

- macOS (Apple Silicon or Intel)
- Ableton Live 12 (any edition — auto-detected by installer)
- Claude Desktop
- Python 3.10+

## Download & Install

### 1. Download

Go to the [Releases page](https://github.com/pixelnmusic/ableton-claude-bridge/releases) and download the latest `Ableton-Claude-Bridge-v*.zip` file.

Alternatively, download directly from the terminal:

```bash
curl -L -o ~/Downloads/Ableton-Claude-Bridge.zip \
  https://github.com/pixelnmusic/ableton-claude-bridge/releases/latest/download/Ableton-Claude-Bridge-v2.2.0.zip
```

### 2. Extract

Double-click the ZIP file in Finder to extract it, or:

```bash
cd ~/Downloads && unzip Ableton-Claude-Bridge.zip
```

### 3. Install

1. Open the extracted folder and double-click `install.command` in Finder
2. If macOS warns about an unidentified developer, right-click the file and select **Open**
3. The installer will:
   - Copy the Control Surface to Ableton's Remote Scripts
   - Create a Python virtual environment with dependencies
   - Configure Claude Desktop's MCP settings
   - Auto-detect your Ableton Live app name

### 4. Activate in Ableton

1. Open Ableton Live
2. Go to **Preferences > MIDI**
3. Set any Control Surface slot to **ClaudeBridge** (no MIDI port needed)
4. Verify in **Help > Show Live Log File**: `[ClaudeBridge] ClaudeBridge ready on port 8765`
5. Restart Claude Desktop

### Plugin Registry (Optional)

Double-click `scan_plugins.command` to generate `plugin_registry.json`. This lets Claude suggest processing chains from your installed plugins.

### Accessibility Permissions

Audio export uses AppleScript to drive Ableton's Export dialog. Grant Accessibility access to Claude Desktop (and Terminal if using CLI) in **System Preferences > Privacy & Security > Accessibility**.

## How Parameters Work

Ableton-Claude-Bridge uses a three-tier system to set parameters accurately:

### Native Devices (EQ Eight, Compressor, etc.)

Binary search using `str_for_value()` — the Control Surface API can probe any value's display string without actually changing the parameter. This is **non-destructive**: the bridge finds the exact raw value that produces the desired display value (e.g. "-3.0 dB") without the parameter audibly moving.

### 3rd-Party Plugins (VST/AU) with Target Display Value

When `apply_changes` includes a `target_display_value`, an iterative set-read-converge loop is used. It must actually set the parameter to read back the display string (VST/AU plugins don't support non-destructive probing). The loop converges within a few iterations.

### 3rd-Party Plugins without Target Display Value

Direct raw value set — the caller provides a 0.0-1.0 value and it is set immediately.

### Mixer Faders & Sends

Ableton's volume fader uses a proprietary curve (0.85 = 0 dB, 1.0 = +6 dB). Binary search with `str_for_value()` finds the correct raw value for any dB target.

## Configuration

Ableton-Claude-Bridge reads settings from `claude_bridge_config.json` in the project root:

```json
{
  "audio_directory": "~/Downloads"
}
```

| Setting | Description |
|---|---|
| `audio_directory` | Directory where `list_audio_files` searches for audio files. Supports `~` for home directory. Default: `~/Downloads` |

## Architecture

```
Claude Desktop
      |
   stdio (MCP protocol)
      |
MCP Server  (mcp_server/claude_mcp_server.py)
      |                    |
HTTP POST (port 8765)   AppleScript (osascript)
      |                    |
ClaudeBridge               Export dialog
Control Surface
      |
Ableton Live API (Live Object Model)
```

## File Structure

```
ClaudeBridge/                  Control Surface (copied to Remote Scripts)
    __init__.py                create_instance() entry point
    claude_bridge.py           HTTP server + Live API access

mcp_server/
    claude_mcp_server.py       MCP server with all tools
    requirements.txt           Python dependencies
    venv/                      Python venv (created by installer)
    tests/                     Metering and analysis tests

exports/                       WAV exports written here
claude_bridge_config.json      Audio directory and other settings
plugin_registry.json           Generated by scan_plugins.command
install.command                Double-click installer
scan_plugins.command           Plugin scanner
build_release.command          Build distributable ZIP
```

## MCP Tools

| Tool | Description |
|---|---|
| `check_connection` | Verify the bridge is running |
| `get_session_overview` | Lightweight session summary |
| `get_track_detail` | Full track structure (no param values) |
| `get_all_tracks` | All tracks at once |
| `get_return_tracks` | Returns with send routing |
| `get_master_bus_state` | Master track detail |
| `get_device_params` | Full parameter list for one device |
| `get_metering` | Instantaneous peak levels |
| `get_metering_snapshot` | Statistical metering over time |
| `get_alerts` | Clipping and hot level detection |
| `get_transport_state` | Position, tempo, loop state |
| `set_playhead_position` | Move playhead |
| `transport_play` / `stop` | Playback control |
| `transport_play_stop` | Toggle playback |
| `set_loop` | Set loop region |
| `enable_loop` / `disable_loop` | Toggle loop on/off |
| `export_track_audio` | Open Export Audio dialog (user saves manually) |
| `analyse_audio` | Full audio analysis: LUFS, true peak, RMS, crest factor, 8-band spectrum, resonances, stereo/phase, transients, Atmos safety. Optional second file for masking analysis. |
| `analyse_stem_set` | Stem QC: validates sample rate, length, overs, headroom, and DC offset across stems with PASS/WARN/FAIL verdict |
| `list_audio_files` | List audio files in configured directory |
| `apply_changes` | Set parameter values and mixer properties (volume, pan, mute, solo, arm); supports `target_display_value` for precise parameter control |
| `restore_snapshot` | Revert parameters |
| `get_plugin_registry` | Installed plugin list |

## Export & Analysis Workflow

Audio export is a two-step process:

1. **`export_track_audio`** — opens Ableton's Export Audio dialog via AppleScript. The user saves the file to their preferred location (the configured `audio_directory` is recommended).
2. **`list_audio_files`** — lists audio files in the configured `audio_directory`, sorted by newest first. Claude can find the newly exported file here.
3. **`analyse_audio`** — full audio analysis returning levels (LUFS, true peak, RMS, crest factor, dynamic range), 8-band spectrum energy, resonance/null detection, stereo/phase analysis, transient profiling, and Atmos mono-folddown safety. Pass a second file path for masking analysis between two tracks.

## Troubleshooting

**ClaudeBridge not in Control Surface dropdown:**
Check that `ClaudeBridge/` folder exists in `~/Music/Ableton/User Library/Remote Scripts/` with both `__init__.py` and `claude_bridge.py`.

**"Cannot connect to Ableton Live":**
Ensure Ableton is open and ClaudeBridge is selected in Preferences > MIDI. Check the Live log for errors.

**Audio export fails:**
Grant Accessibility permissions in System Preferences. Export manually with Cmd+Shift+R and use `analyse_audio` with the file path.

**Port 8765 in use:**
Close other applications using port 8765, or check if a previous Ableton instance is still running.

**Parameter convergence seems slow for VST/AU plugins:**
This is expected — 3rd-party plugins require iterative set-read cycles to converge on the desired display value. Native devices use non-destructive probing and are instant.

## For Developers

To build a distributable ZIP, double-click `build_release.command`. It produces `~/Desktop/Ableton-Claude-Bridge-v2.2.0.zip` containing only the files needed for installation — no venv, caches, or user data.

## About the Author

Created by Markus Korczyk, also known as Mark Ryder.

- Music: [bio.link/markrydermusic](https://bio.link/markrydermusic)
  - [Apple Music](https://music.apple.com/ie/artist/mark-ryder/1523600787)
  - [Bandcamp](https://markryder1.bandcamp.com/)
  - [Deezer](https://dzr.page.link/cw8uoKaS7WRHnBUz5)
  - [SoundCloud](https://soundcloud.com/mark-ryder-692358499)
  - [Spotify](https://open.spotify.com/artist/3QEcsVZjHa5jdRTUsqXhtx?si=GxKrhOJhTBSuQ09SubqfAw)
  - [Tidal](https://tidal.com/artist/13458838)
- Support the project and my music: [Buy Me a Coffee](https://buymeacoffee.com/markrydermusic)
  - or: [GitHub Sponsors](https://github.com/sponsors/pixelnmusic)
