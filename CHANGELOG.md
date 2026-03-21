# Changelog

All notable changes to Ableton-Claude-Bridge are documented in this file.

## [2.2.0] - 2026-03-04

### Added
- **BS.1770-4 loudness metering** using `pyloudnorm` for ITU-compliant integrated LUFS measurement
- **True peak metering** via `scipy.signal.resample_poly` (4x oversampling) per EBU R 128
- **Per-channel RMS and sample peak** with structured output fields
- **Crest factor** reported per channel in dB
- **`analyse_stem_set` tool** for stem QC: validates sample rate, length, overs, headroom, and DC offset across a set of stems with PASS/WARN/FAIL verdict
- **`schema_version` field** in `analyse_audio` output (v2.2.0) for forward compatibility
- **`loudness_notes` and `errors` fields** in analysis output for contextual guidance
- **Request queue with thread safety** — HTTP thread enqueues requests, `update_display()` drains them on Live's main thread

### Fixed
- **Masking analysis truncation bug** — for-loop variable rebind replaced with direct array slicing
- **Fake RMS removed** from all 5 metering locations (was `peak - 3 dB`), replaced with `metering_note` explaining limitation

### Changed
- `analyse_audio` output is backward-compatible but adds new structured fields (`sample_peak`, `true_peak`, `rms`, `crest_factor_per_channel_db`)
- Metering snapshot stays on HTTP thread (uses `time.sleep` polling); all other requests go through the main-thread queue

## [2.0.0] - 2026-03-01

### Added
- **Control Surface architecture** — ClaudeBridge runs as an Ableton Control Surface (no Max for Live required)
- **HTTP bridge on port 8765** — MCP server communicates with the Control Surface over localhost
- **Full mixer control** — volume, pan, mute, solo, arm on any track
- **Native device parameter binary search** using `str_for_value()` for non-destructive, sample-accurate parameter setting
- **3rd-party plugin convergence** — iterative set-read loop for VST/AU/Max for Live plugins with `target_display_value` support
- **Plugin format detection** — `PluginDevice` (VST2/VST3), `AuPluginDevice` (AU), `MxDevice*` (Max for Live) classified as 3rd-party; everything else as native
- **Fader curve handling** — binary search accounts for Ableton's proprietary volume curve (0.85 = 0 dB, 1.0 = +6 dB)
- **Audio file discovery** — `list_audio_files` searches a configurable directory
- **Simplified export workflow** — `export_track_audio` opens Ableton's Export Audio dialog via AppleScript
- **Plugin scanner** — `scan_plugins.command` generates `plugin_registry.json` from installed AU/VST/VST3 plugins
- **Spectral analysis** — 8-band energy, resonance/null detection, stereo/phase correlation, dynamics, transient profiling, Atmos mono-folddown safety, masking analysis
- **Snapshot and restore** — save and revert parameter states with undo support
- **Double-click installer** — `install.command` copies Control Surface, creates venv, configures Claude Desktop
- **Build script** — `build_release.command` produces a distributable ZIP
